"""Tests for the <research>/<ask> contract parsing in cron/subconscious.py
(Marvi freedom spec §1.2/§1.4) — extract_autonomy_requests, and that
process_background_output strips these tags from the delivery-safe text
without disturbing its existing narrative/initiatives behavior.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    from cron import subconscious

    monkeypatch.setattr(subconscious, "get_hermes_home", lambda: tmp_path)
    # add_initiatives/apply_results are imported lazily inside
    # process_background_output from cron.subconscious_initiatives — isolate
    # that module's storage too so these tests don't touch shared state.
    from cron import subconscious_initiatives

    monkeypatch.setattr(subconscious_initiatives, "get_hermes_home", lambda: tmp_path)
    return subconscious


class TestExtractAutonomyRequests:
    def test_extracts_single_research_and_ask_block(self, _isolate):
        text = (
            'Some reasoning.\n'
            '<research>{"question": "Is the Ziraat pattern his salary?", "why": "recurs monthly"}</research>\n'
            '<ask>{"question": "Shift your morning brief later?", "why": "3 late nights this week"}</ask>\n'
            '<narrative>updated model</narrative>'
        )
        research, ask = _isolate.extract_autonomy_requests(text)

        assert research == [{"question": "Is the Ziraat pattern his salary?", "why": "recurs monthly"}]
        assert ask == [{"question": "Shift your morning brief later?", "why": "3 late nights this week"}]

    def test_extracts_multiple_blocks_of_same_tag(self, _isolate):
        text = (
            '<research>{"question": "Q1"}</research>'
            '<research>{"question": "Q2"}</research>'
        )
        research, ask = _isolate.extract_autonomy_requests(text)

        assert [r["question"] for r in research] == ["Q1", "Q2"]
        assert ask == []

    def test_no_blocks_returns_empty_lists(self, _isolate):
        research, ask = _isolate.extract_autonomy_requests("Just a plain narrative update.")
        assert research == []
        assert ask == []

    def test_malformed_json_block_is_skipped_not_fatal(self, _isolate):
        text = (
            '<research>{not valid json}</research>'
            '<research>{"question": "still parses"}</research>'
        )
        research, _ = _isolate.extract_autonomy_requests(text)
        assert research == [{"question": "still parses"}]

    def test_non_object_json_is_skipped(self, _isolate):
        text = '<research>["not", "an", "object"]</research>'
        research, _ = _isolate.extract_autonomy_requests(text)
        assert research == []

    def test_empty_text_returns_empty_lists(self, _isolate):
        assert _isolate.extract_autonomy_requests("") == ([], [])
        assert _isolate.extract_autonomy_requests(None) == ([], [])


class TestProcessBackgroundOutputStripsAutonomyTags:
    def test_research_and_ask_tags_never_leak_to_delivery(self, _isolate):
        text = (
            'Here is what I found.\n'
            '<research>{"question": "Q"}</research>'
            '<ask>{"question": "A"}</ask>'
            '<narrative>durable model</narrative>'
        )
        clean, updated = _isolate.process_background_output(text)

        assert "research" not in clean.lower() or "<research>" not in clean
        assert "<research>" not in clean
        assert "<ask>" not in clean
        assert "<narrative>" not in clean
        assert updated is True
        assert clean.strip() == "Here is what I found."

    def test_existing_narrative_and_initiatives_behavior_unaffected(self, _isolate):
        """Regression guard: adding <research>/<ask> stripping must not
        change process_background_output's existing narrative/initiatives
        contract for callers that never emit the new tags."""
        text = (
            'reply text\n'
            '<narrative>the model</narrative>\n'
            '<initiatives>[{"detail": "follow up", "trigger": "next_tick"}]</initiatives>'
        )
        clean, updated = _isolate.process_background_output(text)

        assert clean.strip() == "reply text"
        assert updated is True
        assert _isolate.read_narrative() == "the model"

        from cron.subconscious_initiatives import list_initiatives

        assert len(list_initiatives()) == 1
        assert list_initiatives()[0]["detail"] == "follow up"

    def test_no_autonomy_tags_present_is_a_noop_for_stripping(self, _isolate):
        clean, updated = _isolate.process_background_output("Plain reply, nothing special.")
        assert clean == "Plain reply, nothing special."
        assert updated is False
