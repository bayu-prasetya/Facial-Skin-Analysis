"""
Recommendation rules 2026-10-v9. Frozen: analyses are stored under this version.

R4 starts at S4 instead of S3, so injections, infusions and subcision are listed
for S3 acne. An add-on can no longer be a treatment already in the ranking. Proxy
rules name their source concern instead of always reading acne, and R3
(scarring) gained pore as a second source, so a patient whose acne has resolved
can still be offered scar treatment. Equal scores are broken by price, cheapest
first, instead of alphabetically, and proxy_options_per_rule went from 2 to 3.

The `maintenance` block did not exist in v9. It is carried here, switched off,
because every version has to share one shape; see `rule_versions/__init__.py`.
"""

RULES = {
    "version": "2026-10-v9",

    # --- Stage 2: level and need ------------------------------------------
    # Fallback S1-S4 ranges on raw_score (higher = healthier) while the
    # calibrated ranges in concern_levels are empty. (level, min_inclusive).
    "default_levels": [("S1", 65), ("S2", 50), ("S3", 35), ("S4", 0)],
    "no_problem_min": 80,
    "level_window": 0,  # hard levels (blended near the edges from v12)
    "level_full_share": 0.5,  # inert with hard levels: a range covers all or nothing

    # Which overall-health number the urgency band reads. v9 used the vendor's
    # `all` score as delivered (the mean of the ui_scores).
    "health_source": "vendor_all",

    # (min health score, band, max_addons, consultation_priority)
    "urgency_bands": [
        (85, "maintenance", 1, "normal"),
        (70, "ringan", 1, "normal"),
        (50, "sedang", 1, "normal"),
        (0, "intensif", 0, "high"),
    ],

    # Proxy rules: conditions the photo does not measure, inferred from one it
    # does. (rule_id, inferred concern, source concern, min source level, factor)
    # Proxy need = factor x the SOURCE concern's need, and only while the
    # inferred concern is itself unmeasured. The factor is below 1.0 on purpose:
    # this is an estimate, not a detection.
    #
    # R1 and R2 are post-acne sequelae — post-inflammatory erythema and
    # hyperpigmentation — so acne is their source by definition.
    #
    # R3 is scarring, and scarring outlives the acne that caused it. Sourced
    # from acne alone it reached nobody whose acne had resolved, which is
    # exactly the patient who comes in for subsisi: ten treatments including all
    # four subsisi tiers were unreachable at acne 85. Pore is the only measured
    # concern that carries textural irregularity, so it is R3's second source.
    # Same rule id on purpose: one inferred concern, one label, two triggers.
    #
    # A rule's source must be a MEASURED concern — an inferred one is never a
    # source, so proxies cannot chain and infer from each other.
    "proxy_rules": [
        ("R1", "redness", "acne", "S2", 0.5),
        ("R2", "pigmentation", "acne", "S2", 0.5),
        ("R3", "texture", "acne", "S3", 0.5),
        ("R3", "texture", "pore", "S2", 0.5),
    ],

    # R4: from this acne level up, injections/infusions/subcision are never
    # listed, and consultation priority is forced to high.
    # Moved S3 -> S4 in v9: at S3 the clinic does want these offered. Note that
    # this one cut-off still drives both halves of R4, so consultation priority
    # is no longer forced high at S3 either — the urgency band decides there.
    "invasive_hidden_from": "S4",
    "invasive_golongan": ["Injeksi", "Infus", "Subsisi"],
    "always_hide_invasive": False,  # True = hide them at every level
    # Gate 3 binds a patient without acne too: any severity range shuts them
    # out (relaxed in v11).
    "severity_range_requires_acne": True,

    # --- Stages 4 and 5: relevance score, ranking, add-ons ----------------
    "ranking": {
        "need_threshold": 80,  # need_c = max(0, (80 - raw_c) / 80)
        "concern_weight": {
            "acne": 1.0, "oiliness": 0.6, "pore": 0.6, "moisture": 0.5, "radiance": 0.3,
            "redness": 0.4, "pigmentation": 0.4, "texture": 0.4, "wrinkle": 0.5,
        },
        "affinity_needed": 1.0,        # concern is a Utama tag (the treatment's primary purpose)
        "affinity_secondary": 0.5,     # concern is a Sekunder tag (a benefit named in FUNGSI)
        "max_per_golongan": 2,
        "top_n": 5,
        "min_score": 30,
        "labels": [(80, "Sangat relevan"), (50, "Relevan"), (30, "Pendukung")],
        # R5 tie-break: when oil and pores are both affected, a treatment whose
        # *secondary* purpose is oil comes first. The clinic's own sub-concern
        # code is the direct marker; an oiliness tag at weight 0.5 is the same
        # statement in the tag vocabulary, and covers the treatments whose
        # sub-concern cell is empty.
        #
        # Note what the second marker costs: in the workbook every OIL-coded
        # treatment also carries oiliness as Sekunder, so the broader marker
        # matches strictly more treatments and the tie-break separates fewer of
        # them. T021 and T030 are the pair it stops separating. Set this to None
        # to go back to the sub-concern code alone.
        "r5_oil_markers": ["OIL"],
        "r5_oil_secondary_concern": None,
        "proxy_options_per_rule": 3,   # proxy treatments go to related_options, not the ranking
        "proxy_min_score": 30,         # v9: the same bar as the ranking
        # Score modifier: absent in v9. cap 0 shows relevance unchanged; the
        # block is carried so every version shares one shape.
        "score_modifier": {
            "cap": 0.0,
            "weights": {"severity_fit": 0.5, "package": 0.5},
            "severity_groups": [["S1"], ["S2"], ["S3"], ["S4"]],  # one level each
            "severity_fit": {1: 1.0, 2: 0.75, 3: 0.5, 4: 0.25},
            "neutral": 0.5,
            "package_member_base": 0.5,
            "package_size_full": 6,
            "floor_at_label": False,
        },
        # v9's tie-break order, and its proxy credit: a treatment tagged with a
        # proxy concern and its source was credited for both.
        "tie_break": ["r5", "severity_width", "price", "id"],
        "proxy_source_counted_once": False,
        "addon_golongan": "Tambahan",
        "addon_min_score": 50,
    },

    # --- Stage 5b: the minimum ---------------------------------------------
    # v9 predates the minimum: a short or empty ranking stays short or empty.
    # The block exists so that every version has the same keys, and these
    # values switch v10's fill off entirely.
    "maintenance": {
        "min_treatments": 0,
        "lowest_concerns": 3,
        "perfect_score": 100,
        "label": "Perawatan",
        "care_focus_concerns": [],
    },

    # --- Stage 6: guardrails ----------------------------------------------
    "disclaimer": "Rekomendasi awal berdasarkan foto. Paket final ditentukan dokter saat konsultasi.",
}
