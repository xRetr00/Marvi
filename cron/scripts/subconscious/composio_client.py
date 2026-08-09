"""Thin, lazily-imported wrapper over the Composio Python SDK.

Composio (https://composio.dev) is how Marvi gets account awareness (Gmail,
GitHub, ...). It is an OPTIONAL dependency -- nothing in this module, or
anything that imports it, pulls in the ``composio`` package at import time.
That mirrors the lazy-dependency convention used throughout the codebase
(see ``tools/lazy_deps.py``'s module docstring for the rationale): a user who
never touches Composio should never pay for it, and a broken/unavailable
Composio install must never take down anything else.

Registered in ``tools/lazy_deps.py``'s ``LAZY_DEPS`` allowlist as
``"integration.composio"`` (mirrors ``tools/presence/media_watcher.py``'s
``winsdk``/``presence.media_watcher`` pattern): the SDK auto-installs on
first real use (:meth:`ComposioClient._client`, via
:func:`_import_composio_sdk`) instead of just telling the user to run pip
themselves. :func:`is_sdk_installed` stays a cheap, install-free pin check
(used by passive status output like ``hermes composio list``) --
:func:`ensure_sdk_installed` is the function that actually triggers an
install attempt, and only ``hermes composio connect`` and the internal
SDK-usage seam call it.

Everything fetchers/CLI need from Composio goes through :class:`ComposioClient`
so the actual SDK call surface (which has shifted across Composio SDK major
versions) is isolated to one seam.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Pin used only as a LAST-RESORT fallback for the remediation hint text, if
# tools.lazy_deps itself is somehow unavailable. The real source of truth is
# tools/lazy_deps.py's LAZY_DEPS["integration.composio"] entry -- see
# _install_hint(), which reads the pin from there so the two can't drift.
COMPOSIO_PACKAGE_SPEC = "composio==0.15.0"


class ComposioUnavailable(RuntimeError):
    """The Composio SDK isn't installed. Optional dependency; Marvi degrades
    gracefully without it -- surfaces just report themselves unavailable."""


class ComposioAuthError(RuntimeError):
    """No API key configured, or Composio rejected it (401/403)."""


class ComposioRateLimited(RuntimeError):
    """Composio returned 429. Callers should back off, not treat as fatal."""

    def __init__(self, message: str = "Composio rate limited (429)", *, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class ComposioTransientError(RuntimeError):
    """5xx / network-ish failure. Worth a backoff+retry, not a hard failure."""


class ComposioSyncTokenExpired(RuntimeError):
    """410 GONE -- a delta cursor (Calendar ``syncToken`` and friends) is no
    longer valid server-side and the surface must reset its cursor and
    re-baseline, exactly like a first run. This is a distinct case from
    :class:`ComposioTransientError`: it is NOT "retry the same request
    later", it is "the incremental-sync window is gone, start a new one" --
    so fetchers that use a sync-token-style cursor catch this specifically
    instead of letting it fall into the generic backoff path."""


def install_hint() -> str:
    """Public remediation text for "Composio SDK not installed" -- used both
    internally (`ComposioUnavailable`) and by the CLI (`hermes composio
    connect`) so the two never drift."""
    return _install_hint()


def _install_hint() -> str:
    try:
        from tools.lazy_deps import feature_install_command

        manual_cmd = feature_install_command("integration.composio")
    except Exception:
        manual_cmd = None
    if not manual_cmd:
        manual_cmd = f"uv pip install {COMPOSIO_PACKAGE_SPEC!r}"
    return (
        "Composio SDK not installed. Install it with:\n"
        f"  {manual_cmd}\n"
        f"  (or: pip install {COMPOSIO_PACKAGE_SPEC})\n"
        "Composio powers Marvi's account-awareness sync (Gmail, GitHub, etc.) "
        "and is entirely optional -- Marvi runs fine without it until you "
        "run `hermes composio connect <app>`, which now installs it "
        "automatically -- this hint is only shown if that auto-install "
        "itself couldn't proceed (see the reason above)."
    )


def is_sdk_installed() -> bool:
    """Cheap pinned-version check that never raises and never triggers an install
    -- used by passive CLI status output (``hermes composio list``). Callers
    that actually need the SDK available should go through
    :func:`ensure_sdk_installed` (auto-installs on demand) instead."""
    if "composio" not in sys.modules:
        try:
            import composio  # type: ignore  # noqa: F401
        except ImportError:
            return False
    from tools.lazy_deps import is_available

    return is_available("integration.composio")


def ensure_sdk_installed(*, prompt: bool = False) -> bool:
    """Make sure the Composio SDK is importable, auto-installing it via
    ``tools.lazy_deps`` (feature ``integration.composio``) when missing --
    the SAME lazy-install convention every other optional backend in this
    codebase follows (see ``tools/lazy_deps.py``'s module docstring),
    instead of just telling the user to run pip themselves.

    ``prompt``: forwarded to ``tools.lazy_deps.ensure`` -- True for
    interactive CLI call sites (``hermes composio connect``, which already
    has a terminal to ask "install now? [Y/n]" on), False for unattended
    call sites (the subconscious cron fetchers, ``hermes composio list``'s
    passive status probe never calls this at all).

    Returns True on success. Raises :class:`ComposioUnavailable` (never a
    bare ``ImportError`` or ``lazy_deps.FeatureUnavailable``) when the
    install is declined/disabled/fails -- callers that just want a
    best-effort probe should catch that and degrade, matching every other
    ``ComposioUnavailable`` call site in this module.
    """
    if is_sdk_installed():
        return True

    from tools.lazy_deps import FeatureUnavailable, ensure

    try:
        ensure("integration.composio", prompt=prompt)
    except FeatureUnavailable as exc:
        raise ComposioUnavailable(str(exc)) from exc
    except Exception as exc:
        logger.debug("lazy_deps.ensure failed for integration.composio", exc_info=True)
        raise ComposioUnavailable(_install_hint()) from exc

    if not is_sdk_installed():
        # ensure() reported success but the package still isn't importable
        # (see its own post-install verification) -- surface the generic
        # hint since there's no more specific reason to report here.
        raise ComposioUnavailable(_install_hint())
    return True


def _import_composio_sdk():
    """Import the ``composio`` package, auto-installing it on first use when
    missing (via :func:`ensure_sdk_installed`, ``prompt=False`` -- this is
    the internal SDK-usage seam :class:`ComposioClient` calls from
    unattended contexts, not an interactive CLI command). Raises
    :class:`ComposioUnavailable` with a clear remediation hint on failure --
    never a bare ``ImportError``."""
    ensure_sdk_installed(prompt=False)  # raises ComposioUnavailable on failure

    if "composio" in sys.modules:
        return sys.modules["composio"]
    import composio  # type: ignore  # just verified importable above
    return composio


def get_api_key(config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Resolve the Composio credential from the secret store.

    A legacy ``composio.api_key`` is still accepted for compatibility, but a
    real config read migrates it to ``.env`` and installs the official
    Composio Connect MCP entry.
    """
    loaded_from_disk = config is None
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    composio_cfg = (config or {}).get("composio") if isinstance(config, dict) else None
    legacy = composio_cfg.get("api_key") if isinstance(composio_cfg, dict) else None
    if loaded_from_disk and isinstance(legacy, str) and legacy.strip():
        try:
            from hermes_cli.composio_config import configure_composio_connect

            configure_composio_connect()
        except Exception:
            logger.warning("Could not migrate the legacy Composio key out of config.yaml", exc_info=True)
    try:
        from hermes_cli.config import get_env_value_prefer_dotenv

        env_key = str(get_env_value_prefer_dotenv("COMPOSIO_API_KEY") or "").strip()
    except Exception:
        env_key = os.environ.get("COMPOSIO_API_KEY", "").strip()
    return env_key or (legacy.strip() if isinstance(legacy, str) and legacy.strip() else None)


class ComposioClient:
    """Thin wrapper isolating callers from the Composio SDK's exact call
    surface, with SDK exceptions normalized into the module's error types
    so fetchers can react (retry/backoff/skip) without knowing SDK internals.
    """

    def __init__(self, api_key: str):
        if not api_key:
            raise ComposioAuthError(
                "No Composio API key configured. Save COMPOSIO_API_KEY in "
                "the Marvi secret store (or run `hermes composio connect`)."
            )
        self._api_key = api_key
        self._sdk_client = None

    def _client(self):
        if self._sdk_client is None:
            composio = _import_composio_sdk()
            self._sdk_client = composio.Composio(api_key=self._api_key)
        return self._sdk_client

    @staticmethod
    def _classify_and_raise(exc: Exception) -> None:
        """Re-raise ``exc`` as one of the module's typed errors when it looks
        like an HTTP rate-limit/auth/server error. Different Composio SDK
        versions surface the status code in different places (httpx response,
        a bare ``status_code`` attribute, ...); check the common spots rather
        than depending on one exact exception class. No-op (returns
        normally) if the exception doesn't look HTTP-shaped -- caller wraps
        it as a generic :class:`ComposioTransientError`.
        """
        status = getattr(exc, "status_code", None) or getattr(
            getattr(exc, "response", None), "status_code", None
        )
        if status == 429:
            retry_after = None
            headers = getattr(getattr(exc, "response", None), "headers", None)
            if headers:
                try:
                    retry_after = float(headers.get("Retry-After"))
                except (TypeError, ValueError):
                    retry_after = None
            raise ComposioRateLimited(retry_after=retry_after) from exc
        if status in (401, 403):
            raise ComposioAuthError(f"Composio rejected the API key ({status}).") from exc
        if status == 410:
            raise ComposioSyncTokenExpired(
                "Composio reported the delta cursor as gone (410); it must be reset and re-baselined."
            ) from exc
        if isinstance(status, int) and status >= 500:
            raise ComposioTransientError(f"Composio server error ({status}).") from exc

    @staticmethod
    def _to_dict(result: Any) -> Dict[str, Any]:
        if isinstance(result, dict):
            return result
        to_dict = getattr(result, "model_dump", None) or getattr(result, "dict", None)
        if callable(to_dict):
            try:
                return to_dict()
            except Exception:
                pass
        return {"data": result}

    @staticmethod
    def _call_with_optional_user_id(method, *, user_id: str = "default", **kwargs):
        """Call SDK resources across versions that added/removed ``user_id``."""
        try:
            return method(user_id=user_id, **kwargs)
        except TypeError as exc:
            if "user_id" not in str(exc):
                raise
            return method(**kwargs)

    def execute_action(
        self,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        user_id: str = "default",
    ) -> Dict[str, Any]:
        """Execute a single Composio tool/action (e.g. ``GMAIL_LIST_MESSAGES``,
        ``GITHUB_LIST_NOTIFICATIONS_FOR_THE_AUTHENTICATED_USER``) and return its
        result payload as a dict.

        Raises :class:`ComposioRateLimited` / :class:`ComposioAuthError` /
        :class:`ComposioTransientError` / :class:`ComposioSyncTokenExpired`
        on failure so fetchers can decide whether to back off and retry
        later (or, for the sync-token case, reset their cursor and
        re-baseline) rather than crash the tick.
        """
        client = self._client()
        try:
            tools = getattr(client, "tools", None)
            if tools is not None and hasattr(tools, "execute"):
                # Manual execution requires pinned per-toolkit versions or this
                # explicit opt-out; without one, every call fails with "Toolkit
                # version not specified" (SDK >= 0.17). Older SDKs without the
                # kwarg raise TypeError -> retry without it.
                try:
                    result = self._call_with_optional_user_id(
                        tools.execute,
                        user_id=user_id,
                        slug=action,
                        arguments=params or {},
                        dangerously_skip_version_check=True,
                    )
                except TypeError as exc:
                    if "dangerously_skip_version_check" not in str(exc):
                        raise
                    result = self._call_with_optional_user_id(
                        tools.execute,
                        user_id=user_id,
                        slug=action,
                        arguments=params or {},
                    )
            else:
                actions = getattr(client, "actions")
                result = self._call_with_optional_user_id(
                    actions.execute,
                    user_id=user_id,
                    action=action,
                    params=params or {},
                )
        except (
            ComposioRateLimited,
            ComposioAuthError,
            ComposioTransientError,
            ComposioSyncTokenExpired,
        ):
            raise
        except Exception as e:
            self._classify_and_raise(e)
            raise ComposioTransientError(f"Composio action {action!r} failed: {e}") from e
        return self._to_dict(result)

    def initiate_connection(self, app: str, *, user_id: str = "default") -> Dict[str, Any]:
        """Start (or resume) a connected-account auth flow for ``app``.

        Returns a dict with at minimum a ``status`` key
        (``"active"``/``"pending"``/``"failed"``) and, when a manual
        authorization step is needed, a ``redirect_url``.
        """
        client = self._client()
        try:
            current = self.get_connection_status(app, user_id=user_id)
            if current["connected"]:
                return current
            configs = client.auth_configs.list(toolkit_slug=app)
            enabled = [
                item
                for item in configs.items
                if str(getattr(item, "status", "")).upper() == "ENABLED"
            ]
            auth_config = next(
                (item for item in enabled if getattr(item, "is_composio_managed", False)),
                enabled[0] if enabled else None,
            )
            if auth_config is None:
                auth_config = client.auth_configs.create(
                    toolkit=app,
                    options={"type": "use_composio_managed_auth", "name": app},
                )
            result = client.connected_accounts.link(user_id, auth_config.id)
        except (ComposioRateLimited, ComposioAuthError, ComposioTransientError):
            raise
        except Exception as e:
            self._classify_and_raise(e)
            raise ComposioTransientError(
                f"Could not initiate Composio connection for {app!r}: {e}"
            ) from e
        return {
            "id": getattr(result, "id", None),
            "status": getattr(result, "status", "pending"),
            "redirect_url": getattr(result, "redirect_url", None),
        }

    def get_connection_status(self, app: str, *, user_id: str = "default") -> Dict[str, Any]:
        """Return ``{"connected": bool, "status": str}`` for ``app``."""
        client = self._client()
        try:
            connect = getattr(client, "connected_accounts", None) or getattr(client, "connections", None)
            result = connect.list(user_ids=[user_id], toolkit_slugs=[app])
        except (ComposioRateLimited, ComposioAuthError, ComposioTransientError):
            raise
        except Exception as e:
            self._classify_and_raise(e)
            raise ComposioTransientError(
                f"Could not fetch Composio connection status for {app!r}: {e}"
            ) from e
        items = getattr(result, "items", None)
        if items is None and isinstance(result, dict):
            items = result.get("items")
        items = list(items or [])
        active = next(
            (item for item in items if str(getattr(item, "status", "")).upper() == "ACTIVE"),
            None,
        )
        account = active or (items[0] if items else None)
        status = getattr(account, "status", "not connected")
        connected = str(status).lower() in {"active", "connected", "success"}
        return {"connected": connected, "status": str(status)}

    def list_connections(self, *, user_id: str = "default") -> Dict[str, Dict[str, Any]]:
        """Return the latest connection status for each toolkit."""
        client = self._client()
        try:
            result = client.connected_accounts.list(user_ids=[user_id], limit=100)
        except (ComposioRateLimited, ComposioAuthError, ComposioTransientError):
            raise
        except Exception as e:
            self._classify_and_raise(e)
            raise ComposioTransientError(
                f"Could not list Composio connections: {e}"
            ) from e

        connections: Dict[str, Dict[str, Any]] = {}
        for item in list(getattr(result, "items", None) or []):
            toolkit = getattr(item, "toolkit", None)
            slug = getattr(toolkit, "slug", None)
            if not slug:
                continue
            status = str(getattr(item, "status", "unknown"))
            current = connections.get(slug)
            connected = status.lower() in {"active", "connected", "success"}
            if current is None or connected:
                connections[slug] = {"connected": connected, "status": status}
        return connections

    def verify_auth(self) -> bool:
        """Cheap sanity check that the configured API key actually works.

        Raises :class:`ComposioAuthError` on rejection so callers can show a
        clear "your API key doesn't work" message rather than a stack trace.
        """
        client = self._client()
        try:
            connect = getattr(client, "connected_accounts", None) or getattr(client, "connections", None)
            if connect is not None and hasattr(connect, "list"):
                connect.list(limit=1)
            return True
        except (ComposioRateLimited, ComposioAuthError, ComposioTransientError):
            raise
        except Exception as e:
            self._classify_and_raise(e)
            raise ComposioTransientError(f"Composio auth check could not run: {e}") from e


def unwrap_payload(payload: Any) -> Any:
    """Peel Composio's outer action-result envelope off a raw action result.

    Composio action results have been wrapped under a ``response_data`` or
    ``data`` key depending on SDK version; fetchers call this once instead of
    each hand-rolling the same peel, and get back whatever's inside -- the
    actual provider API shape (Gmail/GitHub/...).
    """
    if isinstance(payload, dict):
        for key in ("response_data", "data"):
            inner = payload.get(key)
            if isinstance(inner, (dict, list)):
                return inner
    return payload


def get_client(api_key: Optional[str] = None) -> ComposioClient:
    """Build a :class:`ComposioClient`, resolving the API key from the secret
    store when not given explicitly.

    Raises :class:`ComposioAuthError` if no key is configured anywhere.
    """
    key = api_key or get_api_key()
    if not key:
        raise ComposioAuthError(
            "No Composio API key configured. Run `hermes composio connect <app>` "
            "or save COMPOSIO_API_KEY in the Marvi secret store."
        )
    return ComposioClient(key)
