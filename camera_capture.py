"""
SkinCode - Camera Capture Prototype
=====================================
Python/OpenCV prototype for the camera capture flow, meant as a reference
for building the actual client-side implementation (browser JavaScript).

IMPORTANT CONTEXT:
SkinCode is deployed as an API service for web/mobile browsers. Camera
capture in production happens on the CLIENT (browser via getUserMedia +
Canvas), NOT on the server. This file is NOT meant to run in production —
it exists so you can prototype and test the capture -> upload check ->
retry flow locally with a laptop webcam before porting the logic to
JavaScript.

Each function below includes a comment mapping it to its browser
equivalent, so this file doubles as a spec for the JS implementation.

Dependencies:
    pip install opencv-python --break-system-packages

Usage:
    python camera_capture.py
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable
import time
import cv2
import numpy as np


# ---------------------------------------------------------------------------
# 1. CAPTURE CONFIGURATION
# ---------------------------------------------------------------------------

@dataclass
class CaptureConfig:
    camera_index: int = 0          # 0 = default webcam
    frame_width: int = 1280
    frame_height: int = 720
    jpeg_quality: int = 90         # 0-100, matches browser canvas.toDataURL quality
    countdown_seconds: int = 3     # delay before auto-capture, gives user time to pose
    max_retries: int = 3           # how many retry attempts before giving up


DEFAULT_CAPTURE_CONFIG = CaptureConfig()


class CaptureStatus(str, Enum):
    SUCCESS = "success"
    CAMERA_UNAVAILABLE = "camera_unavailable"
    USER_CANCELLED = "user_cancelled"
    MAX_RETRIES_EXCEEDED = "max_retries_exceeded"


@dataclass
class CaptureResult:
    status: CaptureStatus
    image_bytes: Optional[bytes] = None
    attempts_used: int = 0


# ---------------------------------------------------------------------------
# 2. CAMERA SESSION
# ---------------------------------------------------------------------------
# Browser equivalent: navigator.mediaDevices.getUserMedia({ video: {...} })
# opens a live camera stream bound to a <video> element. This class wraps
# the same idea using cv2.VideoCapture, keeping the device open across
# multiple capture attempts instead of reopening it every time (reopening
# a webcam repeatedly is slow and causes visible flicker/delay — same
# reasoning applies to keeping a single MediaStream alive in the browser
# across retries rather than calling getUserMedia() again per attempt).

class CameraSession:
    """Wraps a single open camera device for one capture session."""

    def __init__(self, config: CaptureConfig = DEFAULT_CAPTURE_CONFIG):
        self.config = config
        self._cap: Optional[cv2.VideoCapture] = None

    def open(self) -> bool:
        """
        Open the camera device.

        Browser equivalent:
            const stream = await navigator.mediaDevices.getUserMedia({
                video: { width: 1280, height: 720, facingMode: "user" }
            });
            videoElement.srcObject = stream;

        Returns:
            True if the camera opened successfully, False otherwise
            (permission denied, no device, device in use by another app).
        """
        self._cap = cv2.VideoCapture(self.config.camera_index)
        if not self._cap.isOpened():
            return False

        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.frame_width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.frame_height)
        return True

    def read_frame(self) -> Optional[np.ndarray]:
        """
        Read a single frame from the live stream (BGR array).

        Browser equivalent:
            ctx.drawImage(videoElement, 0, 0, canvas.width, canvas.height);
            (draws the CURRENT video frame onto an offscreen canvas)
        """
        if self._cap is None or not self._cap.isOpened():
            return None
        ok, frame = self._cap.read()
        return frame if ok else None

    def encode_frame(self, frame: np.ndarray) -> bytes:
        """
        Encode a raw frame into JPEG bytes, ready to send to the backend.

        Browser equivalent:
            canvas.toBlob(blob => { ... }, "image/jpeg", 0.9);
            // or canvas.toDataURL("image/jpeg", 0.9) for a base64 string
        """
        encode_params = [cv2.IMWRITE_JPEG_QUALITY, self.config.jpeg_quality]
        success, buffer = cv2.imencode(".jpg", frame, encode_params)
        if not success:
            raise RuntimeError("Failed to encode frame to JPEG")
        return buffer.tobytes()

    def close(self):
        """
        Release the camera device.

        Browser equivalent:
            stream.getTracks().forEach(track => track.stop());
            (stops all tracks so the browser turns off the camera light)
        """
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# ---------------------------------------------------------------------------
# 3. CAPTURE WITH COUNTDOWN
# ---------------------------------------------------------------------------
# Browser equivalent: a countdown overlay ("3... 2... 1...") rendered with
# setInterval/requestAnimationFrame over the <video> preview, so the user
# has time to position their face before the frame is grabbed.

def capture_with_countdown(
    session: CameraSession,
    config: CaptureConfig = DEFAULT_CAPTURE_CONFIG,
    on_tick: Optional[Callable[[int], None]] = None,
) -> Optional[np.ndarray]:
    """
    Wait for `countdown_seconds`, showing a live preview, then grab one
    frame.

    Args:
        session: an already-open CameraSession
        config: capture configuration
        on_tick: optional callback invoked once per second with the
                 remaining seconds (e.g. to update a UI label). In the
                 browser this maps to updating the countdown overlay text
                 inside a setInterval callback.

    Returns:
        The captured frame (BGR array), or None if the camera dropped
        out during the countdown.
    """
    window_name = "SkinCode - Camera Preview (prototype)"
    remaining = config.countdown_seconds

    start_time = time.time()
    while True:
        frame = session.read_frame()
        if frame is None:
            cv2.destroyWindow(window_name)
            return None

        elapsed = time.time() - start_time
        current_remaining = max(0, config.countdown_seconds - int(elapsed))
        if current_remaining != remaining:
            remaining = current_remaining
            if on_tick:
                on_tick(remaining)

        preview = frame.copy()
        cv2.putText(
            preview,
            str(remaining) if remaining > 0 else "Capturing...",
            (40, 80),
            cv2.FONT_HERSHEY_SIMPLEX,
            2.0,
            (0, 255, 0),
            3,
        )
        cv2.imshow(window_name, preview)
        cv2.waitKey(1)

        if elapsed >= config.countdown_seconds:
            cv2.destroyWindow(window_name)
            return frame


# ---------------------------------------------------------------------------
# 4. CAPTURE + UPLOAD CHECK + RETRY LOOP
# ---------------------------------------------------------------------------
# Browser equivalent: after canvas.toBlob() produces the JPEG, POST it to
# the /analyze endpoint. The backend only validates file size and minimum
# resolution itself (see main.py's validate_image_for_perfect_corp) —
# actual photo-quality rejection (blur, no face, bad pose, etc.) now
# happens on Perfect Corp's side at no extra cost. If the backend
# responds with a 422 rejection either way, show the rejection message
# to the user and re-open the countdown/capture UI for another attempt,
# up to max_retries.

def capture_with_retry(
    config: CaptureConfig = DEFAULT_CAPTURE_CONFIG,
    quality_check_fn: Optional[Callable[[bytes], tuple[bool, Optional[str]]]] = None,
) -> CaptureResult:
    """
    Full capture flow: open camera, countdown, capture, run quality check,
    retry on failure up to max_retries.

    Args:
        config: capture configuration
        quality_check_fn: a function that takes JPEG bytes and returns
            (passed, message). In production this would hit the backend
            /analyze endpoint over HTTP and inspect the response status
            (202 = accepted, 422 = rejected). Left as a callback here so
            this file has no hard dependency on the backend.

    Returns:
        CaptureResult with the final status and image bytes (if successful)
    """
    with CameraSession(config) as session:
        if session._cap is None or not session._cap.isOpened():
            return CaptureResult(status=CaptureStatus.CAMERA_UNAVAILABLE)

        for attempt in range(1, config.max_retries + 1):
            frame = capture_with_countdown(session, config)
            if frame is None:
                return CaptureResult(
                    status=CaptureStatus.CAMERA_UNAVAILABLE,
                    attempts_used=attempt,
                )

            image_bytes = session.encode_frame(frame)

            if quality_check_fn is None:
                # No quality check wired up — treat capture alone as success.
                return CaptureResult(
                    status=CaptureStatus.SUCCESS,
                    image_bytes=image_bytes,
                    attempts_used=attempt,
                )

            passed, message = quality_check_fn(image_bytes)
            if passed:
                return CaptureResult(
                    status=CaptureStatus.SUCCESS,
                    image_bytes=image_bytes,
                    attempts_used=attempt,
                )

            print(f"[Attempt {attempt}/{config.max_retries}] Rejected: {message}")

        return CaptureResult(
            status=CaptureStatus.MAX_RETRIES_EXCEEDED,
            attempts_used=config.max_retries,
        )


# ---------------------------------------------------------------------------
# 5. USAGE EXAMPLE
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Standalone test: capture with countdown, no upload check wired up.
    # To test the full flow against the backend, see the commented
    # example below.
    result = capture_with_retry(DEFAULT_CAPTURE_CONFIG)

    print(f"Status: {result.status}")
    print(f"Attempts used: {result.attempts_used}")

    if result.status == CaptureStatus.SUCCESS and result.image_bytes:
        with open("captured_photo.jpg", "wb") as f:
            f.write(result.image_bytes)
        print("Saved to captured_photo.jpg")

    # --- Example wiring against the backend /analyze endpoint (uncomment to use) ---
    # import httpx
    #
    # def quality_check_fn(image_bytes: bytes) -> tuple[bool, Optional[str]]:
    #     response = httpx.post(
    #         "http://localhost:8000/analyze",
    #         files={"file": ("photo.jpg", image_bytes, "image/jpeg")},
    #     )
    #     if response.status_code == 202:
    #         return True, None
    #     return False, response.json().get("message")
    #
    # result = capture_with_retry(
    #     DEFAULT_CAPTURE_CONFIG, quality_check_fn=quality_check_fn
    # )
