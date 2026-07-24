"""Browser-driven login + collection flow for the Duzce University student
system — spec §1.3.

Reuses ``tools/browser_tool.py``'s CDP-backed primitives
(``browser_navigate``, ``browser_snapshot``, ``browser_click``,
``browser_type``) rather than a separate browser stack. Credentials are
read from the OS credential store at the moment of login
(``plugins.uni_portal.credentials.read_credentials``) and never persisted or
logged in plaintext by this module.

**2FA / CAPTCHA**: if the login flow detects either after submitting
credentials, it STOPS immediately and routes to the ask-user channel
(``agent.autonomy.ask.ask_user``) — this module never attempts to solve or
bypass either, per the spec's non-goal.

**Live validation note**: the portal's actual field names, page structure,
and navigation paths are configurable (``uni_portal.{portal_url,
login_username_ref_hint, ...}``) because they can't be hand-verified without
a real Duzce account and browser session. The login/collect/diff/notify
control flow itself is exercised by unit tests with fakes
(``tests/plugins/uni_portal/``); the selector heuristics below need live
enrollment (``hermes uni login``) to confirm against the real portal.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TASK_ID = "uni-portal-check"

# Turkish + English keyword heuristics — Duzce's portal is Turkish-language,
# but a heuristic that also matches English catches an alternate/English UI.
_USERNAME_HINTS = ("kullanıcı", "öğrenci no", "ogrenci", "student", "username", "user")
_PASSWORD_HINTS = ("şifre", "parola", "password")
_SUBMIT_HINTS = ("giriş", "gir", "login", "sign in", "oturum aç")
_TWO_FA_HINTS = ("doğrulama kodu", "güvenlik kodu", "sms kod", "verification code", "2fa", "otp")
_CAPTCHA_HINTS = ("captcha", "robot değilim", "i'm not a robot", "güvenlik doğrulaması")
_LOGGED_IN_HINTS = ("çıkış", "logout", "sign out", "hoş geldin", "hoşgeldin")


def _uni_portal_config() -> Dict[str, Any]:
    """``uni_portal`` config section with inline defaults — not UI-edited
    (enrollment is CLI-driven via ``hermes uni login``), mirrors the
    ``cfg_get``-with-inline-defaults style used across ``agent/memory/*``."""
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = load_config()
        return {
            "enabled": bool(cfg_get(cfg, "uni_portal", "enabled", default=False)),
            "check_schedule": str(cfg_get(cfg, "uni_portal", "check_schedule", default="0 18 * * *") or "0 18 * * *"),
            "portal_url": str(cfg_get(cfg, "uni_portal", "portal_url", default="") or ""),
            "grades_path": str(cfg_get(cfg, "uni_portal", "grades_path", default="") or ""),
            "announcements_path": str(cfg_get(cfg, "uni_portal", "announcements_path", default="") or ""),
            "schedule_path": str(cfg_get(cfg, "uni_portal", "schedule_path", default="") or ""),
        }
    except Exception:
        logger.debug("uni_portal: config read failed, using defaults", exc_info=True)
        return {
            "enabled": False,
            "check_schedule": "0 18 * * *",
            "portal_url": "",
            "grades_path": "",
            "announcements_path": "",
            "schedule_path": "",
        }


def _text_contains_any(text: str, hints: tuple) -> bool:
    lowered = (text or "").casefold()
    return any(hint in lowered for hint in hints)


def _find_ref_by_hint(snapshot_text: str, hints: tuple) -> Optional[str]:
    """Best-effort: scan the accessibility-tree snapshot text (YAML-ish,
    ``[ref_N]`` tagged — see ``mcp__Claude_Browser__read_page``'s format,
    which ``browser_snapshot`` mirrors) for a line matching any hint and
    return its ``ref_N`` token. Returns None if nothing matches — callers
    treat that as "selector needs tuning against the live portal" rather
    than a hard failure.
    """
    for line in (snapshot_text or "").splitlines():
        if "ref_" not in line:
            continue
        if _text_contains_any(line, hints):
            match = re.search(r"ref_\d+", line)
            if match:
                return match.group(0)
    return None


class LoginBlocked(RuntimeError):
    """Raised internally when 2FA/CAPTCHA is detected — never caught to
    "work around" it, only to stop cleanly and route to ask_user."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _snapshot_text() -> str:
    from tools.browser_tool import browser_snapshot

    raw = browser_snapshot(task_id=_TASK_ID)
    try:
        data = json.loads(raw)
        return str(data.get("snapshot") or data.get("text") or raw)
    except (TypeError, ValueError):
        return str(raw)


def login() -> bool:
    """Log into the Duzce student system using credentials from the OS
    credential store. Returns True on an apparent successful login.

    Raises :class:`LoginBlocked` if 2FA/CAPTCHA is detected after
    submitting credentials — callers MUST catch this and route to
    ``agent.autonomy.ask.ask_user`` (never retried automatically). Any
    other failure (missing credentials, portal unreachable, selectors not
    found) returns False rather than raising, since those are ordinary
    "couldn't check today" outcomes, not a security boundary.
    """
    from plugins.uni_portal.credentials import read_credentials

    creds = read_credentials()
    if creds is None:
        logger.info("uni_portal: no stored credentials — run `hermes uni login` first")
        return False

    cfg = _uni_portal_config()
    portal_url = cfg["portal_url"]
    if not portal_url:
        logger.warning("uni_portal: uni_portal.portal_url is not configured")
        return False

    try:
        from tools.browser_tool import browser_click, browser_navigate, browser_type

        browser_navigate(portal_url, task_id=_TASK_ID)
        snapshot = _snapshot_text()

        username_ref = _find_ref_by_hint(snapshot, _USERNAME_HINTS)
        password_ref = _find_ref_by_hint(snapshot, _PASSWORD_HINTS)
        if not username_ref or not password_ref:
            logger.warning("uni_portal: could not locate username/password fields on login page")
            return False

        browser_type(username_ref, creds["username"], task_id=_TASK_ID)
        browser_type(password_ref, creds["password"], task_id=_TASK_ID)

        submit_ref = _find_ref_by_hint(snapshot, _SUBMIT_HINTS)
        if submit_ref:
            browser_click(submit_ref, task_id=_TASK_ID)
        else:
            logger.warning("uni_portal: could not locate the login/submit button")
            return False

        post_login_snapshot = _snapshot_text()
        if _text_contains_any(post_login_snapshot, _TWO_FA_HINTS):
            raise LoginBlocked("The portal is asking for a 2FA/verification code.")
        if _text_contains_any(post_login_snapshot, _CAPTCHA_HINTS):
            raise LoginBlocked("The portal is showing a CAPTCHA challenge.")

        return _text_contains_any(post_login_snapshot, _LOGGED_IN_HINTS)
    except LoginBlocked:
        raise
    except Exception:
        logger.debug("uni_portal: login failed", exc_info=True)
        return False
    finally:
        # Drop the local reference to the credentials dict as soon as
        # possible — nothing here retains it beyond this function's frame.
        creds = None  # noqa: F841


def _extract_table_like_rows(snapshot_text: str) -> List[List[str]]:
    """Best-effort generic table/list parser over accessibility-tree text:
    groups consecutive short lines into row-shaped tuples. Deliberately
    generic (not portal-specific) — the real column mapping happens in
    :func:`collect_grades`/:func:`collect_announcements`, which is the part
    that needs live-portal tuning."""
    rows: List[List[str]] = []
    current: List[str] = []
    for raw_line in (snapshot_text or "").splitlines():
        line = raw_line.strip().lstrip("-").strip()
        if not line:
            if current:
                rows.append(current)
                current = []
            continue
        current.append(line)
    if current:
        rows.append(current)
    return rows


def collect_grades() -> List[Dict[str, Any]]:
    """Navigate to the configured grades page and return
    ``[{"course": str, "grade": str}, ...]``. Best-effort — an empty list on
    any failure (never raises), since a bad-day scrape should not crash the
    whole daily check."""
    cfg = _uni_portal_config()
    if not cfg["grades_path"]:
        return []
    try:
        from tools.browser_tool import browser_navigate

        browser_navigate(cfg["grades_path"], task_id=_TASK_ID)
        snapshot = _snapshot_text()
        grades: List[Dict[str, Any]] = []
        for row in _extract_table_like_rows(snapshot):
            if len(row) >= 2 and not _text_contains_any(row[0], ("ref_",)):
                grades.append({"course": row[0], "grade": row[-1]})
        return grades
    except Exception:
        logger.debug("uni_portal: collect_grades failed", exc_info=True)
        return []


def collect_announcements() -> List[Dict[str, Any]]:
    """Navigate to the configured announcements page and return
    ``[{"title": str, "date": str}, ...]``. Best-effort, never raises."""
    cfg = _uni_portal_config()
    if not cfg["announcements_path"]:
        return []
    try:
        from tools.browser_tool import browser_navigate

        browser_navigate(cfg["announcements_path"], task_id=_TASK_ID)
        snapshot = _snapshot_text()
        announcements: List[Dict[str, Any]] = []
        for row in _extract_table_like_rows(snapshot):
            if row:
                announcements.append({"title": row[0], "date": row[-1] if len(row) > 1 else None})
        return announcements
    except Exception:
        logger.debug("uni_portal: collect_announcements failed", exc_info=True)
        return []


def collect_schedule() -> List[Dict[str, Any]]:
    """Navigate to the configured schedule page and return a best-effort
    list of schedule rows. Never raises."""
    cfg = _uni_portal_config()
    if not cfg["schedule_path"]:
        return []
    try:
        from tools.browser_tool import browser_navigate

        browser_navigate(cfg["schedule_path"], task_id=_TASK_ID)
        snapshot = _snapshot_text()
        return [{"raw": " | ".join(row)} for row in _extract_table_like_rows(snapshot)]
    except Exception:
        logger.debug("uni_portal: collect_schedule failed", exc_info=True)
        return []
