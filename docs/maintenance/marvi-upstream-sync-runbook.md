# Marvi Upstream Sync Runbook

This runbook is for agents or maintainers syncing real Hermes upstream changes
from `NousResearch/hermes-agent` into Marvi.

Marvi is allowed to be built over Hermes internally. The goal is not to erase
every internal `hermes` identifier. The goal is:

- users install, launch, update, and see Marvi
- user-facing app, installer, GUI, TUI, CLI text, and visible logs say Marvi
- updates come from `xRetr00/Marvi`
- Hermes upstream fixes and features are preserved underneath

Do not drop a useful upstream patch only because it contains `Hermes`. First
classify whether that `Hermes` use is visible branding, updater identity, or
internal compatibility.

## Source Model

Use this flow:

```text
NousResearch/hermes-agent
  -> reviewed upstream-sync branch
  -> Marvi main at xRetr00/Marvi
  -> user installs and updates only from xRetr00/Marvi
```

Never point the user updater directly at `NousResearch/hermes-agent`. Direct
upstream updates can overwrite Marvi branding and installer identity.

## Allowed Internal Compatibility

These names may remain when they are internal implementation details or deep
compatibility contracts:

- `hermes` CLI command
- `HERMES_HOME`, `HERMES_PROFILE`, and other existing runtime env vars
- install root such as `%LOCALAPPDATA%\hermes`
- marker files such as `.hermes-bootstrap-complete`
- Python package and module names such as `hermes_cli`
- protocol scheme `hermes://`
- database, profile, cache, and test helper names
- references to upstream Hermes as the source project inside maintainer-only
  sync tooling

Do not rename these unless a separate migration plan exists. Renaming deep
compatibility paths is high risk and can break user installs, profiles, tests,
updates, and scripts.

## Must Stay Marvi

These surfaces are Marvi-owned and must not regress during an upstream merge:

- desktop app product name, executable name, icon, intro wordmark, empty state,
  login/auth page, settings UI, installer handoff UI
- bootstrap installer product name, publisher, app identifier, icons, welcome
  text, and generated artifact names
- TUI banner and visible startup branding
- CLI visible version/update/setup/uninstall text
- website or docs hero branding that is shipped as Marvi product surface
- install and update repository URLs
- raw script URLs used by installer/update flows
- GitHub Actions release artifact names for Marvi builds

Important required URL:

```text
https://github.com/xRetr00/Marvi
https://raw.githubusercontent.com/xRetr00/Marvi
```

Forbidden for user install/update paths:

```text
legacy Hermes upstream repo URL
legacy Hermes raw GitHub URL
legacy Hermes docs domain
legacy Hermes GHCR image
```

## Standard Sync Procedure

Start from a clean Marvi checkout:

```powershell
git checkout main
git pull --ff-only origin main
git status --short
```

Fetch and prepare the upstream merge branch:

```powershell
python scripts\prepare_marvi_upstream_sync.py --review-only
python scripts\prepare_marvi_upstream_sync.py
```

The script will:

- ensure the tree is clean
- add or verify the `hermes-upstream` remote
- fetch `origin/main` and `hermes-upstream/main`
- write `.git/marvi-upstream-sync-report.md` with protected-path overlap,
  upstream deletions, and incoming commits
- create a `sync/hermes-upstream-YYYYMMDD-<sha>` branch
- refuse to overwrite an existing sync branch
- merge upstream Hermes without committing first
- run the feature-contract and brand guards before creating the merge commit

`--review-only` stops after fetching and writing the report. Use it before a
large or rebrand-era sync to inspect collisions without changing branches.

If there are conflicts, resolve them manually. Keep useful upstream code and
only override where Marvi-visible identity would regress.

After resolving conflicts, run both guards before committing:

```powershell
python scripts\verify_marvi_upstream_contract.py
python scripts\verify_marvi_brand.py
```

## Conflict Resolution Rules

Use these rules in order:

1. Preserve upstream functional changes by default.
2. Preserve Marvi branding in visible surfaces.
3. Preserve Marvi update/install URLs in every user updater path.
4. Preserve Marvi package names for user-facing workspaces and artifacts.
5. Keep deep `hermes` compatibility names unless the change is visibly shown
   to users.
6. If a file is both functional and visible, merge both sides instead of taking
   `ours` or `theirs` wholesale.

Examples:

- `apps/desktop/package.json`: keep Marvi `productName`, `executableName`,
  artifact names, and app id. Keep upstream functional additions like protocol
  blocks if safe, but use a Marvi visible protocol name.
- `apps/bootstrap-installer/src-tauri/src/install_script.rs`: keep the raw URL
  on `xRetr00/Marvi`.
- `hermes_cli/dashboard_auth/login_page.py`: visible title, wordmark, and
  subtitle must say Marvi or NeuRetro Labs.
- `gateway/`, `agent/`, `tools/`, and most tests: usually take upstream logic
  unless it changes user-visible Marvi identity or update paths.
- `package-lock.json`: after conflict resolution, run
  `npm install --package-lock-only` and verify workspace names are still Marvi.
- Nix files: do not weaken offline/reproducible build guards while accepting
  useful upstream build fixes.

## Brand Guard

Run this after every conflict pass:

```powershell
python scripts\verify_marvi_upstream_contract.py
python scripts\verify_marvi_brand.py
```

The contract guard covers the downstream behavior documented in `AGENTS.md`:
Mind and its APIs/sidebar, subconscious/presence/learning routes, voice
presence and Dynamic Island wiring, the instant voice lane, PocketTTS and
Qwen3-TTS setup, episodic memory, and smart-room integration. If a protected
feature is intentionally moved during the full Marvi rebrand, update the guard
in the same commit as the move; do not delete the check to make a sync pass.

The protected voice/room set also includes instant provider/model selection and
cancellation, ordered phrase-streamed duplex TTS, PocketTTS 2.1 controls, lazy
streaming-provider discovery and Gepard settings, and Smart Room vision. Keep
the camera service, separate pose/gesture and face cadence, reviewed identities,
evidence/history, cognition, plugin tools/dashboard routes, world/proactive
wiring, Desktop preview/settings, optional-dependency boundary, and tests as a
unit when resolving upstream desktop or `web_server.py` rewrites.

The post-August 2026 recovery layer is protected with that unit. Preserve lazy
self-repair for configured PocketTTS and LiveKit Hey Marvi dependencies,
cancellation-safe duplex STT permit release, and the instant lane's natural
session-end/silent accidental-wake contract. For Smart Room, preserve managed
runtime/vision dependency repair, MQTT/Tuya reconnect health and direct probe,
contention-safe event-log trimming, sleep-safe entry lighting and vision-driven
sleep, the dedicated gesture/action workers with release and dropped-frame
tolerance, and the bounded pending-face review queue. Sampling, previews, and
individual/bulk accept or reject must remain exposed through plugin APIs and
Desktop settings, with no automatic identity enrollment.

If a new upstream feature adds a visible surface, update
`scripts/verify_marvi_brand.py` in the same sync. Add either:

- a new visible root to scan
- a required Marvi marker for a key file
- a forbidden pattern for an old visible brand string
- a required asset path if the new UI uses brand images

Do not hide a real visible branding leak by adding a broad allowed snippet.
Allowed snippets are only for internal compatibility names.

## Required Review Passes

Use sub-agents or independent reviewers for these separate checks:

1. Updater/install review
   - verify users update from `xRetr00/Marvi`
   - verify no runtime updater pulls directly from Hermes upstream
   - verify bootstrap installer raw URLs are Marvi URLs

2. Visible branding review
   - inspect desktop, bootstrap installer, TUI, CLI setup/update/uninstall text,
     auth/login pages, and visible assets
   - check new upstream UI features for old visible Hermes or Nous branding

3. Conflict-resolution review
   - inspect `git diff --cached`
   - look for dropped upstream behavior
   - look for Marvi package-lock drift
   - look for build-system regressions

Reviewers should not edit files unless explicitly assigned a bounded write
scope. The main sync agent owns final integration.

## Verification Commands

Run these before committing:

```powershell
python scripts\verify_marvi_upstream_contract.py
python scripts\verify_marvi_brand.py
python -m compileall hermes_cli scripts gateway agent tools tui_gateway run_agent.py cli.py
npm ci
npm run typecheck --workspace apps/bootstrap-installer
npm run typecheck --workspace apps/desktop
npm run typecheck --workspace ui-tui
npm run typecheck --workspace web
npx --prefix apps/desktop vitest run --project electron update-remote.test.ts bootstrap-runner.test.ts
```

Also run focused Python tests for files that had conflicts. For the previous
large sync this was:

```powershell
python -m pytest tests/hermes_cli/test_banner.py tests/hermes_cli/test_web_server.py tests/gateway/test_telegram_audio_vs_voice.py tests/gateway/test_telegram_voice_v0_regressions.py
```

If pytest fails because local plugins are missing, install the missing test
dependencies instead of disabling the test:

```powershell
python -m pip install pytest-timeout pytest-asyncio croniter
```

Only clear pytest addopts for investigation. Do not count that as the final
test pass unless the missing plugin issue has been fixed.

## Commit And Push

After all checks pass:

```powershell
git diff --check --cached
git status --short
git commit -m "chore: sync Hermes upstream under Marvi"
git checkout main
git merge --ff-only <sync-branch>
git push origin main
```

If the sync branch cannot fast-forward into `main`, stop and inspect why.
Do not force-push `main`.

## Build Bootstrap Installer

Build from clean `main` after pushing:

```powershell
git checkout main
git pull --ff-only origin main
$commit = git rev-parse HEAD
$env:HERMES_BUILD_PIN_COMMIT = $commit
$env:HERMES_BUILD_PIN_BRANCH = "main"
npm run tauri:build --workspace apps/bootstrap-installer
```

Expected outputs:

```text
apps/bootstrap-installer/src-tauri/target/release/Marvi-Setup.exe
apps/bootstrap-installer/src-tauri/target/release/bundle/nsis/Marvi_0.0.1_x64-setup.exe
apps/bootstrap-installer/src-tauri/target/release/bundle/msi/Marvi_0.0.1_x64_en-US.msi
```

The build log must show:

```text
marvi-bootstrap: pinning to commit <Marvi main commit>
marvi-bootstrap: pinning to branch main
```

If Tauri changes source files only by line endings during build, restore that
line-ending churn and verify the tree is clean:

```powershell
git status --short
git checkout -- <path-with-line-ending-only-diff>
git status --short
```

Do not restore real source changes.

## How To Handle A New Upstream Commit During The Run

Upstream can move while the sync is running. The sync only includes the
`hermes-upstream/main` commit fetched at the start of the merge.

Before final reporting, check:

```powershell
git fetch hermes-upstream main
git rev-parse hermes-upstream/main
git rev-list --count HEAD..hermes-upstream/main
git log --oneline --max-count=5 hermes-upstream/main
```

If `HEAD..hermes-upstream/main` is greater than zero, Marvi is already behind
live upstream again. Report the exact count and latest upstream commit. Do not
claim Marvi has the latest live Hermes commit unless this check says zero.

## What Not To Do

- Do not bind end-user updates directly to `NousResearch/hermes-agent`.
- Do not reject upstream code only because it contains internal `hermes` names.
- Do not rename deep compatibility paths casually.
- Do not weaken installer/update tests to make a sync pass.
- Do not silence the brand guard by adding broad allowed strings.
- Do not use `git reset --hard` or force push to recover from a bad merge.
- Do not build a bootstrap installer from an unpushed commit unless it is only
  for a private local test and clearly labeled that way.

## Minimal Success Criteria

A successful Marvi-over-Hermes sync has all of these:

- upstream Hermes commits are merged, not reimplemented manually
- visible product surfaces still say Marvi
- user update and install paths still point to `xRetr00/Marvi`
- package lock and workspace manifests agree
- brand guard passes
- focused compile, typecheck, updater, and conflict-area tests pass
- `main` is pushed
- bootstrap installer is built from the pushed Marvi commit
- final report states whether Marvi matches live Hermes upstream at report time
