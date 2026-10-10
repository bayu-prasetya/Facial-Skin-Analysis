"""
POST /api/recommendations — the clinic's service, used as the catalog source.

The flow this implements:

    raw_score per concern  ->  severity 1-4 (ours, see rules.py)
    ->  POST {"concerns": [{"concernId", "severity"}], ...}
    ->  treatments[] with matchedConcerns  ->  a Catalog
    ->  stages 2-6 run here, as usual

Two things about that endpoint shape the design:

`concerns[].score` is rejected (422) until their thresholds are calibrated, so
the raw_score -> severity mapping stays on our side and only severity is sent.
And only problem concerns are sent at all: a concern at or above the healthy
cut-off is simply left out.

The response is a ranked result, not a catalog dump — it holds the treatments
that matched what we asked about, and `matchedConcerns` lists only the concerns
that matched. Two consequences worth knowing before reading a score:

  * `limit` is asked for at its maximum, so our own ranking has a full set of
    candidates to work from rather than re-sorting their top 10.
  * the precision term in the relevance score goes inert. It measures how much
    of a treatment's primary purpose the user actually needs, which requires
    knowing purposes the user does NOT need — and those never come back. Every
    purpose we can see is one we asked about, so precision is always 1. A
    treatment aimed at oil and pores on skin that is not oily scores higher
    here than the design doc's worked example says it should.

Configuration:

    AYUKO_API_BASE_URL   default https://ayuko-api.vercel.app
                         (their docs give http://localhost:8181 for local work)
    AYUKO_API_TIMEOUT    seconds, default 15

As everywhere else in this package, nothing here raises at the caller: a
service that cannot be reached leaves the analysis with an empty recommendation
list rather than failing it.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx

from .catalog import CONCERN_ORDER, Catalog, catalog_from_rows
from .rules import LEVEL_ORDER, RECOMMENDATION_RULES

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://ayuko-api.vercel.app"
RECOMMENDATIONS_PATH = "/api/recommendations"
CONCERNS_PATH = "/api/skin-concerns"
TREATMENTS_PATH = "/api/treatments"
DEFAULT_TIMEOUT_SECONDS = 15.0

# The whole catalog in one page; it is 112 treatments today.
TREATMENTS_PAGE_SIZE = 500

# How long the full catalog is reused. Unlike a recommendations response, this
# one is not shaped by the patient, so caching it serves nobody else's answer.
DEFAULT_TREATMENTS_CACHE_SECONDS = 900.0
_treatments_cache: dict = {"at": None, "by_id": None}

# The endpoint's own ceiling. Asked for in full: we re-rank locally, so a short
# list from them would just truncate our candidates before we start.
MAX_LIMIT = 50


def base_url() -> str:
    return os.environ.get("AYUKO_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _timeout() -> float:
    try:
        return float(os.environ.get("AYUKO_API_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def _cache_seconds() -> float:
    try:
        return float(os.environ.get("AYUKO_TREATMENTS_CACHE_SECONDS",
                                    DEFAULT_TREATMENTS_CACHE_SECONDS))
    except ValueError:
        return DEFAULT_TREATMENTS_CACHE_SECONDS


async def fetch_treatments(client: Optional[httpx.AsyncClient] = None) -> dict:
    """The full catalog from GET /api/treatments, as {treatment_id: item}.

    POST /api/recommendations answers with a ranked result, not a catalog: each
    item carries 13 fields and `matchedConcerns`, which holds only the concerns
    that matched the request. Four things the engine needs are therefore absent
    from it and present here:

        concernTags        every concern tag with its weight and kind. This is
                           what makes the precision term work at all, and what
                           lets a proxy treatment carry a redness/pigmentation
                           purpose so R1-R3 can fire.
        acneSeverityMin/Max  gate 3
        subConcerns        the R5 tie-break ("OIL")
        clinicalFunction   the description shown to the patient

    An empty dict on any failure: the enrichment is an improvement on the
    answer, never a precondition for having one.
    """
    cached_at, by_id = _treatments_cache["at"], _treatments_cache["by_id"]
    if by_id is not None and cached_at is not None and time.monotonic() - cached_at < _cache_seconds():
        return by_id

    url = f"{base_url()}{TREATMENTS_PATH}?limit={TREATMENTS_PAGE_SIZE}"
    try:
        if client is not None:
            response = await client.get(url)
        else:
            async with httpx.AsyncClient(timeout=_timeout()) as owned:
                response = await owned.get(url)
        body = response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("ayuko-api treatment catalog could not be read: %s", e)
        return {}

    if not response.is_success or not isinstance(body, dict) or not body.get("isSuccess"):
        logger.warning("ayuko-api treatment catalog rejected the request (HTTP %s).",
                       response.status_code)
        return {}

    data = body.get("data")
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        logger.warning("ayuko-api treatment catalog has no item list.")
        return {}

    by_id = {str(i["id"]): i for i in items if isinstance(i, dict) and i.get("id")}
    _treatments_cache.update(at=time.monotonic(), by_id=by_id)
    logger.info("ayuko-api treatment catalog: %d treatments cached.", len(by_id))
    return by_id


def merge_treatment_details(items: list, by_id: dict) -> list:
    """Fill each recommendation item out with its full catalog record.

    The recommendation's own fields win on conflict: it is the answer to this
    patient's request, while the catalog record is the same for everyone.
    """
    if not by_id:
        return items
    merged = []
    for item in items:
        if not isinstance(item, dict):
            continue
        nested = item.get("treatment") if isinstance(item.get("treatment"), dict) else {}
        tid = nested.get("id") or item.get("id")
        detail = by_id.get(str(tid)) if tid else None
        if detail is None:
            merged.append(item)
            continue
        full = dict(detail)
        full.update(nested)
        merged.append({**item, "treatment": full})
    return merged


def severity_for(raw_score: Optional[float]) -> Optional[int]:
    """raw_score -> severity 1-4, or None when the concern is not a problem.

    Perfect Corp scores run the other way round (higher = healthier), and
    severity always means "how bad" — including for moisture, where a high
    severity is drier skin, not more hydrated.
    """
    if raw_score is None:
        return None
    if raw_score >= RECOMMENDATION_RULES["no_problem_min"]:
        return None
    # Derived from the same default_levels the local pipeline uses, rather than
    # a second copy of the thresholds: the two must not be able to disagree.
    # ("S2" -> 2, which is exactly the API's encoding.)
    return next(LEVEL_ORDER[level] for level, minimum in RECOMMENDATION_RULES["default_levels"]
                if raw_score >= minimum)


def concerns_for(raw_scores: dict) -> list[dict]:
    """Build the request's `concerns`, problem concerns only, in a stable order
    so the same photo always sends the same body."""
    out = []
    for concern in CONCERN_ORDER:
        severity = severity_for(raw_scores.get(concern))
        if severity is not None:
            out.append({"concernId": concern, "severity": severity})
    return out


async def fetch_recommendations(concerns: list[dict], limit: int = MAX_LIMIT,
                                package_limit: int = 0,
                                client: Optional[httpx.AsyncClient] = None) -> Optional[dict]:
    """POST the concerns and return the `data` object, or None on any failure.

    `isSuccess` is checked as well as the status code: the service reports
    failure in the body, so a 200 does not by itself mean a usable answer.
    """
    if not concerns:
        return None  # the endpoint requires at least one concern

    url = base_url() + RECOMMENDATIONS_PATH
    payload = {"concerns": concerns, "limit": limit, "packageLimit": package_limit}
    response = None
    try:
        if client is not None:
            response = await client.post(url, json=payload)
        else:
            async with httpx.AsyncClient(timeout=_timeout()) as owned:
                response = await owned.post(url, json=payload)
        body = response.json()
    except httpx.TimeoutException:
        logger.warning("ayuko-api did not answer within %.0fs.", _timeout())
        return None
    except httpx.HTTPError as e:
        logger.warning("ayuko-api request failed: %s", e)
        return None
    except ValueError:
        # Reached when the body is not JSON at all — an HTML error page from a
        # proxy, most often. response is bound by then; the guard is for the
        # case where the request itself is what failed.
        logger.warning("ayuko-api response is not valid JSON (HTTP %s).",
                       response.status_code if response is not None else "no response")
        return None

    if not response.is_success or not isinstance(body, dict) or not body.get("isSuccess"):
        detail = body.get("message") if isinstance(body, dict) else response.text[:200]
        # 422 is the documented answer to sending a raw score; worth naming,
        # because it would otherwise look like a generic rejection.
        logger.warning("ayuko-api rejected the request (HTTP %s): %s",
                       response.status_code, detail or "no message")
        return None

    return body.get("data")


def catalog_from_recommendations(data: dict, details: Optional[dict] = None) -> Optional[Catalog]:
    """Turn the response into a Catalog our stages can score against.

    Tags come from each item's matchedConcerns (concernId + kind
    primary/secondary), not from the treatment's `tag` field — that one is the
    golongan. Everything descriptive comes from the nested treatment object.
    """
    if not isinstance(data, dict):
        return None
    items = data.get("treatments")
    if not isinstance(items, list) or not items:
        logger.warning("ayuko-api returned no treatments.")
        return None
    catalog = catalog_from_rows(merge_treatment_details(items, details or {}))
    return catalog if catalog.treatments else None


async def full_catalog(client: Optional[httpx.AsyncClient] = None) -> Optional[Catalog]:
    """Every treatment the clinic offers, as the candidate set for our rules.

    This, not POST /api/recommendations, is what production scores against.
    That endpoint answers with a list their side has already filtered, and the
    filtering overlaps ours: measured against the live service, it withholds
    every invasive treatment from severity 3 upwards. Building on it would mean
    our own rules could only narrow their decision, never make it — R4 could
    never offer an injection at S3 because an injection would never arrive.
    R4 is a rule of ours, so the candidate set has to be the whole catalog.

    Nothing is lost by widening it: the five gates drop what does not apply,
    and a treatment with no tag for a concern the patient needs scores zero and
    is never ranked. 112 treatments in, 52 past gate 1, five shown.

    `isActive: false` is the one exclusion kept on their side: a treatment the
    clinic has withdrawn is not a candidate at any score. None are withdrawn
    today, which is exactly why the guard has to be here before one is.
    """
    by_id = await fetch_treatments(client=client)
    if not by_id:
        return None

    rows, withdrawn = [], 0
    for row in by_id.values():
        if row.get("isActive") is False:
            withdrawn += 1
            continue
        rows.append(row)

    catalog = catalog_from_rows(rows)
    if withdrawn:
        catalog.issues.append(f"{withdrawn} treatment withdrawn by the clinic, not offered")
    needs_review = sum(1 for t in catalog.treatments.values() if t.needs_review)
    if needs_review:
        catalog.issues.append(f"{needs_review} treatment flagged needsReview by the clinic")
    if not catalog.treatments:
        return None
    logger.info("ayuko-api full catalog: %d treatments as candidates.", len(catalog.treatments))
    return catalog


async def catalog_for_scores(raw_scores: dict,
                             client: Optional[httpx.AsyncClient] = None) -> Optional[Catalog]:
    """The whole round trip: scores in, a scoreable Catalog out."""
    concerns = concerns_for(raw_scores)
    if not concerns:
        logger.info("No concern is above its problem threshold; nothing to ask ayuko-api for.")
        return None
    data = await fetch_recommendations(concerns, client=client)
    if data is None:
        return None
    catalog = catalog_from_recommendations(data, await fetch_treatments(client=client))
    if catalog is not None:
        logger.info("ayuko-api returned %d treatments for %s", len(catalog.treatments),
                    [c["concernId"] for c in concerns])
    return catalog
