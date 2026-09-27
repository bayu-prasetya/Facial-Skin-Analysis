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
    4. If passed -> insert a "pending" analysis record into Supabase
    5. Schedule registering + submitting the Perfect Corp task as a
       BackgroundTask (register file, upload it, create the analysis
       task, store the resulting task_id — no polling here)
    6. Return 202 Accepted immediately with the analysis_id, so the
       client isn't blocked waiting for the vendor API round-trip

Perfect Corp can also push a webhook (POST /webhooks/perfect-corp) the
moment a task finishes — configured separately in their API Console
dashboard, not in this code. That's now the primary way a "pending"
analysis gets resolved. The webhook payload only carries {task_id,
task_status} though, not the actual results, so the handler still does
one Perfect Corp status check to fetch them (same check_skin_analysis_task
used by the polling fallback below).

GET /analyze/{analysis_id} is the fallback path: each call performs a
single Perfect Corp status check (no internal loop) if the analysis is
still "pending" with a task_id on file, and updates Supabase to
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
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from PIL import Image
from pydantic import BaseModel
from supabase import create_client, Client
from svix.webhooks import Webhook, WebhookVerificationError

load_dotenv()


# ---------------------------------------------------------------------------
# 1. APP & SHARED RESOURCES (initialized once at startup, reused per request)
# ---------------------------------------------------------------------------

app = FastAPI(title="SkinCode API")

# Supabase client, created once and reused.
_supabase_client: Optional[Client] = None


@app.on_event("startup")
def startup():
    global _supabase_client

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_KEY environment variables must be set."
        )
    _supabase_client = create_client(supabase_url, supabase_key)


# ---------------------------------------------------------------------------
# 2. RESPONSE MODELS
# ---------------------------------------------------------------------------

class AnalysisAccepted(BaseModel):
    """Returned immediately when the photo passes upload validation."""
    analysis_id: str
    status: str = "pending"
    message: str = "Photo accepted, analysis in progress."


class AnalysisRejected(BaseModel):
    """Returned when the photo fails upload validation."""
    status: str = "rejected"
    message: str


# ---------------------------------------------------------------------------
# 3. UPLOAD VALIDATION (file size + minimum resolution for Perfect Corp SD tier)
# ---------------------------------------------------------------------------

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB - Perfect Corp's hard limit
MIN_IMAGE_DIMENSION_PX = 480             # minimum shortest side for the SD tier


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
#     task_id text,
#     skin_metrics jsonb,
#     skin_health_score integer,
#     recommendations jsonb,
#     error_message text,
#     created_at timestamptz not null default now(),
#     updated_at timestamptz
#   );

def _create_pending_record(analysis_id: str, image_metadata: dict) -> None:
    """Insert the initial 'pending' row before the background task starts."""
    _supabase_client.table("skin_analyses").insert({
        "id": analysis_id,
        "status": "pending",
        "quality_metrics": image_metadata,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


def _mark_task_submitted(analysis_id: str, task_id: str) -> None:
    """Record the Perfect Corp task_id once the task has been created; status stays 'pending'."""
    _supabase_client.table("skin_analyses").update({
        "task_id": task_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", analysis_id).execute()


def _mark_completed(
    analysis_id: str, skin_metrics: dict, skin_health_score: int, recommendations: list
) -> None:
    """Update the record once Perfect Corp analysis succeeds."""
    _supabase_client.table("skin_analyses").update({
        "status": "completed",
        "skin_metrics": skin_metrics,
        "skin_health_score": skin_health_score,
        "recommendations": recommendations,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", analysis_id).execute()


def _mark_failed(analysis_id: str, error_message: str) -> None:
    """Update the record if Perfect Corp analysis fails for any reason."""
    _supabase_client.table("skin_analyses").update({
        "status": "failed",
        "error_message": error_message,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", analysis_id).execute()


def _get_analysis_record(analysis_id: str) -> dict:
    """Fetch a skin_analyses row by id, or raise 404 if it doesn't exist."""
    result = (
        _supabase_client.table("skin_analyses")
        .select("*")
        .eq("id", analysis_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Analysis not found.")
    return result.data[0]


def _find_analysis_by_task_id(task_id: str) -> Optional[dict]:
    """Look up a skin_analyses row by its Perfect Corp task_id (used by the webhook handler,
    which only receives a task_id — not our analysis_id). Returns None if not found."""
    result = (
        _supabase_client.table("skin_analyses")
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
        if "ui_score" in item
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
# Steps 1-3 happen once, in the background task right after upload
# (submit_skin_analysis_task). Step 4 is NOT looped internally — Perfect
# Corp has no webhook/callback, so instead of us polling on a timer,
# GET /analyze/{analysis_id} performs a single step-4 check each time the
# client calls it, driven by the client's own polling cadence.
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


class PerfectCorpError(Exception):
    """Raised when the Perfect Corp API call fails or returns unexpected data."""


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
        raise PerfectCorpError(f"Perfect Corp file registration timed out: {e}") from e
    except httpx.HTTPStatusError as e:
        raise PerfectCorpError(
            f"Perfect Corp file registration returned {e.response.status_code}: {e.response.text}"
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
        raise PerfectCorpError(f"Perfect Corp file upload timed out: {e}") from e
    except httpx.HTTPStatusError as e:
        raise PerfectCorpError(
            f"Perfect Corp file upload returned {e.response.status_code}: {e.response.text}"
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
        raise PerfectCorpError(f"Perfect Corp task creation timed out: {e}") from e
    except httpx.HTTPStatusError as e:
        raise PerfectCorpError(
            f"Perfect Corp task creation returned {e.response.status_code}: {e.response.text}"
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
        raise PerfectCorpError(f"Perfect Corp task status check timed out: {e}") from e
    except httpx.HTTPStatusError as e:
        raise PerfectCorpError(
            f"Perfect Corp task status check returned {e.response.status_code}: {e.response.text}"
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

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
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
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
            data = await _fetch_skin_analysis_task(client, api_key, task_id)
    except PerfectCorpError as e:
        _mark_failed(analysis_id, str(e))
        return _get_analysis_record(analysis_id)

    task_status = data.get("task_status")
    if task_status == "success":
        skin_metrics = data["results"]
        skin_health_score = compute_skin_health_score(skin_metrics)
        recommendations = compute_product_recommendations(skin_metrics, skin_health_score)
        _mark_completed(analysis_id, skin_metrics, skin_health_score, recommendations)
        return _get_analysis_record(analysis_id)
    if task_status != "running":
        # Perfect Corp includes a machine-readable "error" code and a
        # human-readable "error_message" on failed tasks (e.g.
        # error_src_face_too_small) — surface those instead of a generic
        # message so the actual rejection reason isn't lost.
        detail = data.get("error_message") or data.get("error") or "unknown reason"
        _mark_failed(analysis_id, f"Perfect Corp task failed ({task_status}): {detail}")
        return _get_analysis_record(analysis_id)

    return _get_analysis_record(analysis_id)


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
# 7. BACKGROUND TASK
# ---------------------------------------------------------------------------

async def process_analysis(analysis_id: str, image_bytes: bytes, filename: str, content_type: str) -> None:
    """
    Runs after the response has already been sent to the client. Registers
    the photo with Perfect Corp and creates the analysis task (steps 1-3),
    then stores the task_id on the record — status stays "pending".

    The task's completion is checked lazily by GET /analyze/{analysis_id}
    (one Perfect Corp status check per client call) rather than by this
    background task looping/sleeping until it's done.

    NOTE: FastAPI's BackgroundTasks has no built-in retry or persistence —
    if this task crashes before storing a task_id, the analysis stays
    stuck in "pending" unless _mark_failed is reached. This is an accepted
    limitation for the current stage; a retry/queue mechanism is a future
    improvement once traffic justifies the added infrastructure.
    """
    try:
        task_id = await submit_skin_analysis_task(image_bytes, filename, content_type)
        _mark_task_submitted(analysis_id, task_id)
    except PerfectCorpError as e:
        _mark_failed(analysis_id, str(e))
    except Exception as e:
        # Catch-all so an unexpected error still leaves a clear failure
        # record instead of leaving the analysis stuck in "pending" forever.
        _mark_failed(analysis_id, f"Unexpected error: {e}")


# ---------------------------------------------------------------------------
# 8. MAIN ENDPOINT
# ---------------------------------------------------------------------------

@app.post(
    "/analyze",
    response_model=AnalysisAccepted,
    status_code=202,
    responses={422: {"model": AnalysisRejected}},
)
async def analyze_photo(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    """
    Accept a photo upload, validate it meets Perfect Corp's SD-tier
    requirements, and — if it passes — kick off the Perfect Corp
    analysis in the background.
    """
    image_bytes = await file.read()

    # --- Stage 1: upload validation (file size + minimum resolution) ---
    try:
        image_metadata = validate_image_for_perfect_corp(image_bytes)
    except ImageValidationError as e:
        return JSONResponse(
            status_code=422,
            content=AnalysisRejected(message=e.message).model_dump(),
        )

    # --- Stage 2: create the pending record before returning ---
    analysis_id = str(uuid.uuid4())
    try:
        _create_pending_record(analysis_id, image_metadata)
    except Exception as e:
        # If we can't even create the record, don't schedule a background
        # task that will have nothing to update — fail loudly instead.
        raise HTTPException(
            status_code=500, detail=f"Failed to create analysis record: {e}"
        ) from e

    # --- Stage 3: schedule the expensive work for after the response ---
    background_tasks.add_task(
        process_analysis,
        analysis_id,
        image_bytes,
        file.filename or "photo.jpg",
        file.content_type or "image/jpeg",
    )

    # --- Stage 4: respond immediately, client doesn't wait for Perfect Corp ---
    return AnalysisAccepted(analysis_id=analysis_id)


@app.get("/analyze/{analysis_id}")
async def get_analysis_result(analysis_id: str):
    """
    Polling endpoint for the client to check on a previously submitted
    analysis. If it's still "pending" and a Perfect Corp task_id has been
    recorded, this performs a single status check against Perfect Corp
    (see check_skin_analysis_task) and updates Supabase accordingly —
    the client is expected to call this repeatedly until the status is
    no longer "pending". Returns a trimmed view (see _build_analysis_response)
    rather than the raw Supabase row.
    """
    record = _get_analysis_record(analysis_id)

    if record["status"] == "pending" and record.get("task_id"):
        record = await check_skin_analysis_task(analysis_id, record["task_id"])

    return _build_analysis_response(record)


@app.get("/analyze/{analysis_id}/recommendations")
async def get_product_recommendations(analysis_id: str):
    """
    Return product recommendations for a completed analysis. These are
    computed once — the moment GET /analyze/{analysis_id} observes the
    Perfect Corp task succeeding (see check_skin_analysis_task) — and
    stored on the record; this endpoint just reads that stored value,
    computing it on the fly as a fallback for records that predate that.
    """
    record = _get_analysis_record(analysis_id)

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


@app.post("/webhooks/perfect-corp")
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
    """
    body = await request.body()

    webhook_secret = os.environ.get("PERFECT_CORP_WEBHOOK_SECRET")
    if not webhook_secret:
        raise HTTPException(status_code=500, detail="Webhook secret is not configured.")

    try:
        Webhook(webhook_secret).verify(body, dict(request.headers))
    except WebhookVerificationError as e:
        raise HTTPException(status_code=401, detail=f"Invalid webhook signature: {e}") from e

    payload = json.loads(body)
    task_id = payload.get("data", {}).get("task_id")
    if not task_id:
        raise HTTPException(status_code=400, detail="Missing data.task_id in webhook payload.")

    record = _find_analysis_by_task_id(task_id)
    if record is not None and record["status"] == "pending":
        await check_skin_analysis_task(record["id"], task_id)

    # Always 200 once the signature checks out, even if we didn't find a
    # matching (still-pending) record — so Perfect Corp doesn't retry
    # forever over something that isn't a delivery problem on their end.
    return {"status": "ok"}


@app.get("/health")
async def health_check():
    """Basic liveness check for Cloud Run / load balancer probes."""
    return {"status": "ok"}
