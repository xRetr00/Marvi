"""tools/presence/distill.py's episodic mirror (Loop 1, memory-maturity
spec §1.2): a non-empty presence digest records a ``kind=room`` episode
straight from the digest text, at zero additional LLM cost."""

from __future__ import annotations

from tools.presence import distill
from agent.memory import episodic


class TestRecordDigestEpisode:
    def test_non_empty_digest_records_room_episode(self):
        digest = "Presence digest since 2026-07-17T00:00:00+00:00:\nApp usage since last check:\n  - vscode: 2h00m"

        distill._record_digest_episode(digest)

        rows = episodic.recent(limit=5)
        assert len(rows) == 1
        assert rows[0]["kind"] == "room"
        assert rows[0]["actor"] == "marvi"
        assert rows[0]["source"] == "distill"
        assert digest[:50] in rows[0]["summary"]

    def test_title_comes_from_first_digest_line(self):
        digest = "Presence digest since X:\nApp usage since last check:\n  - vscode: 2h00m"

        distill._record_digest_episode(digest)

        assert episodic.recent(limit=1)[0]["title"] == "Presence digest since X:"

    def test_never_raises_on_episodic_failure(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(episodic, "record_episode", _boom)
        # Must not raise.
        distill._record_digest_episode("Presence digest since X:\nsomething")

    def test_print_digest_for_cron_records_episode_end_to_end(self, monkeypatch, capsys):
        monkeypatch.setattr(distill, "build_digest", lambda: "Presence digest since X:\nApp usage:\n  - vscode: 1h")
        monkeypatch.setattr(distill, "mark_run", lambda: None)

        distill.print_digest_for_cron()

        captured = capsys.readouterr()
        assert "Presence digest since X:" in captured.out
        assert episodic.count() == 1

    def test_print_digest_for_cron_skips_episode_when_digest_empty(self, monkeypatch):
        monkeypatch.setattr(distill, "build_digest", lambda: "")
        monkeypatch.setattr(distill, "mark_run", lambda: None)

        distill.print_digest_for_cron()

        assert episodic.count() == 0
