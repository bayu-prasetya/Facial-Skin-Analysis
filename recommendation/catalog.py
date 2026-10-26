"""
The treatment catalog the engine scores against, and how a response becomes one.

The engine never calls an API itself: it takes a Catalog. One thing builds one
in production — the clinic's POST /api/recommendations, read through
`catalog_from_rows` — and one more exists for working offline, a saved copy of
such a response. The Excel harness in recommendation_prototype/ builds the same
object from the workbook for the tests.

What the engine needs from each treatment, and what it uses it for:

    needed / secondary   the Utama / Sekunder tags. The whole score is built on
                         these; without them a treatment is never recommended.
    severity             which acne levels may receive it (gate 3)
    status               whether the benefit is visible in the photo (gate 1)
    golongan             the diversity cap, the "Tambahan" add-on quota, and R4
    sub_concerns         the R5 tie-break
    name / category / fungsi   shown to the patient, never scored on
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from .rules import RECOMMENDATION_RULES

logger = logging.getLogger(__name__)

# The four concerns Perfect Corp measures for us. Order is fixed so that the
# same photo always produces the same request and the same output ordering.
CONCERN_ORDER = ["acne", "pore", "oiliness", "moisture"]

# Where the tags live in a response. Order matters: a list of ALL of a
# treatment's tags must win over matchedConcerns, which holds only the ones that
# matched the request. The precision term is inert without the unmatched ones,
# and a proxy treatment cannot carry a redness/pigmentation purpose, so R1-R3
# never fire either. `concernTags` is that full list, from GET /api/treatments.
AYUKO_TAG_KEYS = ("concernTags", "concerns", "tags", "treatmentConcerns",
                  "skinConcerns", "matchedConcerns")
# `description` first on purpose: it is the typeset version of the clinic's
# raw `clinicalFunction` text ("Mengecilkan pori, mengurangi minyak" against
# "mengecilkan pori,mengurangi minyak"), and this string is shown to the
# patient. clinicalFunction is the fallback for the one record lacking it.
AYUKO_DESCRIPTION_KEYS = ("description", "clinicalFunction", "fungsiKlinik", "summary")
AYUKO_SUB_CONCERN_KEYS = ("subConcern", "sub_concern", "subConcerns")
AYUKO_SEVERITY_KEYS = ("severityAcne", "severities", "severityLevels")
AYUKO_GOLONGAN_KEYS = ("golongan", "modality", "tag")

# The clinic's API answers `photoMatchStatus` in lowercase English while every
# rule, label and test here speaks the clinic's own Indonesian vocabulary (the
# workbook's). Translated at this edge so the engine only ever sees one
# vocabulary — gate 1 compares against the Indonesian values literally, so an
# untranslated "direct" is not a near-miss, it is a silent total rejection.
AYUKO_STATUS_MAP = {
    "direct": "Langsung",
    "partial": "Sebagian",
    "proxy": "Proxy",
    "none": "Tidak",
    "not_applicable": "N/A",
}

# Everything gate 1 knows how to judge. A value outside this set is recorded as
# an issue instead of being passed through quietly: it would be dropped by the
# gate, and an unexplained empty result is the one failure mode that looks
# exactly like a legitimate "nothing suits this patient".
ENGINE_STATUS_VALUES = frozenset(AYUKO_STATUS_MAP.values())

# The clinic's own wording for R1-R3, copied rather than fetched. Their
# `label` field only exists on POST /api/recommendations, which is no longer on
# the analysis path, and no catalog endpoint carries it (/api/proxy-rules and
# /api/rules both 404). One sentence covers all three rules because that is how
# they wrote it: R1-R3 all say the same thing — a condition that commonly
# accompanies the acne the photo did measure, not something measured itself.
#
# Presentation only. It labels related_options and never enters a score, which
# is why it is a constant here rather than a rules parameter. A response that
# carries its own label still wins.
DEFAULT_PROXY_LABELS = {
    "R1": "Sering menyertai jerawat meradang, bukan hasil deteksi",
    "R2": "Sering menyertai jerawat meradang, bukan hasil deteksi",
    "R3": "Sering menyertai jerawat meradang, bukan hasil deteksi",
}


@dataclass
class Treatment:
    id: str
    name: str
    category: str               # Acne, Melasma, Whitening, ...
    golongan: str               # Facial, Laser NdYAG, Skin Booster, Tambahan, ...
    coding: Optional[str]
    severity: Optional[set]     # acne levels allowed to receive it; None = unrestricted
    sub_concerns: set           # {"OIL", "HRM"} — used by tie-break R5
    needed: set                 # Utama tags: the treatment's primary purposes
    secondary: set              # Sekunder tags: benefits named in the clinic's FUNGSI
    status: str                 # Langsung | Sebagian | Proxy | Tidak | N/A
    note: str = ""
    needs_review: bool = False
    fungsi: str = ""            # shown to the patient as the description
    invasive: Optional[bool] = None   # stated by the source; None = derive it
    price: Optional[float] = None     # ordering only — never scored, never returned
    # The clinic's own featured treatments (isFeatured). Ordering only, like
    # price: at an equal score and price the featured one goes first (v11).
    # Missing reads as not featured, so an absent field never promotes one.
    featured: bool = False
    # Treatments in the largest clinic package that contains this one. 0 = in
    # no package; None = package data unavailable, which the score treats as
    # neutral rather than as "in no package".
    package_size: Optional[int] = None

    @property
    def is_invasive(self) -> bool:
        """Whether it is a medical procedure (R4 hides these at acne S3-S4).
        The workbook does not store it, so it is derived from golongan; the
        clinic's API states it outright, and a stated value wins — its modality
        vocabulary is not ours to pattern-match against."""
        if self.invasive is not None:
            return self.invasive
        return self.golongan in RECOMMENDATION_RULES["invasive_golongan"]


@dataclass
class Catalog:
    treatments: dict                        # id -> Treatment
    concerns_available: list                # concerns measured from the photo
    proxy_labels: dict = field(default_factory=dict)   # rule_id -> user-facing label
    thresholds: dict = field(default_factory=dict)     # concern -> [(level, min, max)] when calibrated
    issues: list = field(default_factory=list)


def _split(cell) -> list:
    """Parse a multi-value field; '-' and blanks mean nothing.

    The workbook stores these as one comma-separated cell, the clinic's API
    sends a JSON list (["DEHID"]). Stringifying a list would yield "['DEHID']"
    and quietly tag nothing, so the two shapes are handled apart.
    """
    if cell in (None, "", "-"):
        return []
    parts = cell if isinstance(cell, (list, tuple, set)) else str(cell).split(",")
    return [str(p).strip().rstrip("*") for p in parts
            if str(p).strip() and str(p).strip() != "-"]


def parse_severity(text) -> Optional[set]:
    """'S1-S4' -> {'S1','S2','S3','S4'}; 'S2' -> {'S2'}; 'Tidak dibatasi' -> None
    (unrestricted). An unparseable value yields an empty set, which excludes the
    treatment from every level rather than silently allowing all of them."""
    if isinstance(text, (list, set, tuple)):
        return set(text) or None
    text = str(text or "").strip()
    if not text or text == "Tidak dibatasi":
        return None
    nums = [int(n) for n in re.findall(r"S(\d)", text)]
    if not nums:
        return set()
    return {f"S{n}" for n in range(min(nums), max(nums) + 1)}


def _tag_weight(value) -> Optional[float]:
    """1 = Utama, 0.5 = Sekunder, blank = not tagged. Parsed loosely because the
    same value arrives as a number from JSON and sometimes as text."""
    if value in (None, "", "-"):
        return None
    try:
        weight = float(str(value).replace(",", "."))
    except ValueError:
        return None
    return weight if weight > 0 else None


def _price(value) -> Optional[float]:
    """priceNumeric arrives as a string ("1500000"). Read for ordering only:
    equal-scoring treatments are offered dearest first since v11 (cheapest
    first in v9/v10, the order `tie_break` names). It is never scored on
    and never reaches a response."""
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _flag(value) -> bool:
    """A boolean the source may send as true/"true"/1/"1"/"ya"."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "ya", "yes", "y")
    return bool(value)


def _first(row: dict, *names, default=None):
    for name in names:
        if row.get(name) not in (None, ""):
            return row[name]
    return default


def _category_from_response(row: dict) -> str:
    """categoryId is a slug ("whitening"); the nested category object carries the
    name a human reads. Prefer the name — this value is shown to the patient."""
    category = row.get("category")
    if isinstance(category, dict) and category.get("title"):
        return category["title"]
    return _first(row, "kategori", "categoryId", default="")


def _status_from_response(row: dict) -> tuple[str, Optional[str]]:
    """(status in the engine's vocabulary, an issue to record or None)."""
    raw = _first(row, "status", "photoMatchStatus", default="Langsung")
    status = AYUKO_STATUS_MAP.get(str(raw).strip().lower(), str(raw).strip())
    if status not in ENGINE_STATUS_VALUES:
        return status, f"unrecognised status {raw!r}, so gate 1 will drop it"
    return status, None


def _severity_from_response(row: dict) -> Optional[set]:
    """Their severity is 1-4; ours is S1-S4. A min/max pair, a list, or nothing
    (unrestricted) are all accepted."""
    explicit = _first(row, *AYUKO_SEVERITY_KEYS)
    if explicit is not None:
        return parse_severity([f"S{int(v)}" if isinstance(v, (int, float)) else v
                               for v in explicit] if isinstance(explicit, (list, tuple, set))
                              else explicit)
    # Their name for it is acneSeverityMin/Max; severityMin/Max is the shape
    # the earlier documentation showed. Both are accepted.
    lo = _first(row, "severityMin", "acneSeverityMin")
    hi = _first(row, "severityMax", "acneSeverityMax")
    if lo is None and hi is None:
        return None
    lo, hi = int(lo or 1), int(hi or 4)
    return {f"S{n}" for n in range(min(lo, hi), max(lo, hi) + 1)}


def _tags_from_response(row: dict) -> tuple[set, set]:
    """(Utama, Sekunder) from a list like
    [{"concernId": "acne", "weight": 1, "kind": "primary"}, ...]."""
    entries = _first(row, *AYUKO_TAG_KEYS, default=[])
    needed, secondary = set(), set()
    if not isinstance(entries, list):
        return needed, secondary
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        concern = _first(entry, "concernId", "concern", "code", "id")
        if not concern:
            continue
        kind = str(_first(entry, "kind", "jenis", default="")).lower()
        weight = _tag_weight(entry.get("weight"))
        if kind in ("primary", "utama") or (not kind and weight and weight >= 1):
            needed.add(concern)
        else:
            secondary.add(concern)
    return needed, secondary - needed


def _purposes(needed: set, secondary: set) -> tuple[set, set]:
    """Decide what counts as a treatment's purposes.

    Utama tags come from the clinic's reviewed concern mapping; Sekunder tags
    are keyword-extracted from its FUNGSI text. Normally both are present and
    kept apart, Sekunder carrying half the affinity.

    A treatment with no Utama tag at all is a gap in the mapping, not a
    treatment without a purpose. Its Sekunder tags are then all that is known
    about it, so they are read as its purposes — otherwise it is never scored:
    with an empty `needed`, precision has nothing to divide and gate 5 drops
    every proxy treatment outright.

    What this costs: those concerns are weaker evidence, yet they now carry the
    full Utama affinity. It applies only where the alternative is scoring the
    treatment at zero, so the comparison is against never showing it at all.
    """
    if needed or not secondary:
        return needed, secondary
    return set(secondary), set()


def catalog_from_rows(rows, concerns_available: Optional[list] = None,
                      proxy_labels: Optional[dict] = None,
                      thresholds: Optional[dict] = None) -> Catalog:
    """Build a Catalog from the clinic API's treatment items.

    Each item nests the descriptive fields under `treatment` while the tagging
    stays on the outer object, so the two levels are merged rather than
    descended into — descending throws the tags away and nothing is ever scored.

    A treatment that arrives without tags, or without a severity range, is
    recorded in `issues`: each one silently disables a rule, so it has to be
    visible rather than inferred later from an empty result.
    """
    treatments, issues = {}, []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("treatment"), dict):
            merged = dict(row["treatment"])
            for key, value in row.items():
                if key != "treatment" and key not in merged:
                    merged[key] = value
            row = merged
        if not isinstance(row, dict):
            continue
        tid = _first(row, "treatment_id", "id", "code", "slug")
        if not tid:
            continue

        needed, secondary = _purposes(*_tags_from_response(row))
        if not needed and not secondary:
            issues.append(f"{tid}: no concern tag in the response, never recommended")
        severity = _severity_from_response(row)
        stated_severity = _first(row, *AYUKO_SEVERITY_KEYS, "severityMin",
                                 "acneSeverityMin", "severityMax", "acneSeverityMax")
        if severity is None and stated_severity is None:
            issues.append(f"{tid}: no severity_acne in the response, "
                          "the acne-level filter cannot be applied")

        status, status_issue = _status_from_response(row)
        if status_issue:
            issues.append(f"{tid}: {status_issue}")

        if str(tid) in treatments:
            # Last one wins, as a dict does. Worth saying out loud: the two rows
            # may carry different tags, so the ranking changes with their order.
            issues.append(f"{tid}: appears more than once in the response, later row wins")
        treatments[str(tid)] = Treatment(
            id=str(tid),
            name=_first(row, "nama_jual", "title", "name", default=str(tid)),
            category=_category_from_response(row),
            golongan=_first(row, *AYUKO_GOLONGAN_KEYS, default=""),
            coding=_first(row, "coding_standar", "code"),
            severity=severity,
            sub_concerns=set(_split(_first(row, *AYUKO_SUB_CONCERN_KEYS))),
            needed=needed, secondary=secondary,
            status=status,
            note=_first(row, "catatan", "note", default="") or "",
            fungsi=_first(row, *AYUKO_DESCRIPTION_KEYS, default="") or "",
            invasive=row.get("isInvasive"),
            price=_price(_first(row, "priceNumeric", "harga", "price")),
            featured=_flag(_first(row, "isFeatured", "unggulan", "featured")),
            needs_review=bool(row.get("needsReview") or row.get("perlu_review")),
        )

    if not thresholds:
        issues.append("no calibrated S1-S4 ranges: using the defaults from the rules")

    return Catalog(treatments=treatments,
                   concerns_available=list(concerns_available or CONCERN_ORDER),
                   proxy_labels=dict(proxy_labels or DEFAULT_PROXY_LABELS),
                   thresholds=dict(thresholds or {}), issues=issues)


def catalog_from_response_file(path: Optional[str] = None) -> Optional[Catalog]:
    """Offline escape hatch: build the catalog from a saved API response, so the
    pipeline can be run without the service. The path comes from
    AYUKO_RESPONSE_FILE unless one is passed.

    A whole response, just its `data`, or a bare list of treatment items are all
    accepted, so a response captured straight from the service works as is.
    """
    path = path or os.environ.get("AYUKO_RESPONSE_FILE")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        logger.warning("AYUKO_RESPONSE_FILE could not be read as JSON: %s", path)
        return None

    items = payload
    while isinstance(items, dict):
        nested = items.get("data", items.get("treatments"))
        if nested is None:
            break
        items = nested
    if not isinstance(items, list):
        logger.warning("AYUKO_RESPONSE_FILE holds no treatment list: %s", path)
        return None
    catalog = catalog_from_rows(items)
    return catalog if catalog.treatments else None
