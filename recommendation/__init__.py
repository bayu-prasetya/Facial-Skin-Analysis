"""
Treatment recommendation from Perfect Corp skin analysis scores.

    metrics from the photo  ->  ayuko-api  ->  catalog  ->  ranked treatments

What main.py uses:

    build_recommendations(metrics, health)   the API's `recommendations`, and
                                             the guardrail block stored with them

That one call covers the whole path: ask the clinic's service for this
patient's treatments, run stages 2-6 on them, flatten the result. It returns
both halves of that result — the ranked treatments, and the block that explains
them (rules_version, urgency band, consultation priority, notes, disclaimer) —
because a recommendation whose rules_version was never recorded cannot be
explained once the rules are recalibrated. The pieces underneath stay exported
for tests and scripts.

Only the catalog lookup touches the network, and it is async. The scoring
itself is pure: the same scores against the same catalog always give the same
result.

The clinical design — stages, formula, thresholds, the reason behind every
number — lives in the doc "SkinCode - Desain Sistem Rekomendasi Treatment".
recommendation/README.md says what the service has to supply.
"""

from . import ayuko
from .catalog import (
    CONCERN_ORDER,
    DEFAULT_PROXY_LABELS,
    Catalog,
    Treatment,
    catalog_from_response_file,
    catalog_from_rows,
)
from .engine import (
    compute_recommendations,
    extract_raw_scores,
    recommend,
    skin_health_from_metrics,
    to_recommendation_list,
    to_recommendation_meta,
)
from .rules import RECOMMENDATION_RULES
from .service import build_recommendations, catalog_for_analysis

__all__ = [
    "CONCERN_ORDER",
    "DEFAULT_PROXY_LABELS",
    "RECOMMENDATION_RULES",
    "Catalog",
    "Treatment",
    "ayuko",
    "build_recommendations",
    "catalog_for_analysis",
    "catalog_from_response_file",
    "catalog_from_rows",
    "compute_recommendations",
    "extract_raw_scores",
    "recommend",
    "skin_health_from_metrics",
    "to_recommendation_list",
    "to_recommendation_meta",
]
