"""Shim for tool discovery. Registers `desktop_context` with tools.registry.

The real implementation lives in `tools/presence/` to keep the presence
subsystem's files together (mirrors the `tools/computer_use_tool.py` ->
`tools/computer_use/` pattern). This shim exists because tools.registry
auto-imports `tools/*.py` -- a top-level module is needed to trigger
registration.
"""

from __future__ import annotations

from tools.presence.context import (
    check_desktop_context_requirements,
    handle_desktop_context,
)
from tools.registry import registry

DESKTOP_CONTEXT_SCHEMA = {
    "name": "desktop_context",
    "description": (
        "Read the user's current desktop presence via ActivityWatch (local, "
        "no cloud). mode='now' returns the foreground app/window, parsed "
        "VS Code workspace+file or terminal cwd, now-playing media, afk "
        "state, and how long the user has been on the current window. "
        "mode='today'/'week' return aggregates: top apps by time, coding "
        "time by workspace, and recent media highlights. Degrades to "
        "{'available': false, 'error': ...} when ActivityWatch is not "
        "running -- never fails the turn."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["now", "today", "week"],
                "description": "Which view to return. Defaults to 'now'.",
            },
        },
    },
}

registry.register(
    name="desktop_context",
    toolset="presence",
    schema=DESKTOP_CONTEXT_SCHEMA,
    handler=lambda args, **kw: handle_desktop_context(args, **kw),
    check_fn=check_desktop_context_requirements,
    requires_env=[],
    description=DESKTOP_CONTEXT_SCHEMA["description"],
    emoji="🖥️",
)


__all__ = ["handle_desktop_context"]
