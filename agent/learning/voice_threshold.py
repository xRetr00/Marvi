"""Conservative speaker-ID threshold analysis from structured voice logs."""

from __future__ import annotations

import math
import re
from statistics import median
from typing import Any, Dict, Iterable, Optional

_FIELD = re.compile(r"([a-z_]+)=([^\s]+)")


def analyze(lines: Iterable[str]) -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "samples": 0,
        "direct_owner_scores": [],
        "continuity_owner_scores": [],
        "known_other_scores": [],
        "abstain": 0,
        "competing_drops": 0,
    }
    for line in lines:
        if "[VOICE-ID]" not in line or "barge" in line.lower():
            continue
        fields = {key: value for key, value in _FIELD.findall(line)}
        if fields.get("context") != "utterance":
            continue
        try:
            score = float(fields.get("score", "nan"))
        except ValueError:
            continue
        if not math.isfinite(score):
            continue
        stats["samples"] += 1
        zone = fields.get("zone", "").upper()
        label = fields.get("label", "").lower()
        resolved = fields.get("resolved_by", "").lower()
        ignored = fields.get("ignored", "").lower()
        if zone == "OWNER" and label == "owner" and resolved == "score":
            stats["direct_owner_scores"].append(score)
        elif zone == "ABSTAIN" and label == "owner" and resolved == "continuity":
            stats["continuity_owner_scores"].append(score)
        elif label in {"other", "guest"} or (label == "unknown" and zone == "CONFIDENT_OTHER"):
            stats["known_other_scores"].append(score)
        if zone == "ABSTAIN":
            stats["abstain"] += 1
        if resolved == "competing_drop" or ignored in {"competing_speaker", "competing", "speaker_competing"}:
            stats["competing_drops"] += 1
    stats["owner_abstain_missed"] = len(stats["continuity_owner_scores"])
    stats["abstain_rate"] = stats["abstain"] / stats["samples"] if stats["samples"] else 0.0
    return stats


def _round_step(value: float) -> float:
    return round(round(value / .01) * .01, 2)


def propose_threshold(stats: Dict[str, Any], current: Dict[str, float], *, minimum_samples: int = 200) -> Optional[Dict[str, Any]]:
    """Propose at most one threshold change when owner/other evidence separates."""
    if int(stats.get("samples", 0)) < minimum_samples:
        return None
    owner = float(current.get("threshold", .45))
    reject = float(current.get("reject_threshold", .25))
    continuity = sorted(float(x) for x in stats.get("continuity_owner_scores") or [])
    direct = sorted(float(x) for x in stats.get("direct_owner_scores") or [])
    others = sorted(float(x) for x in stats.get("known_other_scores") or [])

    # Require a meaningful owner sample and a clean margin above the rejection
    # boundary before lowering. The median avoids chasing one noisy utterance.
    if len(continuity) >= 20:
        candidate = max(.30, min(.80, _round_step(max(reject + .05, median(continuity)))))
        if candidate <= owner - .03 and (not others or max(others) < candidate - .03):
            return {
                "path": "voice.speaker_id.threshold",
                "value": candidate,
                "current": owner,
                "rationale": f"{len(continuity)} owner utterances needed continuity below the current threshold.",
            }

    # Raising the reject boundary is only safe with labelled-other evidence and
    # no direct-owner score in the affected interval.
    if len(others) >= 20 and len(direct) >= 20:
        candidate = max(.05, min(.40, _round_step(min(owner - .05, median(others)))))
        if candidate >= reject + .03 and min(direct) > candidate + .03:
            return {
                "path": "voice.speaker_id.reject_threshold",
                "value": candidate,
                "current": reject,
                "rationale": f"{len(others)} labelled non-owner utterances cluster above the current rejection boundary.",
            }
    return None
