#!/usr/bin/env python3
"""Guard visible Marvi branding.

This script intentionally does not ban internal compatibility names such as
``hermes`` commands, ``HERMES_HOME``, Python package names, or test helper
identifiers. It focuses on surfaces that users can see in apps, installers,
TUI, web UI, docs hero assets, and update/install URLs.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

if isinstance(sys.stdout, io.TextIOBase):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

VISIBLE_TEXT_ROOTS = [
    ".github/ISSUE_TEMPLATE",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/workflows",
    "README.md",
    "package.json",
    "pyproject.toml",
    "apps/bootstrap-installer",
    "apps/desktop",
    "ui-tui/src",
    "web/src",
    "web/public",
    "website/docusaurus.config.ts",
    "website/static/img/docs",
    "scripts/install.cmd",
    "scripts/install.ps1",
    "scripts/install.sh",
    "docker/SOUL.md",
    "gateway/platforms/email.py",
    "gateway/platforms/matrix.py",
    "gateway/platforms/whatsapp_common.py",
    "gateway/slash_commands.py",
    "hermes_cli/banner.py",
    "hermes_cli/dashboard_auth/login_page.py",
    "hermes_cli/main.py",
    "hermes_cli/setup.py",
    "hermes_cli/uninstall.py",
    "plugins/google_meet",
    "plugins/observability/nemo_relay/__init__.py",
    "plugins/platforms/homeassistant/adapter.py",
    "plugins/platforms/irc/adapter.py",
    "plugins/platforms/photon/adapter.py",
    "plugins/platforms/photon/auth.py",
    "plugins/platforms/photon/cli.py",
    "tools/mcp_oauth.py",
    "tools/send_message_tool.py",
]

SKIP_PARTS = {
    ".git",
    "node_modules",
    "target",
    "dist",
    "build",
    "release",
    "__pycache__",
    ".pytest_cache",
}

TEXT_EXTENSIONS = {
    "",
    ".cjs",
    ".cmd",
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".mdx",
    ".mjs",
    ".ps1",
    ".py",
    ".rs",
    ".sh",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yml",
    ".yaml",
}

# These patterns should not appear in visible app/install/update surfaces.
FORBIDDEN_PATTERNS = [
    re.compile(r"hermes-agent\.nousresearch\.com", re.IGNORECASE),
    re.compile(r"github\.com/NousResearch/hermes-agent", re.IGNORECASE),
    re.compile(r"raw\.githubusercontent\.com/NousResearch/hermes-agent", re.IGNORECASE),
    re.compile(r"ghcr\.io/nousresearch/hermes-agent", re.IGNORECASE),
    re.compile(r"\bHermes Setup\b"),
    re.compile(r"\bHermes-Setup\b"),
    re.compile(r"\bHERMES AGENT\b"),
    re.compile(r"\bHermes Agent\b"),
    re.compile(r"\bHermes CLI\b"),
    re.compile(r"\bCaduceus banner\b"),
    re.compile(r"\bUpdate Hermes\b"),
    re.compile(r"\bUpdating Hermes\b"),
    re.compile(r"\bOpenHuman\b", re.IGNORECASE),
    re.compile(r"\bTinyHumans\b", re.IGNORECASE),
]

# Code API names and backend compatibility names that are allowed even in
# scanned files. These are not displayed directly to users.
ALLOWED_SNIPPETS = [
    "updateHermes",
    "updatingHermes",
    "HermesConfig",
    "HermesGateway",
    "HERMES_HOME",
    "ACTIVE_HERMES_ROOT",
    "BUILD_PIN_BRANCH",
    "BUILD_PIN_COMMIT",
    "hermes update",
    "hermes desktop",
    "hermes-agent",  # install checkout path / command examples
    ".hermes-bootstrap-complete",
]

REQUIRED_TEXT = {
    "apps/desktop/package.json": ["\"productName\": \"Marvi\"", "\"executableName\": \"Marvi\""],
    "apps/desktop/src/components/chat/intro.tsx": ["const WORDMARK = 'MARVI'"],
    "apps/desktop/src/components/brand-mark.tsx": ["assetPath('hermes.png')"],
    "apps/bootstrap-installer/src/routes/welcome.tsx": [">MARVI<", "Install Marvi"],
    "apps/bootstrap-installer/src-tauri/tauri.conf.json": [
        "\"productName\": \"Marvi\"",
        "\"identifier\": \"com.xretro.marvi.setup\"",
        "\"publisher\": \"xRetro Labs Research\"",
    ],
    "ui-tui/src/banner.ts": ["███╗   ███╗", "xRetro Labs"],
    "scripts/install.ps1": ["https://github.com/xRetr00/Marvi.git"],
    "scripts/install.sh": ["https://github.com/xRetr00/Marvi.git"],
    "apps/bootstrap-installer/src-tauri/src/install_script.rs": [
        "raw.githubusercontent.com/xRetr00/Marvi"
    ],
    "apps/desktop/electron/bootstrap-runner.cjs": [
        "raw.githubusercontent.com/xRetr00/Marvi"
    ],
    "hermes_cli/dashboard_auth/login_page.py": [
        "<title>Sign in - Marvi</title>",
        "xRetro<span class=\"dot\"></span>Labs",
        "continue to the Marvi dashboard",
    ],
}

REQUIRED_ASSETS = [
    "assets/banner.png",
    "apps/desktop/assets/icon.png",
    "apps/desktop/assets/icon.ico",
    "apps/desktop/assets/icon.icns",
    "apps/desktop/public/hermes.png",
    "apps/desktop/public/hermes-sprite.png",
    "apps/desktop/public/ds-assets/filler-bg0.jpg",
    "apps/bootstrap-installer/src-tauri/icons/32x32.png",
    "apps/bootstrap-installer/src-tauri/icons/128x128.png",
    "apps/bootstrap-installer/src-tauri/icons/128x128@2x.png",
    "apps/bootstrap-installer/src-tauri/icons/icon.ico",
    "apps/bootstrap-installer/src-tauri/icons/icon.icns",
    "web/public/favicon.ico",
    "website/static/img/logo.png",
    "website/static/img/nous-logo.png",
    "website/static/img/favicon.ico",
]


def iter_visible_files() -> list[Path]:
    files: list[Path] = []
    for root in VISIBLE_TEXT_ROOTS:
        path = REPO_ROOT / root
        if not path.exists():
            continue
        if path.is_file():
            candidates = [path]
        else:
            candidates = [p for p in path.rglob("*") if p.is_file()]
        for candidate in candidates:
            rel_parts = set(candidate.relative_to(REPO_ROOT).parts)
            if rel_parts & SKIP_PARTS:
                continue
            if candidate.suffix.lower() in TEXT_EXTENSIONS:
                files.append(candidate)
    return sorted(set(files))


def is_allowed_line(line: str) -> bool:
    return any(snippet in line for snippet in ALLOWED_SNIPPETS)


def check_forbidden() -> list[str]:
    failures: list[str] = []
    for path in iter_visible_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            failures.append(f"{path.relative_to(REPO_ROOT)}: could not read: {exc}")
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if is_allowed_line(line):
                continue
            for pattern in FORBIDDEN_PATTERNS:
                if pattern.search(line):
                    rel = path.relative_to(REPO_ROOT)
                    failures.append(f"{rel}:{lineno}: forbidden visible brand: {line.strip()}")
                    break
    return failures


def check_required_text() -> list[str]:
    failures: list[str] = []
    for rel, needles in REQUIRED_TEXT.items():
        path = REPO_ROOT / rel
        if not path.exists():
            failures.append(f"{rel}: required file is missing")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in needles:
            if needle not in text:
                failures.append(f"{rel}: missing required Marvi marker {needle!r}")
    return failures


def check_required_assets() -> list[str]:
    failures: list[str] = []
    for rel in REQUIRED_ASSETS:
        path = REPO_ROOT / rel
        if not path.exists():
            failures.append(f"{rel}: required brand asset is missing")
        elif path.stat().st_size <= 0:
            failures.append(f"{rel}: required brand asset is empty")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify visible Marvi branding")
    parser.add_argument("--quiet", action="store_true", help="Only print failures")
    args = parser.parse_args()

    failures = []
    failures.extend(check_required_text())
    failures.extend(check_required_assets())
    failures.extend(check_forbidden())

    if failures:
        print("Marvi brand verification failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    if not args.quiet:
        print("Marvi brand verification passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
