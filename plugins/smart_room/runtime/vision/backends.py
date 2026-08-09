"""Optional local vision model adapters.

Imports stay lazy so the Smart Room daemon can still run when camera extras
are not installed.  Model assets live in the active Marvi profile.
"""

from __future__ import annotations

import logging
import hashlib
from pathlib import Path
from typing import Any, Dict
from urllib.request import urlopen

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_GESTURE_URL = "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task"
_POSE_URL = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task"
_ASSET_SHA256 = {
    "gesture_recognizer": "97952348cf6a6a4915c2ea1496b4b37ebabc50cbbf80571435643c455f2b0482",
    "pose_landmarker_lite": "59929e1d1ee95287735ddd833b19cf4ac46d29bc7afddbbf6753c459690d574a",
}
_BUFFALO_L_SHA256 = {
    "1k3d68.onnx": "df5c06b8a0c12e422b2ed8947b8869faa4105387f199c477af038aa01f9a45cc",
    "2d106det.onnx": "f001b856447c413801ef5c42091ed0cd516fcd21f2d6b79635b1e733a7109dbf",
    "det_10g.onnx": "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
    "genderage.onnx": "4fde69b1c810857b88c64a335084f1c3fe8f01246c9a191b48c7bb756d6652fb",
    "w600k_r50.onnx": "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _asset(name: str, url: str, config: Dict[str, Any]) -> Path:
    configured = str(config.get(f"{name}_model_path") or "").strip()
    path = Path(configured) if configured else Path(get_hermes_home()) / "smart_room" / "vision" / "models" / f"{name}.task"
    expected = _ASSET_SHA256[name]
    if path.is_file():
        if _sha256(path) != expected:
            raise RuntimeError(f"{name} model checksum mismatch: {path}")
        return path
    if not config.get("auto_download_models", True):
        raise RuntimeError(f"missing {name} model: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".download")
    with urlopen(url, timeout=60) as response, tmp.open("wb") as handle:  # noqa: S310 - fixed Google model URLs
        while chunk := response.read(1024 * 1024):
            handle.write(chunk)
    if _sha256(tmp) != expected:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"downloaded {name} model failed checksum verification")
    tmp.replace(path)
    return path


class MediaPipeBackend:
    """Pose and canned hand-gesture inference using MediaPipe Tasks."""

    def __init__(self, config: Dict[str, Any]):
        import mediapipe as mp

        self._mp = mp
        base = mp.tasks.BaseOptions
        vision = mp.tasks.vision
        pose_path = _asset("pose_landmarker_lite", _POSE_URL, config)
        gesture_path = _asset("gesture_recognizer", _GESTURE_URL, config)
        self._pose = vision.PoseLandmarker.create_from_options(
            vision.PoseLandmarkerOptions(
                base_options=base(model_asset_path=str(pose_path)),
                running_mode=vision.RunningMode.IMAGE,
                num_poses=max(1, int(config.get("max_people", 3))),
                min_pose_detection_confidence=float(config.get("pose_confidence", 0.5)),
            )
        )
        self._gesture = vision.GestureRecognizer.create_from_options(
            vision.GestureRecognizerOptions(
                base_options=base(model_asset_path=str(gesture_path)),
                running_mode=vision.RunningMode.IMAGE,
                num_hands=2,
                min_hand_detection_confidence=float(config.get("gesture_confidence", 0.6)),
            )
        )

    def analyze(self, rgb: Any) -> Dict[str, Any]:
        import numpy as np

        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        poses = []
        result = self._pose.detect(image)
        for landmarks in result.pose_landmarks:
            xs = [float(p.x) for p in landmarks]
            ys = [float(p.y) for p in landmarks]
            visible = [float(getattr(p, "visibility", 1.0)) for p in landmarks]
            center = (sum(xs) / len(xs), sum(ys) / len(ys))
            shoulder_width = abs(xs[11] - xs[12])
            shoulder_dy = abs(ys[11] - ys[12])
            hip_width = abs(xs[23] - xs[24])
            torso_dy = abs(((ys[11] + ys[12]) / 2) - ((ys[23] + ys[24]) / 2))
            horizontal = shoulder_dy > max(shoulder_width * 0.65, 0.08) or torso_dy < max(hip_width * 0.55, 0.07)
            if horizontal:
                posture = "lying"
            elif center[1] > 0.56 and max(ys[25], ys[26]) > 0.72:
                posture = "seated"
            else:
                posture = "standing"
            poses.append({
                "bbox": [max(0.0, min(xs)), max(0.0, min(ys)), min(1.0, max(xs)), min(1.0, max(ys))],
                "center": [round(center[0], 4), round(center[1], 4)],
                "posture": posture,
                "confidence": round(sum(visible) / len(visible), 3),
            })
        gestures = []
        gesture_result = self._gesture.recognize(image)
        for categories in gesture_result.gestures:
            if categories:
                best = categories[0]
                name = str(best.category_name or "").strip()
                # MediaPipe emits a literal "None" category when a hand is
                # visible but no canned gesture matches. It is absence, not a
                # gesture; forwarding it resets the temporal hold candidate.
                if name and name.lower() != "none":
                    gestures.append({"name": name, "confidence": float(best.score)})
        return {"poses": poses, "gestures": gestures}

    def close(self) -> None:
        self._pose.close()
        self._gesture.close()


class InsightFaceBackend:
    """Face detection plus ArcFace embeddings with CUDA/CPU fallback."""

    def __init__(self, config: Dict[str, Any]):
        from insightface.app import FaceAnalysis
        import onnxruntime as ort

        requested = config.get("providers") or ["CUDAExecutionProvider", "CPUExecutionProvider"]
        available = set(ort.get_available_providers())
        providers = [name for name in requested if name in available] or ["CPUExecutionProvider"]
        model_name = str(config.get("face_model", "buffalo_l"))
        root = Path(str(config.get("model_root") or (Path.home() / ".insightface")))
        model_dir = root / "models" / model_name
        if model_name == "buffalo_l" and model_dir.is_dir():
            self._verify_buffalo(model_dir)
        self._app = FaceAnalysis(name=model_name, root=str(root), providers=providers)
        size = int(config.get("face_detection_size", 640))
        self._app.prepare(ctx_id=0, det_size=(size, size))
        if model_name == "buffalo_l":
            self._verify_buffalo(model_dir)

    @staticmethod
    def _verify_buffalo(model_dir: Path) -> None:
        for filename, expected in _BUFFALO_L_SHA256.items():
            path = model_dir / filename
            if not path.is_file() or _sha256(path) != expected:
                raise RuntimeError(f"InsightFace buffalo_l checksum mismatch: {path}")

    def analyze(self, bgr: Any, width: int, height: int) -> list[Dict[str, Any]]:
        faces = []
        for face in self._app.get(bgr):
            x1, y1, x2, y2 = [float(v) for v in face.bbox]
            faces.append({
                "bbox": [x1 / width, y1 / height, x2 / width, y2 / height],
                "center": [((x1 + x2) / 2) / width, ((y1 + y2) / 2) / height],
                "confidence": float(face.det_score),
                "embedding": [float(v) for v in face.normed_embedding],
            })
        return faces
