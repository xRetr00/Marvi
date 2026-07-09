"""Window-title parsing for the desktop_context tool.

VS Code and terminal titles carry structured information (open file,
workspace, working directory) that a plain "app + title" pair doesn't
surface on its own. These parsers are best-effort and never raise --
malformed/unexpected titles just yield fewer fields, never an exception.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from tools.presence.common import is_terminal_app, is_vscode_app

# VS Code (and VS Code Insiders / VSCodium) titles look like:
#   "file.py - myworkspace - Visual Studio Code"
#   "● file.py - myworkspace - Visual Studio Code"   (unsaved changes)
#   "myworkspace - Visual Studio Code"                (no file focused)
#   "Visual Studio Code"                              (empty window)
#   "file.py - Visual Studio Code - Insiders"
_VSCODE_SUFFIX_RE = re.compile(
    r"\s*[-—]\s*Visual Studio Code(?:\s*-\s*Insiders)?\s*$"
)
_DIRTY_MARKER = "●"  # ● — VS Code's "unsaved changes" bullet


def parse_vscode_title(title: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse a VS Code window title into ``{file, workspace, dirty}``.

    Returns None when ``title`` doesn't look like a VS Code title at all.
    ``file``/``workspace`` are None when that part of the title is absent
    (e.g. an empty window, or a workspace with no file open).
    """
    if not title or "Visual Studio Code" not in title:
        return None

    # Bare "Visual Studio Code" (/ "- Insiders") title -- an empty window
    # with no file or workspace open. Handled before the suffix regex
    # below, since that regex requires a leading separator and would
    # otherwise leave the whole string as a single "workspace" part.
    if re.fullmatch(r"Visual Studio Code(?:\s*-\s*Insiders)?", title.strip()):
        return {"editor": "vscode", "file": None, "workspace": None, "dirty": False}

    base = _VSCODE_SUFFIX_RE.sub("", title).strip()
    dirty = base.startswith(_DIRTY_MARKER)
    if dirty:
        base = base[len(_DIRTY_MARKER):].strip()

    if not base:
        return {"editor": "vscode", "file": None, "workspace": None, "dirty": dirty}

    # Split on " - " / " — " separators (VS Code uses en-dash or hyphen
    # depending on platform/locale).
    parts = [p.strip() for p in re.split(r"\s+[-—]\s+", base) if p.strip()]

    if len(parts) >= 2:
        file_part, workspace_part = parts[0], parts[-1]
    elif len(parts) == 1:
        # Ambiguous: could be a bare file name or a bare workspace name.
        # Heuristic: a name with a file extension (has a dot, not a
        # trailing one, and no path separators implying a project dir) is
        # treated as a file; otherwise a workspace.
        only = parts[0]
        if re.search(r"\.[A-Za-z0-9_]{1,10}$", only):
            file_part, workspace_part = only, None
        else:
            file_part, workspace_part = None, only
    else:
        file_part, workspace_part = None, None

    return {"editor": "vscode", "file": file_part, "workspace": workspace_part, "dirty": dirty}


# Windows path: "C:\Users\name\project" — stop at whitespace/quote/pipe/angle
# bracket so we don't swallow trailing title decoration.
_WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"<>|]*")
# POSIX-ish home-relative path as sometimes rendered in WSL / git-bash titles.
_POSIX_PATH_RE = re.compile(r"(?:~|/[\w.\-]+)(?:/[\w.\-]+)*")


def parse_terminal_cwd(app: Optional[str], title: Optional[str]) -> Optional[str]:
    """Best-effort working-directory extraction from a terminal title.

    Terminal apps commonly bake the cwd into the title (PowerShell,
    Windows Terminal tab titles, git-bash "user@host MINGW64 ~/path").
    Returns None when ``app`` isn't recognized as a terminal or no
    path-like substring is found.
    """
    if not title or not is_terminal_app(app):
        return None

    match = _WINDOWS_PATH_RE.search(title)
    if match:
        return match.group(0).rstrip("\\")

    match = _POSIX_PATH_RE.search(title)
    if match and len(match.group(0)) > 1:
        return match.group(0)

    return None


def parse_window(app: Optional[str], title: Optional[str]) -> Dict[str, Any]:
    """Combine the VS Code and terminal parsers into one summary dict.

    Always returns a dict (possibly with all-None values) so callers don't
    need to special-case "no structured info available".
    """
    result: Dict[str, Any] = {"app": app, "title": title}
    if is_vscode_app(app, title):
        vscode = parse_vscode_title(title)
        if vscode:
            result.update(vscode)
    elif is_terminal_app(app):
        cwd = parse_terminal_cwd(app, title)
        if cwd:
            result["cwd"] = cwd
    return result
