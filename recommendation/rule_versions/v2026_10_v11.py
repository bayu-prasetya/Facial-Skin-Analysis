"""
Recommendation rules 2026-10-v11.

Written from the clinic's answers to our question list (LIST PERTANYAAN.xlsx,
2026-10-24). Everything else is v10 unchanged.

Equal scores go to the dearer treatment first, then to the clinic's featured
ones (`isFeatured`). Their answer to question 1: fit to the skin analysis comes
first, which the score already is; among treatments that fit equally, the
higher price first; at an equal price, the clinic's own featured treatments.
This reverses v9/v10, which offered the cheapest first as the conservative
order. A treatment with no price still sorts last, so a missing field never
promotes one.

A severity range no longer shuts out a patient without acne, unless the
treatment is itself for acne (gate 4 still drops those). Question 6: Acne Oil
Pore Laser and Acne Sebum Balance Microneedle "fungsinya sama-sama mengecilkan
pori, mengurangi minyak, mencerahkan kulit. Boleh untuk pasien yang tidak
berjerawat." Their Utama purpose is pore and oiliness, not acne, and four more
carry the same profile (T007, T010, T023, T026). The range says which acne
level the treatment is safe for; for a patient with no acne there is no level
to be outside of.

S3 and S4 count as one band when judging how specific a treatment's range is.
Question 3: S3 and S4 use the same treatments, both being severe acne. An
S3-S4 range is therefore as specific as a single level, not half as specific.

v10 is `rule_versions/v2026_10_v10.py`; the loader keeps both runnable.

Design reference: the doc "SkinCode — Desain Sistem Rekomendasi Treatment",
tab "Alur rekomendasi lengkap". `default_levels` carries more weight than its
size suggests: it is the severity the clinic's API would be sent, so it is the
contract between the two systems.
"""

RULES = {
    "version": "2026-10-v11",

    # --- Stage 2: level and need ------------------------------------------
    # S1-S4 on raw_score (higher = healthier), (level, min_inclusive), while
    # calibrated ranges are absent. Re-pivoted on 70 in v10 (was 65/50/35):
    # Perfect Corp describes its score as a bell curve with 70 the average and
    # anything below it "an area for improvement", so a 65 is no longer mild.
    # Their 70 is on the ui scale, though — their overall score is the ui mean —
    # and ui runs 7-36 points above raw for pore, oiliness and moisture. 70 on
    # raw is therefore stricter than their average, which is the intent: 80 was
    # judged too lenient. A convention, not a calibration; doctor grading decides.
    "default_levels": [("S1", 70), ("S2", 55), ("S3", 40), ("S4", 0)],
    "no_problem_min": 80,
    "level_window": 0,  # hard levels (blended near the edges from v12)
    "level_full_share": 0.5,  # inert with hard levels: a range covers all or nothing

    # Which overall-health number the urgency band reads. The vendor's `all` is
    # the mean of the ui_scores, so v10 bands on the mean of the raw scores
    # instead — the same measurement every other rule reads. It is also the
    # skin_health_score clients receive (main.compute_skin_health_score), so
    # the score shown and the band it produced always agree.
    "health_source": "raw_mean",

    # (min health score, band, max_addons, consultation_priority). Sedang starts
    # at 55 in v10 (was 50), in step with the levels' 15-point bands.
    "urgency_bands": [
        (85, "maintenance", 1, "normal"),
        (70, "ringan", 1, "normal"),
        (55, "sedang", 1, "normal"),
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
    # Gate 3 for a patient without acne. False since v11: a severity range is
    # the acne levels a treatment suits, so it only binds a patient who has
    # acne. Acne-purpose treatments are still dropped for them, by gate 4. The
    # clinic confirmed this for its oil-and-pore treatments (question 6).
    "severity_range_requires_acne": False,

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
        # The bar a proxy treatment clears to be offered in related_options.
        # Lower than min_score since v10: counted once with its source, a proxy
        # treatment's honest relevance sits near 24 where it used to reach 41,
        # and at 30 R3 reached 66% of the faces it fires on (93% before). 20
        # brings R1 100% / R2 96% / R3 91%. These are shown apart, as conditions
        # that commonly accompany a finding, never as a finding.
        "proxy_min_score": 20,
        # Score modifier (v10): the shown score is relevance x ((1 - cap) + cap x
        # F), F a weighted mean of severity_fit (how specific the treatment is
        # to the patient's acne level, by the width of its range) and package
        # (a sales factor: in no clinic bundle 0, in one 0.5 plus a share for
        # the bundle's size, full at 6). Neutral 0.5 where a factor does not
        # apply or its data is missing. The cap bounds it to reordering near-
        # ties; labels and every eligibility test still read relevance.
        #
        # Both factors span 0..1 so the weights mean what they say: severity
        # used to span 0.25..1, which let the package factor dominate at an
        # "equal" 50:50. Clinical leads, 60:40, because the patient reads the
        # number as fit. floor_at_label keeps a number from dropping below the
        # band its own label names ("29 · Pendukung" read as a bug).
        "score_modifier": {
            "cap": 0.10,
            # No package weight (2026-10-26, at the operator's request, in
            # every version): bundle membership no longer moves a number or an
            # order. F is severity_fit alone. The package keys stay so every
            # version keeps one shape; with weight 0 they are inert.
            "weights": {"severity_fit": 1.0, "package": 0.0},
            # Width is counted in these groups. S3 and S4 are one: the clinic
            # treats severe acne the same at both (question 3), so an S3-S4
            # range is as specific as a single level. Three groups, so the
            # widths run 1..3; key 4 is unreachable and kept only because every
            # version carries the same keys.
            "severity_groups": [["S1"], ["S2"], ["S3", "S4"]],
            "severity_fit": {1: 1.0, 2: 0.5, 3: 0.0, 4: 0.0},
            "neutral": 0.5,
            "package_member_base": 0.5,
            "package_size_full": 6,
            "floor_at_label": True,
        },
        # After the shown score. Severity width is NOT here: it is already in
        # the score through severity_fit, and using it again to break ties
        # would count it twice. R5 stays only here — 10 of the 15 OIL-coded
        # treatments also carry an oiliness tag, so as a score factor it would
        # double the oil credit coverage already gives.
        #
        # v11, the clinic's order (question 1): R5 is part of the fit, so it
        # stays first; then the dearer treatment; then the clinic's featured
        # one at an equal price. Care picks follow the same order without R5.
        "tie_break": ["r5", "price_desc", "featured", "id"],
        # A proxy need is derived from a measured concern (texture from pore,
        # for instance). A treatment tagged with both the proxy concern and its
        # source would otherwise be credited twice for one measurement — TCA
        # Cross scored 83 on a pores-only face instead of 50. Counted once, the
        # larger of the two, as two sources of one proxy already are.
        "proxy_source_counted_once": True,
        "addon_golongan": "Tambahan",
        "addon_min_score": 50,
    },

    # --- Stage 5b: the minimum, and care for skin with nothing wrong -------
    # Need is distance below the "no problem" line (80), so above it every
    # concern needs nothing and a healthy face got an empty list. When the
    # ranking holds fewer than min_treatments, it is filled from the
    # lowest_concerns measured concerns with the lowest scores, one treatment
    # per concern in turn, lowest first — with need re-measured against a
    # perfect score, so 82 still counts for more than 95.
    #
    # Those picks are care, not a finding, and carry their own label so the
    # patient never reads "Sangat relevan" on skin that has no problem. Proxy
    # treatments are never used to fill: an inference about a condition the
    # photo cannot see is not maintenance.
    "maintenance": {
        "min_treatments": 3,
        "lowest_concerns": 3,
        "perfect_score": 100,
        "label": "Perawatan",
        # Used only when every measured concern is a perfect 100, so even need
        # against 100 is zero. Radiance is the catalog's general-upkeep purpose:
        # every brightening/glow treatment carries it as Utama.
        "care_focus_concerns": ["radiance"],
    },

    # --- Stage 6: guardrails ----------------------------------------------
    "disclaimer": "Rekomendasi awal berdasarkan foto. Paket final ditentukan dokter saat konsultasi.",
}
