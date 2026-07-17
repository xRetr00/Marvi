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
        '@app.get("/api/memory/episodes")',
        '@app.get("/api/voice/instant/status")',
        '@app.post("/api/audio/transcribe")',
        '@app.websocket("/api/audio/transcribe/stream")',
        '@app.websocket("/api/audio/wake-word/stream")',
        '@app.websocket("/api/voice/duplex")',
        '@app.get("/api/audio/voice-warmup")',
        '@app.get("/api/voice/speakers")',
    ],
    "apps/desktop/src/app/routes.ts": ["MIND_ROUTE = '/mind'"],
    "apps/desktop/src/app/chat/sidebar/index.tsx": ["MIND_ROUTE", "id: 'mind'"],
    "apps/desktop/src/app/contrib/surfaces.tsx": ["MindView", "id: 'voice-presence'", "id: 'voice-pipeline'"],
    "apps/desktop/src/app/contrib/hooks/use-desktop-integrations.ts": ["startProactiveDeliveryPolling"],
    "apps/desktop/src/app/chat/composer/index.tsx": ["dockProximity", "--dock-glow-scale"],
    "apps/desktop/electron/main.ts": ["hermes:island:work", "closeIslandWindow()"],
    "apps/desktop/electron/preload.ts": ["pushWork:", "hermes:island:work"],
    "apps/desktop/src/store/voice-island.ts": ["currentIslandWork", "pushWork(currentIslandWork())"],
    "tools/tts_tool.py": ["Provider: PocketTTS", "def _generate_pockettts"],
    "hermes_cli/tools_config.py": ['"name": "PocketTTS"', "Qwen3-TTS"],
    "tools/voice_instant_lane.py": ["recall_episode", "barge"],
    "tools/episodic_tool.py": ['name="recall_episode"'],
    "cron/subconscious.py": ["recall_episode"],
    "plugins/smart_room/plugin.yaml": ["smart_room"],
    "plugins/smart_room/runtime/clap_dataset.py": ["class ClapDataset"],
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
