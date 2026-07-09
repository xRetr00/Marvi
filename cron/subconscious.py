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

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

JOB_NAME = "Subconscious tick"
DEFAULT_INTERVAL = "20m"
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
_TICK_TOOLSETS = ["goals", "subconscious", "memory", "search"]

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
    "your memory."
)


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

    section["enabled"] = True
    section["interval"] = resolved_interval
    section.setdefault("idle_trigger_minutes", DEFAULT_IDLE_TRIGGER_MINUTES)
    section.setdefault("tiers", {})
    cfg["subconscious"] = section
    save_config(cfg)
    return status()


def disable() -> Dict[str, Any]:
    """Disable the subconscious tick: pause the job (if any) and flip config off."""
    from cron.jobs import pause_job

    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    section = dict(cfg.get("subconscious") or {})
    job_id = section.get("job_id")
    if job_id:
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
    return {
        "enabled": section["enabled"],
        "interval": section["interval"],
        "idle_trigger_minutes": section["idle_trigger_minutes"],
        "tiers": section["tiers"],
        "job_id": section.get("job_id"),
        "job_state": job.get("state") if job else None,
        "last_run_at": job.get("last_run_at") if job else None,
        "next_run_at": job.get("next_run_at") if job else None,
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
    return bool(job)
