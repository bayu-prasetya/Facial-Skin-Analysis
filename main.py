"""
SkinCode - FastAPI Endpoint
=============================
Main API endpoint that ties together a lightweight upload validation
(file size + minimum resolution for Perfect Corp's SD tier), the
Perfect Corp YouCam AI Skin API call, and persistence to Supabase.

Flow for POST /analyze:
    1. Receive the uploaded photo (multipart/form-data)
    2. Validate file size (<10MB) and minimum resolution (>=480px on the
       shortest side) required by Perfect Corp's SD tier
    3. If rejected -> return 422 immediately with the rejection reason
    4. If passed -> register the file with Perfect Corp, upload it, and
       create the analysis task, all inline (submit_skin_analysis_task)
    5. Insert the analysis record into Supabase with the resulting
       task_id already set, in a single write
    6. Return 202 Accepted with the analysis_id

Steps 4-5 are deliberately synchronous rather than a BackgroundTask. The
task_id is the only handle that can ever retrieve a result, and Perfect
Corp offers no way to list tasks or look one up by anything else, so a
task_id that is created but never persisted is a permanently orphaned
unit of quota. Submitting inline means the row is written with its
task_id or not written at all: if any step fails, no record is created
and the client gets the error straight away, instead of discovering it
later by polling a row that can never be resolved. The cost is a few
seconds of response latency, which the client would have spent polling
anyway. Nothing here blocks the event loop — the vendor calls are async
httpx and the Supabase write goes through a threadpool — so one worker
still serves many concurrent uploads.

Perfect Corp can also push a webhook (POST /webhooks/perfect-corp) the
moment a task finishes — configured separately in their API Console
dashboard, not in this code. That's the primary way a "pending"
analysis gets resolved. The webhook payload only carries {task_id,
task_status} though, not the actual results, so the handler still does
one Perfect Corp status check to fetch them (same check_skin_analysis_task
used by the polling fallback below).

GET /analyze/{analysis_id} is the fallback path: each call performs a
single Perfect Corp status check (no internal loop) if the analysis is
still "pending", and updates Supabase to
"completed"/"failed" once Perfect Corp is done. This covers the case
where the webhook is delayed, dropped, or not configured (e.g. no public
URL during local development). Whichever path (webhook or polling) first
observes the task succeeding computes product recommendations right there
too and stores them alongside the skin metrics — so by the time the
client sees status="completed", recommendations are already sitting in
the record.

GET /analyze/{analysis_id}/recommendations just reads that stored value
(computing it on the fly as a fallback for older records). Recommendation
logic is currently a placeholder — the product data source and matching
logic are not decided yet.

Perfect Corp's own API already rejects unsuitable photos (blurry, no
face, bad pose, etc.) at no cost to us, so a local quality-gate
pipeline is unnecessary — the only thing we still need to check
ourselves is what's required to even make a valid SD-tier request.

Dependencies:
    pip install fastapi python-multipart httpx supabase Pillow python-dotenv svix --break-system-packages

Environment variables required (put these in a .env file in this directory;
it's loaded automatically on startup and is gitignored):
    SUPABASE_URL, SUPABASE_KEY          - Supabase project credentials
    PERFECT_CORP_API_KEY                - Perfect Corp YouCam vendor key
    PERFECT_CORP_API_BASE_URL           - Perfect Corp API base URL
    PERFECT_CORP_WEBHOOK_SECRET         - webhook signing secret ("whsec_...")
                                           from Perfect Corp's API Console
                                           Webhook Management page

Run locally:
    uvicorn main:app --reload
"""

import io
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Path, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from PIL import Image
from pydantic import BaseModel, Field
from supabase import create_client, Client
from svix.webhooks import Webhook, WebhookVerificationError

load_dotenv()


# ---------------------------------------------------------------------------
# 1. APP & SHARED RESOURCES (initialized once at startup, reused per request)
# ---------------------------------------------------------------------------

# Supabase client, created once and reused.
_supabase_client: Optional[Client] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _supabase_client

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_KEY environment variables must be set."
        )
    _supabase_client = create_client(supabase_url, supabase_key)
    yield


API_DESCRIPTION = """
Facial skin analysis built on Perfect Corp's YouCam AI Skin API.

## How an analysis flows

Analysis is **asynchronous** — a photo takes several seconds to process, so
`POST /analyze` does not return results. Instead:

1. `POST /analyze` with the photo. This uploads it to Perfect Corp and queues
   the analysis, which takes a few seconds, then returns `202 Accepted` with
   an `analysis_id`.
2. Poll `GET /analyze/{analysis_id}` until `status` is no longer `pending`.
   Every ~3 seconds is a reasonable cadence.
3. On `status: "completed"`, read `skin_metrics`, `skin_age`,
   `skin_health_score`, and `recommendations` from that same response.

Step 1 is all-or-nothing: either you get an `analysis_id` and an analysis
exists, or you get an error and nothing was created. There is no state where
you hold an `analysis_id` that can never produce a result, so any failed
`POST /analyze` is safe to retry.

Recommendations are already computed by the time `status` becomes
`completed`, so step 3 needs no extra call. `GET /analyze/{analysis_id}/recommendations`
exists if you want them on their own.

## Status values

| `status` | Meaning |
| --- | --- |
| `pending` | Still processing. Keep polling. |
| `completed` | Done. Results are in the response. |
| `failed` | Could not be analyzed. `error_message` says why. |

A `failed` analysis is terminal — polling again won't change it. The most
common cause is a photo Perfect Corp rejects on closer inspection (no face
detected, face too small, too blurry), which the upload checks in step 1
can't catch. Treat it as "ask the user for another photo".

## Photo requirements

- At most **10 MB**
- At least **480px on the shortest side**
- One clearly visible, front-facing face

The first two are checked immediately and return `422`. The third is
checked by Perfect Corp and surfaces later as `status: "failed"`.
"""

TAGS_METADATA = [
    {
        "name": "Analysis",
        "description": "Submit a photo and retrieve its analysis results.",
    },
    {
        "name": "Recommendations",
        "description": "Product recommendations derived from a completed analysis.",
    },
    {
        "name": "Webhooks",
        "description": (
            "Called by Perfect Corp, not by API clients. Documented here for "
            "operators configuring the integration."
        ),
    },
    {"name": "System", "description": "Liveness probes."},
]

app = FastAPI(
    title="SkinCode API",
    description=API_DESCRIPTION,
    version="1.0.0",
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# 2. RESPONSE MODELS
# ---------------------------------------------------------------------------

class AnalysisAccepted(BaseModel):
    """Returned immediately when the photo passes upload validation."""
    analysis_id: str = Field(description="Poll GET /analyze/{analysis_id} with this id.")
    status: str = "pending"
    message: str = "Photo accepted, analysis in progress."

    model_config = {
        "json_schema_extra": {
            "example": {
                "analysis_id": "3f9a1c2e-7b44-4d0a-9e51-2c8f6b1d4a77",
                "status": "pending",
                "message": "Photo accepted, analysis in progress.",
            }
        }
    }


class AnalysisRejected(BaseModel):
    """Returned when the photo fails upload validation."""
    status: str = "rejected"
    message: str = Field(description="Which requirement the photo failed, in plain language.")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "rejected",
                "message": "Image resolution is too low (minimum 480px on the shortest side).",
            }
        }
    }


class QualityMetrics(BaseModel):
    """Basic properties read off the uploaded photo."""
    width: int
    height: int
    file_size_bytes: int


class SkinMetric(BaseModel):
    """One skin concern scored by Perfect Corp."""
    type: str = Field(description='Concern name, e.g. "acne", "oiliness", "age_spot".')
    ui_score: int = Field(description="Score for display, 0-100. Higher is better.")


class AnalysisResult(BaseModel):
    """Current state of an analysis. Shape is the same whether it's pending,
    completed, or failed — the result fields are simply null until it completes."""
    id: str
    status: Literal["pending", "completed", "failed"]
    quality_metrics: Optional[QualityMetrics] = None
    skin_metrics: list[SkinMetric] = Field(
        default_factory=list,
        description="Per-concern scores. Empty until the analysis completes.",
    )
    skin_age: Optional[int] = Field(
        default=None, description="Estimated skin age in years. Null until completed."
    )
    skin_health_score: Optional[int] = Field(
        default=None,
        description="Overall skin condition, 0-100. Null until completed.",
    )
    recommendations: Optional[list[dict]] = Field(
        default=None, description="Recommended products. Null until completed."
    )
    error_message: Optional[str] = Field(
        default=None, description='Why the analysis failed. Only set when status is "failed".'
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "3f9a1c2e-7b44-4d0a-9e51-2c8f6b1d4a77",
                "status": "completed",
                "quality_metrics": {
                    "width": 1080,
                    "height": 1440,
                    "file_size_bytes": 842113,
                },
                "skin_metrics": [
                    {"type": "all", "ui_score": 78},
                    {"type": "acne", "ui_score": 64},
                    {"type": "oiliness", "ui_score": 71},
                    {"type": "age_spot", "ui_score": 83},
                    {"type": "skin_type", "ui_score": 69},
                ],
                "skin_age": 32,
                "skin_health_score": 78,
                "recommendations": [],
                "error_message": None,
            }
        }
    }


class RecommendationsResult(BaseModel):
    """Product recommendations for a completed analysis."""
    analysis_id: str
    recommendations: list[dict]


class ErrorDetail(BaseModel):
    """FastAPI's standard error shape."""
    detail: str


class HealthStatus(BaseModel):
    status: str = "ok"


class WebhookAck(BaseModel):
    status: str = "ok"


# ---------------------------------------------------------------------------
# 3. UPLOAD VALIDATION (file size + minimum resolution for Perfect Corp SD tier)
# ---------------------------------------------------------------------------

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB - Perfect Corp's hard limit
MIN_IMAGE_DIMENSION_PX = 480             # minimum shortest side for the SD tier

# Ceiling for the whole multipart body, checked against Content-Length before
# reading anything, so an oversized upload is refused instead of being buffered
# into memory in full just to fail the per-file check below. The 1 MB of slack
# covers multipart boundaries and headers, so a legitimately 10 MB photo still
# reaches validate_image_for_perfect_corp and gets the precise error message.
MAX_REQUEST_BODY_BYTES = MAX_FILE_SIZE_BYTES + 1024 * 1024


class ImageValidationError(Exception):
    """Raised when the uploaded photo doesn't meet Perfect Corp's SD-tier requirements."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def validate_image_for_perfect_corp(image_bytes: bytes) -> dict:
    """
    Validate file size and resolution before sending to Perfect Corp
    (SD tier requires >=480px on the shortest side and a file <10MB).

    Raises:
        ImageValidationError: if the file is too large, too small, or invalid

    Returns:
        dict with basic image metadata (width, height, file_size_bytes)
    """
    if len(image_bytes) > MAX_FILE_SIZE_BYTES:
        raise ImageValidationError(
            f"File size exceeds {MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB."
        )

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            width, height = img.size
    except Exception as e:
        raise ImageValidationError("Photo file is invalid or corrupted.") from e

    if min(width, height) < MIN_IMAGE_DIMENSION_PX:
        raise ImageValidationError(
            f"Image resolution is too low (minimum {MIN_IMAGE_DIMENSION_PX}px on the shortest side)."
        )

    return {"width": width, "height": height, "file_size_bytes": len(image_bytes)}


# ---------------------------------------------------------------------------
# 4. SUPABASE HELPERS
# ---------------------------------------------------------------------------
# Table schema assumed (create this in Supabase):
#
#   create table skin_analyses (
#     id uuid primary key,
#     status text not null default 'pending',  -- pending | completed | failed
#     quality_metrics jsonb,
#     task_id text not null,
#     skin_metrics jsonb,
#     skin_health_score integer,
#     recommendations jsonb,
#     error_message text,
#     created_at timestamptz not null default now(),
#     updated_at timestamptz
#   );

# supabase-py's client is synchronous, so every call below goes through
# run_in_threadpool — calling .execute() directly from an async endpoint
# would block the event loop for the whole Supabase round-trip, stalling
# every other in-flight request.

async def _create_pending_record(
    analysis_id: str, task_id: str, image_metadata: dict
) -> None:
    """Insert the 'pending' row for a task that has already been created at
    Perfect Corp. task_id is written in this same insert on purpose — see the
    module docstring on why it is never allowed to exist only in memory."""
    await run_in_threadpool(
        lambda: _supabase_client.table("skin_analyses").insert({
            "id": analysis_id,
            "status": "pending",
            "task_id": task_id,
            "quality_metrics": image_metadata,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    )


async def _mark_completed(
    analysis_id: str,
    skin_metrics: dict,
    skin_health_score: Optional[int],
    recommendations: list,
) -> None:
    """Update the record once Perfect Corp analysis succeeds."""
    await run_in_threadpool(
        lambda: _supabase_client.table("skin_analyses").update({
            "status": "completed",
            "skin_metrics": skin_metrics,
            "skin_health_score": skin_health_score,
            "recommendations": recommendations,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", analysis_id).execute()
    )


async def _mark_failed(analysis_id: str, error_message: str) -> None:
    """Update the record if Perfect Corp analysis fails for any reason."""
    await run_in_threadpool(
        lambda: _supabase_client.table("skin_analyses").update({
            "status": "failed",
            "error_message": error_message,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", analysis_id).execute()
    )


async def _get_analysis_record(analysis_id: str) -> dict:
    """Fetch a skin_analyses row by id, or raise 404 if it doesn't exist."""
    result = await run_in_threadpool(
        lambda: _supabase_client.table("skin_analyses")
        .select("*")
        .eq("id", analysis_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Analysis not found.")
    return result.data[0]


async def _find_analysis_by_task_id(task_id: str) -> Optional[dict]:
    """Look up a skin_analyses row by its Perfect Corp task_id (used by the webhook handler,
    which only receives a task_id — not our analysis_id). Returns None if not found."""
    result = await run_in_threadpool(
        lambda: _supabase_client.table("skin_analyses")
        .select("*")
        .eq("task_id", task_id)
        .execute()
    )
    return result.data[0] if result.data else None


def _find_metric_score(skin_metrics: dict, metric_type: str) -> Optional[float]:
    """Find a top-level metric entry by type (e.g. "all", "skin_age") in
    Perfect Corp's raw output array and return its "score"."""
    for item in skin_metrics.get("output", []):
        if item.get("type") == metric_type:
            return item.get("score")
    return None


def _summarize_skin_metrics(skin_metrics: Optional[dict]) -> list[dict]:
    """Reduce Perfect Corp's raw skin_metrics (its full "output" array, with
    mask_urls, regions, etc.) down to just {type, ui_score} per concern —
    the full raw payload stays in Supabase, this is only for API responses."""
    if not skin_metrics:
        return []
    return [
        {"type": item["type"], "ui_score": item["ui_score"]}
        for item in skin_metrics.get("output", [])
        if "ui_score" in item and "type" in item
    ]


def _extract_skin_age(skin_metrics: Optional[dict]) -> Optional[int]:
    """Pull Perfect Corp's estimated skin_age metric out of the raw output array."""
    if not skin_metrics:
        return None
    score = _find_metric_score(skin_metrics, "skin_age")
    return round(score) if score is not None else None


def _build_analysis_response(record: dict) -> dict:
    """Shape a skin_analyses row into the trimmed public response for
    GET /analyze/{analysis_id} — drops internal bookkeeping fields
    (task_id, created_at, updated_at) and summarizes skin_metrics."""
    return {
        "id": record["id"],
        "status": record["status"],
        "quality_metrics": record.get("quality_metrics"),
        "skin_metrics": _summarize_skin_metrics(record.get("skin_metrics")),
        "skin_age": _extract_skin_age(record.get("skin_metrics")),
        "skin_health_score": record.get("skin_health_score"),
        "recommendations": record.get("recommendations"),
        "error_message": record.get("error_message"),
    }


# ---------------------------------------------------------------------------
# 5. PERFECT CORP CLIENT
# ---------------------------------------------------------------------------
# Real integration flow per Perfect Corp's AI Skin Analysis API docs
# (https://docs.perfectcorp.com/reference/ai_skin_analysis/section/overview/integration-guide):
#   1. POST /s2s/v2.0/file            -> register file metadata, get file_id + pre-signed upload URL
#   2. PUT  <pre-signed URL>          -> actually upload the image bytes
#   3. POST /s2s/v2.0/task/skin-analysis -> create the analysis task, get task_id
#   4. GET  /s2s/v2.0/task/skin-analysis/<task_id> -> check task_status
#
# Steps 1-3 run inline during POST /analyze (submit_skin_analysis_task), so
# the task_id reaches Supabase in the same request that created it. Step 4 is
# NOT looped internally: it runs once per trigger, either when Perfect Corp's
# webhook reports the task finished (the primary path) or when a client calls
# GET /analyze/{analysis_id} (the fallback, driven by the client's own polling
# cadence).
#
# Quota is consumed when a task reaches "success", not when it is created, and
# tasks that end in "error" cost nothing (per Perfect Corp's integration
# guide). So every rejection path here is free — but a task whose task_id we
# lose still burns a unit once the engine finishes it, with no way to read the
# result. That asymmetry is why steps 1-3 are not deferred.
#
# NOTE: response field names below (file_id, requests[].url/method, task_id,
# task_status, results) are taken from Perfect Corp's published examples but
# MUST be reconfirmed against a live response before going to production —
# the docs don't show a full "error" response example.

# Full set of SD-tier skin concern types (per Perfect Corp's docs). "all"
# (general condition score) and "skin_age" are returned automatically and
# are NOT valid dst_actions values. HD-tier actions (the "hd_" prefixed
# versions) must NOT be mixed in here — SD and HD concern params can't be
# combined in the same request (API returns InvalidParameters if you do).
PERFECT_CORP_SKIN_ANALYSIS_ACTIONS = [
    # "wrinkle",
    # "droopy_upper_eyelid",
    # "droopy_lower_eyelid",
    # "firmness",
    "acne",
    # "moisture",
    # "eye_bag",
    # "dark_circle_v2",
    "age_spot",
    # "radiance",
    # "redness",
    "oiliness",
    # "pore",
    # "texture",
    # "tear_trough",
    "skin_type",
]


# Per-call timeout for every Perfect Corp request. Steps 1-3 now run inside
# POST /analyze, so the worst case a client can wait is three of these back to
# back; keeping it well under a minute leaves room under any reverse proxy's
# request timeout without having to know the exact figure.
PERFECT_CORP_TIMEOUT_SECONDS = 15.0


class PerfectCorpError(Exception):
    """Raised when the Perfect Corp API call fails or returns unexpected data."""


class PerfectCorpTimeout(PerfectCorpError):
    """Perfect Corp didn't answer in time. Separate from PerfectCorpError so
    POST /analyze can distinguish 504 (vendor slow) from 502 (vendor refused)."""


def _get_perfect_corp_credentials() -> tuple[str, str]:
    api_key = os.environ.get("PERFECT_CORP_API_KEY")
    base_url = os.environ.get("PERFECT_CORP_API_BASE_URL")
    if not api_key or not base_url:
        raise PerfectCorpError("Perfect Corp API credentials are not configured.")
    return api_key, base_url


async def _register_file(
    client: httpx.AsyncClient, api_key: str, filename: str, content_type: str, file_size: int
) -> tuple[str, str, str]:
    """Step 1: register file metadata. Returns (file_id, upload_url, upload_method)."""
    try:
        response = await client.post(
            "/s2s/v2.0/file",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"files": [{
                "content_type": content_type,
                "file_name": filename,
                "file_size": file_size,
            }]},
        )
        response.raise_for_status()
    except httpx.TimeoutException as e:
        raise PerfectCorpTimeout(f"Perfect Corp file registration timed out: {e}") from e
    except httpx.HTTPStatusError as e:
        raise PerfectCorpError(
            f"Perfect Corp file registration returned {e.response.status_code}: {e.response.text}"
        ) from e
    except httpx.RequestError as e:
        raise PerfectCorpError(
            f"Could not reach Perfect Corp for file registration: {e!r}"
        ) from e

    try:
        file_entry = response.json()["data"]["files"][0]
        upload_request = file_entry["requests"][0]
        return file_entry["file_id"], upload_request["url"], upload_request.get("method", "PUT")
    except (KeyError, IndexError, TypeError) as e:
        raise PerfectCorpError(f"Unexpected file registration response: {response.text}") from e


async def _upload_file(
    client: httpx.AsyncClient, upload_url: str, upload_method: str, image_bytes: bytes, content_type: str
) -> None:
    """Step 2: upload the actual image bytes to the pre-signed URL from step 1."""
    try:
        response = await client.request(
            upload_method,
            upload_url,
            content=image_bytes,
            headers={"Content-Type": content_type},
        )
        response.raise_for_status()
    except httpx.TimeoutException as e:
        raise PerfectCorpTimeout(f"Perfect Corp file upload timed out: {e}") from e
    except httpx.HTTPStatusError as e:
        raise PerfectCorpError(
            f"Perfect Corp file upload returned {e.response.status_code}: {e.response.text}"
        ) from e
    except httpx.RequestError as e:
        raise PerfectCorpError(
            f"Could not reach Perfect Corp for file upload: {e!r}"
        ) from e


async def _create_skin_analysis_task(client: httpx.AsyncClient, api_key: str, file_id: str) -> str:
    """Step 3: create the analysis task for the uploaded file. Returns task_id."""
    try:
        response = await client.post(
            "/s2s/v2.0/task/skin-analysis",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "src_file_id": file_id,
                "dst_actions": PERFECT_CORP_SKIN_ANALYSIS_ACTIONS,
                "format": "json",
            },
        )
        response.raise_for_status()
    except httpx.TimeoutException as e:
        raise PerfectCorpTimeout(f"Perfect Corp task creation timed out: {e}") from e
    except httpx.HTTPStatusError as e:
        raise PerfectCorpError(
            f"Perfect Corp task creation returned {e.response.status_code}: {e.response.text}"
        ) from e
    except httpx.RequestError as e:
        raise PerfectCorpError(
            f"Could not reach Perfect Corp for task creation: {e!r}"
        ) from e

    try:
        return response.json()["data"]["task_id"]
    except (KeyError, TypeError) as e:
        raise PerfectCorpError(f"Unexpected task creation response: {response.text}") from e


async def _fetch_skin_analysis_task(client: httpx.AsyncClient, api_key: str, task_id: str) -> dict:
    """Step 4: perform a single status check. Returns the raw 'data' payload
    (e.g. {"task_status": "running"} or {"task_status": "success", "results": {...}})."""
    try:
        response = await client.get(
            f"/s2s/v2.0/task/skin-analysis/{task_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
    except httpx.TimeoutException as e:
        raise PerfectCorpTimeout(f"Perfect Corp task status check timed out: {e}") from e
    except httpx.HTTPStatusError as e:
        raise PerfectCorpError(
            f"Perfect Corp task status check returned {e.response.status_code}: {e.response.text}"
        ) from e
    except httpx.RequestError as e:
        raise PerfectCorpError(
            f"Could not reach Perfect Corp for task status check: {e!r}"
        ) from e

    try:
        return response.json()["data"]
    except (KeyError, TypeError) as e:
        raise PerfectCorpError(f"Unexpected task status response: {response.text}") from e


async def submit_skin_analysis_task(image_bytes: bytes, filename: str, content_type: str) -> str:
    """
    Register the file with Perfect Corp, upload it, and create the analysis
    task (steps 1-3). Returns the task_id; the caller stores it and checks
    on it later via _fetch_skin_analysis_task (step 4), one check per call.

    Raises:
        PerfectCorpError: on HTTP failure, timeout, or unexpected response
    """
    api_key, base_url = _get_perfect_corp_credentials()

    async with httpx.AsyncClient(base_url=base_url, timeout=PERFECT_CORP_TIMEOUT_SECONDS) as client:
        file_id, upload_url, upload_method = await _register_file(
            client, api_key, filename, content_type, len(image_bytes)
        )
        await _upload_file(client, upload_url, upload_method, image_bytes, content_type)
        return await _create_skin_analysis_task(client, api_key, file_id)


async def check_skin_analysis_task(analysis_id: str, task_id: str) -> dict:
    """
    Perform a single Perfect Corp status check for a pending analysis and
    persist the outcome to Supabase: 'completed' if the task succeeded,
    'failed' if it errored, or left untouched if it's still running.

    Returns the up-to-date Supabase record.
    """
    try:
        api_key, base_url = _get_perfect_corp_credentials()
        async with httpx.AsyncClient(base_url=base_url, timeout=PERFECT_CORP_TIMEOUT_SECONDS) as client:
            data = await _fetch_skin_analysis_task(client, api_key, task_id)
    except PerfectCorpError as e:
        await _mark_failed(analysis_id, str(e))
        return await _get_analysis_record(analysis_id)

    task_status = data.get("task_status")
    if task_status == "success":
        skin_metrics = data.get("results")
        if skin_metrics is None:
            await _mark_failed(
                analysis_id, "Perfect Corp reported success but returned no results."
            )
            return await _get_analysis_record(analysis_id)
        skin_health_score = compute_skin_health_score(skin_metrics)
        recommendations = compute_product_recommendations(skin_metrics, skin_health_score)
        await _mark_completed(analysis_id, skin_metrics, skin_health_score, recommendations)
        return await _get_analysis_record(analysis_id)
    if task_status != "running":
        # Perfect Corp includes a machine-readable "error" code and a
        # human-readable "error_message" on failed tasks (e.g.
        # error_src_face_too_small) — surface those instead of a generic
        # message so the actual rejection reason isn't lost.
        detail = data.get("error_message") or data.get("error") or "unknown reason"
        await _mark_failed(analysis_id, f"Perfect Corp task failed ({task_status}): {detail}")
        return await _get_analysis_record(analysis_id)

    return await _get_analysis_record(analysis_id)


def compute_skin_health_score(skin_metrics: dict) -> Optional[int]:
    """
    Derive the overall skin health score from Perfect Corp's "all" metric —
    its general skin condition score (0-100) across all analyzed concerns.
    Returns None if that entry isn't present in the results for some reason.
    """
    score = _find_metric_score(skin_metrics, "all")
    return round(score) if score is not None else None


# ---------------------------------------------------------------------------
# 6. PRODUCT RECOMMENDATION
# ---------------------------------------------------------------------------
# TODO: this is a placeholder. The actual product data source (a new
# Supabase table, an external catalog/e-commerce API, etc.) and the
# matching logic (rule-based mapping from skin concerns -> product
# categories, or something else) haven't been decided yet.

def compute_product_recommendations(skin_metrics: dict, skin_health_score: Optional[int]) -> list[dict]:
    """
    Turn a completed analysis's skin metrics + health score into a list of
    recommended products.

    TODO: replace with real product matching logic once the product data
    source is decided. Placeholder below.
    """
    # Placeholder: replace with real product matching logic.
    return []


# ---------------------------------------------------------------------------
# 7. MAIN ENDPOINT
# ---------------------------------------------------------------------------

@app.post(
    "/analyze",
    response_model=AnalysisAccepted,
    status_code=202,
    tags=["Analysis"],
    summary="Submit a photo for analysis",
    description=(
        "Upload a face photo as `multipart/form-data` under the field name `file`.\n\n"
        "This call uploads the photo to Perfect Corp and queues the analysis task "
        "before responding, so expect it to take a few seconds. It returns `202` "
        "with an `analysis_id` once the task is queued — it does **not** wait for "
        "the analysis itself. Poll `GET /analyze/{analysis_id}` for the results.\n\n"
        "A non-2xx response means no analysis was created at all: there is nothing "
        "to poll, and retrying is safe. Only after you receive an `analysis_id` "
        "does an analysis exist.\n\n"
        "The photo must be at most 10 MB and at least 480px on its shortest side. "
        "A photo that fails either check is rejected with `422` and costs nothing. "
        "Whether the photo actually contains a usable face is determined later by "
        "Perfect Corp, and shows up as `status: \"failed\"` when you poll."
    ),
    responses={
        413: {
            "model": ErrorDetail,
            "description": "Request body is too large to even read.",
        },
        422: {
            "model": AnalysisRejected,
            "description": "Photo doesn't meet the size or resolution requirements.",
        },
        500: {"model": ErrorDetail, "description": "Could not record the analysis."},
        502: {
            "model": ErrorDetail,
            "description": "Perfect Corp refused the upload or returned an error. Safe to retry.",
        },
        504: {
            "model": ErrorDetail,
            "description": "Perfect Corp didn't respond in time. Safe to retry.",
        },
    },
)
async def analyze_photo(
    request: Request,
    file: UploadFile = File(
        description="Face photo. JPEG or PNG, max 10 MB, min 480px on the shortest side.",
    ),
):
    """
    Accept a photo upload, validate it meets Perfect Corp's SD-tier
    requirements, submit it to Perfect Corp, and record the resulting task.

    Everything here is synchronous so that the task_id is persisted in the
    same request that created it — see the module docstring.
    """
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_REQUEST_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body is too large.")

    image_bytes = await file.read()

    # --- Stage 1: upload validation (file size + minimum resolution) ---
    try:
        image_metadata = validate_image_for_perfect_corp(image_bytes)
    except ImageValidationError as e:
        return JSONResponse(
            status_code=422,
            content=AnalysisRejected(message=e.message).model_dump(),
        )

    # --- Stage 2: register, upload, and create the task at Perfect Corp ---
    try:
        task_id = await submit_skin_analysis_task(
            image_bytes,
            file.filename or "photo.jpg",
            file.content_type or "image/jpeg",
        )
    except PerfectCorpTimeout as e:
        raise HTTPException(status_code=504, detail=str(e)) from e
    except PerfectCorpError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    # --- Stage 3: persist the task_id in the same request that created it ---
    analysis_id = str(uuid.uuid4())
    try:
        await _create_pending_record(analysis_id, task_id, image_metadata)
    except Exception as e:
        # The task exists at Perfect Corp but we failed to record its id, so it
        # can never be read back. Name the orphaned task_id in the error: it's
        # the only trace left for reconciling against Perfect Corp's usage.
        raise HTTPException(
            status_code=500,
            detail=(
                f"Analysis task {task_id} was created at Perfect Corp but could "
                f"not be recorded: {e}"
            ),
        ) from e

    return AnalysisAccepted(analysis_id=analysis_id)


@app.get(
    "/analyze/{analysis_id}",
    response_model=AnalysisResult,
    tags=["Analysis"],
    summary="Get analysis results (poll this)",
    description=(
        "Returns the current state of an analysis. Call this repeatedly until "
        "`status` is `completed` or `failed` — roughly every 3 seconds.\n\n"
        "While `status` is `pending`, the result fields are `null` or empty. Once "
        "`status` is `completed`, `skin_metrics`, `skin_age`, `skin_health_score`, "
        "and `recommendations` are all populated. If `status` is `failed`, stop "
        "polling and read `error_message` — it will not recover.\n\n"
        "Each call may trigger a status check against Perfect Corp, so don't poll "
        "faster than once per second."
    ),
    responses={404: {"model": ErrorDetail, "description": "No such analysis_id."}},
)
async def get_analysis_result(
    analysis_id: str = Path(description="The id returned by POST /analyze."),
):
    """
    Polling endpoint for the client to check on a previously submitted
    analysis. If it's still "pending", this performs a single status check
    against Perfect Corp (see check_skin_analysis_task) and updates Supabase
    accordingly — the client is expected to call this repeatedly until the
    status is no longer "pending". Returns a trimmed view (see
    _build_analysis_response) rather than the raw Supabase row.

    Every row is written with its task_id (see _create_pending_record), so a
    "pending" row always has one to check.
    """
    record = await _get_analysis_record(analysis_id)

    if record["status"] == "pending":
        record = await check_skin_analysis_task(analysis_id, record["task_id"])

    return _build_analysis_response(record)


@app.get(
    "/analyze/{analysis_id}/recommendations",
    response_model=RecommendationsResult,
    tags=["Recommendations"],
    summary="Get product recommendations only",
    description=(
        "Returns just the recommendations for a completed analysis.\n\n"
        "You usually don't need this — `GET /analyze/{analysis_id}` already includes "
        "`recommendations` once the analysis completes. Use this when you want them "
        "without the rest of the payload.\n\n"
        "Requires `status` to be `completed`; returns `409` otherwise."
    ),
    responses={
        404: {"model": ErrorDetail, "description": "No such analysis_id."},
        409: {
            "model": ErrorDetail,
            "description": "Analysis is still pending, or it failed.",
        },
    },
)
async def get_product_recommendations(
    analysis_id: str = Path(description="The id of a completed analysis."),
):
    """
    Return product recommendations for a completed analysis. These are
    computed once — the moment GET /analyze/{analysis_id} observes the
    Perfect Corp task succeeding (see check_skin_analysis_task) — and
    stored on the record; this endpoint just reads that stored value,
    computing it on the fly as a fallback for records that predate that.
    """
    record = await _get_analysis_record(analysis_id)

    if record["status"] != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Analysis is not completed yet (status: {record['status']}).",
        )

    recommendations = record.get("recommendations")
    if recommendations is None:
        recommendations = compute_product_recommendations(
            record.get("skin_metrics") or {}, record.get("skin_health_score")
        )

    return {"analysis_id": analysis_id, "recommendations": recommendations}


@app.post(
    "/webhooks/perfect-corp",
    response_model=WebhookAck,
    tags=["Webhooks"],
    summary="Perfect Corp task-completion callback",
    description=(
        "**Not for API clients.** Perfect Corp calls this when an analysis task "
        "finishes; the URL is registered in their API Console, not here.\n\n"
        "Requests must be signed with the "
        "[Standard Webhooks](https://www.standardwebhooks.com/) scheme using the "
        "secret from Perfect Corp's Webhook Management page. Unsigned or "
        "mis-signed requests get `401`.\n\n"
        "Responds `503` when the callback can't be resolved yet — the task isn't "
        "on record, or Perfect Corp still reports it as running — so the delivery "
        "is retried rather than dropped."
    ),
    responses={
        400: {"model": ErrorDetail, "description": "Payload is malformed or missing task_id."},
        401: {"model": ErrorDetail, "description": "Signature verification failed."},
        500: {"model": ErrorDetail, "description": "Webhook secret is not configured."},
        503: {"model": ErrorDetail, "description": "Not resolvable yet — retry."},
    },
)
async def perfect_corp_webhook(request: Request):
    """
    Receive Perfect Corp's task-completion webhook (configured separately
    in their API Console — not in this code). The payload only carries
    {task_id, task_status}, not the actual results, so this still performs
    one Perfect Corp status check (check_skin_analysis_task) to fetch them
    and persist the outcome — the same function the GET /analyze/{id}
    polling fallback uses.

    Uses the Standard Webhooks signature scheme (see the svix library) to
    verify the request actually came from Perfect Corp before trusting it.

    Responds 503 when the delivery can't be resolved yet — either the
    task_id isn't on any record (our background task may not have written
    it to Supabase before Perfect Corp fired the webhook) or the status
    check still says "running". Perfect Corp only sends one webhook per
    task, so answering 200 in those cases would silently drop it and leave
    the analysis pending until a client happens to poll; a non-2xx makes
    Standard Webhooks retry with backoff instead.
    """
    body = await request.body()

    webhook_secret = os.environ.get("PERFECT_CORP_WEBHOOK_SECRET")
    if not webhook_secret:
        raise HTTPException(status_code=500, detail="Webhook secret is not configured.")

    try:
        Webhook(webhook_secret).verify(body, dict(request.headers))
    except WebhookVerificationError as e:
        raise HTTPException(status_code=401, detail=f"Invalid webhook signature: {e}") from e

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Malformed webhook payload: {e}") from e

    task_id = payload.get("data", {}).get("task_id")
    if not task_id:
        raise HTTPException(status_code=400, detail="Missing data.task_id in webhook payload.")

    record = await _find_analysis_by_task_id(task_id)
    if record is None:
        raise HTTPException(
            status_code=503, detail="No analysis recorded for this task_id yet."
        )

    if record["status"] != "pending":
        # Already resolved, most likely by a client polling GET /analyze/{id}
        # before this webhook landed. Nothing to do, and no reason to retry.
        return {"status": "ok"}

    record = await check_skin_analysis_task(record["id"], task_id)
    if record["status"] == "pending":
        raise HTTPException(
            status_code=503, detail="Perfect Corp task is still running."
        )

    return {"status": "ok"}


@app.get(
    "/health",
    response_model=HealthStatus,
    tags=["System"],
    summary="Liveness check",
    description=(
        "Returns `200` whenever the process is up. Does not check Supabase or "
        "Perfect Corp connectivity."
    ),
)
async def health_check():
    """Basic liveness check for Cloud Run / load balancer probes."""
    return {"status": "ok"}
