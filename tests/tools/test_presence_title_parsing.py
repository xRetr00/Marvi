"""Tests for tools/presence/title_parsing.py -- VS Code and terminal title
parsing used by the desktop_context tool.
"""

from tools.presence.title_parsing import (
    parse_terminal_cwd,
    parse_vscode_title,
    parse_window,
)


class TestParseVSCodeTitle:
    def test_file_and_workspace(self):
        result = parse_vscode_title("aw_client.py - hermes-agent - Visual Studio Code")
        assert result["editor"] == "vscode"
        assert result["file"] == "aw_client.py"
        assert result["workspace"] == "hermes-agent"
        assert result["dirty"] is False

    def test_em_dash_separator(self):
        result = parse_vscode_title("main.py — myproj — Visual Studio Code")
        assert result["file"] == "main.py"
        assert result["workspace"] == "myproj"

    def test_unsaved_dirty_marker(self):
        result = parse_vscode_title("● main.py - myproj - Visual Studio Code")
        assert result["dirty"] is True
        assert result["file"] == "main.py"
        assert result["workspace"] == "myproj"

    def test_insiders_suffix(self):
        result = parse_vscode_title("main.py - myproj - Visual Studio Code - Insiders")
        assert result["file"] == "main.py"
        assert result["workspace"] == "myproj"

    def test_no_file_open_workspace_only(self):
        result = parse_vscode_title("myproj - Visual Studio Code")
        assert result["workspace"] == "myproj"
        assert result["file"] is None

    def test_bare_file_no_workspace(self):
        result = parse_vscode_title("scratch.md - Visual Studio Code")
        # A single dotted segment is treated as a file, not a workspace.
        assert result["file"] == "scratch.md"
        assert result["workspace"] is None

    def test_empty_window(self):
        result = parse_vscode_title("Visual Studio Code")
        assert result == {"editor": "vscode", "file": None, "workspace": None, "dirty": False}

    def test_non_vscode_title_returns_none(self):
        assert parse_vscode_title("Inbox - Gmail - Google Chrome") is None

    def test_none_and_empty_input(self):
        assert parse_vscode_title(None) is None
        assert parse_vscode_title("") is None


class TestParseTerminalCwd:
    def test_windows_path_in_powershell_title(self):
        cwd = parse_terminal_cwd("powershell.exe", "C:\\Users\\dev\\hermes-agent")
        assert cwd == "C:\\Users\\dev\\hermes-agent"

    def test_windows_terminal_app(self):
        cwd = parse_terminal_cwd("wt.exe", "project - C:\\src\\project - Windows Terminal")
        assert cwd == "C:\\src\\project"

    def test_posix_home_path_git_bash(self):
        cwd = parse_terminal_cwd("git bash", "user@host MINGW64 ~/projects/hermes-agent")
        assert cwd is not None
        assert cwd.startswith("~")

    def test_non_terminal_app_returns_none(self):
        assert parse_terminal_cwd("chrome.exe", "C:\\Users\\dev\\Downloads - Google Chrome") is None

    def test_terminal_app_no_path_in_title(self):
        assert parse_terminal_cwd("powershell.exe", "Windows PowerShell") is None

    def test_empty_title(self):
        assert parse_terminal_cwd("cmd.exe", "") is None
        assert parse_terminal_cwd("cmd.exe", None) is None


class TestParseWindow:
    def test_vscode_window(self):
        result = parse_window("Code.exe", "main.py - hermes-agent - Visual Studio Code")
        assert result["app"] == "Code.exe"
        assert result["file"] == "main.py"
        assert result["workspace"] == "hermes-agent"

    def test_terminal_window(self):
        result = parse_window("powershell.exe", "C:\\Users\\dev\\hermes-agent")
        assert result["cwd"] == "C:\\Users\\dev\\hermes-agent"

    def test_plain_browser_window_no_structured_fields(self):
        result = parse_window("chrome.exe", "Inbox - Gmail - Google Chrome")
        assert result == {"app": "chrome.exe", "title": "Inbox - Gmail - Google Chrome"}
