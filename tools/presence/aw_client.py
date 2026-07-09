"""Thin REST client for a local ActivityWatch server (http://localhost:5600).

ActivityWatch (https://activitywatch.net) is the approved desktop collector
(see docs/superpowers/specs/2026-07-09-marvi-subconscious-presence-design.md,
Contract 3 / Workstream B). This client is intentionally minimal -- it wraps
the handful of REST endpoints Marvi's presence tools need:

  - GET  /api/0/buckets/                       list buckets
  - GET  /api/0/buckets/<id>/events             query events
  - PUT  /api/0/buckets/<id>                    create a bucket (idempotent)
  - POST /api/0/buckets/<id>/heartbeat          heartbeat (merge-on-write)

Every network call is wrapped so a missing/unreachable AW server never
raises into a tool handler -- callers should check :meth:`is_available`
first (or catch :class:`AWUnavailableError`) and degrade with a clear
"presence unavailable -- ActivityWatch not running" message.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:5600/api/0"
DEFAULT_TIMEOUT_SECONDS = 2.5
# How long an is_available() verdict is cached before re-probing. Keeps
# repeated desktop_context calls within one turn cheap without pinning a
# stale "down" verdict for long.
_AVAILABILITY_TTL_SECONDS = 10.0

UNAVAILABLE_MESSAGE = "presence unavailable — ActivityWatch not running"


class AWUnavailableError(Exception):
    """Raised when the ActivityWatch server cannot be reached or errors out."""


def _base_url() -> str:
    return os.environ.get("HERMES_AW_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


class AWClient:
    """Minimal ActivityWatch REST client. Never raises out of ``is_available``."""

    def __init__(self, base_url: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.base_url = (base_url or _base_url()).rstrip("/")
        self.timeout = timeout
        self._available_cache: Optional[bool] = None
        self._available_cache_at: float = 0.0

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self, *, force: bool = False) -> bool:
        """Return True iff the AW server responds to a cheap probe.

        Result is cached for ``_AVAILABILITY_TTL_SECONDS`` so repeated calls
        within one desktop_context turn don't each pay a network round trip.
        """
        now = time.monotonic()
        if (
            not force
            and self._available_cache is not None
            and (now - self._available_cache_at) < _AVAILABILITY_TTL_SECONDS
        ):
            return self._available_cache

        ok = False
        try:
            import requests

            resp = requests.get(f"{self.base_url}/buckets/", timeout=self.timeout)
            ok = resp.status_code == 200
        except Exception as exc:
            logger.debug("ActivityWatch probe failed: %s", exc)
            ok = False

        self._available_cache = ok
        self._available_cache_at = now
        return ok

    # ------------------------------------------------------------------
    # Low-level HTTP helpers
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, *, params: Optional[dict] = None,
                 json_body: Optional[dict] = None) -> Any:
        try:
            import requests
        except ImportError as exc:  # pragma: no cover - requests is a core dep
            raise AWUnavailableError(f"{UNAVAILABLE_MESSAGE} (requests not installed: {exc})") from exc

        url = f"{self.base_url}{path}"
        try:
            resp = requests.request(
                method, url, params=params, json=json_body, timeout=self.timeout,
            )
            resp.raise_for_status()
        except Exception as exc:
            raise AWUnavailableError(f"{UNAVAILABLE_MESSAGE} ({exc})") from exc

        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ------------------------------------------------------------------
    # Buckets
    # ------------------------------------------------------------------

    def list_buckets(self) -> Dict[str, Any]:
        """Return ``{bucket_id: bucket_metadata}`` for every known bucket."""
        result = self._request("GET", "/buckets/")
        return result if isinstance(result, dict) else {}

    def find_bucket_id(self, prefix: str) -> Optional[str]:
        """Return the first bucket id starting with ``prefix``, or None.

        AW bucket ids are conventionally ``<watcher-name>_<hostname>``; we
        don't try to reconstruct the exact hostname suffix, just match by
        watcher-name prefix so this works across machines/locales.
        """
        try:
            buckets = self.list_buckets()
        except AWUnavailableError:
            return None
        for bucket_id in buckets:
            if bucket_id.startswith(prefix):
                return bucket_id
        return None

    def create_bucket(self, bucket_id: str, event_type: str, *,
                       client_name: str = "marvi-presence", hostname: Optional[str] = None) -> None:
        """Create a bucket if it doesn't already exist. Idempotent — AW's
        PUT create endpoint is a no-op (200) when the bucket already exists."""
        import socket

        self._request(
            "PUT",
            f"/buckets/{bucket_id}",
            json_body={
                "client": client_name,
                "type": event_type,
                "hostname": hostname or socket.gethostname(),
            },
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def get_events(self, bucket_id: str, *, start: Optional[str] = None,
                    end: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Return events for a bucket, newest first (AW's default order).

        ``start``/``end`` are ISO-8601 timestamps (AW accepts either bound
        alone). Returns ``[]`` when the bucket is empty or missing.
        """
        params: Dict[str, Any] = {"limit": limit}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        result = self._request("GET", f"/buckets/{bucket_id}/events", params=params)
        return result if isinstance(result, list) else []

    def heartbeat(self, bucket_id: str, data: Dict[str, Any], *, pulsetime: float) -> None:
        """Post a heartbeat event. AW merges it into the previous event when
        the data matches and the gap is under ``pulsetime`` seconds -- the
        standard AW watcher pattern for "still doing the same thing"."""
        from datetime import datetime, timezone

        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration": 0,
            "data": data,
        }
        self._request(
            "POST",
            f"/buckets/{bucket_id}/heartbeat",
            params={"pulsetime": pulsetime},
            json_body=event,
        )

    # ------------------------------------------------------------------
    # Convenience accessors used by desktop_context / goblin
    # ------------------------------------------------------------------

    def get_afk_state(self, *, hostname_hint: Optional[str] = None) -> Optional[str]:
        """Return ``"afk"`` / ``"not-afk"`` from the latest aw-watcher-afk
        event, or None when unavailable/unknown."""
        bucket_id = self.find_bucket_id("aw-watcher-afk")
        if not bucket_id:
            return None
        try:
            events = self.get_events(bucket_id, limit=1)
        except AWUnavailableError:
            return None
        if not events:
            return None
        data = events[0].get("data") or {}
        status = data.get("status")
        return str(status) if status else None

    def get_current_window(self) -> Optional[Dict[str, Any]]:
        """Return the most recent aw-watcher-window event's data (app/title),
        or None when unavailable/empty."""
        bucket_id = self.find_bucket_id("aw-watcher-window")
        if not bucket_id:
            return None
        try:
            events = self.get_events(bucket_id, limit=1)
        except AWUnavailableError:
            return None
        if not events:
            return None
        return events[0]

    def get_current_media(self) -> Optional[Dict[str, Any]]:
        """Return the most recent aw-watcher-media event, or None."""
        bucket_id = self.find_bucket_id("aw-watcher-media")
        if not bucket_id:
            return None
        try:
            events = self.get_events(bucket_id, limit=1)
        except AWUnavailableError:
            return None
        if not events:
            return None
        return events[0]


# Module-level default client, mirroring the rest of tools/'s "singleton
# handle + optionally construct your own" convention (e.g. process_registry).
aw_client = AWClient()
