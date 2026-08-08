"""Bounded visual-event history and evidence storage.

The history is intentionally separate from Marvi memory.  It is operational
evidence that the Smart Room cognition lane can inspect and the user can
review; the subconscious decides what, if anything, becomes durable memory.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
import uuid
from typing import Any, Dict, Iterable, Optional

from hermes_constants import get_hermes_home


class VisionHistory:
    def __init__(self, config: Dict[str, Any]):
        self._config = config
        self._lock = threading.Lock()
        self.root = Path(get_hermes_home()) / "smart_room" / "vision"
        self.evidence_dir = self.root / "evidence"
        self.events_path = self.root / "events.jsonl"
        self.root.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def new_id(prefix: str = "vision") -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return f"{prefix}_{stamp}_{uuid.uuid4().hex[:8]}"

    def append(self, event: Dict[str, Any]) -> Dict[str, Any]:
        record = dict(event)
        record.setdefault("id", self.new_id())
        record.setdefault("at", datetime.now(timezone.utc).isoformat())
        with self._lock:
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._trim_locked()
        return record

    def save_frame(self, frame: Any, *, evidence_id: Optional[str] = None) -> Optional[str]:
        if frame is None:
            return None
        evidence_id = evidence_id or self.new_id("evidence")
        target = self.evidence_dir / f"{evidence_id}.jpg"
        try:
            import cv2

            quality = int(self._config.get("jpeg_quality", 88))
            if not cv2.imwrite(str(target), frame, [cv2.IMWRITE_JPEG_QUALITY, quality]):
                return None
            return str(target)
        except Exception:
            return None

    def query(
        self,
        *,
        limit: int = 20,
        since: str = "",
        event_type: str = "",
        identity: str = "",
        zone: str = "",
    ) -> list[Dict[str, Any]]:
        if not self.events_path.exists():
            return []
        matches: deque[Dict[str, Any]] = deque(maxlen=max(1, min(int(limit), 200)))
        with self._lock, self.events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since and str(item.get("at") or "") < since:
                    continue
                if event_type and str(item.get("type") or "") != event_type:
                    continue
                if identity and identity not in self._identities(item):
                    continue
                if zone and zone not in self._zones(item):
                    continue
                matches.append(item)
        return list(matches)

    @staticmethod
    def _identities(item: Dict[str, Any]) -> set[str]:
        values = {str(item.get("identity") or "")}
        for person in item.get("people") or []:
            if isinstance(person, dict):
                values.add(str(person.get("identity") or ""))
        return values

    @staticmethod
    def _zones(item: Dict[str, Any]) -> set[str]:
        values = {str(item.get("zone") or "")}
        for person in item.get("people") or []:
            if isinstance(person, dict):
                values.add(str(person.get("zone") or ""))
        return values

    def prune(self) -> None:
        retention_hours = max(1, int(self._config.get("retention_hours", 72)))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=retention_hours)
        with self._lock:
            for path in self.evidence_dir.glob("*.jpg"):
                try:
                    changed = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                    if changed < cutoff:
                        path.unlink()
                except OSError:
                    pass
            self._trim_locked()

    def _trim_locked(self) -> None:
        if not self.events_path.exists():
            return
        max_events = max(100, int(self._config.get("max_events", 2000)))
        lines = self.events_path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= max_events:
            return
        tmp = self.events_path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(lines[-max_events:]) + "\n", encoding="utf-8")
        os.replace(tmp, self.events_path)
