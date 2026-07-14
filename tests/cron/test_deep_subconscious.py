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


def test_absent_narrative_block_leaves_file_unchanged(tmp_path, monkeypatch):
    """Contract: 'If it emits no block, the narrative is left unchanged.'"""
    from cron import subconscious

    monkeypatch.setattr(subconscious, "get_hermes_home", lambda: tmp_path)
    subconscious.write_narrative("carried forward from last tick")

    clean, updated = subconscious.process_background_output("Just a normal delivered message, no markers.")

    assert updated is False
    assert clean == "Just a normal delivered message, no markers."
    assert subconscious.read_narrative() == "carried forward from last tick"


def test_narrative_markers_are_stripped_from_delivered_output(tmp_path, monkeypatch):
    """The user must never see the raw <narrative>...</narrative> markers."""
    from cron import subconscious

    monkeypatch.setattr(subconscious, "get_hermes_home", lambda: tmp_path)
    clean, updated = subconscious.process_background_output(
        "Heads up about your calendar.\n<narrative>updated model</narrative>"
    )

    assert updated is True
    assert "<narrative>" not in clean
    assert "</narrative>" not in clean
    assert clean == "Heads up about your calendar."


def test_oversize_narrative_is_truncated_at_cap(tmp_path, monkeypatch):
    from cron import subconscious

    monkeypatch.setattr(subconscious, "get_hermes_home", lambda: tmp_path)
    oversize = "x" * (subconscious.NARRATIVE_CAP + 500)

    clean, updated = subconscious.process_background_output(f"<narrative>{oversize}</narrative>")

    assert updated is True
    persisted = subconscious.read_narrative()
    assert len(persisted) == subconscious.NARRATIVE_CAP
    # write_narrative keeps the LAST NARRATIVE_CAP chars ("[-NARRATIVE_CAP:]"),
    # so the tail of the oversize text survives, not the head.
    assert persisted == oversize[-subconscious.NARRATIVE_CAP:]


def test_cold_start_absent_narrative_file_reads_empty(tmp_path, monkeypatch):
    from cron import subconscious

    monkeypatch.setattr(subconscious, "get_hermes_home", lambda: tmp_path)

    assert subconscious.read_narrative() == ""
    assert subconscious.read_narrative_history() == []


def test_narrative_rotation_shifts_previous_versions(tmp_path, monkeypatch):
    """write_narrative must rotate narrative.md -> .1 -> .2 -> .3 on every
    write, keeping at most the 3 previous versions (spec §1)."""
    from cron import subconscious

    monkeypatch.setattr(subconscious, "get_hermes_home", lambda: tmp_path)

    subconscious.write_narrative("version 1")
    subconscious.write_narrative("version 2")
    subconscious.write_narrative("version 3")
    subconscious.write_narrative("version 4")

    assert subconscious.read_narrative() == "version 4"

    path = subconscious.narrative_path()
    assert path.with_name(f"{path.name}.1").read_text(encoding="utf-8") == "version 3"
    assert path.with_name(f"{path.name}.2").read_text(encoding="utf-8") == "version 2"
    assert path.with_name(f"{path.name}.3").read_text(encoding="utf-8") == "version 1"

    history = subconscious.read_narrative_history()
    assert [entry["text"] for entry in history] == ["version 3", "version 2", "version 1"]
    assert [entry["version"] for entry in history] == [1, 2, 3]


def test_narrative_rotation_stops_at_three_previous_versions(tmp_path, monkeypatch):
    from cron import subconscious

    monkeypatch.setattr(subconscious, "get_hermes_home", lambda: tmp_path)

    for i in range(1, 6):
        subconscious.write_narrative(f"version {i}")

    path = subconscious.narrative_path()
    assert not path.with_name(f"{path.name}.4").exists()
    history = subconscious.read_narrative_history()
    assert len(history) == 3
    assert [entry["text"] for entry in history] == ["version 4", "version 3", "version 2"]


def test_narrative_history_skips_missing_versions_on_early_ticks(tmp_path, monkeypatch):
    """Cold start: fewer than 3 prior ticks means fewer than 3 history entries,
    not padding/errors."""
    from cron import subconscious

    monkeypatch.setattr(subconscious, "get_hermes_home", lambda: tmp_path)

    subconscious.write_narrative("first")
    subconscious.write_narrative("second")

    history = subconscious.read_narrative_history()

    assert [entry["text"] for entry in history] == ["first"]


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
