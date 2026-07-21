"""Gmail document collector -- pulls durable reference material (PDF/DOCX/
TXT/MD attachments, long-form newsletter/report bodies) into the Brain.

Distinct from ``cron/scripts/subconscious/gmail.py``'s delta-notification
fetcher (which summarizes new important/unread mail for the subconscious's
narrative): this module has its own cursor
(``HERMES_HOME/brain/collectors/email.json``, via
``tools/brain/collectors/state.py``) and its own purpose -- durable
DOCUMENTS worth indexing, not a "what changed" notification. It reuses the
SAME read-only Composio client seam
(``cron/scripts/subconscious/composio_client.py``) but never touches the
other fetcher's snapshot state, and is safe to run alongside it.

Runs inside the "Brain indexer" cron job, after the discovery pass (see
``tools/brain/indexer.py::run_brain_indexer_job``). Guarded: an unconfigured
Composio (no API key saved) is a clean, reported skip -- never an exception
that would take the indexer down.
"""

from __future__ import annotations

import base64
from typing import Any, Callable, Dict, List, Optional

from cron.scripts.subconscious.composio_client import (
    ComposioAuthError,
    ComposioUnavailable,
    get_api_key,
    get_client,
    unwrap_payload,
)
from hermes_time import now as _hermes_now
from tools.brain.collected import write_collected_document
from tools.brain.collectors.state import load_collector_state, save_collector_state

SOURCE = "email"
STATE_NAME = "email"

# Composio Gmail toolkit action slugs. Best-effort names mirroring
# cron/scripts/subconscious/gmail.py's convention (kept as module constants
# so a future SDK/toolkit rename is a one-line change).
ACTION_LIST_MESSAGES = "GMAIL_LIST_MESSAGES"
ACTION_GET_MESSAGE = "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID"
ACTION_GET_ATTACHMENT = "GMAIL_GET_ATTACHMENT"

MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
DOC_ATTACHMENT_EXTENSIONS = frozenset({".pdf", ".docx", ".txt", ".md"})
LONG_BODY_MIN_CHARS = 2000
MAX_MESSAGES_PER_RUN = 20
MAX_REMEMBERED_IDS = 500


def _headers_dict(payload_part: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for h in (payload_part or {}).get("headers") or []:
        name = str(h.get("name", "")).strip().lower()
        if name and name not in out:
            out[name] = h.get("value") or ""
    return out


def _iter_parts(payload_part: Dict[str, Any]):
    if not isinstance(payload_part, dict):
        return
    yield payload_part
    for sub in payload_part.get("parts") or []:
        yield from _iter_parts(sub)


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode_body_text(part: Dict[str, Any]) -> str:
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    try:
        return _b64url_decode(data).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _message_body_text(message: Dict[str, Any]) -> str:
    payload = message.get("payload") or {}
    chunks = [
        _decode_body_text(part)
        for part in _iter_parts(payload)
        if str(part.get("mimeType") or "") == "text/plain"
    ]
    return "\n".join(c for c in chunks if c)


def _attachment_parts(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    payload = message.get("payload") or {}
    return [
        part
        for part in _iter_parts(payload)
        if part.get("filename") and (part.get("body") or {}).get("attachmentId")
    ]


def _attachment_extension(filename: str) -> str:
    return ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""


def _attachment_text(filename: str, raw: bytes) -> str:
    ext = _attachment_extension(filename)
    if ext in {".txt", ".md"}:
        return raw.decode("utf-8", errors="replace")
    if ext == ".pdf":
        try:
            import io

            from pypdf import PdfReader

            return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(raw)).pages)
        except Exception:
            return ""
    if ext == ".docx":
        import os
        import tempfile

        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".docx")
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            from tools.read_extract import extract_document_text

            return extract_document_text(tmp_path)
        except Exception:
            return ""
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    return ""


def _since_query(since_iso: str) -> str:
    # Gmail search syntax wants YYYY/MM/DD.
    return f"after:{since_iso[:10].replace('-', '/')}"


def collect_email_documents(
    *,
    client: Any = None,
    cursor_state: Optional[Dict[str, Any]] = None,
    save_cursor_state: Optional[Callable[[Dict[str, Any]], None]] = None,
    max_messages: int = MAX_MESSAGES_PER_RUN,
) -> Dict[str, Any]:
    """Collect document attachments + long-form bodies from recent Gmail
    messages into the Brain.

    ``client``/``cursor_state``/``save_cursor_state`` are injection points
    for tests (a fake client, an in-memory cursor dict) -- production
    callers leave them unset and get the real Composio client plus the
    on-disk cursor file. Returns a summary dict; a not-configured Composio
    is reported as ``{"ok": True, "skipped": "composio_not_configured", ...}``,
    never raised.
    """
    if client is None:
        if not get_api_key():
            return {"ok": True, "skipped": "composio_not_configured", "collected": 0}
        try:
            client = get_client()
        except (ComposioAuthError, ComposioUnavailable) as exc:
            return {"ok": True, "skipped": f"composio_unavailable: {exc}", "collected": 0}

    if cursor_state is None or save_cursor_state is None:
        cursor_state = load_collector_state(STATE_NAME)
        save_cursor_state = lambda state: save_collector_state(STATE_NAME, state)  # noqa: E731

    state = dict(cursor_state)
    since = state.get("since")

    if not since:
        # First run: establish the baseline only -- never dump the whole
        # mailbox history as "new" documents on the very first pass (matches
        # the "never dump the inbox on first run" convention every other
        # Composio surface fetcher in this codebase follows).
        state["since"] = _hermes_now().isoformat()
        save_cursor_state(state)
        return {"ok": True, "collected": 0, "first_run": True}

    try:
        list_payload = client.execute_action(
            ACTION_LIST_MESSAGES, {"query": _since_query(since), "max_results": max_messages}
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "collected": 0}

    body = unwrap_payload(list_payload)
    message_stubs = (body.get("messages") if isinstance(body, dict) else None) or []

    already_seen = set(state.get("seen_ids") or [])
    seen_now = list(already_seen)
    collected = skipped = errors = 0

    for stub in message_stubs[:max_messages]:
        msg_id = stub.get("id") if isinstance(stub, dict) else None
        if not msg_id or msg_id in already_seen:
            continue
        try:
            message_payload = client.execute_action(ACTION_GET_MESSAGE, {"message_id": msg_id})
        except Exception:
            errors += 1
            continue
        message = unwrap_payload(message_payload)
        if not isinstance(message, dict):
            continue
        seen_now.append(msg_id)

        headers = _headers_dict(message.get("payload") or {})
        subject = headers.get("subject") or "(no subject)"
        sender = headers.get("from") or "unknown sender"

        body_text = _message_body_text(message)
        if len(body_text) >= LONG_BODY_MIN_CHARS:
            result = write_collected_document(
                source=SOURCE, title=f"{subject} ({sender})", text=body_text, ref=f"gmail-body:{msg_id}"
            )
            collected += 1 if result.get("written") else 0
            skipped += 0 if result.get("written") else 1

        for part in _attachment_parts(message):
            filename = str(part.get("filename") or "")
            if _attachment_extension(filename) not in DOC_ATTACHMENT_EXTENSIONS:
                continue
            size = int((part.get("body") or {}).get("size") or 0)
            if size > MAX_ATTACHMENT_BYTES:
                continue
            attachment_id = (part.get("body") or {}).get("attachmentId")
            try:
                att_payload = client.execute_action(
                    ACTION_GET_ATTACHMENT, {"message_id": msg_id, "attachment_id": attachment_id}
                )
            except Exception:
                errors += 1
                continue
            att_body = unwrap_payload(att_payload)
            data = att_body.get("data") if isinstance(att_body, dict) else None
            if not data:
                continue
            try:
                raw = _b64url_decode(data)
            except Exception:
                continue
            text = _attachment_text(filename, raw)
            if not text.strip():
                continue
            result = write_collected_document(
                source=SOURCE,
                title=f"{filename} ({subject})",
                text=text,
                ref=f"gmail-attachment:{msg_id}:{filename}",
            )
            collected += 1 if result.get("written") else 0
            skipped += 0 if result.get("written") else 1

    state["seen_ids"] = seen_now[-MAX_REMEMBERED_IDS:]
    state["since"] = _hermes_now().isoformat()
    save_cursor_state(state)
    return {
        "ok": True,
        "collected": collected,
        "skipped": skipped,
        "errors": errors,
        "messages_scanned": len(message_stubs),
    }
