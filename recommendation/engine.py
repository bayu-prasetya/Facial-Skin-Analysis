"""
The scoring pipeline: four Perfect Corp photo scores in, ranked treatments out.

Deterministic and pure — the same scores against the same catalog always give
the same result. Nothing here reads a database, a file or the clock.

Stages (the numbering follows the doc "SkinCode — Desain Sistem Rekomendasi
Treatment", tab "Alur rekomendasi lengkap"):

    1. extract_raw_scores()  Perfect Corp output -> raw_score per concern
    2. derive_signals()      raw_score -> level S1-S4, need 0-1, proxy needs, urgency band
    4. rank_treatments()     eligibility gates, then relevance 0-100 per treatment
    5. related_options() / pick_addons()
    6. apply_guardrails()    disclaimer, consultation priority, medical flags

Stage 3 (picking a curated package) is deliberately absent in rules 2026-10-v8.

The score is coverage x precision:

    coverage  = how much of this user's measured need the treatment addresses
    precision = how much of the treatment's own primary purpose this user needs

Coverage alone would let an oil-control treatment score high on skin that is not
oily, as long as one minor benefit matched; precision alone would reward narrow
treatments regardless of how little of the face they help. Proxy needs (R1-R3)
count in coverage's numerator only — they are estimates, so they must never
dilute a treatment that addresses something the photo actually measured.
"""
from __future__ import annotations

from typing import Optional

from .catalog import Catalog
from .rules import LEVEL_ORDER, RECOMMENDATION_RULES


# ---------------------------------------------------------------------------
# Stage 1 — reading the photo scores
# ---------------------------------------------------------------------------

def _metric_items(skin_metrics) -> list:
    """Accept Perfect Corp's {"output": [...]} or an already-flattened list."""
    if isinstance(skin_metrics, list):
        return skin_metrics
    return (skin_metrics or {}).get("output", [])


def extract_raw_scores(skin_metrics, concerns: list) -> tuple[dict, list]:
    """Pull raw_score per concern, with ui_score as a noted fallback.

    raw_score is the measurement; ui_score is Perfect Corp's presentation value,
    adjusted upward so results look better to the patient (oiliness raw 67 is
    shown as ui 75). Scoring on ui_score would therefore understate need, so
    whenever it has to be used the concern is flagged in the notes.
    """
    raw, notes = {}, []
    for item in _metric_items(skin_metrics):
        concern = item.get("type")
        if concern not in concerns:
            continue
        if item.get("raw_score") is not None:
            raw[concern] = float(item["raw_score"])
        elif item.get("ui_score") is not None:
            raw[concern] = float(item["ui_score"])
            notes.append(f"{concern}_used_ui_score")
    return raw, notes


def skin_health_from_metrics(skin_metrics) -> Optional[int]:
    """Perfect Corp's "all" metric — the overall condition score across the
    analyzed concerns — used here as the urgency band input."""
    item = next((i for i in _metric_items(skin_metrics) if i.get("type") == "all"), None)
    score = (item or {}).get("score", (item or {}).get("raw_score"))
    return round(score) if score is not None else None


# ---------------------------------------------------------------------------
# Stage 2 — level and need
# ---------------------------------------------------------------------------

def level(concern: str, score: Optional[float], catalog: Catalog) -> Optional[str]:
    """raw_score -> 'S1'..'S4', or None when there is no problem / no score.
    Calibrated ranges from concern_levels win; the rules defaults are the
    fallback while calibration is pending."""
    if score is None:
        return None
    for lvl, lo, hi in catalog.thresholds.get(concern, []):
        if lo <= score <= hi:
            return lvl
    if catalog.thresholds.get(concern):
        return None  # calibrated and outside every range = no problem
    if score >= RECOMMENDATION_RULES["no_problem_min"]:
        return None
    return next(lvl for lvl, lo in RECOMMENDATION_RULES["default_levels"] if score >= lo)


def at_least(lvl: Optional[str], minimum: str) -> bool:
    return lvl is not None and LEVEL_ORDER[lvl] >= LEVEL_ORDER[minimum]


def derive_signals(raw: dict, skin_health_score: Optional[float], catalog: Catalog) -> dict:
    """Turn raw scores into the levels, needs and bands the later stages read."""
    R, rk = RECOMMENDATION_RULES, RECOMMENDATION_RULES["ranking"]
    measured = [c for c in catalog.concerns_available if c in raw]
    lv = {c: level(c, raw[c], catalog) for c in measured}
    # Need is how far a score sits below the "no problem" line, linearly, so
    # healthy skin always needs 0 and two concerns are directly comparable.
    need = {c: max(0.0, (rk["need_threshold"] - raw[c]) / rk["need_threshold"]) for c in measured}

    # Proxy needs. A rule fires when its source concern is bad enough and the
    # concern it infers was not measured at all. Several rules may infer the
    # same concern from different sources — the strongest estimate wins, since
    # they are alternative evidence for one condition, not separate conditions
    # to be added up. `lv` only holds measured concerns, so a source that is
    # itself inferred never qualifies and proxies cannot chain.
    active_proxies = {}
    for rule_id, concern, source, min_level, factor in R["proxy_rules"]:
        if concern in measured or not at_least(lv.get(source), min_level):
            continue
        inferred = factor * need[source]
        if inferred <= need.get(concern, 0.0):
            continue
        need[concern] = inferred
        active_proxies[concern] = rule_id

    health = skin_health_score if skin_health_score is not None else 0
    band = next(b for b in R["urgency_bands"] if health >= b[0])
    return {
        "raw": raw,
        "level": lv,
        "need": need,
        "measured": measured,
        "active_proxies": active_proxies,
        "acne_level": lv.get("acne"),
        "r5_oil_and_pore": bool(lv.get("pore") and lv.get("oiliness")),
        "band": band[1],
        "max_addons": band[2],
        "consultation_priority": band[3],
        "missing": [c for c in catalog.concerns_available if c not in raw],
    }


# ---------------------------------------------------------------------------
# Stage 4 — eligibility and relevance score
# ---------------------------------------------------------------------------

def _hide_invasive(sig: dict) -> bool:
    R = RECOMMENDATION_RULES
    return R["always_hide_invasive"] or at_least(sig["acne_level"], R["invasive_hidden_from"])


def score_treatment(t, sig: dict) -> Optional[dict]:
    """Relevance 0-100, or None when the treatment is not eligible for this user.

    The five gates below are the ones in the design doc; failing any one hides
    the treatment entirely, whatever it would have scored.
    """
    rk = RECOMMENDATION_RULES["ranking"]

    # 1. Is the benefit visible in the photo at all?
    if t.status not in ("Langsung", "Sebagian", "Proxy"):
        return None
    # 2. Is it safe for this user's acne right now? (R4)
    if t.is_invasive and _hide_invasive(sig):
        return None
    # 3. Does it match this user's acne level?
    if t.severity is not None and (sig["acne_level"] is None or sig["acne_level"] not in t.severity):
        return None
    # 4. Is the user actually acne-affected, for an acne-purpose treatment?
    if "acne" in t.needed and sig["acne_level"] is None:
        return None
    # 5. For a proxy treatment: is its rule active?
    is_proxy = t.status == "Proxy"
    if is_proxy and not any(c in sig["active_proxies"] for c in t.needed):
        return None

    w, need, proxies = rk["concern_weight"], sig["need"], sig["active_proxies"]
    affinity = {c: rk["affinity_secondary"] for c in t.secondary}
    affinity.update({c: rk["affinity_needed"] for c in t.needed})
    contrib = {c: w.get(c, 0) * need[c] * affinity.get(c, 0) for c in need}

    # Coverage — proxy needs add to the numerator only (see the module docstring).
    total = sum(w.get(c, 0) * need[c] for c in need if c not in proxies)
    if total == 0 or sum(contrib.values()) == 0:
        return None
    coverage = min(1.0, sum(contrib.values()) / total)

    # Precision — purposes the photo cannot observe (e.g. radiance) are left out,
    # so a "Sebagian" treatment is judged only on the part the photo can see.
    purposes = [c for c in t.needed if c in w and c in need]
    precision = (sum(w[c] for c in purposes if need.get(c, 0) > 0) / sum(w[c] for c in purposes)) if purposes else 1.0
    if precision == 0:
        return None

    return {
        "t": t,
        "score": round(100 * coverage * precision),
        "coverage": round(coverage, 3),
        "precision": round(precision, 3),
        "concerns": [c for c, v in sorted(contrib.items(), key=lambda kv: -kv[1]) if v > 0],
        "proxy_rules": sorted({proxies[c] for c in t.needed if c in proxies}) if is_proxy else [],
    }


# ---------------------------------------------------------------------------
# Stage 5 — ranking, related options, add-ons
# ---------------------------------------------------------------------------

def _addresses_oil_secondarily(t) -> bool:
    """The R5 tie-break: oil and pores both affected -> prefer a treatment whose
    *secondary* purpose is oil.

    Two markers count. The clinic's own sub-concern code ("OIL") is the direct
    one, but it is filled in for only about half the catalog, so an oiliness tag
    at weight 0.5 — Sekunder, which is the same statement in the tag vocabulary —
    counts as well.
    """
    rk = RECOMMENDATION_RULES["ranking"]
    if any(marker in t.sub_concerns for marker in rk["r5_oil_markers"]):
        return True
    secondary_marker = rk.get("r5_oil_secondary_concern")
    return bool(secondary_marker) and secondary_marker in t.secondary


def rank_treatments(sig: dict, catalog: Catalog) -> tuple[list, list]:
    """Return (the ranked list shown to the user, every scored treatment)."""
    rk = RECOMMENDATION_RULES["ranking"]
    scored = []
    for t in catalog.treatments.values():
        s = score_treatment(t, sig)
        if s is None:
            continue
        s["_r5"] = sig["r5_oil_and_pore"] and _addresses_oil_secondarily(t)
        s["_spec"] = len(t.severity) if t.severity else 9
        s["_price"] = t.price if t.price is not None else float("inf")
        scored.append(s)
    # Ties: R5 (oil + pore both affected -> AC_*_OIL first) -> narrowest
    # severity range (more specific to this level) -> lowest price -> id.
    #
    # Price is the tie-break because exact ties are common and the catalog
    # gives nothing else to separate them: the ten scar treatments all carry
    # the identical tag profile (Utama texture, Sekunder pore) with no severity
    # range, so they score the same to the decimal. Before this, the order was
    # alphabetical by treatment_id and the consequence was arbitrary —
    # `canula-subsisi` at Rp3,000,000 was offered while `subsisi-mild` at
    # Rp1,500,000 never appeared in any of 625 score combinations, because c
    # precedes s. Cheapest first is the conservative order: the lighter option
    # is the one to try before the heavier.
    #
    # Deliberately NOT ordered by invasiveness first. It reads as the safer
    # rule, but every subsisi tier is invasive, so it would bury the whole
    # golongan behind the non-invasive scar treatments — the exact treatments
    # R3's second source was widened to reach. Invasiveness is already handled
    # where it belongs, in gate 2 (R4).
    #
    # A treatment with no price sorts last rather than first, so a missing
    # field never promotes a treatment over one whose price is known.
    scored.sort(key=lambda s: (-s["score"], not s["_r5"], s["_spec"], s["_price"], s["t"].id))

    top, per_golongan = [], {}
    for s in (s for s in scored if not s["proxy_rules"]):
        if s["score"] < rk["min_score"] or len(top) == rk["top_n"]:
            break
        golongan = s["t"].golongan
        # Without a cap the list fills up with near-identical skin boosters.
        if per_golongan.get(golongan, 0) >= rk["max_per_golongan"]:
            continue
        per_golongan[golongan] = per_golongan.get(golongan, 0) + 1
        top.append(s)
    return top, scored


def related_options(scored: list, sig: dict) -> list:
    """Proxy treatments (R1-R3), listed apart from the ranking so the patient and
    the doctor can tell an estimate from a detection."""
    rk = RECOMMENDATION_RULES["ranking"]
    out = []
    for rule_id in sorted(set(sig["active_proxies"].values())):
        picks = [s for s in scored if rule_id in s["proxy_rules"] and s["score"] >= rk["min_score"]]
        out.extend((rule_id, s) for s in picks[: rk["proxy_options_per_rule"]])
    return out


def pick_addons(scored: list, sig: dict, top: list) -> list:
    """Highest-scoring "Tambahan" treatments *not already ranked*, within the
    urgency band's quota.

    The exclusion is the whole point of this function's shape. "Tambahan" may
    also hold up to max_per_golongan slots in the ranking itself, and the add-on
    is by construction the highest-scoring one of them — so without the
    exclusion the add-on is normally a treatment the patient is already looking
    at, shown a second time under a different reason ("skor tertinggi" beside
    its own "Relevan"), in a section that claims to list what is additional to
    the ranking. It happened in two of five representative cases.

    v7 had this condition against the chosen package; it was dropped together
    with stage 3 in v8 and nothing replaced it. `top` is required rather than
    defaulted for that reason — a default would silently restore the bug.

    An empty add-on list is a real answer: nothing further is worth suggesting,
    and the treatment itself is still shown in the ranking.
    """
    rk = RECOMMENDATION_RULES["ranking"]
    already_ranked = {s["t"].id for s in top}
    out = []
    for s in scored:
        if len(out) >= sig["max_addons"]:
            break
        if (s["t"].golongan == rk["addon_golongan"] and not s["proxy_rules"]
                and not s["t"].is_invasive and s["score"] >= rk["addon_min_score"]
                and s["t"].id not in already_ranked):
            out.append({"treatment_id": s["t"].id, "name": s["t"].name, "score": s["score"],
                        "reason": "Treatment tambahan dengan skor tertinggi"})
    return out


def label_for(score: Optional[float]) -> Optional[str]:
    """Patient-facing relevance label. The numeric score is for staff."""
    if score is None:
        return None
    return next((name for cutoff, name in RECOMMENDATION_RULES["ranking"]["labels"] if score >= cutoff), None)


# ---------------------------------------------------------------------------
# Stage 6 — guardrails and output
# ---------------------------------------------------------------------------

def apply_guardrails(top: list, options: list, addons: list, sig: dict, catalog: Catalog) -> dict:
    """Assemble the result, with the safety rules that no weight or threshold can
    switch off. The point is that the output stays a consultation suggestion."""
    R = RECOMMENDATION_RULES
    notes = []
    priority = sig["consultation_priority"]
    if at_least(sig["acne_level"], R["invasive_hidden_from"]):
        priority = "high"  # R4
        notes.append(f"R4: jerawat {R['invasive_hidden_from']} atau lebih berat, "
                     "tampilkan ajakan konsultasi dokter")
    if sig["acne_level"] is None:
        notes.append("Tidak ada jerawat aktif terdeteksi: rekomendasi bersifat perawatan umum")
    if sig["missing"]:
        notes.append("insufficient_data:" + ",".join(sig["missing"]))

    return {
        "rules_version": R["version"],
        "urgency_band": sig["band"],
        "needs_clinical_confirmation": True,
        "consultation_priority": priority,
        "levels": sig["level"],
        "active_proxy_rules": sorted(set(sig["active_proxies"].values())),
        "ranked_treatments": [
            {"rank": i + 1, "treatment_id": s["t"].id, "name": s["t"].name,
             "golongan": s["t"].golongan, "score": s["score"], "label": label_for(s["score"]),
             "concerns": s["concerns"], "status": s["t"].status,
             **({"medical_procedure": True} if s["t"].is_invasive else {})}
            for i, s in enumerate(top)
        ],
        "related_options": [
            {"rule": rule_id, "label": catalog.proxy_labels.get(rule_id),
             "treatment_id": s["t"].id, "name": s["t"].name,
             "golongan": s["t"].golongan, "score": s["score"]}
            for rule_id, s in options
        ],
        "addons": addons,
        "notes": notes,
        "disclaimer": R["disclaimer"],
    }


def _empty_result(notes: list, catalog: Optional[Catalog] = None) -> dict:
    return {
        "rules_version": RECOMMENDATION_RULES["version"],
        "urgency_band": None,
        "needs_clinical_confirmation": True,
        "consultation_priority": "normal",
        "levels": {},
        "active_proxy_rules": [],
        "ranked_treatments": [],
        "related_options": [],
        "addons": [],
        "notes": notes,
        "disclaimer": RECOMMENDATION_RULES["disclaimer"],
    }


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def recommend(raw: dict, skin_health_score: Optional[float], catalog: Catalog) -> dict:
    """Run the whole pipeline on {concern: raw_score}."""
    if not catalog.treatments:
        return _empty_result(["catalog_unavailable"], catalog)
    # Acne drives most of the catalog's eligibility and every proxy rule, so a
    # result without it is a thinner result: gate 4 drops every acne-purpose
    # treatment and R1-R3 stay inactive. That is a reason to say so in the
    # notes — apply_guardrails does, from sig["missing"] — not a reason to
    # withhold the oil, pore and moisture treatments the photo did measure.
    # Only a photo that measured nothing at all has no answer to give.
    if not any(raw.get(c) is not None for c in catalog.concerns_available):
        return _empty_result(
            ["insufficient_data:" + ",".join(catalog.concerns_available)], catalog)

    sig = derive_signals(raw, skin_health_score, catalog)
    top, scored = rank_treatments(sig, catalog)
    result = apply_guardrails(top, related_options(scored, sig),
                              pick_addons(scored, sig, top), sig, catalog)
    result["skin_health_score"] = skin_health_score
    return result


def compute_recommendations(skin_metrics, skin_health_score: Optional[int],
                            catalog: Optional[Catalog]) -> dict:
    """Full result from Perfect Corp's skin_metrics. catalog=None (nothing
    published yet) yields an empty result noted "catalog_unavailable"."""
    if catalog is None:
        return _empty_result(["catalog_unavailable"])
    raw, notes = extract_raw_scores(skin_metrics, catalog.concerns_available)
    if skin_health_score is None:
        skin_health_score = skin_health_from_metrics(skin_metrics)
    result = recommend(raw, skin_health_score, catalog)
    result["notes"] = notes + result["notes"]
    result["scores_used"] = {c: round(v, 2) for c, v in raw.items()}
    return result


def to_recommendation_list(result: dict, catalog: Optional[Catalog]) -> list[dict]:
    """Flatten a result into the API's `recommendations` list, best first.

    Every descriptive field comes straight from the catalog; rank, score and
    label are the only computed ones.
    """
    if catalog is None:
        return []
    out = []
    for item in result.get("ranked_treatments", []):
        t = catalog.treatments.get(item["treatment_id"])
        if t is None:
            continue
        out.append({
            "rank": item["rank"],
            "treatment_id": t.id,
            "nama_jual": t.name,
            "kategori": t.category,
            "golongan": t.golongan,
            "fungsi_klinik": t.fungsi or None,
            "invasif": t.is_invasive,
            "score": item["score"],
            "label": item["label"],
        })
    return out


def to_recommendation_meta(result: dict) -> dict:
    """Everything in a result except the ranked treatments themselves.

    to_recommendation_list keeps only the treatments, which leaves the whole
    guardrail block with nowhere to go: rules_version, the urgency band, the
    consultation priority, the notes and the disclaimer. Stored next to the
    recommendations, that block is what still explains a result afterwards —
    rules_version names the parameters it was computed under, so an old
    recommendation stays readable across a recalibration, and the notes say what
    the engine could not see ("catalog_unavailable", "insufficient_data:acne").
    The disclaimer travels with it because it is a guardrail: the output is a
    consultation suggestion, and nothing downstream may present it as more.

    skin_health_score is left out — it has a column of its own, and two copies
    of one number can disagree.
    """
    return {key: value for key, value in result.items()
            if key not in ("ranked_treatments", "skin_health_score")}
