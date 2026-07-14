"""Subconscious tick — Marvi's periodic world-diff + goal-aware reasoning pass.

Workstream A's slice of the "subconscious" concept from the
2026-07-09-marvi-subconscious-presence design spec. Owns exactly ONE
built-in cron job (``cron.jobs.create_job`` — NO second job engine) that:

  1. runs a mechanical pre-script (Contract 1:
     ``cron/scripts/subconscious_snapshot.py``, owned by Workstream C) that
     prints the literal line ``NO_CHANGE`` or a human-readable diff. When
     the script prints ``NO_CHANGE``, ``cron.scheduler._parse_wake_gate``
     short-circuits the tick BEFORE the LLM stage — zero LLM cost when
     nothing in the user's world changed.
  2. otherwise runs a stage-2 LLM pass with the diff injected as context
     (via the job's ``script`` field — the standard cron script-injection
     mechanism) plus the active goal store (already in every system
     prompt, see ``agent/system_prompt.py``) and recent memory. The pass
     ends the turn with ``[SILENT]``, a delivered proactive message, or a
     registered suggestion (``cron/suggestions.py``, source="subconscious",
     via the ``suggest_automation`` tool).

``hermes subconscious enable|disable|status`` (``hermes_cli/subconscious.py``)
is the CLI surface. Config keys live under ``subconscious.*`` in
config.yaml per Contract 3.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

JOB_NAME = "Subconscious tick"
REFLECTION_JOB_NAME = "Subconscious reflection"
DEFAULT_INTERVAL = "20m"
DEFAULT_REFLECTION_SCHEDULE = "30 3 * * *"
DEFAULT_IDLE_TRIGGER_MINUTES = 15
SNAPSHOT_SHIM_NAME = "subconscious_snapshot.py"

# The real Contract-1 script, owned by Workstream C, lives alongside this
# module in the installed package.
_REAL_SNAPSHOT_SCRIPT = Path(__file__).resolve().parent / "scripts" / "subconscious_snapshot.py"

# Toolsets the tick job is restricted to (via create_job's
# enabled_toolsets) — enough to read/steer goals, register a suggestion,
# and use memory, without paying the token cost of the full default
# toolset on every tick. NOTE: cron-spawned agents can never receive the
# ``cronjob`` toolset (force-disabled by ``_resolve_cron_disabled_toolsets``
# in cron/scheduler.py), so the "auto"-tier auto-create path lives inside
# the ``suggest_automation`` tool handler (tools/goal_tools.py), not here.
# "web" (not "search" — there is no toolset registered under that name;
# web_search/web_extract live under "web", see tools/web.py and
# tools/presence/goblin.py's INVESTIGATION_TOOLSETS for the same name)
# gives the tick the ability to actually look something up while deciding
# whether a diff item is worth surfacing.
_TICK_TOOLSETS = ["goals", "subconscious", "memory", "web"]

NARRATIVE_CAP = 8_000
_NARRATIVE_RE = re.compile(r"<narrative>\s*(.*?)\s*</narrative>", re.DOTALL | re.IGNORECASE)
_INITIATIVES_RE = re.compile(r"<initiatives>\s*(.*?)\s*</initiatives>", re.DOTALL | re.IGNORECASE)
_INITIATIVE_RESULTS_RE = re.compile(
    r"<initiative-results>\s*(.*?)\s*</initiative-results>", re.DOTALL | re.IGNORECASE
)

_TICK_PROMPT = (
    "[Subconscious tick] You woke up on your own schedule, not because the "
    "user messaged you. Any '## Script Output' block above this message is "
    "a mechanical diff of what changed in the user's world since the last "
    "tick (email, calendar, code activity, or other connected surfaces) — "
    "you only reached this prompt because something changed; NO_CHANGE "
    "ticks are filtered out before you're woken. Your active goals are "
    "listed in your system prompt.\n\n"
    "Decide what, if anything, deserves the user's attention right now:\n"
    "- If the diff is noise, or nothing in it advances an active goal or "
    "matters to the user, respond with exactly \"[SILENT]\" and nothing "
    "else.\n"
    "- If something is genuinely worth a short proactive nudge, write it as "
    "a normal reply — brief, and skip anything you already told the user.\n"
    "- If the right move is a new recurring automation rather than a "
    "one-off interruption, call suggest_automation to propose it — never "
    "attempt to create the job yourself. The tool is consent-first: it "
    "registers a pending suggestion the user accepts with one tap, and "
    "only auto-creates when the user pre-approved the category as an "
    "'auto' tier in subconscious.tiers.\n"
    "Never invent activity that isn't supported by the diff, your goals, or "
    "your memory. End with one compact <narrative>...</narrative> block that "
    "updates your durable working model. You may also emit JSON arrays inside "
    "<initiatives>...</initiatives> and <initiative-results>...</initiative-results>. "
    "These blocks are persisted and removed before anything is shown to the user."
)

_REFLECTION_PROMPT = (
    "[Nightly subconscious reflection] Quietly consolidate the supplied narrative, "
    "recent activity, goals, suggestions, rhythm and durable memory. Improve the "
    "working model without inventing facts. Infer useful goals from repeated behavior "
    "or memory only as consent-first goal suggestions. If essential intent is uncertain, "
    "ask one short clarifying question in normal prose and do not propose that goal yet. "
    "Never activate a goal without acceptance. Return the refreshed model in exactly one "
    "<narrative>...</narrative> block. Optionally return up to five follow-ups as a JSON "
    "array in <initiatives>...</initiatives>. Once per calendar week, also review "
    "active goals for progress, staleness, duplication, or completion and propose any "
    "change rather than applying it silently."
)


def _subconscious_dir() -> Path:
    return get_hermes_home() / "subconscious"


def narrative_path() -> Path:
    return _subconscious_dir() / "narrative.md"


def read_narrative() -> str:
    try:
        return narrative_path().read_text(encoding="utf-8")[:NARRATIVE_CAP]
    except OSError:
        return ""


def write_narrative(text: str) -> None:
    """Atomically persist the bounded narrative and retain three revisions."""
    value = (text or "").strip()[-NARRATIVE_CAP:]
    path = narrative_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for index in range(3, 0, -1):
        source = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
        target = path.with_name(f"{path.name}.{index}")
        if source.exists():
            try:
                os.replace(source, target)
            except OSError:
                logger.debug("narrative rotation failed", exc_info=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".narrative_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _json_blocks(pattern: re.Pattern[str], text: str) -> List[Dict[str, Any]]:
    matches = pattern.findall(text or "")
    if not matches:
        return []
    try:
        value = json.loads(matches[-1])
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def process_background_output(text: str) -> Tuple[str, bool]:
    """Persist well-formed private blocks and return delivery-safe prose."""
    raw = text or ""
    narratives = _NARRATIVE_RE.findall(raw)
    updated = False
    if narratives:
        write_narrative(narratives[-1])
        updated = True
    from cron.subconscious_initiatives import add_initiatives, apply_results

    add_initiatives(_json_blocks(_INITIATIVES_RE, raw))
    apply_results(_json_blocks(_INITIATIVE_RESULTS_RE, raw))
    clean = _NARRATIVE_RE.sub("", raw)
    clean = _INITIATIVES_RE.sub("", clean)
    clean = _INITIATIVE_RESULTS_RE.sub("", clean)
    return clean.strip(), updated


def _recent_activity_summary(hours: int = 24) -> str:
    path = _subconscious_dir() / "activity.jsonl"
    if not path.exists():
        return "No recent background activity."
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows: List[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "No recent background activity."
    for line in lines[-200:]:
        try:
            item = json.loads(line)
            at = datetime.fromisoformat(str(item.get("at") or "").replace("Z", "+00:00"))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at >= cutoff:
            rows.append(f"- {item.get('source', 'tick')}: {item.get('summary') or item.get('outcome') or 'completed'}")
    return "\n".join(rows[-30:]) or "No recent background activity."


def build_runtime_context(job_name: str) -> str:
    """Build run-time context without mutating any long-lived chat prefix."""
    from agent.goal_store import format_active_goals_for_prompt
    from cron.subconscious_initiatives import due_initiatives
    from cron.suggestions import list_pending

    narrative = read_narrative() or "No durable narrative yet."
    due = due_initiatives()
    parts = [f"## Durable narrative\n{narrative}"]
    if due:
        parts.append("## Due initiatives\n" + json.dumps(due, ensure_ascii=False))
    if job_name == REFLECTION_JOB_NAME:
        try:
            from tools.presence.rhythm import rhythm_summary_line

            rhythm = rhythm_summary_line() or "No learned rhythm yet."
        except Exception:
            rhythm = "Rhythm unavailable."
        try:
            from tools.presence.distill import build_digest

            presence_digest = build_digest()[:6000]
        except Exception:
            presence_digest = "Presence digest unavailable."
        parts.extend(
            [
                f"## Last 24 hours\n{_recent_activity_summary()}",
                f"## Presence digest\n{presence_digest}",
                f"## Rhythm\n{rhythm}",
                format_active_goals_for_prompt() or "## Active goals\nNone",
                "## Pending suggestions\n" + json.dumps(list_pending(), ensure_ascii=False)[:6000],
            ]
        )
    return "\n\n".join(parts)


def _subconscious_cfg(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the ``subconscious`` config section with Contract 3 defaults filled in."""
    from hermes_cli.config import cfg_get, load_config

    cfg = cfg if cfg is not None else load_config()
    section = cfg_get(cfg, "subconscious", default={}) or {}
    if not isinstance(section, dict):
        section = {}
    tiers = section.get("tiers")
    return {
        "enabled": bool(section.get("enabled", False)),
        "interval": str(section.get("interval") or DEFAULT_INTERVAL),
        "idle_trigger_minutes": _coerce_positive_int(
            section.get("idle_trigger_minutes"), DEFAULT_IDLE_TRIGGER_MINUTES
        ),
        "tiers": dict(tiers) if isinstance(tiers, dict) else {},
        "job_id": section.get("job_id"),
        "reflection_job_id": section.get("reflection_job_id"),
        "reflection_schedule": str(section.get("reflection_schedule") or DEFAULT_REFLECTION_SCHEDULE),
    }


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def is_enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Whether ``subconscious.enabled`` is set."""
    return _subconscious_cfg(cfg)["enabled"]


def idle_trigger_minutes(cfg: Optional[Dict[str, Any]] = None) -> int:
    return _subconscious_cfg(cfg)["idle_trigger_minutes"]


def _normalize_schedule(interval: str) -> str:
    interval = (interval or DEFAULT_INTERVAL).strip()
    if not interval:
        interval = DEFAULT_INTERVAL
    # Accept a bare duration ("20m") or an already-recurring form
    # ("every 20m" / a cron expression) — only prefix "every " for a bare
    # duration so a user-supplied cron expression passes through untouched.
    lowered = interval.lower()
    if lowered.startswith("every ") or " " in interval:
        return interval
    return f"every {interval}"


def _write_snapshot_shim() -> Path:
    """Materialize the Contract-1 pre-run script under HERMES_HOME/scripts/.

    Cron pre-run scripts are sandboxed to ``HERMES_HOME/scripts/`` (see
    ``cron.scheduler._run_job_script``, which rejects any path resolving
    outside it), so the real implementation at
    ``cron/scripts/subconscious_snapshot.py`` inside the installed package
    can't be referenced directly by an absolute path. This writes a tiny
    shim that runs the real script by absolute path via ``runpy`` — no
    import/PYTHONPATH assumptions, so it works the same for source
    checkouts, editable installs, and packaged installs. Regenerated on
    every ``enable()`` so a package upgrade's path is kept current.
    """
    scripts_dir = get_hermes_home() / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    shim_path = scripts_dir / SNAPSHOT_SHIM_NAME
    shim_path.write_text(
        (
            '"""Auto-generated shim -- regenerated by `hermes subconscious enable`.\n\n'
            "Runs the real subconscious snapshot script (Contract 1, owned by\n"
            "Workstream C) by absolute path. Cron pre-run scripts are sandboxed to\n"
            "this directory, so the installed package path can't be referenced\n"
            'directly. Do not edit by hand -- edit cron/scripts/subconscious_snapshot.py.\n"""\n'
            "import runpy\n\n"
            f"runpy.run_path({str(_REAL_SNAPSHOT_SCRIPT)!r}, run_name='__main__')\n"
        ),
        encoding="utf-8",
    )
    return shim_path


def enable(interval: Optional[str] = None) -> Dict[str, Any]:
    """Enable the subconscious tick: persist config and create the one cron job.

    Idempotent — if a job is already tracked (``subconscious.job_id``) and
    still exists, this resumes it (if paused) and updates its schedule
    instead of creating a duplicate. "No second job engine" means exactly
    one job total, not one per ``enable()`` call.
    """
    from cron.jobs import create_job, get_job, resume_job, update_job

    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    section = dict(cfg.get("subconscious") or {})

    resolved_interval = (interval or section.get("interval") or DEFAULT_INTERVAL).strip() or DEFAULT_INTERVAL
    schedule = _normalize_schedule(resolved_interval)
    shim_path = _write_snapshot_shim()

    existing_id = section.get("job_id")
    job = get_job(existing_id) if existing_id else None
    if job is None:
        job = create_job(
            prompt=_TICK_PROMPT,
            schedule=schedule,
            name=JOB_NAME,
            script=shim_path.name,
            deliver="local",
            enabled_toolsets=list(_TICK_TOOLSETS),
        )
        section["job_id"] = job["id"]
    else:
        if job.get("state") == "paused":
            job = resume_job(job["id"]) or job
        if job.get("schedule_display") != schedule:
            try:
                update_job(job["id"], {"schedule": schedule})
            except Exception:
                logger.debug("subconscious enable: schedule update failed", exc_info=True)

    reflection_id = section.get("reflection_job_id")
    reflection = get_job(reflection_id) if reflection_id else None
    reflection_schedule = str(section.get("reflection_schedule") or DEFAULT_REFLECTION_SCHEDULE)
    if reflection is None:
        reflection = create_job(
            prompt=_REFLECTION_PROMPT,
            schedule=reflection_schedule,
            name=REFLECTION_JOB_NAME,
            deliver="local",
            enabled_toolsets=list(_TICK_TOOLSETS),
        )
        section["reflection_job_id"] = reflection["id"]
    else:
        if reflection.get("state") == "paused":
            reflection = resume_job(reflection["id"]) or reflection
        if reflection.get("schedule_display") != reflection_schedule:
            update_job(reflection["id"], {"schedule": reflection_schedule})

    section["enabled"] = True
    section["interval"] = resolved_interval
    section.setdefault("idle_trigger_minutes", DEFAULT_IDLE_TRIGGER_MINUTES)
    section.setdefault("tiers", {})
    section.setdefault("reflection_schedule", DEFAULT_REFLECTION_SCHEDULE)
    cfg["subconscious"] = section
    save_config(cfg)
    return status()


def disable() -> Dict[str, Any]:
    """Disable the subconscious tick: pause the job (if any) and flip config off."""
    from cron.jobs import pause_job

    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    section = dict(cfg.get("subconscious") or {})
    for job_id in (section.get("job_id"), section.get("reflection_job_id")):
        if not job_id:
            continue
        try:
            pause_job(job_id, reason="subconscious disabled")
        except Exception:
            logger.debug("subconscious disable: pause_job failed", exc_info=True)
    section["enabled"] = False
    cfg["subconscious"] = section
    save_config(cfg)
    return status()


def status() -> Dict[str, Any]:
    """Return current subconscious config + the tracked job's live state."""
    from cron.jobs import get_job

    section = _subconscious_cfg()
    job = get_job(section["job_id"]) if section.get("job_id") else None
    reflection = get_job(section["reflection_job_id"]) if section.get("reflection_job_id") else None
    return {
        "enabled": section["enabled"],
        "interval": section["interval"],
        "idle_trigger_minutes": section["idle_trigger_minutes"],
        "tiers": section["tiers"],
        "job_id": section.get("job_id"),
        "job_state": job.get("state") if job else None,
        "last_run_at": job.get("last_run_at") if job else None,
        "next_run_at": job.get("next_run_at") if job else None,
        "reflection_schedule": section["reflection_schedule"],
        "reflection_job_id": section.get("reflection_job_id"),
        "reflection_job_state": reflection.get("state") if reflection else None,
        "reflection_last_run_at": reflection.get("last_run_at") if reflection else None,
        "reflection_next_run_at": reflection.get("next_run_at") if reflection else None,
    }


def _should_defer_for_resource_policy() -> bool:
    """True when the presence resource policy says to hold off the tick
    (heavy foreground app -- fullscreen game, video editor, 3D tool).

    Guarded import: ``tools/presence/resource_policy.py`` is a sibling
    workstream's module and any failure to import/evaluate it must never
    block the subconscious tick -- it resolves to "don't defer". Only the
    subconscious tick job is affected; other scheduled cron jobs go through
    the normal ticker untouched.
    """
    try:
        from tools.presence.resource_policy import should_defer_background_work

        return bool(should_defer_background_work())
    except Exception:
        logger.debug("subconscious: resource-policy check failed; not deferring", exc_info=True)
        return False


def _pending_trigger_marker_path() -> Path:
    return get_hermes_home() / "subconscious" / "pending_trigger_reason.json"


def _mark_pending_trigger_reason(reason: str) -> None:
    """Best-effort marker so the activity log (cron/scheduler.py) can
    attribute the next fired tick run to WHY it fired (idle silence vs the
    normal schedule) instead of always logging a plain "tick" source.

    Consumed (read-and-deleted) by
    ``cron.scheduler._consume_pending_trigger_reason`` the moment that run
    completes its wake-gate/agent-completion hook. A stale marker (the
    consumer enforces a max age) is simply ignored rather than mis-attributing
    a later, unrelated regular tick — this is a visibility nicety, never
    allowed to affect the tick itself.
    """
    try:
        path = _pending_trigger_marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"reason": reason, "at": _hermes_now().isoformat()}),
            encoding="utf-8",
        )
    except Exception:
        logger.debug("subconscious: failed to write pending-trigger marker", exc_info=True)


def trigger_tick(reason: str = "idle") -> bool:
    """Fire the subconscious tick job once, immediately.

    Reuses the tracked job (``cron.jobs.trigger_job`` just sets
    ``next_run_at`` to now; the existing ticker picks it up on its next
    loop iteration) — no second engine, no direct agent invocation here.
    Returns True iff a trigger was actually issued (subconscious enabled,
    a job is tracked, and the trigger call succeeded).
    """
    section = _subconscious_cfg()
    if not section["enabled"] or not section.get("job_id"):
        return False
    if _should_defer_for_resource_policy():
        logger.info("subconscious: deferring tick (reason=%s) -- heavy foreground app", reason)
        return False
    from cron.jobs import trigger_job

    try:
        job = trigger_job(section["job_id"])
    except Exception:
        logger.debug("subconscious trigger_tick failed", exc_info=True)
        return False
    if job:
        logger.info("subconscious: tick triggered (reason=%s)", reason)
        _mark_pending_trigger_reason(reason)
    return bool(job)
