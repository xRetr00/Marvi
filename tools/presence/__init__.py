"""Presence subsystem — desktop awareness for Marvi (Workstream B).

Reads local ActivityWatch (localhost:5600) data plus an optional Windows
SMTC media watcher to give Marvi a cheap, always-local sense of what the
user is doing: foreground app/window, coding context, now-playing media,
afk/idle state, and rollups over a day/week.

Modules:
    aw_client     -- thin REST client for the ActivityWatch server.
    common        -- shared helpers: denylist filtering, focus-app heuristics.
    title_parsing -- window-title parsing (VS Code, terminals).
    context       -- desktop_context tool implementation (now/today/week).
    media_watcher -- optional winsdk-based now-playing poller -> AW heartbeats.
    goblin        -- opt-in stuck-detection heuristic + session priming text.
    distill       -- helper for the nightly presence distiller cron job.

Nothing in this package raises when ActivityWatch is unreachable -- every
public entry point degrades to a clear "presence unavailable" message.
"""

from __future__ import annotations
