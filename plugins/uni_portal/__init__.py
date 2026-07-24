"""Duzce University student-portal plugin — Marvi freedom spec §1.3.

Checks the user's own Duzce University student system (grades,
announcements, schedule) via browser automation on a daily schedule, and
tells the user proactively when something changes. Off by default
(``uni_portal.enabled``, default false) until the user enrolls via
``hermes uni login`` (``hermes_cli/subcommands/uni.py``). See ``SKILL.md``
for the credential/security boundary this plugin operates under.

Bundled + ``kind: backend`` (mirrors ``plugins/spotify``): the tool always
registers so it's introspectable (``hermes tools``), but dispatch is gated
by ``_check_uni_portal_available`` on ``uni_portal.enabled`` — the plugin
never touches the network or the browser until the user has explicitly
enrolled.
"""

from __future__ import annotations

import logging

from plugins.uni_portal.tools import (
    UNI_PORTAL_CHECK_SCHEMA,
    _check_uni_portal_available,
    _handle_uni_portal_check,
)

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Register the uni_portal_check tool. Called once by the plugin loader."""
    ctx.register_tool(
        name="uni_portal_check",
        toolset="uni_portal",
        schema=UNI_PORTAL_CHECK_SCHEMA,
        handler=_handle_uni_portal_check,
        check_fn=_check_uni_portal_available,
        emoji="🎓",
    )

    # If the user already enrolled and enabled this before the current
    # process started (e.g. after a restart), make sure the daily cron job
    # exists — mirrors cron/subconscious.py's own idempotent enable() call
    # pattern. Best-effort: must never block plugin load / other plugins.
    try:
        if _check_uni_portal_available():
            from plugins.uni_portal.check import ensure_uni_portal_job

            ensure_uni_portal_job()
    except Exception:
        logger.debug("uni_portal: startup job-sync failed", exc_info=True)
