"""Read-only learning status assembled for the Desktop Mind panel."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from cron.suggestions import list_pending, load_suggestions
from hermes_cli.config import cfg_get, load_config

from .config_registry import current_value
from .outcomes import recent

LOOP_CONFIG_PATHS = {
    "trust": "learning.trust.enabled",
    "room_habit": "learning.room.enabled",
    "voice_threshold": "learning.voice_tuning.enabled",
    "focus_apps": "learning.focus_apps.enabled",
    "escalation": "learning.escalation.enabled",
    "timing": "learning.timing.enabled",
}


def _enabled(cfg: Dict[str, Any], path: str) -> bool:
    parts = path.split(".")
    return bool(cfg_get(cfg, *parts, default=False if parts[1] == "timing" else True))


def _sample_count(loop: str) -> int:
    if loop == "trust":
        return sum(
            row.get("event") in {"accepted", "dismissed"}
            for row in recent(days=30, limit=2_000)
        )
    if loop == "room_habit":
        try:
            from .room_habit import load_state

            return len(load_state().get("observations") or [])
        except Exception:
            return 0
    if loop == "voice_threshold":
        try:
            from .reflection import _voice_lines
            from .voice_threshold import analyze

            return int(analyze(_voice_lines()).get("samples", 0))
        except Exception:
            return 0
    if loop == "escalation":
        return sum(row.get("event") == "corrected" for row in recent(loop=loop))
    if loop == "timing":
        return sum(row.get("event") == "delivered" for row in recent(loop=loop))
    if loop == "focus_apps":
        try:
            from tools.presence.aw_client import aw_client

            bucket = aw_client.find_bucket_id("aw-watcher-window")
            if not bucket:
                return 0
            start = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
            return len(aw_client.get_events(bucket, start=start, limit=20_000))
        except Exception:
            return 0
    return len(recent(loop=loop))


def build_summary() -> Dict[str, Any]:
    cfg = load_config()
    all_suggestions = load_suggestions()
    pending = list_pending()
    loops = []
    for loop, config_path in LOOP_CONFIG_PATHS.items():
        matching = [row for row in all_suggestions if row.get("loop") == loop]
        matching.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        loops.append({
            "loop": loop,
            "config_path": config_path,
            "enabled": _enabled(cfg, config_path),
            "samples": _sample_count(loop),
            "last_proposal": matching[0].get("title") if matching else None,
            "pending": sum(row.get("loop") == loop for row in pending),
        })

    learned_tiers = []
    seen = set()
    for row in recent(loop="trust"):
        if row.get("event") != "accepted":
            continue
        detail = row.get("detail") or {}
        path, value = detail.get("path"), detail.get("value")
        if not isinstance(path, str) or not path.startswith("subconscious.tiers.") or path in seen:
            continue
        seen.add(path)
        try:
            if current_value(path, cfg) == value:
                learned_tiers.append(path.rsplit(".", 1)[-1])
        except Exception:
            continue
    return {"loops": loops, "learned_tiers": learned_tiers}
