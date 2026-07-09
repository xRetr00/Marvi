"""GitHub notifications delta fetcher -- Composio smart sync for Marvi.

Uses GitHub's notifications ``since`` cursor plus (when Composio exposes it)
ETag-based conditional requests: a quiet interval costs a cheap
not-modified check instead of pulling the full notification list.

First run establishes the baseline ``since`` cursor at "now" and reports
nothing changed -- everything already sitting in the notifications inbox at
connect time is pre-existing noise, not a new delta to surface.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from cron.scripts.subconscious.composio_client import get_client, unwrap_payload
from cron.scripts.subconscious.snapshot_store import SurfaceStore
from hermes_time import now as _hermes_now

APP = "github"

# Composio GitHub toolkit action slug for notifications.
ACTION_LIST_NOTIFICATIONS = "GITHUB_LIST_NOTIFICATIONS_FOR_THE_AUTHENTICATED_USER"

# Cap on how many notifications get summarized in one diff -- a big CI-noise
# burst shouldn't turn one tick into a wall of text.
MAX_SUMMARIZED_NOTIFICATIONS = 15

MAX_REMEMBERED_IDS = 500


def _extract_notifications(payload: Any) -> List[Dict[str, Any]]:
    body = unwrap_payload(payload)
    if isinstance(body, list):
        return [n for n in body if isinstance(n, dict)]
    if isinstance(body, dict):
        items = body.get("notifications") or body.get("items")
        if isinstance(items, list):
            return [n for n in items if isinstance(n, dict)]
    return []


def _summarize_notification(n: Dict[str, Any]) -> Optional[str]:
    subject = n.get("subject") if isinstance(n.get("subject"), dict) else {}
    title = subject.get("title") or "(untitled)"
    ntype = subject.get("type") or n.get("reason") or "notification"
    repo_info = n.get("repository") if isinstance(n.get("repository"), dict) else {}
    repo = repo_info.get("full_name") or "unknown/repo"
    return f"{repo} [{ntype}] {title}"


def fetch_delta(store: SurfaceStore) -> Optional[str]:
    """Fetch new GitHub notifications since the stored cursor and summarize.

    Returns ``None`` when nothing changed (or on first run), else a compact
    multi-line summary of new notifications.
    """
    client = get_client()

    if store.is_first_run():
        store.set_cursor({"since": _hermes_now().isoformat(), "etag": None})
        return None

    since = store.cursor.get("since")
    etag = store.cursor.get("etag")
    params: Dict[str, Any] = {"all": False, "participating": False}
    if since:
        params["since"] = since
    if etag:
        # Composio exposes conditional-request semantics on some action
        # versions via an explicit param; harmless no-op where it doesn't,
        # in which case we fall back to the `since` filter alone.
        params["if_none_match"] = etag

    raw_result = client.execute_action(ACTION_LIST_NOTIFICATIONS, params)

    # A conditional-request hit -- nothing changed since the stored ETag.
    # Composio surfaces this as an explicit flag on some action versions;
    # treat it as "no change" without spending a summarization pass.
    if isinstance(raw_result, dict) and raw_result.get("not_modified"):
        store.update_cursor(since=_hermes_now().isoformat())
        return None

    notifications = _extract_notifications(raw_result)
    new_etag = raw_result.get("etag") if isinstance(raw_result, dict) else None
    # Advance `since` to now regardless of result -- GitHub notifications
    # `since` is a lower bound, and re-using the exact API response's
    # Last-Modified header isn't reliably exposed through Composio, so "now"
    # is the safe conservative advance (never re-walks a window; the
    # seen-ids state below is what prevents duplicate summaries for
    # notifications that straddle a boundary).
    store.update_cursor(since=_hermes_now().isoformat(), etag=new_etag or etag)

    if not notifications:
        return None

    already_seen = set(store.state.get("seen_ids") or [])
    seen_now: List[str] = list(already_seen)
    lines: List[str] = []
    for n in notifications[:MAX_SUMMARIZED_NOTIFICATIONS]:
        nid = n.get("id")
        if nid and nid in already_seen:
            continue
        if nid:
            seen_now.append(nid)
        summary = _summarize_notification(n)
        if summary:
            lines.append(summary)

    store.update_state(seen_ids=seen_now[-MAX_REMEMBERED_IDS:])

    if not lines:
        return None

    header = f"GitHub: {len(lines)} new notification(s)"
    return header + "\n" + "\n".join(f"  - {line}" for line in lines)
