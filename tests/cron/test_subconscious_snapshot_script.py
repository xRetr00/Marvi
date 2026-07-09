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


def _set_composio_config(monkeypatch, *, surfaces=None, min_interval_seconds=None):
    cfg = {"composio": {"surfaces": surfaces or []}}
    if min_interval_seconds is not None:
        cfg["composio"]["min_interval_seconds"] = min_interval_seconds
    monkeypatch.setattr(hermes_config, "load_config", lambda: cfg)


def test_no_surfaces_configured_is_no_change(monkeypatch):
    _set_composio_config(monkeypatch, surfaces=[])
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
