"""OS credential store seam for the uni_portal plugin — spec §1.3.

Marvi NEVER sees the Duzce University student-system password in plaintext
outside of the interactive ``hermes uni login`` capture -> store round-trip
(``hermes_cli/subcommands/uni.py``). Storage uses the Windows Credential
Manager (the OS credential store the spec calls out — the same mechanism
smart_room's own secrets are kept out of config.yaml with) via a thin
``ctypes`` wrapper around ``Advapi32.dll``'s CredWrite/CredRead/CredDelete —
no new third-party dependency (no ``keyring`` package in this repo today).

**Testability seam**: the three low-level functions ``_raw_write``,
``_raw_read``, ``_raw_delete`` are the only functions that touch the real
Windows API. Every public function above them (``store_credentials``,
``read_credentials``, ``delete_credentials``, ``has_credentials``) is a thin,
platform-independent wrapper — tests monkeypatch the three ``_raw_*``
functions with an in-memory fake dict instead of touching the real OS store
or requiring a Windows credential-manager round-trip in CI.
"""

from __future__ import annotations

import ctypes
import json
import logging
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# One generic-credential target name for the plugin's single stored account.
# Duzce's student system does not support multiple simultaneous accounts per
# Marvi install, so there is exactly one target rather than one per username.
TARGET_NAME = "hermes:uni_portal:duzce_student_system"

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2

_IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# ctypes CREDENTIAL struct + Advapi32 bindings (Windows only). Import-safe on
# any platform — the ctypes DLL handle and struct definitions are only
# touched inside the _raw_* functions, which fail closed (return
# None/False) with a clear log line on non-Windows rather than raising at
# import time.
# ---------------------------------------------------------------------------


def _advapi32():
    if not _IS_WINDOWS:
        return None
    return ctypes.WinDLL("Advapi32.dll")


def _credential_struct():
    """Build the CREDENTIAL ctypes.Structure class lazily (only touches
    ``ctypes.wintypes``, which doesn't exist on non-Windows)."""
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    return CREDENTIAL


def _raw_write(target: str, blob: bytes) -> bool:
    """Write ``blob`` to the Windows Credential Manager under ``target``.
    Returns False (never raises) on any failure or non-Windows platform."""
    if not _IS_WINDOWS:
        logger.warning("uni_portal credentials: Windows Credential Manager unavailable on this platform")
        return False
    try:
        advapi32 = _advapi32()
        CREDENTIAL = _credential_struct()
        blob_buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = CREDENTIAL()
        cred.Flags = 0
        cred.Type = CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.Comment = "Marvi uni_portal — Duzce student system"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(blob_buf, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = CRED_PERSIST_LOCAL_MACHINE
        cred.AttributeCount = 0
        cred.Attributes = None
        cred.TargetAlias = None
        cred.UserName = None
        ok = advapi32.CredWriteW(ctypes.byref(cred), 0)
        return bool(ok)
    except Exception:
        logger.debug("uni_portal credentials: CredWrite failed", exc_info=True)
        return False


def _raw_read(target: str) -> Optional[bytes]:
    if not _IS_WINDOWS:
        return None
    try:
        advapi32 = _advapi32()
        CREDENTIAL = _credential_struct()
        pcred = ctypes.POINTER(CREDENTIAL)()
        ok = advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred))
        if not ok:
            return None
        try:
            size = pcred.contents.CredentialBlobSize
            blob_ptr = pcred.contents.CredentialBlob
            data = ctypes.string_at(blob_ptr, size)
            return bytes(data)
        finally:
            advapi32.CredFree(pcred)
    except Exception:
        logger.debug("uni_portal credentials: CredRead failed", exc_info=True)
        return None


def _raw_delete(target: str) -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        advapi32 = _advapi32()
        ok = advapi32.CredDeleteW(target, CRED_TYPE_GENERIC, 0)
        return bool(ok)
    except Exception:
        logger.debug("uni_portal credentials: CredDelete failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Public, platform-independent API
# ---------------------------------------------------------------------------


def store_credentials(username: str, password: str) -> bool:
    """Store the student-system username/password in the OS credential
    store. Returns True on success. Never raises."""
    username = str(username or "").strip()
    password = str(password or "")
    if not username or not password:
        return False
    try:
        payload = json.dumps({"username": username, "password": password}).encode("utf-8")
        return _raw_write(TARGET_NAME, payload)
    except Exception:
        logger.debug("uni_portal credentials: store_credentials failed", exc_info=True)
        return False


def read_credentials() -> Optional[Dict[str, str]]:
    """Read back ``{"username": ..., "password": ...}``, or ``None`` if
    nothing is stored / the store is unavailable. Never raises. Marvi's
    runtime code should call this ONLY at the moment it's about to log in —
    never cache the plaintext password beyond that call."""
    try:
        raw = _raw_read(TARGET_NAME)
        if raw is None:
            return None
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            return None
        username = str(data.get("username") or "")
        password = str(data.get("password") or "")
        if not username or not password:
            return None
        return {"username": username, "password": password}
    except Exception:
        logger.debug("uni_portal credentials: read_credentials failed", exc_info=True)
        return None


def has_credentials() -> bool:
    """Cheap presence check that never reads the actual password into a
    Python string the caller has to remember to discard — used by status
    surfaces (``hermes uni status``, the plugin's availability check) that
    only need a yes/no."""
    return read_credentials() is not None


def delete_credentials() -> bool:
    """Remove the stored credentials (``hermes uni login --logout``).
    Returns True on success or if nothing was stored. Never raises."""
    try:
        return _raw_delete(TARGET_NAME) or _raw_read(TARGET_NAME) is None
    except Exception:
        logger.debug("uni_portal credentials: delete_credentials failed", exc_info=True)
        return False
