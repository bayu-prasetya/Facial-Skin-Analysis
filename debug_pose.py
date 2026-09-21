"""
Diagnostic script - run this on your actual photo to print the raw
landmark coordinates and intermediate solvePnP values that produce the
999.0 sentinel. Paste the output back so we can find the real root cause
instead of guessing with synthetic landmarks.

Usage:
    python debug_pose.py path/to/your_photo.jpg
"""

import sys
import cv2
import numpy as np
from quality_gate import FaceAnalyzer, QualityThresholds


def debug_pose(image_path: str):
    with open(image_path, "rb") as f:
        image_bytes = f.read()

    np_arr = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    height, width = image.shape[:2]
    print(f"Image size: {width}x{height}")

    analyzer = FaceAnalyzer()
    image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    detection = analyzer.detect(image_rgb)
    analyzer.close()

    if detection.face_count == 0:
        print("No face detected.")
        return

    landmarks = detection.landmarks
    print(f"Face count: {detection.face_count}")

    # Print the 6 raw landmark points used by estimate_head_pose
    landmark_indices = {
        "nose_tip (1)": 1,
        "chin (152)": 152,
        "left_eye (33)": 33,
        "right_eye (263)": 263,
        "left_mouth (61)": 61,
        "right_mouth (291)": 291,
    }
    print("\n--- Raw normalized landmarks (0-1) ---")
    for name, idx in landmark_indices.items():
        lm = landmarks[idx]
        print(f"{name}: x={lm.x:.4f} y={lm.y:.4f} z={lm.z:.4f}")

    print("\n--- Pixel coordinates ---")
    for name, idx in landmark_indices.items():
        lm = landmarks[idx]
        print(f"{name}: px=({lm.x * width:.1f}, {lm.y * height:.1f})")

    # Reproduce the solvePnP call manually with verbose output
    model_points_3d = np.array([
        (0.0, 0.0, 0.0),
        (0.0, 63.6, -12.5),
        (-43.3, -32.7, -26.0),
        (43.3, -32.7, -26.0),
        (-28.9, 28.9, -24.1),
        (28.9, 28.9, -24.1),
    ], dtype=np.float64)

    idx_list = [1, 152, 33, 263, 61, 291]
    image_points_2d = np.array([
        (landmarks[i].x * width, landmarks[i].y * height) for i in idx_list
    ], dtype=np.float64)

    focal_length = width
    center = (width / 2, height / 2)
    camera_matrix = np.array([
        [focal_length, 0, center[0]],
        [0, focal_length, center[1]],
        [0, 0, 1],
    ], dtype=np.float64)
    dist_coeffs = np.zeros((4, 1))

    success, rotation_vector, translation_vector = cv2.solvePnP(
        model_points_3d, image_points_2d, camera_matrix, dist_coeffs,
        flags=cv2.SOLVEPNP_ITERATIVE,
    )

    print(f"\n--- solvePnP result ---")
    print(f"success: {success}")
    print(f"translation_vector: {translation_vector.ravel()}")
    print(f"translation_vector[2] (tvec_z): {translation_vector[2, 0]:.2f}  "
          f"({'OK, positive' if translation_vector[2, 0] > 0 else 'NEGATIVE -> triggers 999.0 sentinel'})")

    rotation_matrix, _ = cv2.Rodrigues(rotation_vector)
    print(f"\nrotation_matrix:\n{rotation_matrix}")

    # Also try alternate solver flags for comparison
    print("\n--- Comparison with other solvePnP flags ---")
    for flag_name, flag in [
        ("ITERATIVE", cv2.SOLVEPNP_ITERATIVE),
        ("EPNP", cv2.SOLVEPNP_EPNP),
        ("SQPNP", cv2.SOLVEPNP_SQPNP),
    ]:
        s, rv, tv = cv2.solvePnP(
            model_points_3d, image_points_2d, camera_matrix, dist_coeffs, flags=flag
        )
        print(f"{flag_name}: tvec_z={tv[2,0]:.1f}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python debug_pose.py path/to/photo.jpg")
        sys.exit(1)
    debug_pose(sys.argv[1])
