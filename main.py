"""
SkinCode - FastAPI Endpoint
=============================
Main API endpoint that ties together the photo quality gate
(quality_gate.py), the Perfect Corp YouCam AI Skin API call, and
persistence to Supabase.

Flow for POST /analyze:
    1. Receive the uploaded photo (multipart/form-data)
    2. Run the quality gate SYNCHRONOUSLY (fast, local, no external cost)
    3. If rejected -> return 422 immediately with the rejection reason
    4. If passed -> insert a "pending" analysis record into Supabase
    5. Schedule the expensive part (Perfect Corp API call + result
       persistence) as a BackgroundTask
    6. Return 202 Accepted immediately with the analysis_id, so the
       client isn't blocked waiting for the vendor API round-trip
    7. The background task later calls Perfect Corp, then updates the
       Supabase record to "completed" (or "failed")

Dependencies:
    pip install fastapi python-multipart httpx supabase --break-system-packages

Environment variables required:
    SUPABASE_URL, SUPABASE_KEY          - Supabase project credentials
    PERFECT_CORP_API_KEY                - Perfect Corp YouCam vendor key
    PERFECT_CORP_API_BASE_URL           - Perfect Corp API base URL

Run locally:
    uvicorn main:app --reload
"""

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from supabase import create_client, Client

from quality_gate import run_quality_gate, FaceAnalyzer, QualityCheckResult


# ---------------------------------------------------------------------------
# 1. APP & SHARED RESOURCES (initialized once at startup, reused per request)
# ---------------------------------------------------------------------------

app = FastAPI(title="SkinCode API")

# FaceAnalyzer loads the MediaPipe model once and is reused across all
# requests — re-creating it per request would reload the model file every
# time, which is slow and unnecessary.
_face_analyzer: Optional[FaceAnalyzer] = None

# Supabase client, also created once and reused.
_supabase_client: Optional[Client] = None


@app.on_event("startup")
def startup():
    global _face_analyzer, _supabase_client

    _face_analyzer = FaceAnalyzer()

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_KEY environment variables must be set."
        )
    _supabase_client = create_client(supabase_url, supabase_key)


@app.on_event("shutdown")
def shutdown():
    global _face_analyzer
    if _face_analyzer is not None:
        _face_analyzer.close()


# ---------------------------------------------------------------------------
# 2. RESPONSE MODELS
# ---------------------------------------------------------------------------

class AnalysisAccepted(BaseModel):
    """Returned immediately when the photo passes the quality gate."""
    analysis_id: str
    status: str = "pending"
    message: str = "Photo accepted, analysis in progress."


class AnalysisRejected(BaseModel):
    """Returned when the photo fails the quality gate."""
    status: str = "rejected"
    reasons: list[str]
    message: str
    metrics: dict


# ---------------------------------------------------------------------------
# 3. SUPABASE HELPERS
# ---------------------------------------------------------------------------
# Table schema assumed (create this in Supabase):
#
#   create table skin_analyses (
#     id uuid primary key,
#     status text not null default 'pending',  -- pending | completed | failed
#     quality_metrics jsonb,
#     skin_metrics jsonb,
#     skin_health_score integer,
#     error_message text,
#     created_at timestamptz not null default now(),
#     updated_at timestamptz
#   );

def _create_pending_record(analysis_id: str, quality_metrics: dict) -> None:
    """Insert the initial 'pending' row before the background task starts."""
    _supabase_client.table("skin_analyses").insert({
        "id": analysis_id,
        "status": "pending",
        "quality_metrics": quality_metrics,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


def _mark_completed(analysis_id: str, skin_metrics: dict, skin_health_score: int) -> None:
    """Update the record once Perfect Corp analysis succeeds."""
    _supabase_client.table("skin_analyses").update({
        "status": "completed",
        "skin_metrics": skin_metrics,
        "skin_health_score": skin_health_score,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", analysis_id).execute()


def _mark_failed(analysis_id: str, error_message: str) -> None:
    """Update the record if Perfect Corp analysis fails for any reason."""
    _supabase_client.table("skin_analyses").update({
        "status": "failed",
        "error_message": error_message,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", analysis_id).execute()


# ---------------------------------------------------------------------------
# 4. PERFECT CORP CLIENT
# ---------------------------------------------------------------------------
# NOTE: this is a skeleton based on the general shape of a REST vendor API
# (submit image -> receive metrics). The actual Perfect Corp YouCam AI Skin
# API request/response format must be confirmed against their official
# docs before going live — field names below are placeholders.

class PerfectCorpError(Exception):
    """Raised when the Perfect Corp API call fails or returns unexpected data."""


async def call_perfect_corp_skin_analysis(image_bytes: bytes) -> dict:
    """
    Send the photo to Perfect Corp YouCam AI Skin API and return the raw
    skin metrics response.

    Raises:
        PerfectCorpError: on HTTP failure, timeout, or unexpected response
    """
    api_key = os.environ.get("PERFECT_CORP_API_KEY")
    base_url = os.environ.get("PERFECT_CORP_API_BASE_URL")
    if not api_key or not base_url:
        raise PerfectCorpError("Perfect Corp API credentials are not configured.")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                f"{base_url}/v1/skin-analysis",  # placeholder path
                headers={"Authorization": f"Bearer {api_key}"},
                files={"image": ("photo.jpg", image_bytes, "image/jpeg")},
            )
            response.raise_for_status()
        except httpx.TimeoutException as e:
            raise PerfectCorpError(f"Perfect Corp API timed out: {e}") from e
        except httpx.HTTPStatusError as e:
            raise PerfectCorpError(
                f"Perfect Corp API returned {e.response.status_code}: {e.response.text}"
            ) from e

    return response.json()


def compute_skin_health_score(skin_metrics: dict) -> int:
    """
    Convert raw Perfect Corp metrics into a single 0-100 skin health score.

    TODO: implement the actual deterministic weighted-scoring rule set
    (per the project's decision to use rule-based scoring, not LLM
    reasoning, for consistency and auditability). Placeholder below.
    """
    # Placeholder: replace with real weighted scoring logic.
    return 75


# ---------------------------------------------------------------------------
# 5. BACKGROUND TASK
# ---------------------------------------------------------------------------

async def process_analysis(analysis_id: str, image_bytes: bytes) -> None:
    """
    Runs after the response has already been sent to the client.
    Calls Perfect Corp, computes the health score, and persists the
    result (or failure) to Supabase.

    NOTE: FastAPI's BackgroundTasks has no built-in retry or persistence —
    if this task crashes (e.g. Perfect Corp is down), the analysis stays
    stuck in "pending" unless _mark_failed is reached. This is an accepted
    limitation for the current stage; a retry/queue mechanism is a future
    improvement once traffic justifies the added infrastructure.
    """
    try:
        skin_metrics = await call_perfect_corp_skin_analysis(image_bytes)
        skin_health_score = compute_skin_health_score(skin_metrics)
        _mark_completed(analysis_id, skin_metrics, skin_health_score)
    except PerfectCorpError as e:
        _mark_failed(analysis_id, str(e))
    except Exception as e:
        # Catch-all so an unexpected error still leaves a clear failure
        # record instead of leaving the analysis stuck in "pending" forever.
        _mark_failed(analysis_id, f"Unexpected error: {e}")


# ---------------------------------------------------------------------------
# 6. MAIN ENDPOINT
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
    Accept a photo upload, run the quality gate, and — if it passes —
    kick off the Perfect Corp analysis in the background.
    """
    image_bytes = await file.read()

    # --- Stage 1: quality gate (synchronous, fast, local) ---
    check_result: QualityCheckResult = run_quality_gate(
        image_bytes, face_analyzer=_face_analyzer
    )

    if not check_result.passed:
        return JSONResponse(
            status_code=422,
            content=AnalysisRejected(
                reasons=[r.value for r in check_result.reasons],
                message=check_result.user_message() or "Photo rejected.",
                metrics=check_result.metrics,
            ).model_dump(),
        )

    # --- Stage 2: create the pending record before returning ---
    analysis_id = str(uuid.uuid4())
    try:
        _create_pending_record(analysis_id, check_result.metrics)
    except Exception as e:
        # If we can't even create the record, don't schedule a background
        # task that will have nothing to update — fail loudly instead.
        raise HTTPException(
            status_code=500, detail=f"Failed to create analysis record: {e}"
        ) from e

    # --- Stage 3: schedule the expensive work for after the response ---
    background_tasks.add_task(process_analysis, analysis_id, image_bytes)

    # --- Stage 4: respond immediately, client doesn't wait for Perfect Corp ---
    return AnalysisAccepted(analysis_id=analysis_id)


@app.get("/analyze/{analysis_id}")
async def get_analysis_result(analysis_id: str):
    """
    Polling endpoint for the client to check on a previously submitted
    analysis (since the actual result arrives asynchronously via the
    background task).
    """
    result = (
        _supabase_client.table("skin_analyses")
        .select("*")
        .eq("id", analysis_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Analysis not found.")
    return result.data[0]


@app.get("/health")
async def health_check():
    """Basic liveness check for Cloud Run / load balancer probes."""
    return {"status": "ok"}
