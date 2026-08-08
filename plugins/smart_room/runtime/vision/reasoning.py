"""Pure temporal reasoning for zones, gestures, and sleep state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import time
from typing import Any, Dict, Optional


def point_in_polygon(x: float, y: float, polygon: list[list[float]]) -> bool:
    inside = False
    if len(polygon) < 3:
        return False
    j = len(polygon) - 1
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
            inside = not inside
        j = i
    return inside


def locate_zone(point: tuple[float, float], zones: Dict[str, Any]) -> str:
    x, y = point
    for name, polygon in zones.items():
        if isinstance(polygon, list) and point_in_polygon(x, y, polygon):
            return str(name)
    return "room"


class SleepTracker:
    """Conservative temporal sleep state machine."""

    def __init__(self, config: Dict[str, Any]):
        self._config = config
        self.state = "unknown"
        self._bed_since = 0.0
        self._still_since = 0.0
        self._last_center: Optional[tuple[float, float]] = None

    def update(
        self,
        *,
        owner_visible: bool,
        owner_zone: str,
        posture: str,
        center: Optional[tuple[float, float]],
        mmwave_occupied: bool,
        now_monotonic: Optional[float] = None,
    ) -> str:
        now = now_monotonic if now_monotonic is not None else time.monotonic()
        in_bed = owner_zone == "bed" and posture in {"lying", "reclined"}
        if owner_visible and in_bed:
            if not self._bed_since:
                self._bed_since = now
            movement = 1.0
            if center is not None and self._last_center is not None:
                movement = math.dist(center, self._last_center)
            if movement <= float(self._config.get("stillness_distance", 0.025)):
                if not self._still_since:
                    self._still_since = now
            else:
                self._still_since = now
            self._last_center = center
            settling = float(self._config.get("settling_seconds", 120))
            likely = float(self._config.get("likely_sleeping_seconds", 600))
            still_for = now - (self._still_since or now)
            bed_for = now - self._bed_since
            if bed_for >= likely and still_for >= likely:
                self.state = "likely_sleeping"
            elif bed_for >= settling:
                self.state = "settling"
            else:
                self.state = "in_bed_awake"
        elif not owner_visible and self.state in {"settling", "likely_sleeping", "sleeping"} and mmwave_occupied:
            # Blanket or darkness may hide the owner; preserve the last strong
            # state instead of interpreting failed vision as absence.
            if self.state == "likely_sleeping":
                self.state = "sleeping"
        elif owner_visible:
            self.state = "awake"
            self._bed_since = self._still_since = 0.0
            self._last_center = center
        elif not mmwave_occupied:
            self.state = "unknown"
            self._bed_since = self._still_since = 0.0
            self._last_center = None
        return self.state


@dataclass
class GestureDecision:
    gesture: str
    command: Optional[str] = None
    params: Dict[str, Any] = None
    armed: bool = False

    def __post_init__(self) -> None:
        if self.params is None:
            self.params = {}


class GestureController:
    """Hold/debounce/arming logic over MediaPipe gesture labels."""

    def __init__(self, config: Dict[str, Any]):
        self._config = config
        self._candidate = ""
        self._candidate_since = 0.0
        self._armed_until = 0.0
        self._last_fired_at = 0.0
        self._last_fired_gesture = ""

    @property
    def armed_until_iso(self) -> Optional[str]:
        remaining = self._armed_until - time.monotonic()
        if remaining <= 0:
            return None
        return datetime.fromtimestamp(time.time() + remaining, timezone.utc).isoformat()

    def update(self, gesture: str, confidence: float, *, now_monotonic: Optional[float] = None) -> GestureDecision:
        now = now_monotonic if now_monotonic is not None else time.monotonic()
        gesture = str(gesture or "").strip()
        if not gesture or confidence < float(self._config.get("confidence", 0.65)):
            self._candidate = ""
            self._candidate_since = 0.0
            return GestureDecision(gesture=gesture, armed=now < self._armed_until)
        if gesture != self._candidate:
            self._candidate = gesture
            self._candidate_since = now
            return GestureDecision(gesture=gesture, armed=now < self._armed_until)
        hold = float(self._config.get("hold_seconds", 0.65))
        if now - self._candidate_since < hold:
            return GestureDecision(gesture=gesture, armed=now < self._armed_until)
        cooldown = float(self._config.get("cooldown_seconds", 1.5))
        if gesture == self._last_fired_gesture and now - self._last_fired_at < cooldown:
            return GestureDecision(gesture=gesture, armed=now < self._armed_until)

        wake = str(self._config.get("wake_gesture", "Open_Palm"))
        if gesture == wake:
            self._armed_until = now + float(self._config.get("armed_seconds", 8.0))
            command = "gesture_armed"
        elif now >= self._armed_until:
            return GestureDecision(gesture=gesture, armed=False)
        else:
            mapping = self._config.get("mapping") or {
                "Thumb_Up": {"command": "brightness_up", "step": 15},
                "Thumb_Down": {"command": "brightness_down", "step": 15},
                "Closed_Fist": {"command": "cancel"},
                "Victory": {"command": "voice_mode"},
                "Pointing_Up": {"command": "toggle_light"},
                "ILoveYou": {"command": "set_mode", "mode": "relax"},
            }
            item = mapping.get(gesture) if isinstance(mapping, dict) else None
            if not isinstance(item, dict):
                return GestureDecision(gesture=gesture, armed=True)
            command = str(item.get("command") or "")
            params = {key: value for key, value in item.items() if key != "command"}
            self._last_fired_at = now
            self._last_fired_gesture = gesture
            return GestureDecision(gesture=gesture, command=command, params=params, armed=True)

        self._last_fired_at = now
        self._last_fired_gesture = gesture
        return GestureDecision(gesture=gesture, command=command, armed=True)
