"""Tests for tools/presence/context.py -- the desktop_context tool
implementation, against a mocked AWClient (no real ActivityWatch needed).
"""

import tools.presence.context as context_mod


class FakeAWClient:
    """Minimal stand-in for AWClient exposing exactly what context.py uses."""

    def __init__(self, *, available=True, afk="not-afk", window=None, media=None,
                 window_events=None, media_events=None, buckets=None):
        self.available = available
        self.afk = afk
        self.window = window
        self.media = media
        self.window_events = window_events or []
        self.media_events = media_events or []
        self.buckets = buckets or [
            "aw-watcher-window_host", "aw-watcher-afk_host", "aw-watcher-media_host",
        ]

    def is_available(self, force=False):
        return self.available

    def get_afk_state(self):
        return self.afk

    def get_current_window(self):
        return self.window

    def get_current_media(self):
        return self.media

    def find_bucket_id(self, prefix):
        for bucket_id in self.buckets:
            if bucket_id.startswith(prefix):
                return bucket_id
        return None

    def get_events(self, bucket_id, start=None, end=None, limit=100):
        if bucket_id.startswith("aw-watcher-window"):
            return self.window_events
        if bucket_id.startswith("aw-watcher-media"):
            return self.media_events
        return []


def _install_fake_client(monkeypatch, fake):
    monkeypatch.setattr(context_mod, "aw_client", fake)


class TestDesktopContextUnavailable:
    def test_unavailable_reports_clear_message(self, monkeypatch):
        fake = FakeAWClient(available=False)
        _install_fake_client(monkeypatch, fake)
        result = context_mod.desktop_context("now")
        assert result["available"] is False
        assert "ActivityWatch" in result["error"]

    def test_invalid_mode(self, monkeypatch):
        fake = FakeAWClient(available=True)
        _install_fake_client(monkeypatch, fake)
        result = context_mod.desktop_context("nonsense")
        assert result["available"] is False
        assert "invalid mode" in result["error"]


class TestDesktopContextNow:
    def test_now_returns_parsed_vscode_window_and_media(self, monkeypatch):
        window_event = {
            "data": {"app": "Code.exe", "title": "main.py - hermes-agent - Visual Studio Code"},
        }
        media_event = {"data": {"title": "Song Title", "artist": "Some Artist", "status": "playing"}}
        window_events = [
            {"data": {"app": "Code.exe", "title": "main.py - hermes-agent - Visual Studio Code"}, "duration": 120},
            {"data": {"app": "chrome.exe", "title": "Docs - Chrome"}, "duration": 30},
        ]
        fake = FakeAWClient(
            available=True, afk="not-afk", window=window_event, media=media_event,
            window_events=window_events,
        )
        _install_fake_client(monkeypatch, fake)

        result = context_mod.desktop_context("now")

        assert result["available"] is True
        assert result["afk"] == "not-afk"
        assert result["window"]["app"] == "Code.exe"
        assert result["window"]["file"] == "main.py"
        assert result["window"]["workspace"] == "hermes-agent"
        assert result["now_playing"]["title"] == "Song Title"
        assert result["now_playing"]["artist"] == "Some Artist"
        # Session length sums only the leading run of matching (app, title)
        # events -- 120s here, not 150s (the chrome event breaks the run).
        assert result["session_length_seconds"] == 120

    def test_now_afk(self, monkeypatch):
        fake = FakeAWClient(available=True, afk="afk", window=None, media=None)
        _install_fake_client(monkeypatch, fake)
        result = context_mod.desktop_context("now")
        assert result["afk"] == "afk"
        assert "window" not in result

    def test_now_with_no_media_playing(self, monkeypatch):
        fake = FakeAWClient(available=True, media={"data": {"title": ""}})
        _install_fake_client(monkeypatch, fake)
        result = context_mod.desktop_context("now")
        assert "now_playing" not in result


class TestDesktopContextAggregate:
    def test_today_top_apps_and_workspace_totals(self, monkeypatch):
        window_events = [
            {"data": {"app": "Code.exe", "title": "a.py - proj1 - Visual Studio Code"}, "duration": 3600},
            {"data": {"app": "Code.exe", "title": "b.py - proj2 - Visual Studio Code"}, "duration": 1800},
            {"data": {"app": "chrome.exe", "title": "Docs - Chrome"}, "duration": 900},
        ]
        media_events = [
            {"data": {"title": "Track A", "artist": "Artist A"}},
            {"data": {"title": "Track A", "artist": "Artist A"}},  # duplicate, deduped
            {"data": {"title": "Track B", "artist": "Artist B"}},
        ]
        fake = FakeAWClient(
            available=True, window_events=window_events, media_events=media_events,
        )
        _install_fake_client(monkeypatch, fake)

        result = context_mod.desktop_context("today")

        assert result["available"] is True
        assert result["range"] == "today"
        top_apps = {entry["app"]: entry["seconds"] for entry in result["top_apps"]}
        assert top_apps["Code.exe"] == 5400
        assert top_apps["chrome.exe"] == 900

        workspaces = {entry["workspace"]: entry["seconds"] for entry in result["coding_time_by_workspace"]}
        assert workspaces["proj1"] == 3600
        assert workspaces["proj2"] == 1800

        titles = [h["title"] for h in result["media_highlights"]]
        assert titles.count("Track A") == 1
        assert "Track B" in titles

    def test_week_mode_uses_same_shape(self, monkeypatch):
        fake = FakeAWClient(available=True, window_events=[], media_events=[])
        _install_fake_client(monkeypatch, fake)
        result = context_mod.desktop_context("week")
        assert result["range"] == "week"
        assert result["top_apps"] == []
        assert result["coding_time_by_workspace"] == []

    def test_denylist_strips_matching_events(self, monkeypatch):
        from tools.presence import common as common_mod

        monkeypatch.setattr(common_mod, "get_denylist", lambda: ["proj1"])
        monkeypatch.setattr(context_mod, "get_denylist", lambda: ["proj1"])

        window_events = [
            {"data": {"app": "Code.exe", "title": "a.py - proj1 - Visual Studio Code"}, "duration": 3600},
            {"data": {"app": "Code.exe", "title": "b.py - proj2 - Visual Studio Code"}, "duration": 1800},
        ]
        fake = FakeAWClient(available=True, window_events=window_events, media_events=[])
        _install_fake_client(monkeypatch, fake)

        result = context_mod.desktop_context("today")
        apps = {entry["app"]: entry["seconds"] for entry in result["top_apps"]}
        assert apps["Code.exe"] == 1800  # only the proj2 event counted
