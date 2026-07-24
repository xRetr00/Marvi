"""``uni_portal_check`` tool — manual/cron trigger for the daily portal
check (spec §1.3). Registered into the ``uni_portal`` toolset; gated by
``_check_uni_portal_available`` the same way ``plugins/spotify/tools.py``
gates its tools on auth status — the tool is always registered (so it shows
up in ``hermes tools``), but dispatch is blocked until the user has enrolled
AND ``uni_portal.enabled`` is true.
"""

from __future__ import annotations

from typing import Any

from tools.registry import tool_error, tool_result


def _check_uni_portal_available() -> bool:
    try:
        from plugins.uni_portal.portal import _uni_portal_config

        return bool(_uni_portal_config().get("enabled"))
    except Exception:
        return False


def _handle_uni_portal_check(args: dict, **kw) -> str:
    try:
        from plugins.uni_portal.check import run_daily_check

        result = run_daily_check()
        if result.get("error") == "disabled":
            return tool_error(
                "uni_portal is disabled. Enroll with `hermes uni login` to enable it."
            )
        return tool_result(result)
    except Exception as exc:
        return tool_error(f"uni_portal_check failed: {type(exc).__name__}: {exc}")


UNI_PORTAL_CHECK_SCHEMA = {
    "name": "uni_portal_check",
    "description": (
        "Run today's Duzce University student-portal check now: log in with the "
        "stored credentials, collect grades/announcements/schedule, diff against "
        "the last snapshot, and send a proactive notification only if something "
        "actually changed. If the portal asks for 2FA or a CAPTCHA, this stops "
        "and asks the user instead of trying to get past it."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}
