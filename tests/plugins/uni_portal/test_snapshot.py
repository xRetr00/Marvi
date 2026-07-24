"""Tests for plugins/uni_portal/snapshot.py's diff logic (Marvi freedom spec
§1.3) — pure, dependency-free, no browser/network/credentials involved.
"""

from __future__ import annotations

import pytest

from plugins.uni_portal import snapshot as snap


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(snap, "get_hermes_home", lambda: tmp_path)


class TestDiffGrades:
    def test_new_course_is_reported(self):
        old = {"grades": []}
        new = {"grades": [{"course": "CS101", "grade": "AA"}]}

        diff = snap.diff_snapshots(old, new)

        assert diff["new_grades"] == [{"course": "CS101", "grade": "AA"}]
        assert diff["changed_grades"] == []

    def test_changed_grade_is_reported(self):
        old = {"grades": [{"course": "CS101", "grade": "Pending"}]}
        new = {"grades": [{"course": "CS101", "grade": "AA"}]}

        diff = snap.diff_snapshots(old, new)

        assert diff["new_grades"] == []
        assert diff["changed_grades"] == [{"course": "CS101", "old_grade": "Pending", "new_grade": "AA"}]

    def test_unchanged_grade_is_not_reported(self):
        old = {"grades": [{"course": "CS101", "grade": "AA"}]}
        new = {"grades": [{"course": "CS101", "grade": "AA"}]}

        diff = snap.diff_snapshots(old, new)

        assert diff["new_grades"] == []
        assert diff["changed_grades"] == []

    def test_course_name_matching_is_case_insensitive(self):
        old = {"grades": [{"course": "cs101", "grade": "AA"}]}
        new = {"grades": [{"course": "CS101", "grade": "AA"}]}

        diff = snap.diff_snapshots(old, new)

        assert diff["new_grades"] == []
        assert diff["changed_grades"] == []


class TestDiffAnnouncements:
    def test_new_announcement_by_id(self):
        old = {"announcements": [{"id": "1", "title": "Old"}]}
        new = {"announcements": [{"id": "1", "title": "Old"}, {"id": "2", "title": "New one"}]}

        diff = snap.diff_snapshots(old, new)

        assert diff["new_announcements"] == [{"id": "2", "title": "New one"}]

    def test_new_announcement_falls_back_to_title_date_when_no_id(self):
        old = {"announcements": [{"title": "Midterm schedule", "date": "2026-05-01"}]}
        new = {
            "announcements": [
                {"title": "Midterm schedule", "date": "2026-05-01"},
                {"title": "Final exam schedule", "date": "2026-06-01"},
            ]
        }

        diff = snap.diff_snapshots(old, new)

        assert diff["new_announcements"] == [{"title": "Final exam schedule", "date": "2026-06-01"}]

    def test_no_duplicate_announcement_when_title_date_match(self):
        old = {"announcements": [{"title": "Midterm schedule", "date": "2026-05-01"}]}
        new = {"announcements": [{"title": "Midterm schedule", "date": "2026-05-01"}]}

        diff = snap.diff_snapshots(old, new)

        assert diff["new_announcements"] == []


class TestDiffMalformedInput:
    def test_none_inputs_degrade_to_empty(self):
        diff = snap.diff_snapshots(None, None)
        assert diff == {"new_grades": [], "changed_grades": [], "new_announcements": []}

    def test_non_dict_rows_are_skipped(self):
        old = {"grades": ["not a dict"]}
        new = {"grades": [{"course": "CS101", "grade": "AA"}, "also not a dict"]}

        diff = snap.diff_snapshots(old, new)

        assert diff["new_grades"] == [{"course": "CS101", "grade": "AA"}]

    def test_missing_keys_degrade_gracefully(self):
        diff = snap.diff_snapshots({}, {})
        assert diff == {"new_grades": [], "changed_grades": [], "new_announcements": []}


class TestHasChangesAndSummary:
    def test_has_changes_true_when_any_bucket_nonempty(self):
        assert snap.has_changes({"new_grades": [{"course": "x"}], "changed_grades": [], "new_announcements": []})
        assert not snap.has_changes({"new_grades": [], "changed_grades": [], "new_announcements": []})

    def test_format_diff_summary_includes_every_change(self):
        diff = {
            "new_grades": [{"course": "CS101", "grade": "AA"}],
            "changed_grades": [{"course": "CS102", "old_grade": "Pending", "new_grade": "BB"}],
            "new_announcements": [{"title": "Final exam schedule", "date": "2026-06-01"}],
        }

        summary = snap.format_diff_summary(diff)

        assert "CS101" in summary
        assert "AA" in summary
        assert "Pending" in summary and "BB" in summary
        assert "Final exam schedule" in summary

    def test_format_diff_summary_never_raises_on_garbage(self):
        assert snap.format_diff_summary({"new_grades": "not a list"}) == ""


class TestPersistence:
    def test_save_then_load_round_trips(self):
        data = {"grades": [{"course": "CS101", "grade": "AA"}], "announcements": [], "schedule": [], "captured_at": "now"}
        snap.save_snapshot(data)

        loaded = snap.load_snapshot()

        assert loaded["grades"] == [{"course": "CS101", "grade": "AA"}]
        assert loaded["captured_at"] == "now"

    def test_load_missing_file_returns_empty_snapshot(self):
        loaded = snap.load_snapshot()
        assert loaded["grades"] == []
        assert loaded["announcements"] == []

    def test_load_malformed_file_degrades_to_empty(self):
        path = snap.snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        loaded = snap.load_snapshot()
        assert loaded["grades"] == []

    def test_end_to_end_diff_against_persisted_snapshot(self):
        snap.save_snapshot({"grades": [{"course": "CS101", "grade": "Pending"}], "announcements": [], "schedule": []})
        old = snap.load_snapshot()
        new = {"grades": [{"course": "CS101", "grade": "AA"}], "announcements": [{"title": "New!"}], "schedule": []}

        diff = snap.diff_snapshots(old, new)
        assert snap.has_changes(diff)
        assert diff["changed_grades"][0]["new_grade"] == "AA"
        assert diff["new_announcements"][0]["title"] == "New!"
