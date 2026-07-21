#!/usr/bin/env python3
"""Verify downstream Marvi features that an Hermes merge must preserve."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_TEXT = {
    "AGENTS.md": ["**Upstream sync rule:**", "NeuTTS and KittenTTS are intentionally blocked"],
    "skills/autonomous-ai-agents/hermes-agent/SKILL.md": [
        "streaming STT",
        "NeuTTS and KittenTTS are intentionally blocked",
    ],
    "website/docs/developer-guide/contributing.md": [
        "instant voice lane",
        "NeuTTS and KittenTTS are deliberately blocked",
    ],
    "hermes_cli/web_server.py": [
        '@app.get("/api/mind")',
        '@app.post("/api/subconscious/enable")',
        '@app.post("/api/presence/setup")',
        '@app.get("/api/learning/summary")',
        '@app.get("/api/marvi/knowledge")',
        '@app.get("/api/brain/status")',
        '@app.put("/api/brain/config")',
        '@app.get("/api/memory/episodes")',
        '@app.get("/api/memory/archived")',
        '@app.post("/api/memory/restore/{entry_id}")',
        '@app.get("/api/voice/instant/status")',
        '@app.post("/api/audio/transcribe")',
        '@app.websocket("/api/audio/transcribe/stream")',
        '@app.websocket("/api/audio/wake-word/stream")',
        '@app.websocket("/api/voice/duplex")',
        '@app.get("/api/audio/voice-warmup")',
        '@app.get("/api/voice/speakers")',
        "first_word=not tts_started",
        '"type": "speaker_update"',
        'provider == "parakeet"',
    ],
    "hermes_cli/config.py": ["live voice defaults to no thinking"],
    "apps/desktop/src/app/routes.ts": ["MIND_ROUTE = '/mind'"],
    "apps/desktop/src/app/chat/sidebar/index.tsx": ["MIND_ROUTE", "id: 'mind'"],
    "apps/desktop/src/app/contrib/surfaces.tsx": [
        "MindView",
        "id: 'voice-presence'",
        "id: 'voice-pipeline'",
        "wakeWordConfig",
    ],
    "apps/desktop/src/app/contrib/hooks/use-desktop-integrations.ts": ["startProactiveDeliveryPolling"],
    "apps/desktop/src/app/chat/composer/index.tsx": ["dockProximity", "--dock-glow-scale"],
    "apps/desktop/electron/main.ts": ["hermes:island:work", "closeIslandWindow()"],
    "apps/desktop/electron/preload.ts": ["pushWork:", "hermes:island:work"],
    "apps/desktop/src/store/voice-island.ts": ["currentIslandWork", "pushWork(currentIslandWork())"],
    "apps/desktop/src/app/voice-island/duplex-protocol.ts": ["DuplexSpeakerUpdateEvent", "utterance_id"],
    "apps/desktop/src/app/voice-island/duplex-session.ts": ["case 'speaker_update'", "utteranceId"],
    "agent/transports/chat_completions.py": ["deepseek-v4-flash", 'extra_body["thinking"]'],
    "tools/tts_tool.py": ["Provider: PocketTTS", "def _generate_pockettts"],
    "hermes_cli/tools_config.py": ['"name": "PocketTTS"', "Qwen3-TTS"],
    "tools/voice_instant_lane.py": ["recall_episode", "barge", "_LOCAL_TIME_QUERY_RE"],
    "tools/episodic_tool.py": ['name="recall_episode"'],
    "cron/subconscious.py": [
        "recall_episode",
        'REFLECTION_JOB_NAME = "Subconscious reflection"',
        'DREAMING_JOB_NAME = "Subconscious dreaming"',
    ],
    "gateway/run.py": ["reconciled missing jobs at startup"],
    "plugins/smart_room/plugin.yaml": ["smart_room"],
    "plugins/smart_room/runtime/clap_dataset.py": ["class ClapDataset"],
    "plugins/smart_room/runtime/state_store.py": ["append_location_report", "load_location_reports"],
    "plugins/smart_room/runtime/app.py": [
        "_on_owntracks",
        "append_location_report",
        '"room_entry"',
        "unreported_visitor_entries",
    ],
    "plugins/smart_room/runtime/models.py": ["unreported_visitor_entries"],
    "plugins/smart_room/bridge.py": ["unreported_visitor_entries"],
    "plugins/smart_room/runtime/command_router.py": ["location_history", "location_limit"],
    "plugins/smart_room/tools.py": ["location_since", "location_zone"],
    "apps/desktop/src/app/settings/smart-room-settings.tsx": ["location_history", "Recent reports"],
    # Memory maturity (episodic store, decay/dedup, retrieval weighting).
    "agent/memory/episodic.py": ["def record_episode", "def episodic_config"],
    "agent/memory/decay.py": ["def relevance_score", "def _run_dedup_pass"],
    "agent/memory/retrieval.py": ["def rank_entries", "def capture_previous_batch_outcome"],
    # Learning loops (outcomes ledger feeding trust/room/focus/voice/timing/config proposals).
    "agent/learning/outcomes.py": ["def record(loop: str"],
    "agent/learning/trust.py": ["def evaluate_trust"],
    "agent/learning/room_habit.py": ["def propose(state: Dict[str, Any]"],
    "agent/learning/focus_apps.py": ["def derive(events: Iterable"],
    "agent/learning/voice_threshold.py": ["def propose_threshold"],
    "agent/learning/escalation.py": ["def mine_patterns"],
    "agent/learning/timing.py": ["def propose_windows"],
    "agent/learning/config_registry.py": ["class ConfigRule", "def apply_config_spec"],
    "agent/learning/reflection.py": ["def run_reflection"],
    # Brain self-feeding (document discovery/ingestion + collectors, recall_files fix).
    "tools/brain_ingest_tool.py": ['name="brain_store_document"'],
    "tools/brain/discovery.py": ["def discover_document_folders", "def run_discovery"],
    "tools/brain/collectors/email_docs.py": ["def collect_email_documents"],
    "tools/brain/collectors/github_docs.py": ["def collect_github_documents"],
    "tools/brain_tool.py": ["def _recall_files", "tool_result"],
    # Proactive wiring (world/idle triggers, flow gate, smart-room subconscious fetcher, noise filter).
    "gateway/world_trigger.py": ["def is_wake_worthy", "def is_arrival_event"],
    "gateway/flow_gate.py": ["def request_flush_check"],
    "gateway/idle_trigger.py": ["def _should_defer_for_resource_policy"],
    "cron/scripts/subconscious/smart_room.py": ["def fetch_delta"],
    "cron/scripts/subconscious/base.py": [
        "FETCHERS[_smart_room.APP] = _smart_room.fetch_delta",
        "BUILTIN_SURFACES[_smart_room.APP] = _smart_room_looks_active",
    ],
    "cron/scheduler.py": ["_world_activity_is_meaningful", "_select_rotation_keep_lines"],
    # Composio subconscious fetchers.
    "cron/scripts/subconscious/gmail.py": ["_extract_new_message_ids"],
    "cron/scripts/subconscious/github.py": ["_extract_notifications"],
    "cron/scripts/subconscious/calendar.py": ["_fetch_baseline_sync_token"],
    "cron/scripts/subconscious/slack.py": ["_slack_ts_now"],
    # Goals (inferred-goal origin tracking).
    "agent/goal_store.py": ["VALID_ORIGINS = frozenset({\"user\", \"inferred\"})", "DEFAULT_ORIGIN = \"user\""],
    "tools/goal_tools.py": ['origin="inferred"'],
    # Voice (three-zone speaker focus, instant lane curated defaults).
    "tools/voice_speaker_id.py": ["def focus_mode_setting", "three-zone identify"],
}


def check_contract(root: Path = REPO_ROOT) -> list[str]:
    failures: list[str] = []
    for relative_path, markers in REQUIRED_TEXT.items():
        path = root / relative_path
        if not path.is_file():
            failures.append(f"{relative_path}: required file is missing")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in markers:
            if marker not in text:
                failures.append(f"{relative_path}: missing protected marker {marker!r}")
    return failures


def main() -> int:
    failures = check_contract()
    if failures:
        print("Marvi upstream contract check failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"Marvi upstream contract check passed ({len(REQUIRED_TEXT)} protected surfaces).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
