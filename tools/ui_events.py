"""Fire-and-forget UI events from tools to the connected client.

Mirrors the per-session callback registry in tools/approval.py, but for
non-blocking UI surfacing (e.g. show_card). A platform adapter registers a
callback per session that forwards the event onto that run's event stream;
tools call emit_ui_event() to push a card to the user's presence overlay.
"""

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_ui_event_cbs: dict[str, Callable[[dict], None]] = {}


def register_ui_event_notify(session_key: str, cb: Callable[[dict], None]) -> None:
    """Register the per-session UI-event forwarder."""
    if not session_key:
        return
    with _lock:
        _ui_event_cbs[session_key] = cb


def unregister_ui_event_notify(session_key: str) -> None:
    """Remove the per-session UI-event forwarder."""
    if not session_key:
        return
    with _lock:
        _ui_event_cbs.pop(session_key, None)


def emit_ui_event(session_key: str, event: dict) -> bool:
    """Send a UI event to the session's client. Returns True if delivered.

    Never raises -- a missing listener (CLI, cron, tests) is a no-op.
    """
    with _lock:
        cb = _ui_event_cbs.get(session_key)
    if cb is None:
        return False
    try:
        cb(event)
        return True
    except Exception as exc:
        logger.debug("emit_ui_event delivery failed: %s", exc)
        return False
