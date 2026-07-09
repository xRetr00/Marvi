"""Gmail delta fetcher -- Composio smart sync for Marvi.

Uses Gmail's History API semantics (via Composio) instead of listing the
inbox: the cursor is a Gmail ``historyId``, and each fetch asks "what
changed since this historyId" rather than re-scanning every message.

First run establishes the baseline ``historyId`` and reports nothing changed
-- per Contract 1 / Workstream C task 3, a fresh surface must never dump the
whole inbox on its first tick.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from cron.scripts.subconscious.composio_client import get_client, unwrap_payload
from cron.scripts.subconscious.snapshot_store import SurfaceStore

APP = "gmail"

# Composio Gmail toolkit action slugs, kept as module constants so a future
# SDK rename is a one-line change instead of a find-and-replace.
ACTION_GET_PROFILE = "GMAIL_GET_PROFILE"  # cheap call; returns current historyId
ACTION_LIST_HISTORY = "GMAIL_LIST_HISTORY"  # delta fetch: changes since a historyId
ACTION_GET_MESSAGE = "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID"

# Cap on how many new messages get an individual detail fetch + summary line.
# A mailing-list flood shouldn't turn one diff into a wall of text (and,
# more importantly, shouldn't turn one tick into dozens of API calls).
MAX_SUMMARIZED_MESSAGES = 10

# How many message ids to remember across runs, so a message doesn't get
# re-summarized if history-list ever returns overlapping entries. Bounded so
# the snapshot file doesn't grow without limit.
MAX_REMEMBERED_IDS = 500


def _extract_history_id(payload: Dict[str, Any]) -> Optional[str]:
    body = unwrap_payload(payload)
    if isinstance(body, dict) and body.get("historyId"):
        return str(body["historyId"])
    return None


def _extract_new_message_ids(history_payload: Dict[str, Any]) -> List[str]:
    """Pull newly-added message ids out of a Gmail ``history.list`` result."""
    body = unwrap_payload(history_payload)
    history = body.get("history") if isinstance(body, dict) else None
    ids: List[str] = []
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        for added in entry.get("messagesAdded") or []:
            msg = added.get("message") if isinstance(added, dict) else None
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            if msg_id and msg_id not in ids:
                ids.append(msg_id)
    return ids


def _summarize_message(message_payload: Dict[str, Any]) -> Optional[str]:
    """Return "sender: subject" for an important/unread message, else None."""
    body = unwrap_payload(message_payload)
    if not isinstance(body, dict):
        return None
    label_ids = body.get("labelIds") or []
    if "IMPORTANT" not in label_ids and "UNREAD" not in label_ids:
        return None
    headers: Dict[str, str] = {}
    payload = body.get("payload") or {}
    for h in payload.get("headers") or []:
        name = str(h.get("name", "")).strip().lower()
        if name in ("from", "subject") and name not in headers:
            headers[name] = h.get("value") or ""
    sender = headers.get("from") or "unknown sender"
    subject = headers.get("subject") or "(no subject)"
    return f"{sender}: {subject}"


def fetch_delta(store: SurfaceStore) -> Optional[str]:
    """Fetch Gmail's delta since the stored ``historyId`` and summarize it.

    Returns ``None`` when nothing changed (or on first run), else a compact
    multi-line summary of new important/unread messages.
    """
    client = get_client()

    history_id = store.cursor.get("history_id")
    if store.is_first_run() or not history_id:
        # Establish (or repair) the baseline historyId only. Never report a
        # diff here -- that would mean summarizing the whole inbox as "new",
        # exactly the OpenHuman-style anti-pattern this sync avoids.
        profile = client.execute_action(ACTION_GET_PROFILE, {})
        new_history_id = _extract_history_id(profile)
        if new_history_id:
            store.set_cursor({"history_id": new_history_id})
        return None

    history_payload = client.execute_action(
        ACTION_LIST_HISTORY,
        {"history_types": ["messageAdded"], "start_history_id": history_id},
    )
    new_message_ids = _extract_new_message_ids(history_payload)

    # Advance the cursor past this window regardless of whether any message
    # turns out "important enough" to summarize -- the delta window must
    # never be re-walked on the next tick.
    new_history_id = _extract_history_id(history_payload) or history_id
    store.set_cursor({"history_id": new_history_id})

    if not new_message_ids:
        return None

    already_seen = set(store.state.get("seen_message_ids") or [])
    seen_now: List[str] = list(already_seen)
    lines: List[str] = []
    for msg_id in new_message_ids[:MAX_SUMMARIZED_MESSAGES]:
        if msg_id in already_seen:
            continue
        seen_now.append(msg_id)
        message = client.execute_action(ACTION_GET_MESSAGE, {"message_id": msg_id})
        summary = _summarize_message(message)
        if summary:
            lines.append(summary)

    store.update_state(seen_message_ids=seen_now[-MAX_REMEMBERED_IDS:])

    if not lines:
        return None

    header = f"Gmail: {len(lines)} new important/unread message(s)"
    return header + "\n" + "\n".join(f"  - {line}" for line in lines)
