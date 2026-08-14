"""Reviewed local face-embedding library for Smart Room."""

from __future__ import annotations

import json
import math
import os
import base64
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
        self._lock = threading.RLock()
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_mtime_ns = -1
        self._pending_path_cache: Dict[str, str] = {}
        self._match_index: Optional[tuple[Any, list[str]]] = None

    def load(self) -> Dict[str, Any]:
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            mtime_ns = -1
        if self._cache is not None and mtime_ns == self._cache_mtime_ns:
            return self._cache
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
        self._cache = data
        self._cache_mtime_ns = mtime_ns
        self._match_index = None
        return data

    def list_people(self) -> Dict[str, Any]:
        data = self.load()
        self._pending_path_cache = {
            str(event_id): str(entry.get("evidence_path") or "")
            for event_id, entry in (data.get("pending") or {}).items()
            if isinstance(entry, dict)
        }
        pending_items = []
        for event_id, entry in reversed(list((data.get("pending") or {}).items())):
            if not isinstance(entry, dict):
                continue
            match = self._match_data(data, entry.get("embedding") or [])
            candidate = match.get("candidate")
            score = float(match.get("score", -1))
            pending_items.append({
                "event_id": event_id,
                "evidence_path": str(entry.get("evidence_path") or ""),
                "preview_available": bool(entry.get("evidence_path")),
                "captured_at": str(entry.get("captured_at") or ""),
                "visibility": str(entry.get("visibility") or "unknown"),
                "candidate": candidate,
                "match_percent": round(max(0.0, score) * 100, 1),
                "match_status": match.get("status"),
                "match_label": self._match_label(candidate, score, str(match.get("status") or "unknown")),
            })
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
            "sampling_enabled": bool(self._config.get("sampling_enabled", True)),
            "sampling_full": len(data.get("pending") or {}) >= int(self._config.get("max_pending", 30)),
            "pending_items": pending_items,
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
            entry["embeddings"] = self._append_unique(entry.get("embeddings") or [], valid)
            entry["reviewed"] = True
            if owner or data.get("owner") is None:
                data["owner"] = name
            self._write(data)
        return self.list_people()

    def match(self, embedding: list[float]) -> Dict[str, Any]:
        with self._lock:
            data = self.load()
            return self._match_data(data, embedding)

    def _match_data(self, data: Dict[str, Any], embedding: list[float]) -> Dict[str, Any]:
        best_name = "unknown"
        best_score = -1.0
        try:
            import numpy as np

            if self._match_index is None:
                vectors = []
                names = []
                for name, entry in (data.get("people") or {}).items():
                    if not isinstance(entry, dict) or not entry.get("reviewed"):
                        continue
                    for enrolled in entry.get("embeddings") or []:
                        if enrolled:
                            vectors.append(enrolled)
                            names.append(str(name))
                if vectors:
                    matrix = np.asarray(vectors, dtype=np.float32)
                    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                    matrix = matrix / np.maximum(norms, 1e-12)
                else:
                    matrix = np.empty((0, 0), dtype=np.float32)
                self._match_index = (matrix, names)
            matrix, names = self._match_index
            query = np.asarray(embedding, dtype=np.float32)
            query_norm = float(np.linalg.norm(query))
            if names and query_norm > 0 and matrix.shape[1] == query.shape[0]:
                scores = matrix @ (query / query_norm)
                index = int(np.argmax(scores))
                best_name, best_score = names[index], float(scores[index])
        except (ImportError, TypeError, ValueError):
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

    @staticmethod
    def _match_label(candidate: Any, score: float, status: str) -> str:
        if not candidate or score < 0:
            return "No enrolled-face match"
        percent = round(score * 100)
        if status == "matched":
            return f"{candidate} · {percent}% match"
        if status == "ambiguous":
            return f"Possibly {candidate} · {percent}% match"
        return f"No reliable match · closest is {candidate} at {percent}%"

    def add_pending(
        self,
        event_id: str,
        embedding: list[float],
        evidence_path: str = "",
        *,
        captured_at: str = "",
        visibility: str = "unknown",
    ) -> bool:
        with self._lock:
            data = self.load()
            if not self._can_add_pending_data(data, embedding):
                return False
            data["pending"][event_id] = {
                "embedding": [float(value) for value in embedding],
                "evidence_path": evidence_path,
                "captured_at": captured_at,
                "visibility": visibility,
            }
            self._write(data)
        return True

    def should_add_pending(self, embedding: list[float]) -> bool:
        with self._lock:
            return self._can_add_pending_data(self.load(), embedding)

    def _can_add_pending_data(self, data: Dict[str, Any], embedding: list[float]) -> bool:
        if not self._config.get("sampling_enabled", True) or not embedding:
            return False
        pending = data.get("pending") or {}
        if len(pending) >= max(1, int(self._config.get("max_pending", 30))):
            return False
        duplicate_at = float(self._config.get("pending_similarity_threshold", 0.72))
        return not any(
            cosine_similarity(embedding, item.get("embedding") or []) >= duplicate_at
            for item in pending.values()
            if isinstance(item, dict)
        )

    def review(self, event_id: str, *, name: str = "", reject: bool = False, owner: bool = False) -> Dict[str, Any]:
        clean_name = str(name or "").strip()
        if not reject and not clean_name:
            raise ValueError("name is required when accepting a face")
        with self._lock:
            data = self.load()
            pending = data["pending"].pop(event_id, None)
            if pending is None:
                raise ValueError(f"unknown pending face event: {event_id}")
            if not reject:
                entry = data["people"].setdefault(clean_name, {"embeddings": [], "reviewed": True})
                entry["embeddings"] = self._append_unique(entry.get("embeddings") or [], [pending["embedding"]])
                entry["reviewed"] = True
                if owner or data.get("owner") is None:
                    data["owner"] = clean_name
            self._write(data)
        return self.list_people()

    def review_all(self, *, name: str = "", reject: bool = False, owner: bool = False) -> Dict[str, Any]:
        if not reject and not str(name or "").strip():
            raise ValueError("name is required when accepting faces")
        with self._lock:
            data = self.load()
            pending = list((data.get("pending") or {}).values())
            if not reject:
                clean_name = str(name).strip()
                entry = data["people"].setdefault(clean_name, {"embeddings": [], "reviewed": True})
                samples = [item.get("embedding") or [] for item in pending if isinstance(item, dict)]
                entry["embeddings"] = self._append_unique(entry.get("embeddings") or [], samples)
                entry["reviewed"] = True
                if owner or data.get("owner") is None:
                    data["owner"] = clean_name
            data["pending"] = {}
            self._write(data)
        return self.list_people()

    def pending_preview(self, event_id: str) -> Dict[str, Any]:
        cached_path = self._pending_path_cache.get(event_id)
        if cached_path is None:
            data = self.load()
            entry = (data.get("pending") or {}).get(event_id)
            if not isinstance(entry, dict):
                raise ValueError(f"unknown pending face event: {event_id}")
            cached_path = str(entry.get("evidence_path") or "")
            self._pending_path_cache[event_id] = cached_path
        path = Path(cached_path)
        if not path.is_file():
            return {"available": False, "error": "face preview is unavailable"}
        suffix = path.suffix.lower()
        mime = "image/png" if suffix == ".png" else "image/jpeg"
        return {
            "available": True,
            "image": f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii"),
        }

    def set_sampling(self, enabled: bool) -> Dict[str, Any]:
        self._config["sampling_enabled"] = bool(enabled)
        return self.list_people()

    def delete(self, name: str) -> Dict[str, Any]:
        with self._lock:
            data = self.load()
            data["people"].pop(name, None)
            if data.get("owner") == name:
                data["owner"] = None
            self._write(data)
        return self.list_people()

    def _append_unique(self, existing: list[list[float]], samples: list[list[float]]) -> list[list[float]]:
        result = [[float(value) for value in sample] for sample in existing if sample]
        duplicate_at = float(self._config.get("learning_similarity_threshold", 0.985))
        for sample in samples:
            valid = [float(value) for value in sample]
            if valid and not any(cosine_similarity(valid, old) >= duplicate_at for old in result):
                result.append(valid)
        return result[-max(3, int(self._config.get("max_samples_per_person", 100))):]

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
            self._cache = data
            self._match_index = None
            try:
                self._cache_mtime_ns = self.path.stat().st_mtime_ns
            except OSError:
                self._cache_mtime_ns = -1
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
