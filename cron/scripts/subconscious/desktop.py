"""ActivityWatch delta source for the proactive subconscious tick."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from cron.scripts.subconscious.snapshot_store import SurfaceStore


APP = "desktop"


def _stable_context(data: Dict[str, Any]) -> Dict[str, Any]:
    """Keep decision-relevant fields; exclude ever-changing session duration."""
    window = data.get("window") if isinstance(data.get("window"), dict) else {}
    media = data.get("now_playing") if isinstance(data.get("now_playing"), dict) else {}
    return {
        "afk": data.get("afk"),
        "window": {
            key: window.get(key)
            for key in ("app", "workspace", "file", "cwd", "redacted", "reason")
            if window.get(key) is not None
        },
        "now_playing": {
            key: media.get(key)
            for key in ("title", "artist", "status", "redacted", "reason")
            if media.get(key) is not None
        },
    }


def _describe(context: Dict[str, Any]) -> str:
    window = context.get("window") or {}
    if window.get("redacted"):
        place = "a privacy-redacted app"
    elif window.get("workspace"):
        suffix = f" / {window['file']}" if window.get("file") else ""
        place = f"{window['workspace']}{suffix}"
    elif window.get("cwd"):
        place = f"terminal at {window['cwd']}"
    else:
        place = str(window.get("app") or "unknown app")
    afk = "away" if context.get("afk") == "afk" else "active"
    media = context.get("now_playing") or {}
    listening = f"; playing {media['title']}" if media.get("title") and not media.get("redacted") else ""
    return f"{afk} in {place}{listening}"


def fetch_delta(store: SurfaceStore) -> Optional[str]:
    from tools.presence.context import desktop_context

    raw = desktop_context("now")
    if not raw.get("available"):
        return None
    current = _stable_context(raw)
    signature = json.dumps(current, sort_keys=True, ensure_ascii=False)
    previous = store.state.get("context") if isinstance(store.state.get("context"), dict) else None

    store.set_cursor({"signature": signature})
    store.set_state({"context": current})
    if previous is None:
        return None
    previous_signature = json.dumps(previous, sort_keys=True, ensure_ascii=False)
    if previous_signature == signature:
        return None
    return f"ActivityWatch desktop context changed: {_describe(previous)} -> {_describe(current)}"
