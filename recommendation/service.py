"""
Where a catalog comes from for one analysis, and the recommendation built on it.

This is the seam main.py calls.

The catalog always comes from ayuko-api's POST /api/recommendations, asked fresh
for each analysis. That answer is shaped by the patient's own concerns, so it is
never cached: one patient's treatments must not be served to the next.

The only thing that comes before it is AYUKO_RESPONSE_FILE, a saved copy of
such a response that stands in for the service so the pipeline can be run
offline. It is inert unless the variable is set, so it never applies in
production.

With the service unreachable, the analysis completes with an empty
recommendation list and the note "catalog_unavailable" — never an error.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import ayuko
from .catalog import Catalog, catalog_from_response_file
from .engine import (
    compute_recommendations,
    to_recommendation_list,
    to_recommendation_meta,
)

logger = logging.getLogger(__name__)


async def catalog_for_analysis() -> Optional[Catalog]:
    """The catalog to score an analysis against, or None if there is none.

    It takes no scores: the candidate set is the clinic's whole catalog, the
    same for every patient, so the eligibility decisions are made here by the
    rules rather than inherited from a pre-filtered list. That also means it is
    cacheable, and an analysis spends no round trip of its own on it.
    """
    offline = catalog_from_response_file()
    if offline is not None:
        logger.info("Using the saved response from AYUKO_RESPONSE_FILE.")
        return offline

    return await ayuko.full_catalog()


async def build_recommendations(
    skin_metrics, skin_health_score: Optional[int]
) -> tuple[list[dict], dict]:
    """Everything between a completed analysis and the API's `recommendations`.

    Returns both halves of the result: the ranked treatments, and the guardrail
    block they were produced under (rules_version, urgency band, consultation
    priority, notes, disclaimer). The caller stores both — the treatments alone
    stop being explainable the moment the rules are recalibrated, and the
    disclaimer is a guardrail that has to reach whoever reads them.
    """
    catalog = await catalog_for_analysis()
    result = compute_recommendations(skin_metrics, skin_health_score, catalog)
    if result["notes"]:
        logger.info("Recommendation notes: %s", result["notes"])
    return to_recommendation_list(result, catalog), to_recommendation_meta(result)
