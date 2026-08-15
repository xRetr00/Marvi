#!/usr/bin/env python3
"""Verify downstream Marvi features that an Hermes merge must preserve."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_TEXT = {
    "AGENTS.md": [
        "**Upstream sync rule:**",
        'Marvi\'s only wake-word implementation is `voice.wake_word`',
        "NeuTTS and KittenTTS are intentionally blocked",
        "Graph Mind, autonomy, and Composio",
        "August 2026 voice and Smart Room additions",
        "post-August 2026 voice and Smart Room recovery additions",
    ],
    "skills/autonomous-ai-agents/hermes-agent/SKILL.md": [
        "streaming STT",
        "wake word is exclusively the `voice.wake_word`",
        "NeuTTS and KittenTTS are intentionally blocked",
        "Graph Mind, autonomy, and Composio",
        "August 2026 voice and room stack",
        "post-August 2026 voice and Smart Room recovery additions",
    ],
    "website/docs/developer-guide/contributing.md": [
        "instant voice lane",
        "wake word is exclusively the `voice.wake_word`",
        "NeuTTS and KittenTTS are deliberately blocked",
        "Graph Mind, autonomy, and Composio",
        "August 2026 protected additions",
        "post-August 2026 recovery additions",
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
        '@app.get("/api/memory/graph")',
        '@app.get("/api/autonomy/status")',
        '@app.get("/api/composio/status")',
        '@app.get("/api/cron/delivery-targets")',
        '@app.get("/api/voice/instant/status")',
        '@app.post("/api/audio/transcribe")',
        '@app.websocket("/api/audio/transcribe/stream")',
        '@app.websocket("/api/audio/wake-word/stream")',
        '@app.websocket("/api/voice/duplex")',
        '@app.get("/api/audio/voice-warmup")',
        '@app.get("/api/voice/speakers")',
        '"type": "speaker_update"',
        'provider == "parakeet"',
        "class _DuplexTtsPipeline",
        '"tts.gepard.endpoint"',
        "_stt_slot_lock",
        "_release_stt_session(recycle=not init_in_flight)",
    ],
    "hermes_cli/config_defaults.py": [
        "live voice defaults to no thinking",
        '"gepard": {',
        '"language": "english",  # PocketTTS 2.1',
    ],
    "hermes_constants.py": [
        'PRODUCT_NAME = "Marvi Agent"',
        'MARVI_HOME_ENV = "MARVI_HOME"',
        "def _get_platform_default_marvi_home",
    ],
    "hermes_cli/main.py": [
        'for executable_name in ("Marvi.exe", "Hermes.exe")',
        'marvi = [p for p in existing if p.name.casefold().startswith("marvi")]',
    ],
    "apps/desktop/src/app/routes.ts": ["MIND_ROUTE = '/mind'"],
    "apps/desktop/src/app/chat/voice-mode-stage.tsx": ["export function VoiceModeViewport"],
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
    "apps/desktop/src/store/proactive-delivery.ts": [
        "DEFERRED_RUNS_KEY",
        "proactiveDeliveryAction",
        "isEnglishTtsText",
    ],
    "apps/desktop/src/app/mind/graph-tab.tsx": ["export function GraphTab", "/api/memory/graph"],
    "apps/desktop/src/app/mind/autonomy-panel.tsx": ["export function AutonomyPanel", "/api/autonomy/"],
    "apps/desktop/src/app/mind/composio-tab.tsx": ["export function ComposioTab", "/api/composio/"],
    "apps/desktop/src/app/voice-island/duplex-protocol.ts": ["DuplexSpeakerUpdateEvent", "utterance_id"],
    "apps/desktop/src/app/voice-island/duplex-session.ts": ["case 'speaker_update'", "utteranceId"],
    "agent/transports/chat_completions.py": ["deepseek-v4-flash", 'extra_body["thinking"]'],
    "tools/tts_tool.py": [
        "Provider: PocketTTS",
        "def _generate_pockettts",
        'BLOCKED_TTS_PROVIDERS = frozenset({"neutts", "kittentts"})',
        "is blocked in Marvi; use PocketTTS or Piper",
        '"quantize": quantize',
        'ensure("tts.pockettts", prompt=False)',
    ],
    "tools/lazy_deps.py": ['"tts.pockettts"', '"voice.wakeword.livekit"'],
    "tools/streaming_stt.py": ["Could not restore the LiveKit wake-word dependencies automatically"],
    "tools/tts_streaming.py": ["_ensure_plugins_discovered"],
    "apps/desktop/src/app/settings/constants.ts": [
        "Gepard Streaming Endpoint",
        "PocketTTS 2.1 language checkpoint",
    ],
    "hermes_cli/tools_config.py": ['"name": "PocketTTS"', "Qwen3-TTS"],
    "tools/voice_instant_lane.py": [
        "recall_episode",
        "barge",
        "_LOCAL_TIME_QUERY_RE",
        "def _resolve_fallback_instant_model",
        "cancel_event: Optional[threading.Event]",
        "conversational judgment, not a keyword rule",
    ],
    "tools/episodic_tool.py": ['name="recall_episode"'],
    "cron/subconscious.py": [
        "recall_episode",
        'REFLECTION_JOB_NAME = "Subconscious reflection"',
        'DREAMING_JOB_NAME = "Subconscious dreaming"',
    ],
    "gateway/run.py": ["reconciled missing jobs at startup"],
    "plugins/smart_room/plugin.yaml": ["smart_room", "ai-edge-litert=="],
    "plugins/smart_room/SKILL.md": ["Recovery (start it the same way the supervisor does)", "Tuya device \"offline\""],
    "plugins/smart_room/process_manager.py": [
        "def _ensure_runtime_dependencies",
        "def _ensure_vision_dependencies",
    ],
    "plugins/smart_room/runtime/clap_dataset.py": ["class ClapDataset"],
    "plugins/smart_room/runtime/state_store.py": [
        "append_location_report",
        "load_location_reports",
        "Deferred Smart Room event-log trim after file contention",
    ],
    "plugins/smart_room/runtime/app.py": [
        "_on_owntracks",
        "append_location_report",
        '"room_entry"',
        "unreported_visitor_entries",
        "_mmwave_occupied_since",
        "def _apply_entry_reflex",
        '"sleep_mode"',
    ],
    "plugins/smart_room/runtime/mqtt/client.py": ["def _build_client", '"reconnect_count"', "def health"],
    "plugins/smart_room/runtime/tuya/controller.py": ["def refresh", '"last_reconnect"', '"reconnect_count"'],
    "plugins/smart_room/runtime/presence_fusion.py": ["identity_sticky", "ble_sticky_mmwave"],
    "plugins/smart_room/runtime/models.py": ["unreported_visitor_entries"],
    "plugins/smart_room/bridge.py": ["unreported_visitor_entries"],
    "plugins/smart_room/runtime/command_router.py": ["location_history", "location_limit"],
    "plugins/smart_room/tools.py": ["location_since", "location_zone"],
    "plugins/smart_room/runtime/vision/service.py": [
        "class VisionService",
        "def _face_loop",
        "def _gesture_loop",
        "def _gesture_action_loop",
        'action == "pending_preview"',
        'action == "set_sampling"',
    ],
    "plugins/smart_room/runtime/vision/faces.py": [
        "class FaceLibrary",
        "def add_pending",
        "def review_all",
        "def pending_preview",
        "def set_sampling",
        '"sampling_full"',
    ],
    "plugins/smart_room/runtime/vision/history.py": ["class VisionHistory", "def save_frame"],
    "plugins/smart_room/runtime/cognition.py": ["class CognitionWorker", "def should_reason"],
    "plugins/smart_room/dashboard/plugin_api.py": [
        '@router.get("/vision/preview")',
        '@router.post("/vision/observe")',
        '@router.post("/vision/faces/review-all")',
        '@router.put("/vision/faces/sampling")',
        '@router.get("/vision/faces/pending/{event_id}/preview")',
    ],
    "plugins/smart_room/scripts/probe_tuya.py": ["def probe", "status()"],
    "plugins/smart_room/tools.py": [
        '"name": "smart_room_observe"',
        '"name": "smart_room_vision_history"',
        '"name": "smart_room_faces"',
    ],
    "apps/desktop/src/app/settings/smart-room-settings.tsx": [
        "location_history",
        "Recent reports",
        "Live Smart Room camera preview",
        "Face recognition",
        "Hand gesture controls",
        "Pending face capacity",
        "Auto-activate sleep from vision",
        "Collect unknown face samples",
        "Safety arming gesture",
    ],
    "apps/desktop/src/hermes.ts": [
        "reviewAllSmartRoomFaces",
        "setSmartRoomFaceSampling",
        "getSmartRoomPendingFacePreview",
    ],
    # Memory maturity (episodic store, decay/dedup, retrieval weighting).
    "agent/memory/episodic.py": ["def record_episode", "def episodic_config"],
    "agent/memory/decay.py": ["def relevance_score", "def _run_dedup_pass"],
    "agent/memory/retrieval.py": ["def rank_entries", "def capture_previous_batch_outcome"],
    "agent/memory/graph.py": ["def upsert_node", "def neighbors", "CREATE TABLE IF NOT EXISTS edges"],
    "agent/memory/graph_builder.py": ["def record_from_episode", "def record_from_memory_entry"],
    "agent/autonomy/budget.py": ["def try_spend"],
    "agent/autonomy/research.py": ["def run_research_question"],
    "agent/autonomy/ask.py": ["def ask_user"],
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
    "cron/scheduler.py": [
        "_world_activity_is_meaningful",
        "_select_rotation_keep_lines",
        "def cron_delivery_targets",
        "from gateway.channel_directory import load_directory",
        "def _is_channel_dm_topic",
    ],
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
    "tools/send_message_tool.py": ["def _agent_send_enabled", 'name="send_message"'],
    "tools/show_card.py": ['"weather"', '"time"', 'name="show_card"'],
    "plugins/uni_portal/plugin.yaml": ["uni_portal_check", "OS credential store"],
}

PROHIBITED_PATHS = (
    "tools/wake_word.py",
    "tools/wakewords/hey_hermes.onnx",
    "tools/wakewords/hey_hermes.tflite",
    "apps/desktop/src/store/wake-word.ts",
    "ui-tui/src/app/slash/commands/wake.ts",
)

PROHIBITED_TEXT = {
    "hermes_cli/config_defaults.py": [
        '"provider": "openwakeword"',
        '"phrase": "hey hermes"',
        '"model": "hey_hermes"',
    ],
    "hermes_cli/commands.py": ['CommandDef("wake"'],
    "tui_gateway/server.py": ['@method("wake.start")', '@method("wake.status")'],
    "pyproject.toml": ["wake = ["],
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
    for relative_path in PROHIBITED_PATHS:
        if (root / relative_path).exists():
            failures.append(f"{relative_path}: blocked upstream wake-word surface exists")
    for relative_path, markers in PROHIBITED_TEXT.items():
        text = (root / relative_path).read_text(encoding="utf-8", errors="replace")
        for marker in markers:
            if marker in text:
                failures.append(f"{relative_path}: blocked upstream wake-word marker {marker!r}")
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
