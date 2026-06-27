# Marvi voice presence ("AI OS" edge glow) — design

Date: 2026-06-27
Status: approved for planning

## Summary

An always-available voice presence for the Marvi (Hermes) desktop app. When the
app is minimized to the system tray, Marvi keeps running and the user can talk to
it hands-free while working in other apps. The presence is rendered as an
Apple-Intelligence-style **colorful glow around the edge of the whole screen**
that reacts to the voice state machine. When the agent needs to show something
(an approval, a result, a short card), a monochrome glass capsule appears at the
bottom-center inside the glow.

Voice modes only. No chat transcript or text-message UI in the overlay.

## Hard constraints

- **Do not modify the existing voice or wake-word engines.** They already work.
  The presence only *consumes* the status/level they already emit. Any change to
  voice/wake-word capture, VAD, STT, or TTS must be confirmed with the user first.
- Brand identity stays monochrome ink (the Marvi logo style): tray icon, glass
  card, app chrome. The edge glow is the *ambient voice signal*, not the brand.
- Idle = glow fully off (Apple-style). It ignites only when the wake word fires.
  Presence while idle is indicated by the tray icon only.

## What already exists (reused, not rebuilt)

- **Overlay window machinery** — `apps/desktop/electron/main.cjs`
  (`spawnPetOverlayWindow`, ~line 5701): frameless, transparent, always-on-top,
  cross-desktop, click-through (`setIgnoreMouseEvents`), focusable toggle. IPC
  channels `hermes:pet-overlay:{open,close,set-bounds,ignore-mouse,set-focusable,state,control}`.
- **State bridge (main renderer → overlay)** — `apps/desktop/src/store/pet-overlay.ts`,
  `apps/desktop/src/app/pet-overlay/{overlay-root,pet-overlay-app}.tsx`, loaded via
  the `?win=overlay` URL flag. Main renderer is the single source of truth and
  mirrors state into the overlay over IPC.
- **Voice state machine** — `apps/desktop/src/app/chat/composer/hooks/use-voice-conversation.ts`
  exposes `status: idle|listening|transcribing|thinking|speaking`, plus mic `level`
  and `muted`. `use-wake-word.ts` exposes its own `status` (`armed|woken|listening|…`).
- **Tray** — `apps/desktop/electron/main.cjs` (~line 5597) already creates a `Tray`.
- **Agent surfacing** — `tools/approval.py` (agent requests user approval),
  `tools/registry.py` (tool registration), and rich-content embeds
  (`apps/desktop/src/components/assistant-ui/embeds/rich-boundary.tsx`).

## Architecture

Four units, each with one job.

### 1. `$voiceState` store atom (the one new wire into voice)

New nanostore atom (e.g. `apps/desktop/src/store/voice-presence.ts`):

```
$voiceState = atom<{ phase: VoicePhase; level: number; muted: boolean }>
VoicePhase = 'off' | 'wake' | 'listening' | 'transcribing' | 'thinking' | 'speaking'
```

The composer (`apps/desktop/src/app/chat/composer/index.tsx`, where
`useVoiceConversation` and `useWakeWord` are already consumed) publishes the
values these hooks already return into this atom. **Read-only mirror — no change
to the hooks themselves.** `phase` is derived: wake-word `woken` → `wake`; voice
`status` maps directly; nothing active → `off`.

### 2. Edge-glow overlay window (fullscreen, transparent, click-through)

A new overlay surface loaded with a `?win=glow` flag (sibling of the existing
`?win=overlay` pet surface, reusing `overlay-root.tsx` boot pattern). Main process
gets a `spawnGlowOverlayWindow` modeled on `spawnPetOverlayWindow` but sized to the
full work area of the active display, `setIgnoreMouseEvents(true)` always (it never
needs clicks except on the glass card — see unit 4).

Renderer paints only the animated glow plus the caption line. The glow is
**free-form, not a rectangular frame**: organic blurred multi-color blobs that
bloom and flow inward from the edges (bottom-weighted, asymmetric, drifting) — it
must never read as a uniform border outline tracing the screen. Intensity/flow
driven by mirrored `$voiceState` (level → amplitude, phase → speed/behavior). Idle
phase `off` → window hidden or zero-opacity (no GPU spend).

### 3. Tray minimize behavior

Minimizing / closing the main window hides it to the existing tray instead of
quitting. The main renderer keeps running (mic, wake-word, STT, TTS, and the
`$voiceState` mirror all continue). Tray menu: show app, mute mic, quit.

### 4. `show_card` agent tool + island queue (the "show me something" path)

Two sources feed a single island card queue:

- **Stream reuse (free):** the glow overlay subscribes to the approval/rich-content
  events the agent already emits and renders a compact card. Covers approvals and
  most results with no new tool.
- **`show_card` tool (intentional):** new tool in `tools/` registered via
  `tools/registry.py`:
  `show_card(title, body, kind="info|result|approval", actions=[...])`.
  It emits an event that travels the existing gateway → desktop renderer path, then
  is forwarded to the glow window over the `hermes:pet-overlay`-style IPC bridge.

Cards land in a queue ported from the Marvex prototype's `islandQueue.ts`
(active + queued, auto-dismiss, force, update, dismiss). The glass capsule
(monochrome ink style) renders the active card bottom-center inside the glow and
dissolves when dismissed/expired. Approval cards' action buttons resolve the
existing approval flow. The capsule is the only part of the glow window that
becomes click-interactive (flip `ignore-mouse` off while a card with actions is
shown, like the existing composer focusable pattern).

## Data flow

```
wake word fires → useWakeWord status → composer publishes $voiceState.phase='wake'
  → pet-overlay-style bridge pushes state → glow window ignites
voice turn runs → useVoiceConversation status/level → $voiceState → glow reacts
agent needs input → approval/rich event OR show_card tool → gateway → renderer
  → island queue → glass capsule renders → user taps Approve → resolves agent
turn ends → $voiceState.phase='off' → glow fades out
```

## Out of scope (v1)

- No changes to wake-word/voice/STT/TTS internals.
- No text chat / transcript in the overlay.
- No per-display glow on multiple monitors at once (active display only; multi-monitor
  is a follow-up).
- No new brand assets beyond using the existing monochrome logo for tray/card.

## Testing

- `islandQueue` port: unit test the queue (show/queue/promote/auto-dismiss/force/
  dismiss) — port the existing Marvex `islandQueue.test.ts`.
- `$voiceState` derivation: unit test phase mapping from wake-word + voice statuses.
- `show_card` tool: a runnable check that the tool emits the expected event payload.
- Manual: minimize to tray, say wake word, confirm glow ignites and clicks pass
  through to a background app; trigger an approval and confirm the capsule is
  clickable while the rest stays click-through.

## Risks / open notes

- Fullscreen transparent always-on-top click-through window with animated blur:
  acceptable GPU cost only because idle is fully off. Verify on the user's Windows
  setup; provide a "reduce motion / solid rim" fallback if blur is heavy.
- Click-through correctness: the capsule must capture clicks without the rest of the
  window stealing them — reuse the existing enter/leave `ignore-mouse` toggling.
