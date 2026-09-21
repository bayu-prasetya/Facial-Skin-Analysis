"""
SkinCode - Photo Quality Gate
==============================
Module for validating photo quality BEFORE sending it to the Perfect Corp
YouCam AI Skin API. Purpose: save vendor API quota by filtering out photos
that clearly aren't suitable for analysis (blurry, too dark, no face
detected, tilted pose, etc.) before incurring a per-call cost.

Dependencies:
    pip install mediapipe opencv-python numpy --break-system-packages

Model file (must be downloaded manually once, not bundled with the
mediapipe package):
    wget -O face_landmarker.task \
      https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
    Save it at the path referenced by MODEL_PATH below, or override it
    via the FaceAnalyzer(model_path=...) parameter.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import numpy as np
import cv2
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import (
    FaceLandmarker,
    FaceLandmarkerOptions,
    RunningMode,
)


# ---------------------------------------------------------------------------
# 1. THRESHOLD CONFIGURATION
# ---------------------------------------------------------------------------
# All values below are a starting point and MUST be recalibrated using
# real sample photos from SkinCode's target users (average phone lighting
# conditions, front vs back camera, etc.).

@dataclass
class QualityThresholds:
    # --- Resolution ---
    min_width: int = 450
    min_height: int = 450

    # --- Blur (variance of Laplacian) ---
    # The lower the value, the blurrier the image. Common reference: 100-150.
    min_laplacian_variance: float = 100.0

    # --- Brightness (mean grayscale intensity, 0-255 scale) ---
    min_brightness: float = 60.0
    max_brightness: float = 200.0

    # --- Face detection ---
    min_face_detection_confidence: float = 0.6
    # Face must fill at least this % of the frame (avoid faces too far away)
    min_face_area_ratio: float = 0.08

    # --- Pose (degrees) ---
    max_yaw_degrees: float = 20.0     # turning left/right
    max_pitch_degrees: float = 20.0   # looking down/up
    max_roll_degrees: float = 15.0    # tilted head

    # --- Eyes ---
    min_eye_openness_ratio: float = 0.15  # minimum eye aspect ratio


DEFAULT_THRESHOLDS = QualityThresholds()


# ---------------------------------------------------------------------------
# 2. RESULT STRUCTURE
# ---------------------------------------------------------------------------

class RejectionReason(str, Enum):
    NO_FACE_DETECTED = "no_face_detected"
    MULTIPLE_FACES = "multiple_faces"
    FACE_TOO_SMALL = "face_too_small"
    LOW_RESOLUTION = "low_resolution"
    TOO_BLURRY = "too_blurry"
    TOO_DARK = "too_dark"
    TOO_BRIGHT = "too_bright"
    POSE_YAW_EXCEEDED = "pose_yaw_exceeded"
    POSE_PITCH_EXCEEDED = "pose_pitch_exceeded"
    POSE_ROLL_EXCEEDED = "pose_roll_exceeded"
    EYES_CLOSED = "eyes_closed"
    INVALID_IMAGE = "invalid_image"


# User-facing messages (can be mapped to i18n later)
REJECTION_MESSAGES: dict[RejectionReason, str] = {
    RejectionReason.NO_FACE_DETECTED: "No face detected. Make sure your face is clearly visible and facing the camera.",
    RejectionReason.MULTIPLE_FACES: "More than one face detected. Make sure only your face is in the photo.",
    RejectionReason.FACE_TOO_SMALL: "Face is too small in the frame. Move the camera closer to your face.",
    RejectionReason.LOW_RESOLUTION: "Photo resolution is too low. Use a photo with higher resolution.",
    RejectionReason.TOO_BLURRY: "Photo is too blurry. Make sure the camera is in focus and your hand is steady.",
    RejectionReason.TOO_DARK: "Photo is too dark. Take the photo in a better lit area.",
    RejectionReason.TOO_BRIGHT: "Photo is too bright/overexposed. Avoid excessive direct light.",
    RejectionReason.POSE_YAW_EXCEEDED: "Face the camera straight on (don't turn your head).",
    RejectionReason.POSE_PITCH_EXCEEDED: "Face the camera straight on (don't look up or down).",
    RejectionReason.POSE_ROLL_EXCEEDED: "Straighten your head position (don't tilt it).",
    RejectionReason.EYES_CLOSED: "Make sure your eyes are open when the photo is taken.",
    RejectionReason.INVALID_IMAGE: "Photo file is invalid or corrupted.",
}


@dataclass
class QualityCheckResult:
    passed: bool
    reasons: list[RejectionReason] = field(default_factory=list)
    # Raw metrics, useful for logging/analytics & future threshold tuning
    metrics: dict = field(default_factory=dict)

    def user_message(self) -> Optional[str]:
        """Get the first (most relevant) message to show the user."""
        if not self.reasons:
            return None
        return REJECTION_MESSAGES[self.reasons[0]]

    def all_messages(self) -> list[str]:
        return [REJECTION_MESSAGES[r] for r in self.reasons]


# ---------------------------------------------------------------------------
# 3. FACE DETECTOR (MediaPipe) - initialize once, reuse
# ---------------------------------------------------------------------------

DEFAULT_MODEL_PATH = "face_landmarker.task"


class FaceAnalyzer:
    """Wrapper around MediaPipe Face Landmarker (Tasks API) for face detection + landmarks + pose."""

    def __init__(
        self,
        thresholds: QualityThresholds = DEFAULT_THRESHOLDS,
        model_path: str = DEFAULT_MODEL_PATH,
    ):
        self.thresholds = thresholds
        options = FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=model_path),
            running_mode=RunningMode.IMAGE,
            num_faces=2,  # detect up to 2 so we can flag "multiple_faces"
            min_face_detection_confidence=thresholds.min_face_detection_confidence,
            min_face_presence_confidence=thresholds.min_face_detection_confidence,
        )
        self._landmarker = FaceLandmarker.create_from_options(options)

    def close(self):
        self._landmarker.close()

    def detect(self, image_rgb: np.ndarray) -> "FaceDetectionOutput":
        """
        Run face landmark detection.

        Args:
            image_rgb: image array in RGB format (H, W, 3)

        Returns:
            FaceDetectionOutput containing landmarks (if any) & face count
        """
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=image_rgb)
        result = self._landmarker.detect(mp_image)

        if not result.face_landmarks:
            return FaceDetectionOutput(face_count=0, landmarks=None)

        face_count = len(result.face_landmarks)
        # Take the first (most dominant) face for further analysis
        # result.face_landmarks[0] is a list of NormalizedLandmark
        primary_landmarks = result.face_landmarks[0]

        return FaceDetectionOutput(
            face_count=face_count,
            landmarks=primary_landmarks,
        )


@dataclass
class FaceDetectionOutput:
    face_count: int
    landmarks: Optional[object]  # list of NormalizedLandmark (Tasks API)


# ---------------------------------------------------------------------------
# 4. INDIVIDUAL CHECK FUNCTIONS
# ---------------------------------------------------------------------------

def check_resolution(
    image: np.ndarray, thresholds: QualityThresholds = DEFAULT_THRESHOLDS
) -> tuple[bool, dict]:
    """Check whether the photo resolution meets the minimum requirement."""
    height, width = image.shape[:2]
    passed = width >= thresholds.min_width and height >= thresholds.min_height
    return passed, {"width": width, "height": height}


def check_blur(
    image_gray: np.ndarray, thresholds: QualityThresholds = DEFAULT_THRESHOLDS
) -> tuple[bool, dict]:
    """
    Check image sharpness using variance of Laplacian.
    A low value means the image is blurry (lacking edge detail).
    """
    laplacian_var = cv2.Laplacian(image_gray, cv2.CV_64F).var()
    passed = laplacian_var >= thresholds.min_laplacian_variance
    return passed, {"laplacian_variance": round(float(laplacian_var), 2)}


def check_brightness(
    image_gray: np.ndarray, thresholds: QualityThresholds = DEFAULT_THRESHOLDS
) -> tuple[bool, dict]:
    """Check whether the photo is neither too dark nor too bright."""
    mean_brightness = float(np.mean(image_gray))
    passed = thresholds.min_brightness <= mean_brightness <= thresholds.max_brightness
    return passed, {"mean_brightness": round(mean_brightness, 2)}


def check_face_presence(
    detection: FaceDetectionOutput,
) -> tuple[bool, list[RejectionReason], dict]:
    """Check face count: must be exactly 1."""
    reasons = []
    if detection.face_count == 0:
        reasons.append(RejectionReason.NO_FACE_DETECTED)
    elif detection.face_count > 1:
        reasons.append(RejectionReason.MULTIPLE_FACES)

    passed = len(reasons) == 0
    return passed, reasons, {"face_count": detection.face_count}


def check_face_size(
    landmarks,
    image_shape: tuple[int, int],
    thresholds: QualityThresholds = DEFAULT_THRESHOLDS,
) -> tuple[bool, dict]:
    """
    Check whether the face is large enough within the frame (landmark
    bounding box relative to the total image area).
    """
    height, width = image_shape[:2]
    # landmarks: list of NormalizedLandmark (Tasks API) — coords in 0-1 range
    xs = [lm.x for lm in landmarks]
    ys = [lm.y for lm in landmarks]

    face_width_ratio = max(xs) - min(xs)
    face_height_ratio = max(ys) - min(ys)
    face_area_ratio = face_width_ratio * face_height_ratio

    passed = face_area_ratio >= thresholds.min_face_area_ratio
    return passed, {"face_area_ratio": round(face_area_ratio, 4)}


def estimate_head_pose(
    landmarks, image_shape: tuple[int, int]
) -> dict:
    """
    Estimate head yaw/pitch/roll from MediaPipe landmarks using
    cv2.solvePnP: matching 2D landmark points (in the photo) against a
    generic 3D face model (average human anatomy).

    IMPORTANT solver flag note:
    A single solvePnP flag was found to be unreliable on its own:
    SOLVEPNP_ITERATIVE can converge to a mirrored/inverted solution
    (negative Z translation) on some real frontal-face landmarks from
    MediaPipe, a known "planar ambiguity" failure mode for 6-point
    solvePnP when points lie close to one plane (as facial landmarks
    largely do). SOLVEPNP_EPNP and SOLVEPNP_SQPNP were found to agree
    with each other and return the correct (positive Z) solution on real
    captured landmarks where ITERATIVE failed — but neither flag alone is
    guaranteed correct in every case.
    To stay robust without a large validation dataset, this function
    runs EPNP and SQPNP and cross-checks them: if they roughly agree, the
    result is trusted; if they diverge sharply, the pose is treated as
    unreliable and the rejection sentinel is returned instead of a
    silently wrong angle.

    Important notes:
    - The 3D model below is a generic APPROXIMATION (in mm, not
      individually precise). Accurate enough for quality-gate purposes
      (checking "frontal enough or not"), NOT for high-precision face
      measurement.
    - Camera focal length is estimated from image width (assuming a
      normal FOV), since we don't have real camera calibration data for
      each user's device.

    Reference points (MediaPipe Face Landmarker indices, 468-point mesh):
        1   = nose tip
        152 = chin
        33  = left eye corner (from the camera's point of view)
        263 = right eye corner
        61  = left mouth corner
        291 = right mouth corner

    Args:
        landmarks: list of NormalizedLandmark (0-1 coords) from FaceAnalyzer
        image_shape: (height, width) or (height, width, channels)

    Returns:
        dict with yaw, pitch, roll in degrees
    """
    height, width = image_shape[:2]

    # --- Generic 3D face model (in mm) ---
    # Axis convention MUST be consistent with image coordinates: X right,
    # Y DOWN (not up), Z toward the camera (face surface = larger/closer Z
    # compared to points that are more "recessed").
    # IMPORTANT: solvePnP is sensitive to this convention — if Y is flipped
    # (following the standard math convention "Y up"), the result is a
    # mirrored/inverted solution (pitch/roll ~180°) even though yaw looks
    # correct.
    model_points_3d = np.array([
        (0.0, 0.0, 0.0),          # nose tip
        (0.0, 63.6, -12.5),       # chin (positive Y = further down)
        (-43.3, -32.7, -26.0),    # left eye corner (negative Y = further up)
        (43.3, -32.7, -26.0),     # right eye corner
        (-28.9, 28.9, -24.1),     # left mouth corner
        (28.9, 28.9, -24.1),      # right mouth corner
    ], dtype=np.float64)

    landmark_indices = [1, 152, 33, 263, 61, 291]
    image_points_2d = np.array([
        (landmarks[idx].x * width, landmarks[idx].y * height)
        for idx in landmark_indices
    ], dtype=np.float64)

    # --- Estimate camera parameters (assuming no lens distortion) ---
    focal_length = width  # common approximation: focal length ≈ image width
    center = (width / 2, height / 2)
    camera_matrix = np.array([
        [focal_length, 0, center[0]],
        [0, focal_length, center[1]],
        [0, 0, 1],
    ], dtype=np.float64)
    dist_coeffs = np.zeros((4, 1))  # assume no lens distortion

    def _solve(flag) -> Optional[tuple[np.ndarray, np.ndarray]]:
        """Run solvePnP with a given flag, returning (rvec, tvec) or None."""
        ok, rvec, tvec = cv2.solvePnP(
            model_points_3d, image_points_2d, camera_matrix, dist_coeffs, flags=flag
        )
        if not ok or tvec[2, 0] <= 0:
            return None
        return rvec, tvec

    def _to_euler_degrees(rvec: np.ndarray) -> tuple[float, float, float]:
        rotation_matrix, _ = cv2.Rodrigues(rvec)
        sy = np.sqrt(rotation_matrix[0, 0] ** 2 + rotation_matrix[1, 0] ** 2)
        singular = sy < 1e-6
        if not singular:
            pitch = np.arctan2(rotation_matrix[2, 1], rotation_matrix[2, 2])
            yaw = np.arctan2(-rotation_matrix[2, 0], sy)
            roll = np.arctan2(rotation_matrix[1, 0], rotation_matrix[0, 0])
        else:
            pitch = np.arctan2(-rotation_matrix[1, 2], rotation_matrix[1, 1])
            yaw = np.arctan2(-rotation_matrix[2, 0], sy)
            roll = 0.0
        return np.degrees(yaw), np.degrees(pitch), np.degrees(roll)

    result_epnp = _solve(cv2.SOLVEPNP_EPNP)
    result_sqpnp = _solve(cv2.SOLVEPNP_SQPNP)

    if result_epnp is None and result_sqpnp is None:
        # Neither solver found a physically valid (positive-depth) solution.
        return {"yaw": 999.0, "pitch": 999.0, "roll": 999.0}

    if result_epnp is None or result_sqpnp is None:
        # Only one solver succeeded — use it, but this case is rare enough
        # that it's worth knowing about if it shows up a lot in practice.
        rvec, _ = result_epnp if result_epnp is not None else result_sqpnp
        yaw, pitch, roll = _to_euler_degrees(rvec)
        return {
            "yaw": round(float(yaw), 2),
            "pitch": round(float(pitch), 2),
            "roll": round(float(roll), 2),
        }

    # Both solvers succeeded — cross-check agreement before trusting them.
    rvec_epnp, _ = result_epnp
    rvec_sqpnp, _ = result_sqpnp
    yaw_e, pitch_e, roll_e = _to_euler_degrees(rvec_epnp)
    yaw_s, pitch_s, roll_s = _to_euler_degrees(rvec_sqpnp)

    # Threshold for "the two solvers roughly agree". A generous margin
    # (30 degrees) is used because EPNP/SQPNP naturally differ by a few
    # degrees even in good conditions; a large gap signals an unstable/
    # ambiguous pose rather than normal numerical noise.
    agreement_threshold_degrees = 30.0
    diffs = [abs(yaw_e - yaw_s), abs(pitch_e - pitch_s), abs(roll_e - roll_s)]
    if max(diffs) > agreement_threshold_degrees:
        # Solvers disagree sharply -> pose is unreliable, reject rather
        # than silently return one of the two possibly-wrong answers.
        return {"yaw": 999.0, "pitch": 999.0, "roll": 999.0}

    # They agree -> average the two estimates for a slightly more stable result.
    return {
        "yaw": round(float((yaw_e + yaw_s) / 2), 2),
        "pitch": round(float((pitch_e + pitch_s) / 2), 2),
        "roll": round(float((roll_e + roll_s) / 2), 2),
    }


def check_pose(
    landmarks,
    image_shape: tuple[int, int],
    thresholds: QualityThresholds = DEFAULT_THRESHOLDS,
) -> tuple[bool, list[RejectionReason], dict]:
    """Check whether the face pose is frontal enough (not turned/tilted/looking away)."""
    pose = estimate_head_pose(landmarks, image_shape)
    reasons = []

    if abs(pose["yaw"]) > thresholds.max_yaw_degrees:
        reasons.append(RejectionReason.POSE_YAW_EXCEEDED)
    if abs(pose["pitch"]) > thresholds.max_pitch_degrees:
        reasons.append(RejectionReason.POSE_PITCH_EXCEEDED)
    if abs(pose["roll"]) > thresholds.max_roll_degrees:
        reasons.append(RejectionReason.POSE_ROLL_EXCEEDED)

    passed = len(reasons) == 0
    return passed, reasons, pose


def _eye_aspect_ratio(landmarks, eye_indices: list[int]) -> float:
    """
    Compute the Eye Aspect Ratio (EAR) for one eye.

    EAR = (vertical top-bottom distance) / (horizontal left-right distance)
    The value drops sharply when the eye is closed (upper and lower
    eyelids move toward each other), and stays relatively stable when
    the eye is open.

    Args:
        landmarks: list of NormalizedLandmark
        eye_indices: [left, right, top1, bottom1, top2, bottom2] — 6 points
                     surrounding the eye, ordered per the standard EAR
                     convention
    """
    p_left, p_right, p_top1, p_bottom1, p_top2, p_bottom2 = [
        np.array([landmarks[i].x, landmarks[i].y]) for i in eye_indices
    ]

    vertical_1 = np.linalg.norm(p_top1 - p_bottom1)
    vertical_2 = np.linalg.norm(p_top2 - p_bottom2)
    horizontal = np.linalg.norm(p_left - p_right)

    if horizontal < 1e-6:
        return 0.0

    return (vertical_1 + vertical_2) / (2.0 * horizontal)


def check_eyes_open(
    landmarks,
    thresholds: QualityThresholds = DEFAULT_THRESHOLDS,
) -> tuple[bool, dict]:
    """
    Check whether the eyes are open using the Eye Aspect Ratio (EAR) from
    eye landmarks.

    Reference points (MediaPipe Face Landmarker indices, 468-point mesh):
        Left eye (from the camera's point of view):
            left corner=33, right corner=133, upper lid=159/158, lower=145/153
        Right eye:
            left corner=362, right corner=263, upper lid=386/385, lower=374/380

    EAR is computed for both eyes and averaged — so a momentary blink in
    one eye (camera capturing mid-blink) doesn't immediately reject an
    otherwise good photo.
    """
    LEFT_EYE = [33, 133, 159, 145, 158, 153]
    RIGHT_EYE = [362, 263, 386, 374, 385, 380]

    ear_left = _eye_aspect_ratio(landmarks, LEFT_EYE)
    ear_right = _eye_aspect_ratio(landmarks, RIGHT_EYE)
    ear_avg = (ear_left + ear_right) / 2.0

    passed = ear_avg >= thresholds.min_eye_openness_ratio
    return passed, {
        "eye_aspect_ratio": round(ear_avg, 3),
        "eye_aspect_ratio_left": round(ear_left, 3),
        "eye_aspect_ratio_right": round(ear_right, 3),
    }


# ---------------------------------------------------------------------------
# 5. MAIN PIPELINE — orchestrates all checks
# ---------------------------------------------------------------------------

def run_quality_gate(
    image_bytes: bytes,
    thresholds: QualityThresholds = DEFAULT_THRESHOLDS,
    face_analyzer: Optional[FaceAnalyzer] = None,
) -> QualityCheckResult:
    """
    Main entry point. Runs all quality gate stages in sequence, failing
    fast as soon as one stage fails to save compute.

    Order (cheap -> expensive):
        1. Decode & validate the image file
        2. Check resolution
        3. Detect face (MediaPipe)
        4. Check face count & size
        5. Check blur & brightness
        6. Check pose & eyes

    Args:
        image_bytes: raw photo file content (from upload)
        thresholds: threshold configuration, can be overridden
        face_analyzer: an already-initialized FaceAnalyzer instance
                        (reuse across requests to avoid reloading the
                        model every time)

    Returns:
        QualityCheckResult — passed=True if all stages pass
    """
    metrics: dict = {}

    # --- Stage 1: decode image ---
    image = _decode_image(image_bytes)
    if image is None:
        return QualityCheckResult(
            passed=False,
            reasons=[RejectionReason.INVALID_IMAGE],
            metrics=metrics,
        )

    # --- Stage 2: resolution ---
    res_passed, res_metrics = check_resolution(image, thresholds)
    metrics.update(res_metrics)
    if not res_passed:
        return QualityCheckResult(
            passed=False,
            reasons=[RejectionReason.LOW_RESOLUTION],
            metrics=metrics,
        )

    # --- Stage 3: face detection ---
    analyzer = face_analyzer or FaceAnalyzer(thresholds)
    image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    detection = analyzer.detect(image_rgb)

    face_passed, face_reasons, face_metrics = check_face_presence(detection)
    metrics.update(face_metrics)
    if not face_passed:
        return QualityCheckResult(passed=False, reasons=face_reasons, metrics=metrics)

    # --- Stage 4: face size within frame ---
    size_passed, size_metrics = check_face_size(detection.landmarks, image.shape, thresholds)
    metrics.update(size_metrics)
    if not size_passed:
        return QualityCheckResult(
            passed=False,
            reasons=[RejectionReason.FACE_TOO_SMALL],
            metrics=metrics,
        )

    # --- Stage 5: blur & brightness ---
    image_gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    blur_passed, blur_metrics = check_blur(image_gray, thresholds)
    metrics.update(blur_metrics)
    if not blur_passed:
        return QualityCheckResult(
            passed=False,
            reasons=[RejectionReason.TOO_BLURRY],
            metrics=metrics,
        )

    brightness_passed, brightness_metrics = check_brightness(image_gray, thresholds)
    metrics.update(brightness_metrics)
    if not brightness_passed:
        reason = (
            RejectionReason.TOO_DARK
            if brightness_metrics["mean_brightness"] < thresholds.min_brightness
            else RejectionReason.TOO_BRIGHT
        )
        return QualityCheckResult(passed=False, reasons=[reason], metrics=metrics)

    # --- Stage 6: pose ---
    pose_passed, pose_reasons, pose_metrics = check_pose(
        detection.landmarks, image.shape, thresholds
    )
    metrics.update(pose_metrics)
    if not pose_passed:
        return QualityCheckResult(passed=False, reasons=pose_reasons, metrics=metrics)

    # --- Stage 7: eyes open ---
    eyes_passed, eyes_metrics = check_eyes_open(detection.landmarks, thresholds)
    metrics.update(eyes_metrics)
    if not eyes_passed:
        return QualityCheckResult(
            passed=False,
            reasons=[RejectionReason.EYES_CLOSED],
            metrics=metrics,
        )

    # --- All checks passed ---
    return QualityCheckResult(passed=True, reasons=[], metrics=metrics)


def _decode_image(image_bytes: bytes) -> Optional[np.ndarray]:
    """Decode bytes into an OpenCV array (BGR). Returns None if decoding fails/corrupt."""
    try:
        np_arr = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        return image  # automatically None if decoding fails
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 6. USAGE EXAMPLE (will later be called from a FastAPI endpoint)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Manual local test example
    with open("captured_photo.jpg", "rb") as f:
        image_bytes = f.read()

    analyzer = FaceAnalyzer()
    result = run_quality_gate(image_bytes, face_analyzer=analyzer)
    analyzer.close()

    print(f"Passed: {result.passed}")
    print(f"Reasons: {result.reasons}")
    print(f"Message: {result.user_message()}")
    print(f"Metrics: {result.metrics}")