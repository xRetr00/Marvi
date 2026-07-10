"""Slack delta fetcher -- Composio smart sync for Marvi.

Cursor is a last-seen Slack timestamp (``ts``, Slack's float-seconds string
format) per surface, mirroring GitHub's ``since`` cursor: each fetch asks
"what's new since this timestamp" rather than pulling full channel history.

First run establishes the baseline timestamp at "now" and reports nothing
changed -- everything already sitting unread at connect time is pre-existing
noise, not a new delta to surface (same rationale as ``github.py``).

Deliberately does NOT fetch full channel history (``conversations.history``
style pulls): the assumed Composio action returns activity *summaries*
(unread direct-message conversations and thread mentions, each already
carrying a short preview of the latest relevant message) rather than raw
message logs, so one call covers a tick without walking every channel.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from cron.scripts.subconscious.composio_client import get_client, unwrap_payload
from cron.scripts.subconscious.snapshot_store import SurfaceStore
from hermes_time import now as _hermes_now

APP = "slack"

# Composio Slack toolkit action slug, kept as a module constant so a future
# SDK rename is a one-line change instead of a find-and-replace. Assumed
# response shape: a list of activity-summary items (under ``conversations``
# or ``items``), each with ``id`` (conversation id), ``kind``
# (``"im"``/``"mpim"`` for direct messages, anything else treated as a
# thread/channel mention), ``name`` (sender/channel display name), ``ts``
# (Slack timestamp of the latest relevant message) and ``text`` (a preview
# of that message) -- deliberately NOT a full message history.
ACTION_LIST_CONVERSATIONS = "SLACK_LIST_CONVERSATIONS"

_DM_KINDS = ("im", "mpim")

MAX_PREVIEW_CHARS = 80

# Cap on how many items get an individual summary line in one diff -- a
# notification flood shouldn't turn one tick into a wall of text. Anything
# beyond the cap is rolled up into a single "+N more" line.
MAX_ITEMS_PER_RUN = 20

# How many "seen" item keys to remember across runs, so an item doesn't get
# re-summarized if the list ever returns overlapping entries. Bounded so the
# snapshot file doesn't grow without limit.
MAX_REMEMBERED_IDS = 500


def _slack_ts_now() -> str:
    """Slack-style timestamp string (float seconds, 6 decimal places) for
    "now" -- used both as the first-run baseline and as the cursor advance
    after each delta fetch."""
    return f"{_hermes_now().timestamp():.6f}"


def _extract_items(payload: Any) -> List[Dict[str, Any]]:
    body = unwrap_payload(payload)
    if isinstance(body, dict):
        items = body.get("conversations") or body.get("items")
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict)]
    if isinstance(body, list):
        return [i for i in body if isinstance(i, dict)]
    return []


def _item_key(item: Dict[str, Any]) -> Optional[str]:
    item_id = item.get("id")
    ts = item.get("ts")
    if item_id and ts:
        return f"{item_id}:{ts}"
    return item_id or ts


def _truncate(text: str, limit: int = MAX_PREVIEW_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _summarize_item(item: Dict[str, Any]) -> str:
    kind = item.get("kind")
    name = item.get("name") or "unknown"
    preview = _truncate(str(item.get("text") or ""))
    if kind in _DM_KINDS:
        return f"DM {name}: {preview}"
    channel = item.get("channel")
    label = f"Mention {channel} ({name})" if channel else f"Mention {name}"
    return f"{label}: {preview}"


def fetch_delta(store: SurfaceStore) -> Optional[str]:
    """Fetch Slack's new unread direct messages and thread mentions since
    the stored last-seen timestamp and summarize them.

    Returns ``None`` when nothing changed (or on first run), else a compact
    multi-line summary capped at :data:`MAX_ITEMS_PER_RUN` items, with a
    "+N more" trailer when more items existed than the cap.
    """
    client = get_client()

    if store.is_first_run():
        # Establish the baseline timestamp only. Never report a diff here --
        # that would mean summarizing every unread DM/mention sitting in the
        # workspace at connect time as "new".
        store.set_cursor({"last_ts": _slack_ts_now()})
        return None

    last_ts = store.cursor.get("last_ts")
    params: Dict[str, Any] = {"oldest": last_ts} if last_ts else {}
    raw_result = client.execute_action(ACTION_LIST_CONVERSATIONS, params)
    items = _extract_items(raw_result)

    # Advance the cursor to "now" regardless of what came back -- `oldest`
    # is a lower bound, and re-using the exact latest item's `ts` risks
    # re-walking part of the window on the next tick; "now" never does.
    store.update_cursor(last_ts=_slack_ts_now())

    if not items:
        return None

    already_seen = set(store.state.get("seen_ids") or [])
    seen_now: List[str] = list(already_seen)
    new_items: List[Dict[str, Any]] = []
    for item in items:
        key = _item_key(item)
        if not key or key in already_seen:
            continue
        seen_now.append(key)
        new_items.append(item)

    store.update_state(seen_ids=seen_now[-MAX_REMEMBERED_IDS:])

    if not new_items:
        return None

    dm_count = sum(1 for it in new_items if it.get("kind") in _DM_KINDS)
    mention_count = len(new_items) - dm_count

    shown = new_items[:MAX_ITEMS_PER_RUN]
    extra = len(new_items) - len(shown)

    lines = [_summarize_item(it) for it in shown]
    if extra > 0:
        lines.append(f"+{extra} more")

    header = f"Slack: {dm_count} new direct message(s), {mention_count} thread mention(s)"
    return header + "\n" + "\n".join(f"  - {line}" for line in lines)
