def test_narrative_last_block_wins_and_private_blocks_are_stripped(tmp_path, monkeypatch):
    from cron import subconscious

    monkeypatch.setattr(subconscious, "get_hermes_home", lambda: tmp_path)
    clean, updated = subconscious.process_background_output(
        "Visible\n<narrative>old</narrative>\n<narrative>new model</narrative>"
    )
    assert updated is True
    assert clean == "Visible"
    assert subconscious.read_narrative() == "new model"


def test_malformed_narrative_is_not_persisted(tmp_path, monkeypatch):
    from cron import subconscious

    monkeypatch.setattr(subconscious, "get_hermes_home", lambda: tmp_path)
    clean, updated = subconscious.process_background_output("hello <narrative>unfinished")
    assert updated is False
    assert clean == "hello <narrative>unfinished"


def test_initiatives_are_bounded_and_next_tick_is_due(tmp_path, monkeypatch):
    from cron import subconscious_initiatives as initiatives

    monkeypatch.setattr(initiatives, "get_hermes_home", lambda: tmp_path)
    created = initiatives.add_initiatives(
        [{"detail": f"follow up {index}", "trigger": "next_tick"} for index in range(8)]
    )
    assert len(created) == initiatives.MAX_NEW_PER_RUN
    assert len(initiatives.due_initiatives()) == initiatives.MAX_EXECUTIONS_PER_DAY
    initiatives.apply_results([{"id": row["id"], "outcome": "done"} for row in created[:3]])
    assert initiatives.due_initiatives() == []


def test_brain_fts_searches_indexed_chunks(tmp_path):
    from tools.brain.store import BrainStore

    store = BrainStore(tmp_path / "brain.db")
    try:
        store.replace_file("notes.md", 1.0, 10, "2026-07-14T00:00:00+00:00", ["Moonshine streaming voice notes"])
        results = store.search("streaming voice")
        assert results[0]["path"] == "notes.md"
    finally:
        store.close()


def test_memory_topics_are_backward_compatible():
    from tools.memory_tool import split_topic

    assert split_topic("[preferences/voice] Likes concise cues") == (
        "preferences/voice",
        "Likes concise cues",
    )
    assert split_topic("Legacy flat entry") == ("Uncategorized", "Legacy flat entry")


def test_accepting_inferred_goal_is_consent_first(tmp_path, monkeypatch):
    from agent import goal_store
    from cron import suggestions

    monkeypatch.setattr(goal_store, "GOALS_FILE", tmp_path / "goals.json")
    monkeypatch.setattr(suggestions, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(suggestions, "SUGGESTIONS_FILE", tmp_path / "cron" / "suggestions.json")
    proposal = suggestions.add_suggestion(
        title="Protect focused work",
        description="Repeated memory suggests this matters.",
        source="subconscious",
        kind="goal",
        goal_spec={"action": "add", "title": "Protect focused work", "horizon": "long"},
        dedup_key="goal:focus",
        category="goal",
    )
    assert goal_store.load_goals() == []
    accepted = suggestions.accept_suggestion(proposal["id"])
    assert accepted["title"] == "Protect focused work"
    assert len(goal_store.load_goals()) == 1
