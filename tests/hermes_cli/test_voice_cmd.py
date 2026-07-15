"""Tests for ``hermes voice speakers``'s adaptive-ring and model-mismatch
surfacing (spec Part 1.4 / Part 3) -- CLI-level, mocking
``tools.voice_speaker_id`` so no real speaker store/model is touched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import voice_cmd
from tools import voice_speaker_id as vsid


def _args(**overrides):
    base = {"remove": None, "reset_adaptive": False}
    base.update(overrides)
    return SimpleNamespace(**base)


def test_speakers_shows_adaptive_count(monkeypatch, capsys):
    monkeypatch.setattr(
        vsid,
        "list_speakers",
        lambda *a, **k: [
            {
                "name": "Alice", "key": "alice", "is_owner": True, "embeddings": 3,
                "adaptive": 5, "consistency": 0.95, "samples_needed": 0, "ready": True,
                "model_mismatch": False,
            }
        ],
    )

    assert voice_cmd._cmd_speakers(_args()) == 0
    out = capsys.readouterr().out
    assert "3 embeddings" in out
    assert "+5 adaptive" in out


def test_speakers_omits_adaptive_note_when_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        vsid,
        "list_speakers",
        lambda *a, **k: [
            {
                "name": "Alice", "key": "alice", "is_owner": True, "embeddings": 3,
                "adaptive": 0, "consistency": 0.95, "samples_needed": 0, "ready": True,
                "model_mismatch": False,
            }
        ],
    )

    assert voice_cmd._cmd_speakers(_args()) == 0
    out = capsys.readouterr().out
    assert "adaptive" not in out


def test_speakers_shows_model_mismatch_banner(monkeypatch, capsys):
    monkeypatch.setattr(
        vsid,
        "list_speakers",
        lambda *a, **k: [
            {
                "name": "Alice", "key": "alice", "is_owner": True, "embeddings": 3,
                "adaptive": 0, "consistency": 0.95, "samples_needed": 0, "ready": True,
                "model_mismatch": True,
            }
        ],
    )

    assert voice_cmd._cmd_speakers(_args()) == 0
    out = capsys.readouterr().out
    assert "re-enroll needed" in out


def test_speakers_no_mismatch_banner_when_models_match(monkeypatch, capsys):
    monkeypatch.setattr(
        vsid,
        "list_speakers",
        lambda *a, **k: [
            {
                "name": "Alice", "key": "alice", "is_owner": True, "embeddings": 3,
                "adaptive": 0, "consistency": 0.95, "samples_needed": 0, "ready": True,
                "model_mismatch": False,
            }
        ],
    )

    assert voice_cmd._cmd_speakers(_args()) == 0
    out = capsys.readouterr().out
    assert "re-enroll needed" not in out


def test_reset_adaptive_flag_clears_ring(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(vsid, "reset_adaptive", lambda *a, **k: calls.append(1))

    assert voice_cmd._cmd_speakers(_args(reset_adaptive=True)) == 0
    assert calls == [1]
    out = capsys.readouterr().out
    assert "adaptive" in out.lower()


def test_reset_adaptive_takes_priority_over_listing(monkeypatch, capsys):
    """--reset-adaptive short-circuits before the normal listing path."""
    monkeypatch.setattr(vsid, "reset_adaptive", lambda *a, **k: None)
    list_calls = []
    monkeypatch.setattr(vsid, "list_speakers", lambda *a, **k: list_calls.append(1) or [])

    voice_cmd._cmd_speakers(_args(reset_adaptive=True))
    assert list_calls == []


def test_remove_still_takes_priority_over_reset_adaptive(monkeypatch):
    """--remove is handled first if both are somehow passed."""
    monkeypatch.setattr(vsid, "remove_speaker", lambda name: True)
    reset_calls = []
    monkeypatch.setattr(vsid, "reset_adaptive", lambda *a, **k: reset_calls.append(1))

    assert voice_cmd._cmd_speakers(_args(remove="alice", reset_adaptive=True)) == 0
    assert reset_calls == []
