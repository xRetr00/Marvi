"""Tests for ``cron/scripts/subconscious_snapshot.py`` -- the Contract 1
entry point.

Verifies the exact stdout contract (``NO_CHANGE`` vs. a combined diff),
that a failing surface never blocks the others or crashes the script, and
that throttle/backoff skips a surface without attempting a fetch.
"""

from __future__ import annotations

import pytest

import cron.scripts.subconscious.base as sub_base
import hermes_cli.config as hermes_config
from cron.scripts import subconscious_snapshot as script
from cron.scripts.subconscious.snapshot_store import open_store


def _set_composio_config(
    monkeypatch, *, surfaces=None, min_interval_seconds=None, quiet_backoff_max=None
):
    cfg = {"composio": {"surfaces": surfaces or []}}
    if min_interval_seconds is not None:
        cfg["composio"]["min_interval_seconds"] = min_interval_seconds
    if quiet_backoff_max is not None:
        cfg["composio"]["quiet_backoff_max"] = quiet_backoff_max
    monkeypatch.setattr(hermes_config, "load_config", lambda: cfg)


def test_no_surfaces_configured_is_no_change(monkeypatch):
    _set_composio_config(monkeypatch, surfaces=[])
    assert script.run() == script.NO_CHANGE_MARKER


def test_activitywatch_context_change_wakes_tick(monkeypatch):
    from tools.presence import common as presence_common
    from tools.presence import context as presence_context

    _set_composio_config(monkeypatch, surfaces=[])
    monkeypatch.setattr(presence_common, "get_presence_config", lambda: {"enabled": True})
    contexts = iter(
        [
            {"available": True, "afk": "not-afk", "window": {"app": "Code.exe", "workspace": "marvi"}},
            {"available": True, "afk": "not-afk", "window": {"app": "chrome.exe"}},
        ]
    )
    monkeypatch.setattr(presence_context, "desktop_context", lambda mode="now": next(contexts))

    assert script.run() == script.NO_CHANGE_MARKER  # silent baseline
    output = script.run()
    assert "## desktop" in output
    assert "ActivityWatch desktop context changed" in output


def test_paused_presence_does_not_read_activitywatch(monkeypatch):
    from tools.presence import common as presence_common
    from tools.presence import context as presence_context

    _set_composio_config(monkeypatch, surfaces=[])
    monkeypatch.setattr(presence_common, "get_presence_config", lambda: {"enabled": False})
    monkeypatch.setattr(
        presence_context,
        "desktop_context",
        lambda mode="now": pytest.fail("paused presence must not read ActivityWatch"),
    )
    assert script.run() == script.NO_CHANGE_MARKER


def test_config_load_failure_is_no_change(monkeypatch):
    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(hermes_config, "load_config", _boom)
    assert script.run() == script.NO_CHANGE_MARKER


def test_fetcher_reports_nothing_changed(monkeypatch):
    _set_composio_config(monkeypatch, surfaces=["gmail"])
    monkeypatch.setitem(sub_base.FETCHERS, "gmail", lambda store: None)

    assert script.run() == script.NO_CHANGE_MARKER


def test_fetcher_diff_is_grouped_by_surface(monkeypatch):
    _set_composio_config(monkeypatch, surfaces=["gmail", "github"])
    monkeypatch.setitem(sub_base.FETCHERS, "gmail", lambda store: "1 new message")
    monkeypatch.setitem(sub_base.FETCHERS, "github", lambda store: None)

    output = script.run()

    assert output != script.NO_CHANGE_MARKER
    assert "## gmail" in output
    assert "1 new message" in output
    assert "## github" not in output  # github reported nothing


def test_multiple_surfaces_with_changes_are_all_included(monkeypatch):
    _set_composio_config(monkeypatch, surfaces=["gmail", "github"])
    monkeypatch.setitem(sub_base.FETCHERS, "gmail", lambda store: "gmail diff")
    monkeypatch.setitem(sub_base.FETCHERS, "github", lambda store: "github diff")

    output = script.run()

    assert "## gmail\ngmail diff" in output
    assert "## github\ngithub diff" in output


def test_unimplemented_surface_is_skipped_not_fatal(monkeypatch, capsys):
    _set_composio_config(monkeypatch, surfaces=["carrier_pigeon"])

    output = script.run()

    assert output == script.NO_CHANGE_MARKER
    err = capsys.readouterr().err
    assert "carrier_pigeon" in err
    assert "not implemented" in err


def test_failing_surface_does_not_block_others(monkeypatch, capsys):
    _set_composio_config(monkeypatch, surfaces=["gmail", "github"])

    def _boom(store):
        raise RuntimeError("composio is down")

    monkeypatch.setitem(sub_base.FETCHERS, "gmail", _boom)
    monkeypatch.setitem(sub_base.FETCHERS, "github", lambda store: "github diff")

    output = script.run()

    assert "## github\ngithub diff" in output
    assert "## gmail" not in output
    err = capsys.readouterr().err
    assert "gmail" in err
    assert "composio is down" in err

    # The failure must be recorded so the surface backs off on the next tick.
    reopened = open_store("gmail")
    assert reopened._snapshot.consecutive_failures == 1
    assert reopened.is_backoff_active()


def test_failing_surface_never_raises_out_of_run(monkeypatch):
    _set_composio_config(monkeypatch, surfaces=["gmail"])

    def _boom(store):
        raise ValueError("kaboom")

    monkeypatch.setitem(sub_base.FETCHERS, "gmail", _boom)

    # run() must not propagate the exception.
    output = script.run()
    assert output == script.NO_CHANGE_MARKER


def test_backoff_prevents_refetch_until_retry_time(monkeypatch, capsys):
    _set_composio_config(monkeypatch, surfaces=["gmail"])

    calls = []

    def _fetcher(store):
        calls.append(1)
        raise RuntimeError("still down")

    monkeypatch.setitem(sub_base.FETCHERS, "gmail", _fetcher)

    script.run()  # first failure -> backoff window opens
    assert len(calls) == 1

    script.run()  # second tick, still inside the backoff window
    assert len(calls) == 1  # fetcher was NOT called again

    err = capsys.readouterr().err
    assert "backing off" in err


def test_throttle_prevents_refetch_within_min_interval(monkeypatch):
    _set_composio_config(monkeypatch, surfaces=["gmail"], min_interval_seconds=3600)

    calls = []

    def _fetcher(store):
        calls.append(1)
        return None

    monkeypatch.setitem(sub_base.FETCHERS, "gmail", _fetcher)

    script.run()
    assert len(calls) == 1

    script.run()  # immediately again -- should be throttled
    assert len(calls) == 1


def test_main_prints_output_and_returns_zero(monkeypatch, capsys):
    _set_composio_config(monkeypatch, surfaces=[])
    rc = script.main()
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == script.NO_CHANGE_MARKER


def test_no_change_tick_grows_quiet_streak(monkeypatch):
    _set_composio_config(monkeypatch, surfaces=["gmail"], min_interval_seconds=100)
    monkeypatch.setitem(sub_base.FETCHERS, "gmail", lambda store: None)

    assert script.run() == script.NO_CHANGE_MARKER

    store = open_store("gmail", min_interval_seconds=100)
    assert store.quiet_streak == 1


def test_change_tick_resets_quiet_streak(monkeypatch):
    _set_composio_config(monkeypatch, surfaces=["gmail"], min_interval_seconds=100)

    # Prime a quiet streak by backdating so throttling doesn't block re-fetches.
    from datetime import timedelta

    from hermes_time import now as hermes_now

    store = open_store("gmail", min_interval_seconds=100)
    store.record_success(changed=False)
    store.record_success(changed=False)
    store._snapshot.last_fetch_at = (hermes_now() - timedelta(seconds=1000)).isoformat()
    store._dirty = True
    store.save()
    assert store.quiet_streak == 2

    monkeypatch.setitem(sub_base.FETCHERS, "gmail", lambda s: "1 new message")
    output = script.run()
    assert "1 new message" in output

    reopened = open_store("gmail", min_interval_seconds=100)
    assert reopened.quiet_streak == 0


def test_quiet_backoff_max_config_is_wired_through(monkeypatch):
    # With scaling disabled (quiet_backoff_max=1), repeated no-change ticks
    # must not push the effective interval past the base -- so the surface
    # is fetched again as soon as the base interval elapses, even after a
    # long quiet streak.
    _set_composio_config(
        monkeypatch, surfaces=["gmail"], min_interval_seconds=5, quiet_backoff_max=1
    )

    calls = []

    def _fetcher(store):
        calls.append(1)
        return None

    monkeypatch.setitem(sub_base.FETCHERS, "gmail", _fetcher)

    from datetime import timedelta

    from hermes_time import now as hermes_now

    store = open_store("gmail", min_interval_seconds=5, quiet_backoff_max=1)
    for _ in range(5):
        store.record_success(changed=False)
    store._snapshot.last_fetch_at = (hermes_now() - timedelta(seconds=6)).isoformat()
    store._dirty = True
    store.save()

    script.run()
    assert len(calls) == 1  # base interval (5s < 6s elapsed) allowed the fetch


def test_invalid_surface_name_in_config_is_skipped_not_fatal(monkeypatch, capsys):
    # A surface name that passes the FETCHERS lookup (so it isn't rejected
    # for being "unimplemented") but fails the snapshot store's path-safety
    # validation -- must be skipped gracefully, never crash the script.
    monkeypatch.setitem(sub_base.FETCHERS, "bad name!", lambda store: "x")
    _set_composio_config(monkeypatch, surfaces=["bad name!"])

    output = script.run()

    assert output == script.NO_CHANGE_MARKER
    err = capsys.readouterr().err
    assert "invalid" in err.lower()
