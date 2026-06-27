# Marvi voice presence (edge-glow overlay) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an always-available, Apple-Intelligence-style free-form edge glow that ignites on the existing wake word and reacts to the voice state machine, rendered in a fullscreen transparent click-through window that stays alive while the app is minimized to the tray.

**Architecture:** A new nanostore atom (`$voiceState`) mirrors the statuses the existing voice + wake-word hooks already emit (read-only — engines untouched). A new fullscreen, transparent, click-through `?win=glow` BrowserWindow (modeled on the existing pet overlay) subscribes to that state over IPC and paints only an organic, bottom-weighted color glow. Idle = window hidden (zero GPU). Minimizing the app hides it to the existing tray so the renderer keeps running.

**Tech Stack:** Electron (main + preload), React + nanostores (renderer), Vitest, Canvas 2D for the glow.

This is Plan A of two. Plan B (`show_card` tool + island card capsule) builds on this.

---

### Task 1: `$voiceState` store + phase derivation

**Files:**
- Create: `apps/desktop/src/store/voice-presence.ts`
- Test: `apps/desktop/src/store/voice-presence.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
import { describe, expect, it } from 'vitest'

import { deriveVoicePhase } from './voice-presence'

describe('deriveVoicePhase', () => {
  it('is off when nothing is active', () => {
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'armed' })).toBe('off')
  })

  it('lights as wake the moment the hotword is detected', () => {
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'woken' })).toBe('wake')
  })

  it('does not light for background hotword listening', () => {
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'listening' })).toBe('off')
  })

  it('maps an active conversation status straight through', () => {
    expect(deriveVoicePhase({ active: true, voiceStatus: 'listening', wakeStatus: 'idle' })).toBe('listening')
    expect(deriveVoicePhase({ active: true, voiceStatus: 'speaking', wakeStatus: 'idle' })).toBe('speaking')
  })

  it('is off when a conversation is active but idle between turns', () => {
    expect(deriveVoicePhase({ active: true, voiceStatus: 'idle', wakeStatus: 'idle' })).toBe('off')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/desktop && npx vitest run src/store/voice-presence.test.ts`
Expected: FAIL — `deriveVoicePhase` is not exported / module not found.

- [ ] **Step 3: Write minimal implementation**

```ts
import { atom } from 'nanostores'

export type VoicePhase = 'off' | 'wake' | 'listening' | 'transcribing' | 'thinking' | 'speaking'

export interface VoiceState {
  phase: VoicePhase
  /** Live mic amplitude 0..1 from the recorder; drives glow reactivity. */
  level: number
  muted: boolean
}

/** Conversation status from use-voice-conversation.ts. */
type VoiceStatus = 'idle' | 'listening' | 'transcribing' | 'thinking' | 'speaking'
/** Wake-word status from use-wake-word.ts. */
type WakeStatus = 'idle' | 'arming' | 'armed' | 'woken' | 'listening' | 'transcribing'

export const $voiceState = atom<VoiceState>({ phase: 'off', level: 0, muted: false })

/**
 * Collapse the two engines' statuses into one glow phase. The wake-word loop is
 * always listening in the background for the hotword — that must NOT light the
 * glow, so only `woken` (hotword just fired) counts. An active conversation's
 * status maps straight through; anything else is `off` (glow dark).
 */
export function deriveVoicePhase(args: {
  active: boolean
  voiceStatus: VoiceStatus
  wakeStatus: WakeStatus
}): VoicePhase {
  const { active, voiceStatus, wakeStatus } = args

  if (active && voiceStatus !== 'idle') {
    return voiceStatus
  }

  if (wakeStatus === 'woken') {
    return 'wake'
  }

  return 'off'
}

/** Publish the latest derived state for the glow overlay to mirror. */
export function publishVoiceState(next: VoiceState): void {
  $voiceState.set(next)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/desktop && npx vitest run src/store/voice-presence.test.ts`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src/store/voice-presence.ts apps/desktop/src/store/voice-presence.test.ts
git commit -m "feat(desktop): add \$voiceState store and phase derivation"
```

---

### Task 2: Glow render model (pure)

**Files:**
- Create: `apps/desktop/src/app/glow-overlay/glow-model.ts`
- Test: `apps/desktop/src/app/glow-overlay/glow-model.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
import { describe, expect, it } from 'vitest'

import { glowSpeedMs, targetAmplitude } from './glow-model'

describe('targetAmplitude', () => {
  it('is zero when off', () => {
    expect(targetAmplitude('off', 0)).toBe(0)
  })

  it('flares to full on wake', () => {
    expect(targetAmplitude('wake', 0)).toBe(1)
  })

  it('rises with mic level while listening and stays within 0..1', () => {
    expect(targetAmplitude('listening', 0)).toBeCloseTo(0.4)
    expect(targetAmplitude('listening', 1)).toBe(1)
  })

  it('has a steady baseline while thinking', () => {
    expect(targetAmplitude('thinking', 0)).toBeCloseTo(0.5)
  })
})

describe('glowSpeedMs', () => {
  it('flows fast when listening or speaking and slow when idle/thinking', () => {
    expect(glowSpeedMs('listening')).toBeLessThan(glowSpeedMs('thinking'))
    expect(glowSpeedMs('thinking')).toBeLessThan(glowSpeedMs('off'))
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/desktop && npx vitest run src/app/glow-overlay/glow-model.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```ts
import type { VoicePhase } from '@/store/voice-presence'

/** Target glow amplitude (0..1) for a phase + current mic level. */
export function targetAmplitude(phase: VoicePhase, level: number): number {
  switch (phase) {
    case 'off':
      return 0
    case 'wake':
      return 1
    case 'listening':
      return Math.min(1, 0.4 + level * 0.9)
    case 'transcribing':
      return 0.45
    case 'thinking':
      return 0.5
    case 'speaking':
      return Math.min(1, 0.55 + level * 0.6)
    default:
      return 0
  }
}

/** Blob-drift animation duration per phase (ms). Lower = faster flow. */
export function glowSpeedMs(phase: VoicePhase): number {
  switch (phase) {
    case 'listening':
    case 'speaking':
      return 3000
    case 'wake':
      return 2500
    case 'thinking':
      return 6000
    default:
      return 14000
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/desktop && npx vitest run src/app/glow-overlay/glow-model.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src/app/glow-overlay/glow-model.ts apps/desktop/src/app/glow-overlay/glow-model.test.ts
git commit -m "feat(desktop): add pure glow render model"
```

---

### Task 3: Glow overlay window (main process)

**Files:**
- Modify: `apps/desktop/electron/main.cjs` (add after the pet-overlay block, near line 5818)

- [ ] **Step 1: Add the glow window spawner and IPC**

Add this block immediately after `closePetOverlay()` (after line 5818). It mirrors `spawnPetOverlayWindow` but sizes to the active display's full work area and is always click-through.

```js
// ── Voice presence glow overlay ──────────────────────────────────────────────
// A fullscreen, transparent, always-on-top, click-through window that paints the
// Apple-Intelligence-style edge glow. The main renderer pushes $voiceState into
// it; it never needs clicks (ignore-mouse stays on). Loaded via `?win=glow`.
let glowOverlayWindow = null

function glowOverlayUrl() {
  if (DEV_SERVER) {
    return `${DEV_SERVER.endsWith('/') ? DEV_SERVER.slice(0, -1) : DEV_SERVER}/?win=glow#/`
  }

  return `${pathToFileURL(resolveRendererIndex()).toString()}?win=glow#/`
}

function spawnGlowOverlayWindow() {
  const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint())
  const area = display.workArea

  const win = new BrowserWindow({
    x: area.x,
    y: area.y,
    width: area.width,
    height: area.height,
    frame: false,
    transparent: true,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    hasShadow: false,
    alwaysOnTop: true,
    focusable: false,
    show: false,
    type: IS_MAC ? 'panel' : undefined,
    hiddenInMissionControl: IS_MAC,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      devTools: true,
      backgroundThrottling: false
    }
  })

  win.setAlwaysOnTop(true, IS_MAC ? 'floating' : 'screen-saver')
  win.setIgnoreMouseEvents(true, { forward: true })
  win.setHiddenInMissionControl?.(true)
  try {
    win.setVisibleOnAllWorkspaces(
      true,
      IS_MAC ? { visibleOnFullScreen: true, skipTransformProcessType: true } : undefined
    )
  } catch {
    // Best effort.
  }

  wireCommonWindowHandlers(win)

  win.once('ready-to-show', () => {
    if (!win.isDestroyed()) win.showInactive()
  })

  win.on('closed', () => {
    if (glowOverlayWindow === win) glowOverlayWindow = null
  })

  win.loadURL(glowOverlayUrl())

  return win
}

function openGlowOverlay() {
  if (glowOverlayWindow && !glowOverlayWindow.isDestroyed()) {
    glowOverlayWindow.showInactive()
    return glowOverlayWindow
  }

  glowOverlayWindow = spawnGlowOverlayWindow()
  return glowOverlayWindow
}

function closeGlowOverlay() {
  if (glowOverlayWindow && !glowOverlayWindow.isDestroyed()) {
    glowOverlayWindow.close()
  }
  glowOverlayWindow = null
}

ipcMain.handle('hermes:glow:open', async () => {
  openGlowOverlay()
  return { ok: true }
})
ipcMain.handle('hermes:glow:close', async () => {
  closeGlowOverlay()
  return { ok: true }
})
// Main renderer → glow window: forward the latest voice state.
ipcMain.on('hermes:glow:state', (_event, payload) => {
  if (glowOverlayWindow && !glowOverlayWindow.isDestroyed()) {
    glowOverlayWindow.webContents.send('hermes:glow:state', payload)
  }
})
```

- [ ] **Step 2: Verify `screen` is imported**

Run: `grep -n "require('electron')" apps/desktop/electron/main.cjs` and confirm `screen` is among the destructured imports (the pet overlay uses display bounds, so it should be). If `screen` is missing, add it to the `const { ... } = require('electron')` destructure.

- [ ] **Step 3: Sanity-check the file parses**

Run: `cd apps/desktop && node --check electron/main.cjs`
Expected: no output (exit 0).

- [ ] **Step 4: Commit**

```bash
git add apps/desktop/electron/main.cjs
git commit -m "feat(desktop): add glow overlay window + IPC in main process"
```

---

### Task 4: Preload bridge + types for the glow overlay

**Files:**
- Modify: `apps/desktop/electron/preload.cjs` (after the `petOverlay` block, ~line 35)
- Modify: `apps/desktop/src/global.d.ts` (after the `petOverlay` block, ~line 49)

- [ ] **Step 1: Add the preload API**

Insert after the `petOverlay: { … },` object:

```js
  glowOverlay: {
    open: () => ipcRenderer.invoke('hermes:glow:open'),
    close: () => ipcRenderer.invoke('hermes:glow:close'),
    pushState: payload => ipcRenderer.send('hermes:glow:state', payload),
    onState: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('hermes:glow:state', listener)
      return () => ipcRenderer.removeListener('hermes:glow:state', listener)
    }
  },
```

- [ ] **Step 2: Add the TypeScript type**

In `apps/desktop/src/global.d.ts`, add the import at the top alongside the existing pet-overlay import:

```ts
import type { VoiceState } from './store/voice-presence'
```

Then insert this inside the `hermesDesktop` interface, right after the `petOverlay: { … }` block:

```ts
      // The voice presence glow: a fullscreen transparent click-through window
      // painting the edge glow. The main renderer drives it with $voiceState.
      glowOverlay: {
        open: () => Promise<{ ok: boolean }>
        close: () => Promise<{ ok: boolean }>
        pushState: (payload: VoiceState) => void
        onState: (callback: (payload: VoiceState) => void) => () => void
      }
```

- [ ] **Step 3: Typecheck**

Run: `cd apps/desktop && npx tsc --noEmit`
Expected: no errors from `global.d.ts` (pre-existing unrelated errors, if any, are out of scope — confirm none mention `glowOverlay`).

- [ ] **Step 4: Commit**

```bash
git add apps/desktop/electron/preload.cjs apps/desktop/src/global.d.ts
git commit -m "feat(desktop): expose glowOverlay bridge + types"
```

---

### Task 5: Glow overlay controller (main renderer mirror)

**Files:**
- Create: `apps/desktop/src/store/glow-overlay.ts`

- [ ] **Step 1: Write the controller**

Modeled on `pet-overlay.ts`: open the window when phase leaves `off`, mirror `$voiceState` into it, close it after it returns to `off` (debounced so a quick gap between turns doesn't thrash the window).

```ts
import { $voiceState } from './voice-presence'

/**
 * Main-renderer controller for the voice presence glow window. The glow window
 * carries no gateway — this renderer is the single source of truth and pushes
 * $voiceState into it over IPC (mirrors the pet-overlay pattern). The window is
 * opened lazily on the first non-`off` phase and closed shortly after returning
 * to `off`, so idle costs nothing.
 */

let unsub: (() => void) | null = null
let open = false
let closeTimer: ReturnType<typeof setTimeout> | null = null

// ponytail: 1.2s linger before closing so a brief idle gap between a turn and
// the next wake doesn't tear the window down and respawn it.
const CLOSE_LINGER_MS = 1200

function ensureOpen(): void {
  if (open) {
    return
  }

  open = true
  void window.hermesDesktop?.glowOverlay?.open()
}

function scheduleClose(): void {
  if (closeTimer) {
    return
  }

  closeTimer = setTimeout(() => {
    closeTimer = null
    open = false
    void window.hermesDesktop?.glowOverlay?.close()
  }, CLOSE_LINGER_MS)
}

function cancelClose(): void {
  if (closeTimer) {
    clearTimeout(closeTimer)
    closeTimer = null
  }
}

/** Start mirroring $voiceState into the glow window. Idempotent. */
export function initGlowOverlayBridge(): () => void {
  if (unsub || !window.hermesDesktop?.glowOverlay) {
    return () => {}
  }

  unsub = $voiceState.subscribe(state => {
    if (state.phase === 'off') {
      scheduleClose()
    } else {
      cancelClose()
      ensureOpen()
    }

    if (open) {
      window.hermesDesktop?.glowOverlay?.pushState(state)
    }
  })

  return () => {
    unsub?.()
    unsub = null
    cancelClose()
  }
}
```

- [ ] **Step 2: Typecheck**

Run: `cd apps/desktop && npx tsc --noEmit`
Expected: no new errors mentioning `glow-overlay.ts`.

- [ ] **Step 3: Commit**

```bash
git add apps/desktop/src/store/glow-overlay.ts
git commit -m "feat(desktop): mirror \$voiceState into glow window"
```

---

### Task 6: Glow overlay renderer surface

**Files:**
- Create: `apps/desktop/src/app/glow-overlay/glow-root.tsx`
- Create: `apps/desktop/src/app/glow-overlay/glow-overlay-app.tsx`
- Modify: `apps/desktop/src/main.tsx` (the `win` branch, lines 32-34)

- [ ] **Step 1: Write the boot root**

`apps/desktop/src/app/glow-overlay/glow-root.tsx` — mirrors `overlay-root.tsx`: transparent, no app shell, no gateway.

```tsx
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { ErrorBoundary } from '@/components/error-boundary'

import { GlowOverlayApp } from './glow-overlay-app'

export function mountGlowOverlay(): void {
  const style = document.createElement('style')
  style.textContent = 'html,body,#root{background:transparent !important;overflow:hidden;}'
  document.head.appendChild(style)

  const root = document.getElementById('root')

  if (!root) {
    return
  }

  createRoot(root).render(
    <StrictMode>
      <ErrorBoundary label="glow-overlay">
        <GlowOverlayApp />
      </ErrorBoundary>
    </StrictMode>
  )
}
```

- [ ] **Step 2: Write the glow component**

`apps/desktop/src/app/glow-overlay/glow-overlay-app.tsx` — subscribes to the pushed `$voiceState` and paints the free-form, bottom-weighted glow on a canvas. Amplitude is eased toward `targetAmplitude`; blobs drift organically (never a rectangular frame).

```tsx
import { useEffect, useRef } from 'react'

import { targetAmplitude } from './glow-model'
import type { VoicePhase, VoiceState } from '@/store/voice-presence'

const BLOBS = [
  { hue: '#ff4f9d', ox: 0.18, oy: 1.02, rx: 0.42, ry: 0.5, sx: 1, sy: 0.6, sp: 0.7 },
  { hue: '#7a4bff', ox: 0.82, oy: 1.0, rx: 0.4, ry: 0.5, sx: -0.9, sy: 0.7, sp: 0.55 },
  { hue: '#3f8cff', ox: 0.5, oy: 1.05, rx: 0.5, ry: 0.55, sx: 0.6, sy: 0.5, sp: 0.9 },
  { hue: '#9a5bff', ox: -0.02, oy: 0.2, rx: 0.5, ry: 0.6, sx: 0.5, sy: 0.6, sp: 0.45 },
  { hue: '#34d6d6', ox: 1.02, oy: 0.2, rx: 0.5, ry: 0.6, sx: -0.5, sy: 0.6, sp: 0.5 },
  { hue: '#ff8a4f', ox: 0.8, oy: -0.04, rx: 0.45, ry: 0.5, sx: -0.6, sy: 0.5, sp: 0.6 }
]

export function GlowOverlayApp() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const stateRef = useRef<VoiceState>({ phase: 'off', level: 0, muted: false })

  useEffect(() => {
    const off = window.hermesDesktop?.glowOverlay?.onState(payload => {
      stateRef.current = payload
    })
    return off
  }, [])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) {
      return
    }

    const ctx = canvas.getContext('2d')
    if (!ctx) {
      return
    }

    let raf = 0
    let amp = 0
    let t = 0

    const resize = () => {
      canvas.width = window.innerWidth
      canvas.height = window.innerHeight
    }
    resize()
    window.addEventListener('resize', resize)

    const draw = () => {
      t += 0.016
      const { phase, level } = stateRef.current
      const target = targetAmplitude(phase as VoicePhase, level)
      amp += (target - amp) * 0.12

      const W = canvas.width
      const H = canvas.height
      ctx.clearRect(0, 0, W, H)

      if (amp > 0.01) {
        ctx.globalCompositeOperation = 'lighter'
        ctx.filter = 'blur(80px)'
        for (const b of BLOBS) {
          const dx = Math.sin(t * b.sp) * 60 * b.sx
          const dy = Math.cos(t * b.sp * 0.8) * 50 * b.sy
          const cx = b.ox * W + dx
          const cy = b.oy * H + dy
          const r = Math.max(W, H) * 0.32 * (0.7 + amp * 0.6)
          const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r)
          g.addColorStop(0, hexAlpha(b.hue, 0.55 * amp))
          g.addColorStop(1, hexAlpha(b.hue, 0))
          ctx.fillStyle = g
          ctx.beginPath()
          ctx.ellipse(cx, cy, r * b.rx, r * b.ry, 0, 0, Math.PI * 2)
          ctx.fill()
        }
        ctx.filter = 'none'
        ctx.globalCompositeOperation = 'source-over'
      }

      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)

    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', resize)
    }
  }, [])

  return <canvas ref={canvasRef} style={{ display: 'block', width: '100vw', height: '100vh' }} />
}

function hexAlpha(hex: string, a: number): string {
  const n = parseInt(hex.slice(1), 16)
  const r = (n >> 16) & 255
  const g = (n >> 8) & 255
  const b = n & 255
  return `rgba(${r},${g},${b},${a})`
}
```

- [ ] **Step 3: Route `?win=glow` in main.tsx**

Replace the existing `win` branch (lines 32-34) with:

```tsx
const win = new URLSearchParams(window.location.search).get('win')

if (win === 'overlay') {
  void import('./app/pet-overlay/overlay-root').then(({ mountPetOverlay }) => mountPetOverlay())
} else if (win === 'glow') {
  void import('./app/glow-overlay/glow-root').then(({ mountGlowOverlay }) => mountGlowOverlay())
} else {
```

(Keep the existing `else { createRoot(...) }` body unchanged.)

- [ ] **Step 4: Typecheck**

Run: `cd apps/desktop && npx tsc --noEmit`
Expected: no new errors mentioning `glow-overlay` or `main.tsx`.

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src/app/glow-overlay apps/desktop/src/main.tsx
git commit -m "feat(desktop): render free-form voice glow overlay surface"
```

---

### Task 7: Publish voice state from the composer

**Files:**
- Modify: `apps/desktop/src/app/chat/composer/index.tsx` (after the `conversation` hook, ~line 2007)

- [ ] **Step 1: Confirm the wake-word status is in scope**

Run: `grep -n "useWakeWord\|wakeStatus\|wake" apps/desktop/src/app/chat/composer/index.tsx`
Expected: find where `useWakeWord` is consumed and what its return is bound to (e.g. `const wake = useWakeWord({...})` exposing `wake.status`). Note the exact variable name; the step below assumes `wake.status`. If wake-word is consumed in a different component than the composer, publish from there instead (same pattern) and use `'idle'` as the `wakeStatus` fallback here.

- [ ] **Step 2: Add the publish effect**

Add the import near the other store imports at the top of the file:

```tsx
import { deriveVoicePhase, publishVoiceState } from '@/store/voice-presence'
```

Add this effect right after the `conversation = useVoiceConversation({ … })` block (~line 2007):

```tsx
  // Mirror the live voice + wake-word status into $voiceState so the always-on
  // glow overlay can react. Read-only — does not change the engines.
  useEffect(() => {
    publishVoiceState({
      phase: deriveVoicePhase({
        active: voiceConversationActive,
        voiceStatus: conversation.status,
        wakeStatus: wake.status
      }),
      level: conversation.level,
      muted: conversation.muted
    })
  }, [voiceConversationActive, conversation.status, conversation.level, conversation.muted, wake.status])
```

If `wake.status` is not available in this component, use `wakeStatus: 'idle'` here and add the same `publishVoiceState` effect in the component that owns `useWakeWord`, merging on the wake side.

- [ ] **Step 3: Initialize the bridge once at app start**

In `apps/desktop/src/app/desktop-controller.tsx`, near the other one-time bridge init (search for `initPetOverlayBridge`), add:

```tsx
import { initGlowOverlayBridge } from '@/store/glow-overlay'
```

and, in the same `useEffect` that wires `initPetOverlayBridge()`:

```tsx
    const offGlow = initGlowOverlayBridge()
```

and include `offGlow()` in that effect's cleanup return.

- [ ] **Step 4: Typecheck**

Run: `cd apps/desktop && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src/app/chat/composer/index.tsx apps/desktop/src/app/desktop-controller.tsx
git commit -m "feat(desktop): publish voice state and init glow bridge"
```

---

### Task 8: Hide-to-tray so voice keeps running while minimized

**Files:**
- Modify: `apps/desktop/electron/main.cjs` (the main window `close`/`minimize` handler in `createWindow`, ~line 5820+)

- [ ] **Step 1: Inspect the current close behavior**

Run: `grep -n "isQuitting\|on('close'\|on('minimize'\|mainWindow.on" apps/desktop/electron/main.cjs`
Expected: locate the main window's `close` handler. The tray already exists (`createTray`, line 5592) with `isQuitting`.

- [ ] **Step 2: Ensure close hides to tray instead of quitting**

In `createWindow`, confirm/add a `close` handler on `mainWindow` so that, unless `isQuitting`, the window hides instead of closing (Windows). If it already exists, no change. Otherwise add after the window is created:

```js
  mainWindow.on('close', event => {
    if (!isQuitting && IS_WINDOWS) {
      event.preventDefault()
      mainWindow.hide()
    }
  })
```

This keeps the renderer (mic, wake-word, STT/TTS, `$voiceState`) alive while minimized, so the glow and hands-free voice keep working. `createTray()` already provides Show/Quit.

- [ ] **Step 3: Sanity-check the file parses**

Run: `cd apps/desktop && node --check electron/main.cjs`
Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
git add apps/desktop/electron/main.cjs
git commit -m "feat(desktop): hide main window to tray so voice keeps running"
```

---

### Task 9: Manual end-to-end verification

**Files:** none (manual).

- [ ] **Step 1: Build + run the desktop app**

Run the desktop app in dev (per repo README, e.g. `cd apps/desktop && npm run dev`), connected to a backend with STT/TTS + wake word already configured.

- [ ] **Step 2: Verify idle = dark**

With no voice activity, confirm no glow is drawn and (after the linger) the glow window is closed — check it is not in the window list and CPU/GPU is at rest.

- [ ] **Step 3: Verify wake ignites the glow**

Say the configured wake word. Confirm the edge glow blooms (bottom-weighted, organic — not a rectangular frame), flows while listening (reacts to your voice), swirls while thinking, and pulses while speaking, then fades to dark.

- [ ] **Step 4: Verify click-through + minimized operation**

Minimize the app to the tray. Confirm: (a) the renderer keeps running (wake word still triggers), (b) the glow appears over whatever app is focused, and (c) clicks on screen pass through to the app underneath (the glow never intercepts a click).

- [ ] **Step 5: Commit any fixes found, then finish**

If steps revealed issues, fix and commit. Otherwise this plan is complete.

---

## Self-review notes

- Spec coverage: `$voiceState` wire (Task 1, 7), fullscreen transparent click-through glow window (Task 3, 6), free-form bottom-weighted glow / not a frame (Task 6 renderer + glow-model), idle=off (Task 2 `targetAmplitude('off')=0` + Task 5 close linger), tray keep-alive (Task 8), "don't touch voice/wake-word engines" (Tasks only read hook outputs). Brand monochrome chrome and the `show_card`/card capsule are Plan B.
- Type consistency: `VoiceState`/`VoicePhase` defined in Task 1 are used identically in Tasks 2, 4, 5, 6, 7. `glowOverlay` API shape matches across preload (Task 4), types (Task 4), controller (Task 5), renderer (Task 6).
- Known assumption flagged inline: Task 7 Step 1 verifies where `useWakeWord` lives; if not in the composer, publish wake status from its owner.
