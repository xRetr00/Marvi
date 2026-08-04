"""Local, human-labelled clap samples for future detector improvement."""

from __future__ import annotations

from array import array
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import threading
import uuid
import wave
from typing import Any, Iterable

from hermes_constants import get_hermes_home

TARGET_CONFIRMED_CLAPS = 200


class ClapDataset:
    """Persist model-accepted audio and explicit Yes/No labels locally."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(get_hermes_home()) / "smart_room" / "clap_dataset"
        self.samples_dir = self.root / "samples"
        self.index_path = self.root / "index.json"
        self._lock = threading.RLock()

    def record(
        self,
        waveform: Iterable[float],
        *,
        score: float,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sample_id = uuid.uuid4().hex
        captured_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        filename = f"{sample_id}.wav"

        with self._lock:
            index = self._load()
            confirmed = sum(item.get("label") == "clap" for item in index["samples"])
            pending = sum(item.get("label") == "pending" for item in index["samples"])
            if confirmed >= TARGET_CONFIRMED_CLAPS:
                return {"skipped": "target_complete"}
            if pending >= TARGET_CONFIRMED_CLAPS:
                return {"skipped": "review_queue_full"}

            self.samples_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.samples_dir / f".{filename}.tmp"
            final = self.samples_dir / filename
            pcm = array(
                "h",
                (
                    int(max(-1.0, min(1.0, float(sample))) * (32767 if sample >= 0 else 32768))
                    for sample in waveform
                ),
            )
            if sys.byteorder != "little":
                pcm.byteswap()
            with wave.open(str(temporary), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16_000)
                output.writeframes(pcm.tobytes())
            os.replace(temporary, final)

            sample = {
                "id": sample_id,
                "captured_at": captured_at,
                "score": round(float(score), 5),
                "label": "pending",
                "file": f"samples/{filename}",
                "metadata": metadata or {},
            }
            index["samples"].append(sample)
            self._save(index)
            return dict(sample)

    def review(self, sample_id: str, confirmed: bool) -> dict[str, Any]:
        with self._lock:
            index = self._load()
            sample = next(
                (item for item in index["samples"] if item.get("id") == sample_id),
                None,
            )
            if sample is None:
                raise KeyError("clap sample not found")
            if sample.get("label") == "pending":
                sample["label"] = "clap" if confirmed else "not_clap"
                sample["reviewed_at"] = datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                )
                self._save(index)
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            samples = self._load()["samples"]
            confirmed = sum(item.get("label") == "clap" for item in samples)
            rejected = sum(item.get("label") == "not_clap" for item in samples)
            pending = [item for item in samples if item.get("label") == "pending"]
            next_pending = None
            if pending:
                # Ask about the clap the user can still remember, not the
                # oldest item in a historical backlog.
                item = pending[-1]
                next_pending = {
                    "id": item["id"],
                    "captured_at": item["captured_at"],
                    "score": item.get("score"),
                }
            return {
                "target": TARGET_CONFIRMED_CLAPS,
                "confirmed": confirmed,
                "rejected": rejected,
                "pending": len(pending),
                "remaining": max(0, TARGET_CONFIRMED_CLAPS - confirmed),
                "complete": confirmed >= TARGET_CONFIRMED_CLAPS,
                "next_pending": next_pending,
            }

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("samples"), list):
                return value
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return {"version": 1, "samples": []}

    def _save(self, value: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
        os.replace(temporary, self.index_path)
