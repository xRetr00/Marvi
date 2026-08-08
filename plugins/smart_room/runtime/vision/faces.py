"""Reviewed local face-embedding library for Smart Room."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import stat
import tempfile
import threading
from typing import Any, Dict, Iterable, Optional

from hermes_constants import get_hermes_home


def cosine_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    a = [float(value) for value in left]
    b = [float(value) for value in right]
    if not a or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return dot / norm if norm else -1.0


class FaceLibrary:
    """Atomic embedding store; raw face crops are optional and separate."""

    def __init__(self, config: Dict[str, Any]):
        self._config = config
        self.path = Path(get_hermes_home()) / "smart_room" / "vision" / "faces.json"
        self._lock = threading.Lock()

    def load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("version", 1)
        data.setdefault("owner", None)
        data.setdefault("people", {})
        data.setdefault("pending", {})
        return data

    def list_people(self) -> Dict[str, Any]:
        data = self.load()
        return {
            "owner": data.get("owner"),
            "people": {
                name: {
                    "samples": len(entry.get("embeddings") or []),
                    "reviewed": bool(entry.get("reviewed", False)),
                }
                for name, entry in (data.get("people") or {}).items()
                if isinstance(entry, dict)
            },
            "pending": len(data.get("pending") or {}),
        }

    def enroll(self, name: str, embeddings: list[list[float]], *, owner: bool = False) -> Dict[str, Any]:
        name = str(name or "").strip()
        valid = [[float(value) for value in sample] for sample in embeddings if sample]
        if not name:
            raise ValueError("face name is required")
        minimum = max(3, int(self._config.get("min_enrollment_samples", 8)))
        if len(valid) < minimum:
            raise ValueError(f"at least {minimum} accepted face samples are required")
        with self._lock:
            data = self.load()
            entry = data["people"].setdefault(name, {"embeddings": [], "reviewed": True})
            entry["embeddings"] = (entry.get("embeddings") or []) + valid
            entry["embeddings"] = entry["embeddings"][-100:]
            entry["reviewed"] = True
            if owner or data.get("owner") is None:
                data["owner"] = name
            self._write(data)
        return self.list_people()

    def match(self, embedding: list[float]) -> Dict[str, Any]:
        data = self.load()
        best_name = "unknown"
        best_score = -1.0
        for name, entry in (data.get("people") or {}).items():
            if not isinstance(entry, dict) or not entry.get("reviewed"):
                continue
            for enrolled in entry.get("embeddings") or []:
                score = cosine_similarity(embedding, enrolled)
                if score > best_score:
                    best_name, best_score = str(name), score
        threshold = float(self._config.get("match_threshold", 0.42))
        ambiguity = float(self._config.get("ambiguity_margin", 0.04))
        if best_score < threshold:
            identity = "unknown"
            status = "unknown"
        elif best_score < threshold + ambiguity:
            identity = "ambiguous"
            status = "ambiguous"
        else:
            identity = best_name
            status = "matched"
        return {
            "identity": identity,
            "candidate": best_name if best_score >= 0 else None,
            "score": round(best_score, 4),
            "status": status,
            "is_owner": identity == data.get("owner"),
        }

    def add_pending(self, event_id: str, embedding: list[float], evidence_path: str = "") -> None:
        with self._lock:
            data = self.load()
            data["pending"][event_id] = {
                "embedding": [float(value) for value in embedding],
                "evidence_path": evidence_path,
            }
            items = list(data["pending"].items())[-200:]
            data["pending"] = dict(items)
            self._write(data)

    def review(self, event_id: str, *, name: str = "", reject: bool = False, owner: bool = False) -> Dict[str, Any]:
        with self._lock:
            data = self.load()
            pending = data["pending"].pop(event_id, None)
            if pending is None:
                raise ValueError(f"unknown pending face event: {event_id}")
            if not reject:
                name = str(name or "").strip()
                if not name:
                    raise ValueError("name is required when accepting a face")
                entry = data["people"].setdefault(name, {"embeddings": [], "reviewed": True})
                entry["embeddings"] = (entry.get("embeddings") or []) + [pending["embedding"]]
                entry["embeddings"] = entry["embeddings"][-100:]
                entry["reviewed"] = True
                if owner or data.get("owner") is None:
                    data["owner"] = name
            self._write(data)
        return self.list_people()

    def delete(self, name: str) -> Dict[str, Any]:
        with self._lock:
            data = self.load()
            data["people"].pop(name, None)
            if data.get("owner") == name:
                data["owner"] = None
            self._write(data)
        return self.list_people()

    def _write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".faces-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
