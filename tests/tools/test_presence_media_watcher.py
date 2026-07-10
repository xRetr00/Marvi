"""Tests for tools/presence/media_watcher.py -- the cross-platform
now-playing watcher.

Covers:
  * platform dispatch (``get_current_media`` picks the right adapter)
  * Linux MPRIS parsing from canned ``busctl --json=short`` payloads
  * macOS parsing from canned ``osascript`` output lines
  * failure paths (missing tooling, subprocess errors) -> None, never raise
  * event schema equality across all three adapters

No real D-Bus, osascript, or SMTC calls are made -- transports are
monkeypatched or fed canned payloads; only the pure parser / dispatch
logic is exercised.
"""

from __future__ import annotations

import subprocess
import sys
import types

import pytest

from tools.presence import media_watcher as mw

EXPECTED_SCHEMA_KEYS = {"app_id", "title", "artist", "status"}


# =============================================================================
# Platform dispatch
# =============================================================================


class TestPlatformDispatch:
    def test_windows_dispatches_to_windows_adapter(self, monkeypatch):
        monkeypatch.setattr(mw.sys, "platform", "win32")
        monkeypatch.setattr(mw, "_read_now_playing_windows", lambda: {"marker": "windows"})
        monkeypatch.setattr(mw, "_read_now_playing_linux", lambda: pytest.fail("should not be called"))
        monkeypatch.setattr(mw, "_read_now_playing_macos", lambda: pytest.fail("should not be called"))
        assert mw.get_current_media() == {"marker": "windows"}

    def test_linux_dispatches_to_linux_adapter(self, monkeypatch):
        monkeypatch.setattr(mw.sys, "platform", "linux")
        monkeypatch.setattr(mw, "_read_now_playing_linux", lambda: {"marker": "linux"})
        monkeypatch.setattr(mw, "_read_now_playing_windows", lambda: pytest.fail("should not be called"))
        monkeypatch.setattr(mw, "_read_now_playing_macos", lambda: pytest.fail("should not be called"))
        assert mw.get_current_media() == {"marker": "linux"}

    def test_macos_dispatches_to_macos_adapter(self, monkeypatch):
        monkeypatch.setattr(mw.sys, "platform", "darwin")
        monkeypatch.setattr(mw, "_read_now_playing_macos", lambda: {"marker": "macos"})
        monkeypatch.setattr(mw, "_read_now_playing_windows", lambda: pytest.fail("should not be called"))
        monkeypatch.setattr(mw, "_read_now_playing_linux", lambda: pytest.fail("should not be called"))
        assert mw.get_current_media() == {"marker": "macos"}

    def test_unknown_platform_returns_none(self, monkeypatch):
        monkeypatch.setattr(mw.sys, "platform", "freebsd13")
        assert mw.get_current_media() is None


# =============================================================================
# Linux -- MPRIS / busctl parsing
# =============================================================================


class TestParseMprisBusNames:
    def test_filters_to_mpris_prefix(self):
        payload = {
            "type": "as",
            "data": [
                "org.freedesktop.DBus",
                "org.mpris.MediaPlayer2.spotify",
                "org.mpris.MediaPlayer2.vlc",
                "com.example.NotMpris",
            ],
        }
        assert mw._parse_mpris_bus_names(payload) == [
            "org.mpris.MediaPlayer2.spotify",
            "org.mpris.MediaPlayer2.vlc",
        ]

    def test_accepts_bare_list(self):
        assert mw._parse_mpris_bus_names(["org.mpris.MediaPlayer2.rhythmbox"]) == [
            "org.mpris.MediaPlayer2.rhythmbox"
        ]

    def test_no_mpris_names_returns_empty(self):
        payload = {"type": "as", "data": ["org.freedesktop.DBus"]}
        assert mw._parse_mpris_bus_names(payload) == []

    def test_none_payload_returns_empty(self):
        assert mw._parse_mpris_bus_names(None) == []

    def test_malformed_payload_returns_empty(self):
        assert mw._parse_mpris_bus_names({"type": "as", "data": "not-a-list"}) == []
        assert mw._parse_mpris_bus_names(42) == []


class TestParseMprisPlaybackStatus:
    def test_playing_lowercased(self):
        assert mw._parse_mpris_playback_status({"type": "s", "data": "Playing"}) == "playing"

    def test_paused_lowercased(self):
        assert mw._parse_mpris_playback_status({"type": "s", "data": "Paused"}) == "paused"

    def test_none_payload_returns_none(self):
        assert mw._parse_mpris_playback_status(None) is None

    def test_empty_string_returns_none(self):
        assert mw._parse_mpris_playback_status({"type": "s", "data": ""}) is None

    def test_non_string_data_returns_none(self):
        assert mw._parse_mpris_playback_status({"type": "as", "data": ["Playing"]}) is None


class TestParseMprisMetadata:
    def test_full_metadata(self):
        payload = {
            "type": "a{sv}",
            "data": {
                "xesam:title": {"type": "s", "data": "Bohemian Rhapsody"},
                "xesam:artist": {"type": "as", "data": ["Queen"]},
                "mpris:trackid": {"type": "o", "data": "/org/mpris/MediaPlayer2/Track/1"},
            },
        }
        result = mw._parse_mpris_metadata(payload)
        assert result == {
            "title": "Bohemian Rhapsody",
            "artist": "Queen",
            "trackid": "/org/mpris/MediaPlayer2/Track/1",
        }

    def test_multiple_artists_joined(self):
        payload = {
            "data": {
                "xesam:title": {"type": "s", "data": "Under Pressure"},
                "xesam:artist": {"type": "as", "data": ["Queen", "David Bowie"]},
            }
        }
        result = mw._parse_mpris_metadata(payload)
        assert result["artist"] == "Queen, David Bowie"

    def test_missing_fields_default_empty(self):
        payload = {"data": {}}
        assert mw._parse_mpris_metadata(payload) == {"title": "", "artist": "", "trackid": ""}

    def test_none_payload_returns_defaults(self):
        assert mw._parse_mpris_metadata(None) == {"title": "", "artist": "", "trackid": ""}

    def test_malformed_data_returns_defaults(self):
        assert mw._parse_mpris_metadata({"data": "not-a-dict"}) == {
            "title": "", "artist": "", "trackid": "",
        }


class TestLinuxAdapterFailurePaths:
    def test_busctl_missing_returns_none(self, monkeypatch):
        monkeypatch.setattr(mw, "busctl_available", lambda: False)
        assert mw._read_now_playing_linux() is None

    def test_no_mpris_players_returns_none(self, monkeypatch):
        monkeypatch.setattr(mw, "busctl_available", lambda: True)
        monkeypatch.setattr(mw, "_busctl_json", lambda args: {"type": "as", "data": []})
        assert mw._read_now_playing_linux() is None

    def test_players_with_no_status_returns_none(self, monkeypatch):
        monkeypatch.setattr(mw, "busctl_available", lambda: True)

        def fake_busctl_json(args):
            if "ListNames" in args:
                return {"data": ["org.mpris.MediaPlayer2.stopped_player"]}
            return None  # PlaybackStatus query fails

        monkeypatch.setattr(mw, "_busctl_json", fake_busctl_json)
        assert mw._read_now_playing_linux() is None

    def test_subprocess_exception_never_raises(self, monkeypatch):
        monkeypatch.setattr(mw, "busctl_available", lambda: True)

        def raise_error(args):
            raise RuntimeError("boom")

        monkeypatch.setattr(mw, "_busctl_json", raise_error)
        assert mw._read_now_playing_linux() is None

    def test_busctl_json_handles_missing_binary(self, monkeypatch):
        def fake_run(*a, **kw):
            raise FileNotFoundError("no busctl")

        monkeypatch.setattr(mw.subprocess, "run", fake_run)
        assert mw._busctl_json(["call", "x"]) is None

    def test_busctl_json_handles_timeout(self, monkeypatch):
        def fake_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="busctl", timeout=3.0)

        monkeypatch.setattr(mw.subprocess, "run", fake_run)
        assert mw._busctl_json(["call", "x"]) is None

    def test_busctl_json_handles_nonzero_exit(self, monkeypatch):
        class FakeCompleted:
            returncode = 1
            stdout = ""
            stderr = "no such bus name"

        monkeypatch.setattr(mw.subprocess, "run", lambda *a, **kw: FakeCompleted())
        assert mw._busctl_json(["call", "x"]) is None

    def test_busctl_json_handles_bad_json(self, monkeypatch):
        class FakeCompleted:
            returncode = 0
            stdout = "{not valid json"
            stderr = ""

        monkeypatch.setattr(mw.subprocess, "run", lambda *a, **kw: FakeCompleted())
        assert mw._busctl_json(["call", "x"]) is None


class TestLinuxAdapterHappyPath:
    def test_prefers_playing_over_paused(self, monkeypatch):
        monkeypatch.setattr(mw, "busctl_available", lambda: True)

        def fake_busctl_json(args):
            if "ListNames" in args:
                return {"data": [
                    "org.mpris.MediaPlayer2.vlc",
                    "org.mpris.MediaPlayer2.spotify",
                ]}
            if "PlaybackStatus" in args:
                bus_name = args[1]
                if bus_name.endswith("vlc"):
                    return {"data": "Paused"}
                return {"data": "Playing"}
            if "Metadata" in args:
                bus_name = args[1]
                if bus_name.endswith("spotify"):
                    return {
                        "data": {
                            "xesam:title": {"type": "s", "data": "Song A"},
                            "xesam:artist": {"type": "as", "data": ["Artist A"]},
                            "mpris:trackid": {"type": "o", "data": "/Track/1"},
                        }
                    }
                return {"data": {}}
            return None

        monkeypatch.setattr(mw, "_busctl_json", fake_busctl_json)
        result = mw._read_now_playing_linux()
        assert result == {
            "app_id": "spotify",
            "title": "Song A",
            "artist": "Artist A",
            "status": "playing",
        }
        assert set(result.keys()) == EXPECTED_SCHEMA_KEYS

    def test_falls_back_to_paused_when_none_playing(self, monkeypatch):
        monkeypatch.setattr(mw, "busctl_available", lambda: True)

        def fake_busctl_json(args):
            if "ListNames" in args:
                return {"data": ["org.mpris.MediaPlayer2.vlc"]}
            if "PlaybackStatus" in args:
                return {"data": "Paused"}
            if "Metadata" in args:
                return {
                    "data": {
                        "xesam:title": {"type": "s", "data": "Paused Song"},
                        "xesam:artist": {"type": "as", "data": ["Someone"]},
                    }
                }
            return None

        monkeypatch.setattr(mw, "_busctl_json", fake_busctl_json)
        result = mw._read_now_playing_linux()
        assert result["status"] == "paused"
        assert result["title"] == "Paused Song"


# =============================================================================
# macOS -- AppleScript / osascript parsing
# =============================================================================


class TestParseMacosMediaLine:
    def test_playing_line(self):
        result = mw._parse_macos_media_line("playing|Bohemian Rhapsody|Queen", "Spotify")
        assert result == {
            "app_id": "Spotify",
            "title": "Bohemian Rhapsody",
            "artist": "Queen",
            "status": "playing",
        }

    def test_paused_line(self):
        result = mw._parse_macos_media_line("paused|Some Song|Some Artist", "Music")
        assert result["status"] == "paused"

    def test_not_running_returns_none(self):
        assert mw._parse_macos_media_line("not_running", "Spotify") is None

    def test_none_line_returns_none(self):
        assert mw._parse_macos_media_line(None, "Spotify") is None

    def test_empty_line_returns_none(self):
        assert mw._parse_macos_media_line("", "Spotify") is None

    def test_malformed_line_returns_none(self):
        assert mw._parse_macos_media_line("playing|onlytwoparts", "Spotify") is None

    def test_strips_whitespace(self):
        result = mw._parse_macos_media_line(" playing | Title  |  Artist ", "Music")
        assert result == {
            "app_id": "Music",
            "title": "Title",
            "artist": "Artist",
            "status": "playing",
        }


class TestMacosAdapterFailurePaths:
    def test_osascript_missing_returns_none(self, monkeypatch):
        monkeypatch.setattr(mw, "osascript_available", lambda: False)
        assert mw._read_now_playing_macos() is None

    def test_no_apps_running_returns_none(self, monkeypatch):
        monkeypatch.setattr(mw, "osascript_available", lambda: True)
        monkeypatch.setattr(mw, "_query_macos_app", lambda app: "not_running")
        assert mw._read_now_playing_macos() is None

    def test_subprocess_exception_never_raises(self, monkeypatch):
        monkeypatch.setattr(mw, "osascript_available", lambda: True)

        def raise_error(app):
            raise RuntimeError("boom")

        monkeypatch.setattr(mw, "_query_macos_app", raise_error)
        assert mw._read_now_playing_macos() is None

    def test_query_handles_missing_binary(self, monkeypatch):
        def fake_run(*a, **kw):
            raise FileNotFoundError("no osascript")

        monkeypatch.setattr(mw.subprocess, "run", fake_run)
        assert mw._query_macos_app("Spotify") is None

    def test_query_handles_timeout(self, monkeypatch):
        def fake_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="osascript", timeout=3.0)

        monkeypatch.setattr(mw.subprocess, "run", fake_run)
        assert mw._query_macos_app("Spotify") is None

    def test_query_handles_nonzero_exit(self, monkeypatch):
        class FakeCompleted:
            returncode = 1
            stdout = ""
            stderr = "script error"

        monkeypatch.setattr(mw.subprocess, "run", lambda *a, **kw: FakeCompleted())
        assert mw._query_macos_app("Spotify") is None


class TestMacosAdapterHappyPath:
    def test_prefers_playing_over_paused(self, monkeypatch):
        monkeypatch.setattr(mw, "osascript_available", lambda: True)

        def fake_query(app_name):
            if app_name == "Spotify":
                return "paused|Old Song|Old Artist"
            if app_name == "Music":
                return "playing|New Song|New Artist"
            return "not_running"

        monkeypatch.setattr(mw, "_query_macos_app", fake_query)
        result = mw._read_now_playing_macos()
        assert result == {
            "app_id": "Music",
            "title": "New Song",
            "artist": "New Artist",
            "status": "playing",
        }
        assert set(result.keys()) == EXPECTED_SCHEMA_KEYS

    def test_first_app_wins_when_both_playing(self, monkeypatch):
        monkeypatch.setattr(mw, "osascript_available", lambda: True)

        def fake_query(app_name):
            if app_name == "Spotify":
                return "playing|Spotify Song|Spotify Artist"
            return "playing|Music Song|Music Artist"

        monkeypatch.setattr(mw, "_query_macos_app", fake_query)
        result = mw._read_now_playing_macos()
        assert result["app_id"] == "Spotify"

    def test_falls_back_to_paused_when_none_playing(self, monkeypatch):
        monkeypatch.setattr(mw, "osascript_available", lambda: True)

        def fake_query(app_name):
            if app_name == "Spotify":
                return "not_running"
            return "paused|Music Song|Music Artist"

        monkeypatch.setattr(mw, "_query_macos_app", fake_query)
        result = mw._read_now_playing_macos()
        assert result["app_id"] == "Music"
        assert result["status"] == "paused"


# =============================================================================
# Windows -- schema parity (fully faked winsdk, no real SMTC call)
# =============================================================================


def _install_fake_winsdk(monkeypatch, *, app_id, title, artist, status_code):
    """Register a fake winsdk.windows.media.control module tree in
    sys.modules so `_read_now_playing_windows`'s real import + async code
    path executes end-to-end without a real SMTC session."""

    class FakePlaybackInfo:
        def __init__(self, status_code):
            self.playback_status = status_code

    class FakeMediaProperties:
        def __init__(self, title, artist):
            self.title = title
            self.artist = artist

    class FakeSession:
        def __init__(self):
            self.source_app_user_model_id = app_id

        async def try_get_media_properties_async(self):
            return FakeMediaProperties(title, artist)

        def get_playback_info(self):
            return FakePlaybackInfo(status_code)

    class FakeManager:
        def get_current_session(self):
            return FakeSession()

    class FakeMediaManagerClass:
        @staticmethod
        async def request_async():
            return FakeManager()

    control_mod = types.ModuleType("winsdk.windows.media.control")
    control_mod.GlobalSystemMediaTransportControlsSessionManager = FakeMediaManagerClass
    media_mod = types.ModuleType("winsdk.windows.media")
    media_mod.control = control_mod
    windows_mod = types.ModuleType("winsdk.windows")
    windows_mod.media = media_mod
    winsdk_mod = types.ModuleType("winsdk")
    winsdk_mod.windows = windows_mod

    for key, mod in {
        "winsdk": winsdk_mod,
        "winsdk.windows": windows_mod,
        "winsdk.windows.media": media_mod,
        "winsdk.windows.media.control": control_mod,
    }.items():
        monkeypatch.setitem(sys.modules, key, mod)

    monkeypatch.setattr(mw.platform, "system", lambda: "Windows")


class TestWindowsAdapterSchemaParity:
    def test_returns_expected_schema(self, monkeypatch):
        _install_fake_winsdk(
            monkeypatch, app_id="Spotify.exe", title="Windows Song",
            artist="Windows Artist", status_code=4,  # 4 -> "playing"
        )
        result = mw._read_now_playing_windows()
        assert result == {
            "app_id": "Spotify.exe",
            "title": "Windows Song",
            "artist": "Windows Artist",
            "status": "playing",
        }
        assert set(result.keys()) == EXPECTED_SCHEMA_KEYS


class TestEventSchemaEqualityAcrossAdapters:
    """All three adapters must normalize to the exact same key set, since
    the poll loop's heartbeat construction (media["app_id"], ["title"],
    ["artist"], ["status"]) is shared and platform-agnostic."""

    def test_windows_linux_macos_share_schema(self, monkeypatch):
        _install_fake_winsdk(
            monkeypatch, app_id="Spotify.exe", title="T", artist="A", status_code=4,
        )
        windows_result = mw._read_now_playing_windows()

        monkeypatch.setattr(mw, "busctl_available", lambda: True)

        def fake_busctl_json(args):
            if "ListNames" in args:
                return {"data": ["org.mpris.MediaPlayer2.vlc"]}
            if "PlaybackStatus" in args:
                return {"data": "Playing"}
            if "Metadata" in args:
                return {
                    "data": {
                        "xesam:title": {"type": "s", "data": "T"},
                        "xesam:artist": {"type": "as", "data": ["A"]},
                    }
                }
            return None

        monkeypatch.setattr(mw, "_busctl_json", fake_busctl_json)
        linux_result = mw._read_now_playing_linux()

        monkeypatch.setattr(mw, "osascript_available", lambda: True)
        monkeypatch.setattr(mw, "_query_macos_app", lambda app: "playing|T|A" if app == "Spotify" else "not_running")
        macos_result = mw._read_now_playing_macos()

        for result in (windows_result, linux_result, macos_result):
            assert result is not None
            assert set(result.keys()) == EXPECTED_SCHEMA_KEYS
            assert result["title"] == "T"
            assert result["artist"] == "A"
            assert result["status"] == "playing"


# =============================================================================
# run_forever platform gating (no actual polling -- just the startup gate)
# =============================================================================


class TestRunForeverPlatformGate:
    def test_linux_missing_busctl_exits_with_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(mw.sys, "platform", "linux")
        monkeypatch.setattr(mw, "busctl_available", lambda: False)
        assert mw.run_forever() == 1
        assert "busctl" in capsys.readouterr().out

    def test_macos_missing_osascript_exits_with_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(mw.sys, "platform", "darwin")
        monkeypatch.setattr(mw, "osascript_available", lambda: False)
        assert mw.run_forever() == 1
        assert "osascript" in capsys.readouterr().out

    def test_windows_missing_winsdk_exits_with_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(mw.sys, "platform", "win32")
        monkeypatch.setattr(mw, "ensure_winsdk", lambda prompt=False: False)
        assert mw.run_forever() == 1
        assert "winsdk" in capsys.readouterr().out

    def test_unknown_platform_exits_cleanly(self, monkeypatch, capsys):
        monkeypatch.setattr(mw.sys, "platform", "sunos5")
        assert mw.run_forever() == 1
        assert "unsupported platform" in capsys.readouterr().out
