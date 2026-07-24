"""Keep smart-room tests out of the user's real Marvi profile."""

from __future__ import annotations

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture(autouse=True)
def isolated_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("MARVI_HOME", str(home))
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)
