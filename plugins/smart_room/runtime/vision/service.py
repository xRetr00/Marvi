"""Always-on, bounded-latency camera perception service for Smart Room."""

from __future__ import annotations

import logging
import base64
import json
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

from plugins.smart_room.runtime.models import now_iso
from plugins.smart_room.runtime.state_store import save_state
from plugins.smart_room.runtime.vision.faces import FaceLibrary, cosine_similarity
from plugins.smart_room.runtime.vision.history import VisionHistory
from plugins.smart_room.runtime.vision.reasoning import GestureController, SleepTracker, locate_zone

logger = logging.getLogger(__name__)


class VisionService:
    """Owns one camera thread and publishes a compact latest-truth snapshot."""

    def __init__(self, config: Dict[str, Any], state: Any, state_lock: threading.RLock,
                 emit_event: Callable[[str, Dict[str, Any]], None], gesture_callback: Callable[[str, Dict[str, Any]], None]):
        self.config = config
        self._state = state
        self._lock = state_lock
        self._emit_event = emit_event
        self._gesture_callback = gesture_callback
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._capture_thread: Optional[threading.Thread] = None
        self._analysis_thread: Optional[threading.Thread] = None
        self._gesture_thread: Optional[threading.Thread] = None
        self._face_thread: Optional[threading.Thread] = None
        self._latest_frame = None
        self._latest_lock = threading.Lock()
        self._latest_face_samples: tuple[str, list[Dict[str, Any]]] = ("", [])
        self._burst_until = 0.0
        self._gesture_priority_until = 0.0
        self._history = VisionHistory(config.get("history") or {})
        self._faces = FaceLibrary(config.get("faces") or {})
        self._sleep = SleepTracker(config.get("sleep") or {})
        self._gestures = GestureController(config.get("gestures") or {})
        self._mp = None
        self._face_backend = None
        self._cached_faces: list[Dict[str, Any]] = []
        self._last_face_inference_at = 0.0
        self._analysis_samples = 0
        self._analysis_window_at = time.monotonic()
        self._status: Dict[str, Any] = {"enabled": bool(config.get("enabled", False)), "running": False}
        self._last_event_values: Dict[str, Any] = {}
        self._last_unknown_evidence_at = 0.0
        self._last_persisted_at = 0.0

    def start(self) -> None:
        if not self.config.get("enabled", False) or self._capture_thread:
            return
        self._history.prune()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True, name="smart_room_camera")
        self._analysis_thread = threading.Thread(target=self._analysis_loop, daemon=True, name="smart_room_vision")
        self._gesture_thread = threading.Thread(target=self._gesture_loop, daemon=True, name="smart_room_gestures")
        self._face_thread = threading.Thread(target=self._face_loop, daemon=True, name="smart_room_faces")
        self._capture_thread.start()
        self._analysis_thread.start()
        self._gesture_thread.start()
        self._face_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for thread in (self._capture_thread, self._gesture_thread, self._analysis_thread, self._face_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=5)
        self._mark_camera(False, "service stopped")

    def request_burst(self, seconds: float = 8.0) -> None:
        self._burst_until = max(self._burst_until, time.monotonic() + max(1.0, seconds))
        self._wake.set()

    def observe(self, *, burst_seconds: float = 3.0, save_evidence: bool = False,
                deep: bool = False, question: str = "") -> Dict[str, Any]:
        self.request_burst(burst_seconds)
        deadline = time.monotonic() + min(max(burst_seconds, 0.5), 5.0)
        initial = self._state.vision.last_observed_at
        while time.monotonic() < deadline and self._state.vision.last_observed_at == initial:
            self._wake.wait(0.05)
        with self._lock:
            result = self._state.vision.__dict__.copy()
        if save_evidence:
            frame = self._frame_copy()
            if frame is not None:
                event_id = f"manual-{uuid.uuid4().hex[:12]}"
                result["evidence_path"] = self._history.save_frame(frame, evidence_id=event_id)
        if deep:
            result["deep_analysis"] = self._deep_analyze(question)
        return result

    def history(self, **filters: Any) -> list[Dict[str, Any]]:
        return self._history.query(**filters)

    def faces(self, action: str, **params: Any) -> Dict[str, Any]:
        if action == "list":
            return self._faces.list_people()
        if action == "review":
            return self._faces.review(str(params.get("event_id") or ""), name=str(params.get("name") or ""), reject=bool(params.get("reject")), owner=bool(params.get("owner")))
        if action == "delete":
            return self._faces.delete(str(params.get("name") or ""))
        if action == "enroll_current":
            return self._enroll_current(
                str(params.get("name") or ""),
                owner=bool(params.get("owner", False)),
                samples=max(3, min(int(params.get("samples", 8)), 30)),
                timeout=max(3.0, min(float(params.get("timeout", 20)), 30.0)),
            )
        raise ValueError("face action must be list, review, enroll_current, or delete")

    def status(self) -> Dict[str, Any]:
        return dict(self._status)

    def preview(self, *, width: int = 720, quality: int = 72) -> Dict[str, Any]:
        """Return a bounded JPEG data URL plus the latest perception overlay."""
        frame = self._frame_copy()
        if frame is None:
            return {"available": False, "error": self._status.get("last_error") or "no camera frame is available"}
        try:
            import cv2

            height, original_width = frame.shape[:2]
            target_width = max(320, min(int(width), 960))
            if original_width > target_width:
                frame = cv2.resize(frame, (target_width, round(height * target_width / original_width)))
            ok, encoded = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, max(45, min(int(quality), 88))]
            )
            if not ok:
                raise RuntimeError("failed to encode preview frame")
            with self._lock:
                vision = self._state.vision.__dict__.copy()
            return {
                "available": True,
                "captured_at": now_iso(),
                "image": "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode("ascii"),
                "vision": vision,
            }
        except Exception as exc:
            self._status["preview_error"] = str(exc)
            return {"available": False, "error": str(exc)}

    def _capture_loop(self) -> None:
        """Continuously replace one latest frame; never queue stale frames."""
        retry = max(1.0, float(self.config.get("camera_retry_seconds", 5)))
        was_online = False
        while not self._stop.is_set():
            cap = None
            try:
                import cv2

                backend = cv2.CAP_DSHOW if hasattr(cv2, "CAP_DSHOW") else cv2.CAP_ANY
                cap = cv2.VideoCapture(int(self.config.get("camera_index", 0)), backend)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(self.config.get("width", 1280)))
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self.config.get("height", 720)))
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if not cap.isOpened():
                    raise RuntimeError("camera could not be opened")
                self._mark_camera(True)
                if self._last_event_values.pop("camera_offline_emitted", False):
                    self._emit_event("vision_camera_online", {"summary": "Smart Room camera recovered"})
                was_online = True
                while not self._stop.is_set():
                    ok, frame = cap.read()
                    if not ok:
                        raise RuntimeError("camera frame read failed")
                    with self._latest_lock:
                        self._latest_frame = frame
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                self._status["last_error"] = message
                self._mark_camera(False, message)
                if was_online or not self._last_event_values.get("camera_offline_emitted"):
                    self._last_event_values["camera_offline_emitted"] = True
                    self._emit_event("vision_camera_offline", {"error": message, "summary": "Smart Room camera offline"})
                was_online = False
                self._stop.wait(retry)
            finally:
                if cap is not None:
                    cap.release()

    def _analysis_loop(self) -> None:
        try:
            import cv2
            from plugins.smart_room.runtime.vision.backends import MediaPipeBackend

            self._status["running"] = True
            try:
                self._mp = MediaPipeBackend(self.config)
            except Exception as exc:
                self._status["pose_gesture_error"] = str(exc)
                logger.warning("Vision pose/gesture backend unavailable: %s", exc)
            while not self._stop.is_set():
                if time.monotonic() < self._gesture_priority_until:
                    self._stop.wait(0.02)
                    continue
                frame = self._frame_copy()
                if frame is None:
                    self._stop.wait(0.1)
                    continue
                try:
                    started = time.perf_counter()
                    self._analyze(frame, cv2)
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    self._status["analysis_latency_ms"] = round(elapsed_ms, 1)
                    self._analysis_samples += 1
                    window = time.monotonic() - self._analysis_window_at
                    if window >= 2:
                        self._status["analysis_fps"] = round(self._analysis_samples / window, 2)
                        self._analysis_samples = 0
                        self._analysis_window_at = time.monotonic()
                    self._status.pop("inference_error", None)
                except Exception as exc:
                    # A malformed frame or one model failure must not take the
                    # camera offline; the next frame is an independent retry.
                    self._status["inference_error"] = str(exc)
                    logger.warning("Vision frame inference failed: %s", exc)
                idle_fps = float(self.config.get("standby_fps", 1.5))
                fps = float(self.config.get("active_fps", 12) if time.monotonic() < self._burst_until else idle_fps)
                self._wake.clear()
                cycle_seconds = float(self._status.get("analysis_latency_ms", 0)) / 1000
                self._wake.wait(max(0.005, 1.0 / max(fps, 0.2) - cycle_seconds))
        except Exception as exc:
            logger.exception("Vision analysis service stopped: %s", exc)
            self._status["last_error"] = str(exc)
        finally:
            self._status["running"] = False
            if self._mp is not None:
                self._mp.close()

    def _gesture_loop(self) -> None:
        """Highest-priority perception lane: hand inference and commands only."""
        while not self._stop.is_set() and self._mp is None:
            self._stop.wait(0.02)
        while not self._stop.is_set():
            started = time.perf_counter()
            frame = self._frame_copy()
            try:
                if frame is not None and self._mp is not None:
                    import cv2

                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    gestures = self._mp.recognize_gestures(rgb)
                    self._handle_gestures(gestures)
                    self._status["gesture_latency_ms"] = round(
                        (time.perf_counter() - started) * 1000, 1
                    )
                    self._status.pop("gesture_error", None)
            except Exception as exc:
                self._status["gesture_error"] = str(exc)
                logger.warning("Vision gesture inference failed: %s", exc)
            fps = max(1.0, float(self.config.get("gesture_scan_fps", 20)))
            elapsed = time.perf_counter() - started
            self._stop.wait(max(0.001, 1.0 / fps - elapsed))

    def _handle_gestures(self, gestures: list[Dict[str, Any]]) -> None:
        gesture_name = None
        if not gestures:
            self._gestures.update("", 0.0)
        else:
            # Once a command hand appears, suspend pose and face work so the
            # hold/command frames get exclusive CPU time.
            self._gesture_priority_until = time.monotonic() + 1.0
        for gesture in gestures:
            decision = self._gestures.update(gesture["name"], gesture["confidence"])
            gesture_name = decision.gesture
            if decision.command:
                self._gesture_callback(decision.command, decision.params)
                self._transition(
                    "vision_gesture",
                    f"{decision.command}:{time.monotonic_ns()}",
                    {"gesture": decision.gesture, "command": decision.command},
                )
        with self._lock:
            self._state.vision.active_gesture = gesture_name
            self._state.vision.gesture_armed_until = self._gestures.armed_until_iso

    def _face_loop(self) -> None:
        """Run expensive face detection independently from hand/pose latency."""
        try:
            from plugins.smart_room.runtime.vision.backends import InsightFaceBackend

            self._face_backend = InsightFaceBackend(self.config.get("faces") or {})
            while not self._stop.is_set():
                started = time.perf_counter()
                if time.monotonic() < self._gesture_priority_until:
                    self._stop.wait(0.05)
                    continue
                frame = self._frame_copy()
                visibility = "unavailable"
                with self._lock:
                    visibility = str(self._state.vision.visibility)
                if frame is not None and visibility != "dark":
                    height, width = frame.shape[:2]
                    faces = self._face_backend.analyze(frame, width, height)
                    stamp = now_iso()
                    with self._latest_lock:
                        self._cached_faces = faces
                        self._latest_face_samples = (stamp, faces)
                    self._status["face_latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
                    self._status.pop("face_error", None)
                elif visibility == "dark":
                    with self._latest_lock:
                        self._cached_faces = []
                interval = float(
                    self.config.get("active_face_interval_seconds", 0.35)
                    if time.monotonic() < self._burst_until
                    else self.config.get("face_interval_seconds", 1.0)
                )
                elapsed = time.perf_counter() - started
                self._stop.wait(max(0.05, interval - elapsed))
        except Exception as exc:
            self._status["face_error"] = str(exc)
            logger.exception("Vision face service stopped: %s", exc)

    def _analyze(self, frame: Any, cv2: Any) -> None:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        brightness = float(gray.mean())
        blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        dark_at = float(self.config.get("dark_brightness", 28))
        dim_at = float(self.config.get("dim_brightness", 55))
        visibility = "dark" if brightness < dark_at else "dim" if brightness < dim_at else "blurred" if blur < float(self.config.get("blur_threshold", 35)) else "good"
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        local = {"poses": self._mp.analyze_pose(rgb), "gestures": []} if self._mp is not None else {"poses": [], "gestures": []}
        height, width = frame.shape[:2]
        with self._latest_lock:
            detected_faces = list(self._cached_faces)
        people = []
        owner = None
        zones = self.config.get("zones") or {}
        for index, pose in enumerate(local["poses"]):
            person = dict(pose)
            person["zone"] = locate_zone(tuple(pose["center"]), zones)
            nearest = min(detected_faces, key=lambda face: abs(face["center"][0] - pose["center"][0]), default=None)
            if nearest and abs(nearest["center"][0] - pose["center"][0]) < 0.25:
                match = self._faces.match(nearest["embedding"])
                person.update({key: value for key, value in match.items() if key != "candidate"})
                if match["is_owner"]:
                    owner = person
                elif match["status"] in {"unknown", "ambiguous"} and time.monotonic() - self._last_unknown_evidence_at >= float(self.config.get("unknown_evidence_cooldown_seconds", 30)):
                    evidence_id = f"face-{uuid.uuid4().hex[:12]}"
                    evidence_path = self._history.save_frame(frame, evidence_id=evidence_id) or ""
                    self._faces.add_pending(evidence_id, nearest["embedding"], evidence_path)
                    person["review_event_id"] = evidence_id
                    self._last_unknown_evidence_at = time.monotonic()
            else:
                person.update({"identity": "unknown", "status": "unresolved", "is_owner": False})
            people.append(person)
        # A visible face without a full pose must still count as a person.
        if not people:
            for face in detected_faces:
                match = self._faces.match(face["embedding"])
                item = {**{k: v for k, v in match.items() if k != "candidate"}, "center": face["center"], "zone": locate_zone(tuple(face["center"]), zones), "posture": "unknown"}
                people.append(item)
                if match["is_owner"]:
                    owner = item
        sleep_state = self._sleep.update(owner_visible=owner is not None, owner_zone=str((owner or {}).get("zone", "unknown")), posture=str((owner or {}).get("posture", "unknown")), center=tuple(owner["center"]) if owner else None, mmwave_occupied=bool(self._state.mmwave.occupied))
        with self._lock:
            vision = self._state.vision
            vision.enabled = True
            vision.camera_online = True
            vision.camera_name = str(self.config.get("camera_name", "Smart Room camera"))
            vision.visibility, vision.brightness, vision.blur_score = visibility, round(brightness, 2), round(blur, 2)
            vision.person_count, vision.people = len(people), people
            vision.owner_visible = owner is not None
            vision.owner_zone = str((owner or {}).get("zone", "unknown"))
            vision.owner_activity = str((owner or {}).get("posture", "unknown"))
            vision.sleep_state = sleep_state
            vision.last_observed_at = now_iso()
            vision.last_error = None
            vision.model_versions = {"pose_gesture": "mediapipe-tasks" if self._mp else "unavailable", "face": "insightface" if self._face_backend else "unavailable"}
            now = time.monotonic()
            if now - self._last_persisted_at >= max(0.2, float(self.config.get("state_persist_seconds", 1.0))):
                save_state(self._state)
                self._last_persisted_at = now
        self._transition("vision_sleep_state", sleep_state, {"sleep_state": sleep_state})
        identity_state = "owner" if owner else "unknown_person" if people else "empty"
        self._transition("vision_identity_state", identity_state, {"identity_state": identity_state, "person_count": len(people), "visibility": visibility})
        self._wake.set()

    def _transition(self, event_type: str, value: Any, data: Dict[str, Any]) -> None:
        if self._last_event_values.get(event_type) == value:
            return
        self._last_event_values[event_type] = value
        record = {"id": uuid.uuid4().hex, "at": now_iso(), "type": event_type, **data}
        self._history.append(record)
        self._emit_event(event_type, {**data, "summary": event_type.replace("_", " ")})

    def _mark_camera(self, online: bool, error: Optional[str] = None) -> None:
        self._status["camera_online"] = online
        with self._lock:
            self._state.vision.enabled = bool(self.config.get("enabled", False))
            self._state.vision.camera_online = online
            self._state.vision.last_error = error
            save_state(self._state)

    def _frame_copy(self) -> Any:
        with self._latest_lock:
            return None if self._latest_frame is None else self._latest_frame.copy()

    def _enroll_current(self, name: str, *, owner: bool, samples: int, timeout: float) -> Dict[str, Any]:
        """Collect a short, consistency-checked live embedding session."""
        if not name.strip():
            raise ValueError("face name is required")
        self.request_burst(timeout)
        deadline = time.monotonic() + timeout
        accepted: list[list[float]] = []
        last_stamp = ""
        reference: Optional[list[float]] = None
        minimum_similarity = float((self.config.get("faces") or {}).get("enrollment_similarity", 0.55))
        while time.monotonic() < deadline and len(accepted) < samples:
            with self._latest_lock:
                stamp, faces = self._latest_face_samples
            if stamp and stamp != last_stamp:
                last_stamp = stamp
                if len(faces) != 1:
                    if len(faces) > 1:
                        raise ValueError("enrollment requires exactly one visible face")
                else:
                    embedding = list(faces[0].get("embedding") or [])
                    if embedding and (reference is None or cosine_similarity(reference, embedding) >= minimum_similarity):
                        reference = reference or embedding
                        accepted.append(embedding)
            time.sleep(0.05)
        if len(accepted) < samples:
            raise ValueError(f"captured {len(accepted)}/{samples} consistent face samples before timeout")
        return self._faces.enroll(name, accepted, owner=owner)

    def _deep_analyze(self, question: str) -> Dict[str, Any]:
        """On-demand whole-scene reasoning through the configured vision model."""
        deep_config = self.config.get("deep") or {}
        if not deep_config.get("enabled", True):
            return {"success": False, "error": "deep scene analysis is disabled"}
        frame = self._frame_copy()
        if frame is None:
            return {"success": False, "error": "no camera frame is available"}
        import cv2
        from agent.auxiliary_client import call_llm
        from agent.message_content import flatten_message_text

        height, width = frame.shape[:2]
        max_width = max(320, min(int(deep_config.get("max_width", 960)), 1600))
        if width > max_width:
            frame = cv2.resize(frame, (max_width, round(height * max_width / width)))
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(deep_config.get("jpeg_quality", 82))])
        if not ok:
            return {"success": False, "error": "failed to encode camera frame"}
        data_url = "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode("ascii")
        prompt = str(question or "What is each visible person doing, and is anything unusual or safety-relevant?")[:500]
        response = call_llm(
            task="smart_room_vision",
            provider=str(deep_config.get("provider") or "") or None,
            model=str(deep_config.get("model") or "") or None,
            messages=[
                {"role": "system", "content": (
                    "Analyze this private Smart Room frame conservatively. Return JSON only with keys: "
                    "summary (string), activities (array), objects (array), unusual (boolean), safety (string), confidence (0..1). "
                    "Do not identify a person from appearance; reviewed local face identity is handled separately. "
                    "Say unknown when pixels do not support a conclusion."
                )},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]},
            ],
            temperature=0,
            max_tokens=int(deep_config.get("max_tokens", 350)),
            timeout=float(deep_config.get("timeout", 30)),
        )
        text = flatten_message_text(response.choices[0].message.content).strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        try:
            analysis = json.loads(match.group(0) if match else text)
        except (json.JSONDecodeError, AttributeError):
            analysis = {"summary": text[:1000], "activities": [], "objects": [], "unusual": False, "safety": "unknown", "confidence": 0}
        analysis = analysis if isinstance(analysis, dict) else {"summary": str(analysis)}
        analysis["at"] = now_iso()
        with self._lock:
            self._state.vision.scene_analysis = analysis
            self._state.vision.last_deep_observed_at = analysis["at"]
            save_state(self._state)
        self._history.append({"type": "vision_deep_analysis", **analysis})
        return {"success": True, **analysis}
