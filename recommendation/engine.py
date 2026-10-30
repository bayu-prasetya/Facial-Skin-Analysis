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
    5b. fill_to_minimum()   at least three, topped up from the lowest scores
    6. apply_guardrails()    disclaimer, consultation priority, medical flags

Stage 3 (picking a curated package) is deliberately absent in rules 2026-10-v8.

The score is coverage x precision:

    coverage  = how much of this user's measured need the treatment addresses
    precision = how much of the treatment's own primary purpose this user needs

Coverage alone would let an oil-control treatment score high on skin that is not
oily, as long as one minor benefit matched; precision alone would reward narrow
treatments regardless of how little of the face they help. Proxy needs (R1-R3)
count in coverage's numerator only — they are estimates, so they must never
dilute a treatment that addresses something the photo actually measured. And
since v10 a proxy and the measured concern it was inferred from are counted
once for a treatment tagged with both (the larger credit): the proxy is a
fraction of that same measurement, not a second one.

Nothing is counted twice anywhere else either: severity specificity is in the
shown score (severity_fit) and therefore not in the tie-break, and R5 is in the
tie-break and therefore not in the score — most OIL-coded treatments already
earn oil credit through an oiliness tag.
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


def membership(concern: str, score: Optional[float], catalog: Catalog) -> dict:
    """{level: share} for one score, the shares summing to 1; {} when there is
    no problem or no score.

    Within `level_window` points of a boundary between two levels the score
    belongs to both, linearly — 0.5 / 0.5 at the boundary, all one level at the
    window's edge — so a score that sits on a line no longer flips everything
    that reads the level (v12). Elsewhere, and with a window of 0, it is the
    hard level alone. The 80 line ("no problem") is never blended: need starts
    from zero there, so there is nothing to lean towards.
    """
    hard = level(concern, score, catalog)
    if hard is None:
        return {}
    window = RECOMMENDATION_RULES["level_window"]
    if window:
        calibrated = catalog.thresholds.get(concern)
        floors = ([(lvl, lo) for lvl, lo, _ in calibrated] if calibrated
                  else list(RECOMMENDATION_RULES["default_levels"]))
        floors.sort(key=lambda item: -item[1])
        for (upper, edge), (lower, _) in zip(floors, floors[1:]):
            if edge - window < score < edge + window:
                lower_share = round((edge + window - score) / (2 * window), 3)
                return {upper: round(1 - lower_share, 3), lower: lower_share}
    return {hard: 1.0}


def _share_at_least(shares: dict, minimum: str) -> float:
    """How much of a membership sits at `minimum` or worse."""
    return sum(v for lvl, v in shares.items() if LEVEL_ORDER[lvl] >= LEVEL_ORDER[minimum])


def health_for_band(raw: dict, measured: list, vendor_health: Optional[float]) -> float:
    """The overall-health number the urgency band is read from.

    Perfect Corp's own overall score (`all`, stored as skin_health_score) is the
    mean of the four ui_scores — exactly, on every analysis we hold — so it sits
    on the inflated display scale while everything else here is raw. On the
    worst face we have (raw pore 17) the two disagree by 20 points: ui mean 66.8
    reads as "sedang", raw mean 47.0 as "intensif" with consultation priority
    high. `health_source = "raw_mean"` bands on the measurement; `"vendor_all"`
    keeps the vendor's number, which is what v9 did.

    main.compute_skin_health_score calls this too, so the skin_health_score a
    client receives is the same number the band was read from, under whichever
    rules version is active.
    """
    source = RECOMMENDATION_RULES["health_source"]
    if source == "raw_mean":
        values = [raw[c] for c in measured if raw.get(c) is not None]
        if values:
            return sum(values) / len(values)
        return vendor_health if vendor_health is not None else 0
    return vendor_health if vendor_health is not None else 0


def derive_signals(raw: dict, skin_health_score: Optional[float], catalog: Catalog) -> dict:
    """Turn raw scores into the levels, needs and bands the later stages read."""
    R, rk = RECOMMENDATION_RULES, RECOMMENDATION_RULES["ranking"]
    measured = [c for c in catalog.concerns_available if c in raw]
    lv = {c: level(c, raw[c], catalog) for c in measured}
    shares = {c: membership(c, raw[c], catalog) for c in measured}
    # Need is how far a score sits below the "no problem" line, linearly, so
    # healthy skin always needs 0 and two concerns are directly comparable.
    need = {c: max(0.0, (rk["need_threshold"] - raw[c]) / rk["need_threshold"]) for c in measured}

    # Proxy needs. A rule fires when its source concern is bad enough and the
    # concern it infers was not measured at all. Several rules may infer the
    # same concern from different sources — the strongest estimate wins, since
    # they are alternative evidence for one condition, not separate conditions
    # to be added up. `shares` only holds measured concerns, so a source that is
    # itself inferred never qualifies and proxies cannot chain.
    #
    # A source near its rule's minimum level fires the rule only partly
    # (v12): its strength is the source's membership at that level or worse,
    # so R1 at acne 71 is a third of R1 at acne 67, not nothing.
    active_proxies, proxy_sources, proxy_strength = {}, {}, {}
    for rule_id, concern, source, min_level, factor in R["proxy_rules"]:
        strength = _share_at_least(shares.get(source, {}), min_level)
        if concern in measured or not strength:
            continue
        inferred = factor * need[source] * strength
        if inferred <= need.get(concern, 0.0):
            continue
        need[concern] = inferred
        active_proxies[concern] = rule_id
        proxy_sources[concern] = source
        proxy_strength[concern] = strength

    health = health_for_band(raw, measured, skin_health_score)
    band = next(b for b in R["urgency_bands"] if health >= b[0])
    return {
        "raw": raw,
        "level": lv,
        "membership": shares,
        "need": need,
        "measured": measured,
        "active_proxies": active_proxies,
        "proxy_sources": proxy_sources,
        "proxy_strength": proxy_strength,
        "acne_level": lv.get("acne"),
        "r5_oil_and_pore": bool(lv.get("pore") and lv.get("oiliness")),
        "health_for_band": round(health, 2),
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


def passes_gates(t, sig: dict, photo_statuses=("Langsung", "Sebagian", "Proxy")) -> bool:
    """The five eligibility gates from the design doc. Failing any one hides the
    treatment entirely, whatever it would have scored.

    Kept apart from the score so that a pick made without one — care for skin
    that measured perfect, where every need is zero — is held to exactly the
    same gates rather than to a second copy of them. That pick is the one
    caller that widens gate 1 (`photo_statuses`, v13): nothing on a perfect face
    is a finding, so whether the photo could see one does not apply.
    """
    # 1. Is the benefit visible in the photo at all?
    if t.status not in photo_statuses:
        return False
    # 2. Is it safe for this user's acne right now? (R4)
    if t.is_invasive and _hide_invasive(sig):
        return False
    # 3. Does it match this user's acne level? A range lists the acne levels
    # the treatment suits. Since v11 it binds only a patient who has acne:
    # without acne there is no level to be outside of, and a treatment that is
    # itself for acne is dropped by gate 4 regardless. Since v12 a patient on
    # the edge of two levels matches a range covering either of them.
    if t.severity is not None:
        if sig["acne_level"] is not None:
            if not _range_share(t, sig):
                return False
        elif RECOMMENDATION_RULES["severity_range_requires_acne"]:
            return False
    # 4. Is the user actually acne-affected, for an acne-purpose treatment?
    if "acne" in t.needed and sig["acne_level"] is None:
        return False
    # 5. For a proxy treatment: is its rule active?
    if t.status == "Proxy" and not any(c in sig["active_proxies"] for c in t.needed):
        return False
    return True


def score_treatment(t, sig: dict) -> Optional[dict]:
    """Relevance 0-100, or None when the treatment is not eligible for this user."""
    rk = RECOMMENDATION_RULES["ranking"]
    if not passes_gates(t, sig):
        return None
    is_proxy = t.status == "Proxy"

    w, need, proxies = rk["concern_weight"], sig["need"], sig["active_proxies"]
    affinity = {c: rk["affinity_secondary"] for c in t.secondary}
    affinity.update({c: rk["affinity_needed"] for c in t.needed})
    contrib = {c: w.get(c, 0) * need[c] * affinity.get(c, 0) for c in need}

    # A proxy need is a fraction of its source's need, so a treatment tagged
    # with both would be credited twice for one measurement — TCA Cross reached
    # 83 on a face whose only finding was pores, against 50 for the pore credit
    # alone. Counted once, the larger: the same rule two sources of one proxy
    # already follow (see derive_signals).
    if rk["proxy_source_counted_once"]:
        for concern, source in sig.get("proxy_sources", {}).items():
            if contrib.get(concern) and contrib.get(source):
                lower = concern if contrib[concern] < contrib[source] else source
                contrib[lower] = 0.0

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

    # A treatment for the acne level the patient only leans towards (v12) is
    # less relevant than one for the level they are mostly in. Full relevance
    # from `level_full_share` of the patient's acne covered — any range covering
    # the dominant level covers at least half — and linearly less below it, to
    # nothing at the window's edge, so nothing jumps at the line. With hard
    # levels a range covers all or nothing and this is always 1.
    level_factor = 1.0
    if t.severity is not None and sig["acne_level"] is not None:
        full = RECOMMENDATION_RULES["level_full_share"]
        level_factor = min(1.0, _range_share(t, sig) / full)

    # A proxy treatment is only as relevant as the evidence for the condition
    # it treats. With hard levels that evidence is all or nothing (1); a rule
    # fired by a source on the edge of its level (v12) fires partly, and without
    # this its treatments would still appear in full the moment it fired — most
    # of their relevance comes from the measured concerns they also carry.
    strength = 1.0
    if is_proxy:
        strength = max((sig.get("proxy_strength", {}).get(c, 1.0) for c in t.needed if c in proxies),
                       default=1.0)

    return {
        "t": t,
        "score": round(100 * coverage * precision * strength * level_factor),
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


def _range_share(t, sig: dict) -> float:
    """The share of the patient's acne membership inside a treatment's severity
    range: 1 or 0 for a hard level, in between on the edge of two (v12)."""
    shares = sig.get("membership", {}).get("acne") or {sig["acne_level"]: 1.0}
    return sum(v for lvl, v in shares.items() if lvl in t.severity)


def _severity_width(t) -> int:
    """How many severity groups a treatment's range spans. One group per level
    until v11, where S3 and S4 are one: the clinic treats severe acne alike at
    both, so an S3-S4 range is as specific as a single level."""
    groups = RECOMMENDATION_RULES["ranking"]["score_modifier"]["severity_groups"]
    return sum(1 for group in groups if t.severity & set(group))


def _tie_keys(sig: dict, order: list) -> list:
    """Sort keys for equal shown scores, in the order the rules name them.

    price_desc and price both put a treatment without a price last, so a
    missing field never promotes one; featured puts the clinic's featured
    treatments first and reads a missing flag as not featured."""
    inf = float("inf")
    keys = {
        "r5": lambda s: not (sig["r5_oil_and_pore"] and _addresses_oil_secondarily(s["t"])),
        "severity_width": lambda s: len(s["t"].severity) if s["t"].severity else 9,
        "price": lambda s: s["t"].price if s["t"].price is not None else inf,
        "price_desc": lambda s: -s["t"].price if s["t"].price is not None else inf,
        "featured": lambda s: not s["t"].featured,
        "id": lambda s: s["t"].id,
    }
    return [keys[k] for k in order]


def recommendation_score(t, relevance: int, sig: dict) -> int:
    """The number shown: relevance, nudged down by at most `cap` by two factors.

        score = relevance x ((1 - cap) + cap x F),   F = weighted mean of factors

    Relevance is what the photo says this treatment does for this face. Exact
    ties in it are the normal case — treatments with the same tag profile score
    the same to the decimal, and 35% of multi-finding results showed every
    treatment at one number. F separates them with two things relevance does
    not see:

      severity_fit  how specific the treatment is to the patient's acne level.
                    Gate 3 already requires the level to be inside the range;
                    a range of one level fits better than one spanning all four.
                    Neutral when the patient has no acne or the treatment no range.
      package       a sales factor: in no package scores 0; in one, a base plus
                    a share for its size. Neutral when the package data is
                    missing. **Weighted 0 in every version since 2026-10-26**, at
                    the operator's request — bundle membership no longer moves a
                    number or an order — so F is severity_fit alone. The factor
                    is still computed so a version could weigh it again.

    Bounded so it can only reorder near-ties: a relevance gap larger than `cap`
    is never overturned. It never raises a score, so nothing passes 100. The
    label is still read from relevance alone — a bundle may move a number, but
    not whether a treatment is "Relevan" — and so is every eligibility test.
    With floor_at_label (v10) the number is also held inside that label's band,
    so the two never contradict each other.
    """
    M = RECOMMENDATION_RULES["ranking"]["score_modifier"]
    if not M["cap"]:
        return relevance

    factors = {}
    if sig["acne_level"] and t.severity:
        # Width only. How much of a patient on the edge of two levels the range
        # covers is already in the relevance (v12), so it is not counted here
        # a second time.
        factors["severity_fit"] = M["severity_fit"].get(_severity_width(t), min(M["severity_fit"].values()))
    else:
        factors["severity_fit"] = M["neutral"]
    if t.package_size is None:
        factors["package"] = M["neutral"]
    elif t.package_size == 0:
        factors["package"] = 0.0
    else:
        base = M["package_member_base"]
        factors["package"] = base + (1 - base) * min(1.0, t.package_size / M["package_size_full"])

    weights = M["weights"]
    total = sum(weights.values())
    blend = sum(weights[k] * factors[k] for k in weights) / total if total else M["neutral"]
    shown = round(relevance * ((1 - M["cap"]) + M["cap"] * blend))
    # The label is read from relevance, so without this a relevance of 31 could
    # be shown as "29 · Pendukung" — a number below the band its own label
    # names. Held at the band's lower edge, the modifier still orders within it.
    if M["floor_at_label"]:
        shown = max(shown, label_floor(relevance))
    return shown


def rank_treatments(sig: dict, catalog: Catalog) -> tuple[list, list]:
    """Return (the ranked list shown to the user, every scored treatment)."""
    rk = RECOMMENDATION_RULES["ranking"]
    scored = []
    for t in catalog.treatments.values():
        s = score_treatment(t, sig)
        if s is None:
            continue
        s["relevance"] = s["score"]
        s["score"] = recommendation_score(t, s["relevance"], sig)
        scored.append(s)
    # Ties, in the order `tie_break` names: R5 (oil + pore both affected ->
    # AC_*_OIL first) -> narrowest severity range (more specific to this
    # level; v9 only — from v10 that is already in the score through
    # severity_fit, and counting it here too would count it twice) -> price
    # -> the clinic's featured treatments (v11) -> id.
    #
    # Price is the tie-break because exact ties are common and the catalog
    # gives nothing else to separate them: the ten scar treatments all carry
    # the identical tag profile (Utama texture, Sekunder pore) with no severity
    # range, so they score the same to the decimal. Before this, the order was
    # alphabetical by treatment_id and the consequence was arbitrary —
    # `canula-subsisi` at Rp3,000,000 was offered while `subsisi-mild` at
    # Rp1,500,000 never appeared in any of 625 score combinations, because c
    # precedes s. v9 and v10 went cheapest first, as the conservative order.
    # v11 goes dearest first, then featured at an equal price: that is the
    # clinic's own answer (2026-10-24) — among treatments that fit equally,
    # the higher price, and at the same price their featured ones. Fit itself
    # is never traded for price; it is the score, which sorts before all this.
    #
    # Deliberately NOT ordered by invasiveness first. It reads as the safer
    # rule, but every subsisi tier is invasive, so it would bury the whole
    # golongan behind the non-invasive scar treatments — the exact treatments
    # R3's second source was widened to reach. Invasiveness is already handled
    # where it belongs, in gate 2 (R4).
    #
    # A treatment with no price sorts last rather than first, so a missing
    # field never promotes a treatment over one whose price is known.
    order = _tie_keys(sig, rk["tie_break"])
    scored.sort(key=lambda s: (-s["score"], *(k(s) for k in order)))

    top, per_golongan = [], {}
    for s in (s for s in scored if not s["proxy_rules"]):
        if len(top) == rk["top_n"]:
            break
        # Eligibility is relevance, not the shown score: the modifier orders
        # near-ties, it does not decide what is relevant enough to show. The
        # list is sorted by the shown score, so this skips rather than stops.
        if s["relevance"] < rk["min_score"]:
            continue
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
        picks = [s for s in scored if rule_id in s["proxy_rules"] and s["relevance"] >= rk["proxy_min_score"]]
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
                and not s["t"].is_invasive and s["relevance"] >= rk["addon_min_score"]
                and s["t"].id not in already_ranked):
            out.append({"treatment_id": s["t"].id, "name": s["t"].name, "score": s["score"],
                        "reason": "Treatment tambahan dengan skor tertinggi"})
    return out


def fill_to_minimum(top: list, sig: dict, catalog: Catalog) -> tuple[list, list]:
    """Top the ranking up to the minimum, from the concerns that score lowest.

    Returns (the ranking, the concerns the added picks were taken for).

    Need is measured against the "no problem" line (80), so above it every
    concern needs nothing: healthy skin produced an empty list, and a face with
    a single problem got as many treatments as that one concern has — two, for
    pore or oiliness alone. Neither is a useful answer to a patient who asked.

    The fill re-measures need against a perfect score instead, so 82 still
    counts for more than 95, and takes the lowest_concerns concerns that score
    lowest. It picks round-robin — the best treatment for the lowest concern,
    then the next concern, and round again — so three picks cover three
    concerns rather than three variants for one. Within a concern, the order
    is the ranking's own: score, then price, then id.

    Everything the ranking guards stays guarded. The five gates run as usual
    (acne-purpose treatments stay out when there is no acne), the golongan cap
    still holds, and nothing already ranked is picked twice. Proxy treatments
    never fill: an inference about a condition the photo cannot see is not
    maintenance. Each pick carries the maintenance label instead of a
    relevance one, so skin with no problem never reads as "Sangat relevan".

    A care pick carries no score. Its number would be measured against a
    perfect face rather than against the "no problem" line, so it is not the
    same quantity as a finding's relevance — showing both as percentages side
    by side would invite comparing them. The label says what it is.

    When even that finds nothing — every measured concern at a perfect 100, so
    every need is zero against 100 as well — the picks come from the
    care_focus_concerns instead: treatments whose primary purpose is general
    upkeep (radiance, in this catalog), held to the same five gates, in
    care_focus_order, one per measured concern they also serve before any
    repeats. Until v13 that order was cheapest first, although the docs already
    said dearest; v13 goes dearest, then featured, as the clinic asked.

    v13 also widens gate 1 for these picks to treatments the photo does not
    measure (care_focus_statuses) and keeps invasive ones out
    (care_focus_allow_invasive). Almost every upkeep treatment is one the photo
    cannot see — radiance is not measured — and the catalog of 2026-10-30
    removed six of the seven it could, which left a perfect face with one.
    """
    M = RECOMMENDATION_RULES["maintenance"]
    rk = RECOMMENDATION_RULES["ranking"]
    if len(top) >= M["min_treatments"]:
        return top, []

    perfect = M["perfect_score"]
    lowest = sorted(sig["measured"], key=lambda c: sig["raw"][c])[: M["lowest_concerns"]]
    care_sig = dict(sig, active_proxies={}, proxy_sources={},
                    need={c: max(0.0, (perfect - sig["raw"][c]) / perfect)
                          for c in sig["measured"]})

    taken = {s["t"].id for s in top}
    per_golongan: dict = {}
    for s in top:
        per_golongan[s["t"].golongan] = per_golongan.get(s["t"].golongan, 0) + 1

    pool = []
    for t in catalog.treatments.values():
        if t.id in taken:
            continue
        s = score_treatment(t, care_sig)
        if s is None or s["proxy_rules"]:
            continue
        pool.append(s)
    # The ranking's tie order without R5, which is about a finding (oil and
    # pore both affected) and care picks are not findings.
    order = _tie_keys(sig, [k for k in rk["tie_break"] if k != "r5"])
    pool.sort(key=lambda s: (-s["score"], *(k(s) for k in order)))

    filled, used_for = list(top), []
    while len(filled) < M["min_treatments"]:
        progressed = False
        for concern in lowest:
            if len(filled) >= M["min_treatments"]:
                break
            pick = next((s for s in pool
                         if concern in s["t"].needed
                         and s["t"].id not in taken
                         and per_golongan.get(s["t"].golongan, 0) < rk["max_per_golongan"]),
                        None)
            if pick is None:
                continue
            pick["label"] = M["label"]
            pick["maintenance"] = True
            filled.append(pick)
            taken.add(pick["t"].id)
            per_golongan[pick["t"].golongan] = per_golongan.get(pick["t"].golongan, 0) + 1
            if concern not in used_for:
                used_for.append(concern)
            progressed = True
        if not progressed:
            break

    if len(filled) < M["min_treatments"] and M["care_focus_concerns"]:
        focus = set(M["care_focus_concerns"])
        order = _tie_keys(sig, M["care_focus_order"])
        upkeep = sorted(
            (t for t in catalog.treatments.values()
             if t.id not in taken and t.needed & focus and t.status != "Proxy"
             and (M["care_focus_allow_invasive"] or not t.is_invasive)
             and passes_gates(t, care_sig, photo_statuses=M["care_focus_statuses"])),
            key=lambda t: tuple(k({"t": t}) for k in order))
        for concern in lowest + [None]:   # one per measured concern, then anything left
            for t in upkeep:
                if len(filled) >= M["min_treatments"]:
                    break
                if t.id in taken or (concern is not None and concern not in t.needed):
                    continue
                if per_golongan.get(t.golongan, 0) >= rk["max_per_golongan"]:
                    continue
                filled.append({"t": t, "score": None, "label": M["label"], "maintenance": True,
                               "concerns": sorted(t.needed & focus), "proxy_rules": []})
                taken.add(t.id)
                per_golongan[t.golongan] = per_golongan.get(t.golongan, 0) + 1
                for c in sorted(t.needed & focus):
                    if c not in used_for:
                        used_for.append(c)
                if concern is not None:
                    break                  # one per concern in this round

    # Fewer than the minimum after all of this is the honest answer: nothing
    # left is eligible. Care picks never carry a relevance number.
    for s in filled[len(top):]:
        s["score"] = None
    return filled, used_for


def label_for(score: Optional[float]) -> Optional[str]:
    """Patient-facing relevance label. The numeric score is for staff."""
    if score is None:
        return None
    return next((name for cutoff, name in RECOMMENDATION_RULES["ranking"]["labels"] if score >= cutoff), None)


def label_floor(relevance: float) -> int:
    """The lower edge of the label band `relevance` falls in; 0 below the last."""
    return next((cutoff for cutoff, _ in RECOMMENDATION_RULES["ranking"]["labels"] if relevance >= cutoff), 0)


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
        "health_for_band": sig["health_for_band"],
        "needs_clinical_confirmation": True,
        "consultation_priority": priority,
        "levels": sig["level"],
        **({"level_membership": {c: m for c, m in sig["membership"].items() if len(m) > 1}}
           if R["level_window"] else {}),
        "active_proxy_rules": sorted(set(sig["active_proxies"].values())),
        "ranked_treatments": [
            {"rank": i + 1, "treatment_id": s["t"].id, "name": s["t"].name,
             "golongan": s["t"].golongan, "score": s["score"],
             "relevance": s.get("relevance", s["score"]),
             "label": s.get("label") or label_for(s.get("relevance", s["score"])),
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
    top, care_for = fill_to_minimum(top, sig, catalog)
    result = apply_guardrails(top, related_options(scored, sig),
                              pick_addons(scored, sig, top), sig, catalog)
    if care_for:
        M = RECOMMENDATION_RULES["maintenance"]
        measured = [c for c in care_for if c not in M["care_focus_concerns"]]
        if measured:
            result["notes"].append(
                f"{M['label']}: dilengkapi untuk concern dengan skor terendah "
                f"({', '.join(measured)}), bukan masalah yang terdeteksi")
        else:
            result["notes"].append(
                f"{M['label']}: semua concern terukur sempurna, dipilih treatment "
                f"perawatan umum, bukan masalah yang terdeteksi")
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

    `relevance` keeps, per ranked treatment, the number the shown score was
    derived from. From v10 they differ (the score modifier), and the label, the
    min_score cut and every eligibility test read relevance — without it a
    stored result cannot be audited against its own label. Internal: main.py
    does not return it, for the same reason scores_used is not returned.
    """
    meta = {key: value for key, value in result.items()
            if key not in ("ranked_treatments", "skin_health_score")}
    meta["relevance"] = {item["treatment_id"]: item.get("relevance", item["score"])
                         for item in result.get("ranked_treatments", [])}
    return meta
