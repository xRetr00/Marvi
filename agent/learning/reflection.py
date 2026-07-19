"""One deterministic runner invoked by the existing reflection job."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from cron.suggestions import add_suggestion, get_tiers_config
from hermes_constants import get_hermes_home
from hermes_cli.config import cfg_get, load_config

from .config_registry import current_value
from .escalation import write_hints
from .focus_apps import derive as derive_focus_apps
from .outcomes import recent, record
from .room_habit import accumulate as accumulate_room, load_state as load_room_state, propose as propose_room, save_state as save_room_state
from .timing import propose_windows
from .trust import proposals as trust_proposals
from .voice_threshold import analyze as analyze_voice, propose_threshold

logger = logging.getLogger(__name__)


def _enabled(cfg: Dict[str, Any], loop: str, default: bool = True) -> bool:
    return bool(cfg_get(cfg, "learning", loop, "enabled", default=default))


def episodes_for_prompt(cfg: Optional[Dict[str, Any]] = None, *, limit: int = 15) -> str:
    """Compact "recent episodes" block for the nightly reflection prompt
    (memory-maturity spec §1.4) so reflection reasons over the day's actual
    events, not just diffs. Filters to episodes at/above
    ``memory.episodic.min_importance_for_prompt`` (default 0.4). Fetches a
    wider recent window first so importance filtering doesn't starve the
    block when the newest few episodes happen to be low-importance. Never
    raises — reflection input assembly must never break the reflection job.
    """
    try:
        from agent.memory.episodic import episodic_config, format_episode, query

        cfg = cfg if cfg is not None else load_config()
        econfig = episodic_config(cfg)
        if not econfig["enabled"]:
            return "Episodic memory disabled."
        min_importance = float(econfig["min_importance_for_prompt"])
        episodes = query(limit=max(int(limit) * 4, 50))
        filtered = [ep for ep in episodes if float(ep.get("importance") or 0.0) >= min_importance][: int(limit)]
        if not filtered:
            return "No meaningful recent episodes."
        return "\n".join(format_episode(ep) for ep in filtered)
    except Exception:  # noqa: BLE001 - reflection input must never break the job
        logger.debug("reflection: episodes_for_prompt failed", exc_info=True)
        return "Recent episodes unavailable."


def _weekly(cfg: Dict[str, Any], loop: str, now: datetime) -> bool:
    return now.astimezone().weekday() == int(cfg_get(cfg, "learning", loop, "review_weekday", default=6))


def _config_suggestion(*, loop: str, category: str, title: str, spec: Dict[str, Any], topic: str) -> bool:
    digest = hashlib.sha256(f"{spec['path']}:{topic}".encode()).hexdigest()[:16]
    return add_suggestion(
        title=title,
        description=str(spec["rationale"]),
        source="subconscious",
        kind="config",
        config_spec={**spec, "scope": "user"},
        loop=loop,
        category=category,
        dedup_key=f"learning:{loop}:{digest}",
    ) is not None


def _run_trust(cfg: Dict[str, Any]) -> int:
    # Trust is category-wide: feedback from a room/focus/voice proposal is
    # still evidence about that category's earned autonomy.
    events = recent(days=30, limit=2_000)
    values = trust_proposals(
        events,
        get_tiers_config(),
        never_auto=cfg_get(cfg, "learning", "trust", "never_auto", default=["goal"]) or ["goal"],
        promotion_accepts=int(cfg_get(cfg, "learning", "trust", "promote_after", default=8)),
        demotion_dismissals=int(cfg_get(cfg, "learning", "trust", "demote_after", default=3)),
    )
    made = 0
    for proposal in values:
        path = f"subconscious.tiers.{proposal['category']}"
        direction = proposal["direction"]
        title = (f"Let Marvi handle {proposal['category']} automatically?" if direction == "promote"
                 else f"Make {proposal['category']} suggestions quieter?")
        rationale = (f"You've consistently accepted recent {proposal['category']} suggestions."
                     if direction == "promote" else f"You've dismissed several recent {proposal['category']} suggestions.")
        made += _config_suggestion(
            loop="trust", category=proposal["category"], title=title,
            spec={"path": path, "value": proposal["value"], "current": current_value(path), "rationale": rationale},
            topic=direction,
        )
    return made


def _run_room(cfg: Dict[str, Any]) -> tuple[int, int]:
    try:
        from plugins.smart_room.runtime.state_store import load_transition_events

        state = load_room_state()
        state = accumulate_room(load_transition_events(after_id=int(state.get("last_event_id") or 0)), state)
        save_room_state(state)
    except Exception:  # noqa: BLE001 - optional plugin
        logger.debug("Room habit input unavailable", exc_info=True)
        return 0, 0
    made = 0
    for proposal in propose_room(state, minimum_occurrences=int(cfg_get(cfg, "learning", "room", "habit_min_occurrences", default=4))):
        made += add_suggestion(
            title=proposal["title"], description=proposal["description"], source="subconscious",
            kind="job", job_spec=proposal["job_spec"], loop="room_habit", category="smart_room",
            dedup_key=proposal["dedup_key"],
        ) is not None
    return made, len(state.get("observations") or [])


def _run_focus(cfg: Dict[str, Any]) -> tuple[int, int]:
    try:
        from tools.presence.aw_client import aw_client

        bucket = aw_client.find_bucket_id("aw-watcher-window")
        if not bucket:
            return 0, 0
        start = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        events = aw_client.get_events(bucket, start=start, limit=20_000)
    except Exception:  # noqa: BLE001 - local collector is optional
        logger.debug("Focus-app input unavailable", exc_info=True)
        return 0, 0
    existing = current_value("presence.heavy_apps") or []
    proposal = derive_focus_apps(
        events, existing,
        minimum_minutes=int(cfg_get(cfg, "learning", "focus_apps", "min_session_minutes", default=25)),
        minimum_occurrences=int(cfg_get(cfg, "learning", "focus_apps", "min_occurrences", default=5)),
    )
    if not proposal:
        return 0, len(events)
    made = _config_suggestion(
        loop="focus_apps", category="presence", title="Protect focus time in these applications?",
        spec=proposal, topic="add:" + ",".join(str(item).casefold() for item in proposal["value"] if item not in existing),
    )
    return int(made), len(events)


def _voice_lines() -> list[str]:
    log_dir = get_hermes_home().resolve() / "logs"
    lines: list[str] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    for path in [log_dir / "agent.log", *(log_dir / f"agent.log.{index}" for index in range(1, 5))]:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "[VOICE-ID]" not in line:
                    continue
                # Standard agent.log timestamps begin YYYY-MM-DD HH:MM:SS.
                # If a custom formatter omits one, rotation is still a bounded
                # fallback and we keep the structured sample.
                try:
                    stamp = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    if stamp < cutoff:
                        continue
                except ValueError:
                    pass
                lines.append(line)
        except OSError:
            continue
    return lines


def _run_voice(cfg: Dict[str, Any]) -> tuple[int, int]:
    stats = analyze_voice(_voice_lines())
    proposal = propose_threshold(
        stats,
        {"threshold": float(current_value("voice.speaker_id.threshold")),
         "reject_threshold": float(current_value("voice.speaker_id.reject_threshold"))},
        minimum_samples=int(cfg_get(cfg, "learning", "voice_tuning", "min_samples", default=200)),
    )
    if not proposal:
        return 0, int(stats["samples"])
    made = _config_suggestion(
        loop="voice_threshold", category="voice", title="Tune Marvi's speaker recognition threshold?",
        spec=proposal, topic=proposal["path"].rsplit(".", 1)[-1],
    )
    return int(made), int(stats["samples"])


def run_reflection(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Run enabled loops and return a compact prompt/debug summary. Never raises."""
    now = now or datetime.now(timezone.utc)
    summary: Dict[str, Any] = {"proposals": 0, "samples": {}}
    logger.info("learning reflection started")
    try:
        cfg = load_config()
        if _enabled(cfg, "room"):
            made, samples = _run_room(cfg)
            summary["proposals"] += made
            summary["samples"]["room_habit"] = samples
        if _enabled(cfg, "focus_apps"):
            made, samples = _run_focus(cfg)
            summary["proposals"] += made
            summary["samples"]["focus_apps"] = samples
        if _enabled(cfg, "trust") and _weekly(cfg, "trust", now):
            summary["proposals"] += _run_trust(cfg)
            summary["samples"]["trust"] = sum(
                row.get("event") in {"accepted", "dismissed"}
                for row in recent(days=30, limit=2_000)
            )
        if _enabled(cfg, "voice_tuning") and _weekly(cfg, "voice_tuning", now):
            made, samples = _run_voice(cfg)
            summary["proposals"] += made
            summary["samples"]["voice_threshold"] = samples
        if _enabled(cfg, "escalation") and _weekly(cfg, "trust", now):
            events = recent(loop="escalation", days=30)
            write_hints(
                events,
                maximum_examples=int(cfg_get(cfg, "learning", "escalation", "max_examples", default=5)),
            )
            summary["samples"]["escalation"] = sum(row.get("event") == "corrected" for row in events)
        if _enabled(cfg, "timing", default=False) and _weekly(cfg, "trust", now):
            from .timing import mark_ignored

            mark_ignored(
                window_minutes=int(cfg_get(cfg, "learning", "timing", "engagement_window_minutes", default=60)),
                now=now,
            )
            events = recent(loop="timing", days=30)
            proposal = propose_windows(events, minimum_deliveries=int(cfg_get(cfg, "learning", "timing", "min_samples", default=100)))
            if proposal:
                proposal["current"] = current_value(proposal["path"])
                summary["proposals"] += _config_suggestion(
                    loop="timing", category="timing", title="Quiet proactive delivery during low-engagement hours?",
                    spec=proposal, topic="quiet-hours",
                )
            summary["samples"]["timing"] = sum(row.get("event") == "delivered" for row in events)
    except Exception:  # noqa: BLE001 - reflection learning must never block reflection
        logger.warning("Learning reflection failed", exc_info=True)
        summary["error"] = "learning review unavailable"
    logger.info(
        "learning reflection completed proposals=%d samples=%s error=%s",
        summary["proposals"],
        summary["samples"],
        "error" in summary,
    )
    return summary
