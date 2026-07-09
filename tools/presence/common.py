"""Shared helpers for the presence subsystem: config reads, denylist
filtering, and the focus-app heuristic used by both the flow gate and the
goblin shoulder-tap check.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# Substrings (lowercased) identifying "focus" apps -- IDEs, editors, and
# terminals -- where an interruption is most costly. Used by the flow gate
# (hold proactive deliveries while the user is heads-down here) and the
# title-parsing helpers. Intentionally simple substring matching per the
# design spec ("simple app-name heuristic list") rather than a hardware
# process-classification pass.
FOCUS_APP_KEYWORDS: tuple[str, ...] = (
    # Editors / IDEs
    "visual studio code", "code.exe", "vscodium",
    "pycharm", "intellij", "idea64", "idea.exe",
    "webstorm", "clion", "rider", "goland", "rubymine", "phpstorm",
    "sublime_text", "sublime text",
    "notepad++",
    "vim", "nvim", "neovim",
    "android studio",
    "xcode",
    "visual studio", "devenv.exe",
    "cursor.exe", "cursor",
    "zed.exe", "zed",
    # Terminals
    "windows terminal", "wt.exe",
    "cmd.exe", "conhost.exe",
    "powershell", "pwsh.exe",
    "iterm", "iterm2",
    "terminal.app",
    "alacritty", "wezterm", "hyper.exe", "hyper",
    "kitty",
    "git bash", "mintty",
)

# App-name substrings that identify a terminal specifically (subset of
# FOCUS_APP_KEYWORDS used by title parsing to decide whether to attempt a
# cwd extraction).
TERMINAL_APP_KEYWORDS: tuple[str, ...] = (
    "windows terminal", "wt.exe", "cmd.exe", "conhost.exe",
    "powershell", "pwsh.exe", "iterm", "iterm2", "terminal.app",
    "alacritty", "wezterm", "hyper.exe", "hyper", "kitty",
    "git bash", "mintty",
)


def is_focus_app(app_name: Optional[str]) -> bool:
    """True when ``app_name`` looks like an IDE/editor/terminal."""
    if not app_name:
        return False
    lowered = app_name.lower()
    return any(kw in lowered for kw in FOCUS_APP_KEYWORDS)


def is_terminal_app(app_name: Optional[str]) -> bool:
    if not app_name:
        return False
    lowered = app_name.lower()
    return any(kw in lowered for kw in TERMINAL_APP_KEYWORDS)


def is_vscode_app(app_name: Optional[str], title: Optional[str] = None) -> bool:
    lowered = (app_name or "").lower()
    if "code.exe" in lowered or "visual studio code" in lowered or "vscodium" in lowered:
        return True
    return bool(title) and "Visual Studio Code" in title


# ---------------------------------------------------------------------------
# Config access
# ---------------------------------------------------------------------------

# Contract 3 defaults (hermes_cli/config.py's DEFAULT_CONFIG may not carry
# a "presence" section yet -- these are Workstream B's own fallbacks so
# every presence.* reader behaves correctly even before Workstream A lands
# the shared config schema).
PRESENCE_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "flow_gating": True,
    "distill_schedule": "0 3 * * *",
    "denylist": [],
    "goblin": {
        "shoulder_taps": False,
        "session_priming": False,
    },
}


def get_presence_config() -> Dict[str, Any]:
    """Return the effective ``presence.*`` config, merged over defaults."""
    try:
        from hermes_cli.config import read_raw_config, cfg_get
    except Exception:
        return dict(PRESENCE_DEFAULTS)

    try:
        raw = read_raw_config()
    except Exception:
        raw = {}

    section = cfg_get(raw, "presence", default={})
    if not isinstance(section, dict):
        section = {}

    merged = dict(PRESENCE_DEFAULTS)
    merged.update({k: v for k, v in section.items() if k != "goblin"})
    goblin = dict(PRESENCE_DEFAULTS["goblin"])
    goblin_section = section.get("goblin")
    if isinstance(goblin_section, dict):
        goblin.update(goblin_section)
    merged["goblin"] = goblin
    return merged


def get_denylist() -> List[str]:
    """Return the effective ``presence.denylist`` list of substrings."""
    cfg = get_presence_config()
    denylist = cfg.get("denylist") or []
    if not isinstance(denylist, list):
        return []
    return [str(x) for x in denylist if str(x).strip()]


def matches_denylist(text_blob: str, denylist: Sequence[str]) -> bool:
    if not denylist:
        return False
    lowered = text_blob.lower()
    return any(needle.lower() in lowered for needle in denylist if needle)


def filter_denylisted_events(
    events: List[Dict[str, Any]],
    denylist: Optional[Sequence[str]] = None,
    *,
    app_key: str = "app",
    title_key: str = "title",
    data_key: str = "data",
) -> List[Dict[str, Any]]:
    """Strip events whose app/title matches any denylist substring.

    Single filter function shared by ``context.py`` and ``distill.py`` per
    Contract 3 ("empty-by-default presence.denylist ... strip matching
    titles pre-LLM when set"). Works on both flat ``{"app":..,"title":..}``
    dicts (as returned by :meth:`AWClient.get_current_window`) and raw AW
    event dicts shaped ``{"data": {"app":.., "title":..}, ...}``.
    """
    denylist = list(denylist) if denylist is not None else get_denylist()
    if not denylist:
        return list(events)

    out: List[Dict[str, Any]] = []
    for event in events:
        payload = event.get(data_key) if isinstance(event.get(data_key), dict) else event
        app = str(payload.get(app_key, "") or "")
        title = str(payload.get(title_key, "") or "")
        if matches_denylist(f"{app} {title}", denylist):
            continue
        out.append(event)
    return out


def redact_if_denylisted(app: Optional[str], title: Optional[str],
                          denylist: Optional[Sequence[str]] = None) -> Optional[str]:
    """Return a redaction reason string if app/title matches the denylist,
    else None. Used for single-item (non-list) redaction, e.g. the "now"
    foreground window in desktop_context."""
    denylist = list(denylist) if denylist is not None else get_denylist()
    if not denylist:
        return None
    if matches_denylist(f"{app or ''} {title or ''}", denylist):
        return "redacted (matches presence.denylist)"
    return None
