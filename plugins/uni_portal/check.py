"""Daily check orchestrator for uni_portal — spec §1.3.

:func:`run_daily_check` ties the whole flow together: log in (OS credential
store, spec §1.3), collect grades/announcements/schedule, diff against the
persisted snapshot, and — only on a real change — send one proactive
message, record an episode, and add/update a graph node. The snapshot is
saved on every run (even a no-change one) so the next run's diff is always
against the latest capture.

Never raises: this runs unattended on ``uni_portal.check_schedule`` (default
"0 18 * * *", disabled until the user runs ``hermes uni login``), so every
failure mode degrades to a logged no-op rather than a broken cron job.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

logger = logging.getLogger(__name__)

UNI_PORTAL_JOB_NAME = "uni-portal-daily-check"


def _notify_user(summary: str) -> None:
    """Proactive message through the normal delivery path — a one-shot cron
    job with ``deliver=<platform>``, mirroring
    ``tools/presence/goblin.py``'s ``notify_stuck`` and
    ``agent/autonomy/ask.py``'s ``ask_user`` (both use the identical
    pattern for the same reason: it's the existing proactive delivery path,
    already flow-gated).
    """
    try:
        import uuid

        from agent.autonomy.ask import pick_delivery_target
        from cron.jobs import create_job

        target = pick_delivery_target()
        if not target:
            logger.info("uni_portal: no delivery target configured; summary not sent: %s", summary)
            return
        prompt = (
            "You are Marvi. Your daily check of the user's Duzce University "
            "student portal found something new. Send them a short, warm "
            "message summarizing it — don't just paste this verbatim if it "
            "reads stiffly, but keep every fact:\n\n" + summary
        )
        create_job(
            prompt=prompt,
            schedule="1m",
            name=f"uni-portal-notify-{uuid.uuid4().hex[:8]}",
            repeat=1,
            deliver=target,
        )
    except Exception:
        logger.debug("uni_portal: notify failed", exc_info=True)


def _record_episode_and_graph(summary: str) -> None:
    try:
        from agent.memory.episodic import record_episode

        record_episode(
            kind="task",
            title="Duzce student portal update",
            summary=summary,
            actor="world",
            source="uni_portal",
            importance=0.6,
        )
    except Exception:
        logger.debug("uni_portal: episodic record failed", exc_info=True)
    try:
        from agent.memory.graph_builder import record_from_memory_entry

        record_from_memory_entry(f"[Uni portal] {summary}"[:1000], topic="university")
    except Exception:
        logger.debug("uni_portal: graph record failed", exc_info=True)


def run_daily_check() -> Dict[str, Any]:
    """Run one full check cycle. Returns a summary dict (``ok``, ``changed``,
    ``error``) for logging/tests. Never raises."""
    result: Dict[str, Any] = {"ok": False, "changed": False, "error": None}
    try:
        from plugins.uni_portal.portal import (
            LoginBlocked,
            _uni_portal_config,
            collect_announcements,
            collect_grades,
            collect_schedule,
            login,
        )
        from plugins.uni_portal.snapshot import (
            diff_snapshots,
            format_diff_summary,
            has_changes,
            load_snapshot,
            save_snapshot,
        )

        cfg = _uni_portal_config()
        if not cfg["enabled"]:
            result["error"] = "disabled"
            return result

        try:
            logged_in = login()
        except LoginBlocked as exc:
            _ask_about_login_block(exc.reason)
            result["error"] = f"login_blocked: {exc.reason}"
            return result

        if not logged_in:
            result["error"] = "login_failed"
            return result

        new_snapshot = {
            "grades": collect_grades(),
            "announcements": collect_announcements(),
            "schedule": collect_schedule(),
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        old_snapshot = load_snapshot()
        diff = diff_snapshots(old_snapshot, new_snapshot)
        save_snapshot(new_snapshot)

        if has_changes(diff):
            summary = format_diff_summary(diff)
            if summary:
                _notify_user(summary)
                _record_episode_and_graph(summary)
            result["changed"] = True
        result["ok"] = True
        return result
    except Exception:
        logger.debug("uni_portal: run_daily_check failed", exc_info=True)
        result["error"] = "exception"
        return result


def _ask_about_login_block(reason: str) -> None:
    """2FA/CAPTCHA stop-and-ask (spec §1.3 non-goal: never bypass). Routes
    through the same autonomy ask-user channel as everything else in Part 1."""
    try:
        from agent.autonomy.ask import ask_user

        ask_user(
            "I couldn't finish today's check of your student portal — it "
            f"asked for {reason} I stopped rather than try to get past it "
            "myself. Want to walk me through it, or should I just skip "
            "today's check?",
            context="uni_portal login blocked",
            category="uni_portal",
        )
    except Exception:
        logger.debug("uni_portal: failed to ask about login block", exc_info=True)


# ---------------------------------------------------------------------------
# Idempotent cron job registration — mirrors cron/subconscious.py's own
# enable()/get_job-or-create_job pattern, scoped to this plugin. Called from
# hermes_cli/subcommands/uni.py after a successful `hermes uni login`
# (flipping uni_portal.enabled true), and from the plugin's own register()
# at startup if uni_portal.enabled is already true (e.g. after a restart).
# ---------------------------------------------------------------------------


def ensure_uni_portal_job() -> Dict[str, Any]:
    """Idempotently create/resume the daily uni_portal check job, using the
    prompt-free "script" cron shape (``no_agent`` isn't used here since the
    tool call itself, not an LLM turn, does the work — see
    ``uni_portal_check`` in ``plugins/uni_portal/tools.py``). Never raises;
    returns the job dict, or ``{}`` on failure."""
    try:
        from hermes_cli.config import cfg_get, load_config

        from cron.jobs import create_job, get_job, list_jobs, resume_job, update_job

        cfg = load_config()
        schedule = str(cfg_get(cfg, "uni_portal", "check_schedule", default="0 18 * * *") or "0 18 * * *")

        existing = next((j for j in list_jobs() if j.get("name") == UNI_PORTAL_JOB_NAME), None)
        prompt = (
            "Call uni_portal_check to run today's Duzce student-portal check. "
            "Do not narrate — the tool itself handles login, collection, "
            "diffing, and any user-facing notification."
        )
        if existing is None:
            return create_job(
                prompt=prompt,
                schedule=schedule,
                name=UNI_PORTAL_JOB_NAME,
                deliver="local",
                enabled_toolsets=["uni_portal"],
            )
        job = existing
        if job.get("state") == "paused":
            job = resume_job(job["id"]) or job
        if job.get("schedule_display") != schedule:
            try:
                update_job(job["id"], {"schedule": schedule})
            except Exception:
                logger.debug("uni_portal: schedule update failed", exc_info=True)
        return job
    except Exception:
        logger.debug("uni_portal: ensure_uni_portal_job failed", exc_info=True)
        return {}


def disable_uni_portal_job() -> None:
    """Pause the daily check job (``hermes uni login --logout``). Never
    raises."""
    try:
        from cron.jobs import list_jobs, pause_job

        existing = next((j for j in list_jobs() if j.get("name") == UNI_PORTAL_JOB_NAME), None)
        if existing is not None:
            pause_job(existing["id"])
    except Exception:
        logger.debug("uni_portal: disable_uni_portal_job failed", exc_info=True)
