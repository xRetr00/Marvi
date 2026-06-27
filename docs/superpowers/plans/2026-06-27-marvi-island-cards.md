# Marvi island cards (`show_card` + capsule) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the agent surface compact cards (info / result / approval) on the voice presence — a glass capsule rendered inside the edge-glow window — via a new `show_card` tool, plus mirror the existing `approval.request` stream onto the same capsule.

**Architecture:** A new fire-and-forget per-session UI-event seam (`tools/ui_events.py`, modeled on `tools/approval.py`'s `register_gateway_notify`) lets a tool push an event to the connected client. The gateway forwards it onto the run's event stream (mirroring `_approval_notify`). The desktop event router (`use-message-stream.ts`) routes `card.show` into an island-card queue (ported from the Marvex prototype). The glow overlay renders the active card as a glass capsule and sends card actions back to the main renderer over IPC.

**Tech Stack:** Python (tool + gateway seam), Electron IPC, React + nanostores, Vitest + pytest.

This is Plan B of two. It depends on Plan A (`2026-06-27-marvi-voice-presence-overlay.md`) — the glow window, its IPC bridge, and `$voiceState` must exist first.

---

### Task 1: Port the island card queue (pure)

**Files:**
- Create: `apps/desktop/src/lib/island-queue.ts`
- Test: `apps/desktop/src/lib/island-queue.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
import { describe, expect, it, vi } from 'vitest'

import { createIslandQueue } from './island-queue'

describe('createIslandQueue', () => {
  it('shows the first card as active and queues the rest', () => {
    const q = createIslandQueue()
    expect(q.show({ id: 'a', kind: 'info' })).toBe('a')
    q.show({ id: 'b', kind: 'info' })
    const snap = q.snapshot()
    expect(snap.active?.id).toBe('a')
    expect(snap.queued.map(c => c.id)).toEqual(['b'])
  })

  it('promotes the next card on dismiss', () => {
    const q = createIslandQueue()
    q.show({ id: 'a', kind: 'info' })
    q.show({ id: 'b', kind: 'info' })
    q.dismiss('a')
    expect(q.snapshot().active?.id).toBe('b')
  })

  it('force replaces the active card immediately', () => {
    const q = createIslandQueue()
    q.show({ id: 'a', kind: 'info' })
    q.show({ id: 'urgent', kind: 'approval' }, { force: true })
    expect(q.snapshot().active?.id).toBe('urgent')
  })

  it('auto-dismisses after the duration', () => {
    vi.useFakeTimers()
    const q = createIslandQueue()
    q.show({ id: 'a', kind: 'info', duration: 1000, autoDismiss: true })
    expect(q.snapshot().active?.id).toBe('a')
    vi.advanceTimersByTime(1001)
    expect(q.snapshot().active).toBeNull()
    vi.useRealTimers()
  })

  it('trims the queue to maxQueue', () => {
    const q = createIslandQueue({ maxQueue: 1 })
    q.show({ id: 'a', kind: 'info' })
    q.show({ id: 'b', kind: 'info' })
    q.show({ id: 'c', kind: 'info' })
    expect(q.snapshot().queued.map(c => c.id)).toEqual(['c'])
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/desktop && npx vitest run src/lib/island-queue.test.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

Ported from the Marvex prototype (`islandQueue.ts`), trimmed to what v1 needs.

```ts
export type IslandCardKind = 'info' | 'result' | 'approval'

export interface IslandCardAction {
  id: string
  label: string
  /** Free-text sent back as a user turn, or a choice token resolved by the caller. */
  value?: string
}

export interface IslandCard {
  id: string
  kind: IslandCardKind
  title?: string
  body?: string
  duration?: number
  autoDismiss?: boolean
  actions?: IslandCardAction[]
}

export interface IslandQueueSnapshot {
  active: IslandCard | null
  queued: IslandCard[]
}

export interface IslandQueueOptions {
  maxQueue?: number
  onChange?: (snapshot: IslandQueueSnapshot) => void
}

export function createIslandQueue(options: IslandQueueOptions = {}) {
  let active: IslandCard | null = null
  let queued: IslandCard[] = []
  let timer: ReturnType<typeof setTimeout> | null = null

  const snapshot = (): IslandQueueSnapshot => ({ active, queued: [...queued] })
  const emit = () => options.onChange?.(snapshot())

  const clearTimer = () => {
    if (timer) clearTimeout(timer)
    timer = null
  }

  const trimQueue = () => {
    if (typeof options.maxQueue !== 'number' || options.maxQueue < 0) return
    while (queued.length > options.maxQueue) queued.shift()
  }

  const armTimer = () => {
    clearTimer()
    if (!active) return
    const autoDismiss = active.autoDismiss ?? false
    const duration = active.duration ?? 0
    if (!autoDismiss || duration <= 0) return
    timer = setTimeout(() => dismiss(active?.id), duration)
  }

  const promote = () => {
    active = queued.shift() ?? null
    armTimer()
    emit()
  }

  const show = (card: IslandCard, opts: { force?: boolean } = {}): string => {
    if (opts.force || !active) {
      active = card
      trimQueue()
      armTimer()
      emit()
      return card.id
    }
    queued.push(card)
    trimQueue()
    emit()
    return card.id
  }

  const dismiss = (id?: string) => {
    if (id && active?.id !== id) {
      queued = queued.filter(card => card.id !== id)
      emit()
      return
    }
    clearTimer()
    promote()
  }

  const dismissAll = () => {
    clearTimer()
    active = null
    queued = []
    emit()
  }

  return { show, dismiss, dismissAll, snapshot }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/desktop && npx vitest run src/lib/island-queue.test.ts`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src/lib/island-queue.ts apps/desktop/src/lib/island-queue.test.ts
git commit -m "feat(desktop): port island card queue from prototype"
```

---

### Task 2: Island card store + glow payload extension

**Files:**
- Create: `apps/desktop/src/store/island-cards.ts`
- Modify: `apps/desktop/src/store/glow-overlay.ts` (from Plan A)

- [ ] **Step 1: Write the store**

`island-cards.ts` owns one queue instance and exposes a nanostore atom of its snapshot, plus a submit handler seam (so card actions become real turns), mirroring `pet-overlay.ts`'s `setPetOverlaySubmitHandler`.

```ts
import { atom } from 'nanostores'

import { createIslandQueue, type IslandCard, type IslandQueueSnapshot } from '@/lib/island-queue'

export const $islandCards = atom<IslandQueueSnapshot>({ active: null, queued: [] })

const queue = createIslandQueue({ maxQueue: 3, onChange: snap => $islandCards.set(snap) })

export function showIslandCard(card: IslandCard): void {
  queue.show(card, { force: card.kind === 'approval' })
}

export function dismissIslandCard(id?: string): void {
  queue.dismiss(id)
}

let submitHandler: ((text: string) => void) | null = null

/** Register how a card action's text becomes a real user turn. */
export function setIslandCardSubmitHandler(fn: ((text: string) => void) | null): void {
  submitHandler = fn
}

export function runIslandCardAction(text: string): void {
  submitHandler?.(text)
}
```

- [ ] **Step 2: Push the active card into the glow window**

In `apps/desktop/src/store/glow-overlay.ts`, subscribe to `$islandCards` alongside `$voiceState` so the active card rides into the glow window. Add at the top:

```ts
import { $islandCards } from './island-cards'
```

Inside `initGlowOverlayBridge`, after the existing `$voiceState.subscribe(...)`, add a second subscription:

```ts
  const unsubCards = $islandCards.subscribe(snap => {
    if (snap.active) {
      cancelClose()
      ensureOpen()
    }
    if (open) {
      window.hermesDesktop?.glowOverlay?.pushCard(snap.active)
    }
  })
```

and update the returned disposer to also call `unsubCards()`. (Keep the existing `$voiceState` close-linger; an active card also holds the window open.)

- [ ] **Step 3: Typecheck**

Run: `cd apps/desktop && npx tsc --noEmit`
Expected: errors only about `pushCard` not existing yet (added in Task 3) — proceed.

- [ ] **Step 4: Commit**

```bash
git add apps/desktop/src/store/island-cards.ts apps/desktop/src/store/glow-overlay.ts
git commit -m "feat(desktop): island card store + push active card to glow"
```

---

### Task 3: IPC + preload + types for card push/action

**Files:**
- Modify: `apps/desktop/electron/main.cjs` (the glow IPC block from Plan A)
- Modify: `apps/desktop/electron/preload.cjs` (the `glowOverlay` block from Plan A)
- Modify: `apps/desktop/src/global.d.ts` (the `glowOverlay` type from Plan A)

- [ ] **Step 1: Add main-process IPC**

In `apps/desktop/electron/main.cjs`, after the `hermes:glow:state` handler (Plan A), add:

```js
// Main renderer → glow window: the active island card (or null to clear).
ipcMain.on('hermes:glow:card', (_event, payload) => {
  if (glowOverlayWindow && !glowOverlayWindow.isDestroyed()) {
    glowOverlayWindow.webContents.send('hermes:glow:card', payload)
  }
})
// Glow window → main renderer: a card action (dismiss / submit text).
ipcMain.on('hermes:glow:card-action', (_event, payload) => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('hermes:glow:card-action', payload)
  }
})
// The capsule needs clicks while a card with actions is shown; the glow window
// is otherwise click-through. The renderer toggles this like the pet overlay.
ipcMain.on('hermes:glow:set-ignore-mouse', (_event, ignore) => {
  if (glowOverlayWindow && !glowOverlayWindow.isDestroyed()) {
    glowOverlayWindow.setIgnoreMouseEvents(Boolean(ignore), { forward: true })
  }
})
```

- [ ] **Step 2: Add preload API**

In `apps/desktop/electron/preload.cjs`, extend the `glowOverlay` object with:

```js
    pushCard: card => ipcRenderer.send('hermes:glow:card', card),
    setIgnoreMouse: ignore => ipcRenderer.send('hermes:glow:set-ignore-mouse', ignore),
    cardAction: payload => ipcRenderer.send('hermes:glow:card-action', payload),
    onCard: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('hermes:glow:card', listener)
      return () => ipcRenderer.removeListener('hermes:glow:card', listener)
    },
    onCardAction: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('hermes:glow:card-action', listener)
      return () => ipcRenderer.removeListener('hermes:glow:card-action', listener)
    },
```

- [ ] **Step 3: Add types**

In `apps/desktop/src/global.d.ts`, add the import alongside the Plan A `VoiceState` import:

```ts
import type { IslandCard } from './lib/island-queue'
```

and extend the `glowOverlay` interface block with:

```ts
        pushCard: (card: IslandCard | null) => void
        setIgnoreMouse: (ignore: boolean) => void
        cardAction: (payload: { type: 'dismiss'; id?: string } | { type: 'submit'; text: string }) => void
        onCard: (callback: (card: IslandCard | null) => void) => () => void
        onCardAction: (
          callback: (payload: { type: 'dismiss'; id?: string } | { type: 'submit'; text: string }) => void
        ) => () => void
```

- [ ] **Step 4: Sanity-check + typecheck**

Run: `cd apps/desktop && node --check electron/main.cjs && npx tsc --noEmit`
Expected: main.cjs exit 0; tsc reports no errors mentioning `glowOverlay`/`pushCard`.

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/electron/main.cjs apps/desktop/electron/preload.cjs apps/desktop/src/global.d.ts
git commit -m "feat(desktop): IPC for island card push + actions"
```

---

### Task 4: Render the glass capsule in the glow window

**Files:**
- Create: `apps/desktop/src/app/glow-overlay/island-capsule.tsx`
- Modify: `apps/desktop/src/app/glow-overlay/glow-overlay-app.tsx` (from Plan A)

- [ ] **Step 1: Write the capsule component**

Monochrome ink-style glass capsule (matches the Marvi brand). Bottom-center. Becomes interactive only while shown (toggles `setIgnoreMouse`). Actions send back over IPC.

```tsx
import { useEffect, useState } from 'react'

import type { IslandCard } from '@/lib/island-queue'

export function IslandCapsule() {
  const [card, setCard] = useState<IslandCard | null>(null)

  useEffect(() => {
    const off = window.hermesDesktop?.glowOverlay?.onCard(next => setCard(next))
    return off
  }, [])

  useEffect(() => {
    // The capsule is the only clickable part of the otherwise click-through
    // glow window. Capture clicks while a card with actions is shown.
    const interactive = Boolean(card?.actions?.length)
    window.hermesDesktop?.glowOverlay?.setIgnoreMouse(!interactive)
  }, [card])

  if (!card) {
    return null
  }

  const dismiss = () => {
    window.hermesDesktop?.glowOverlay?.cardAction({ type: 'dismiss', id: card.id })
    setCard(null)
  }

  return (
    <div
      style={{
        position: 'absolute',
        bottom: 48,
        left: '50%',
        transform: 'translateX(-50%)',
        width: 320,
        background: 'rgba(18,18,22,0.72)',
        backdropFilter: 'blur(16px)',
        WebkitBackdropFilter: 'blur(16px)',
        border: '0.5px solid rgba(255,255,255,0.14)',
        borderRadius: 18,
        padding: '16px 18px',
        color: '#f2f2f7',
        fontFamily: 'system-ui, sans-serif'
      }}
    >
      {card.title && (
        <div style={{ fontSize: 11, letterSpacing: '0.12em', textTransform: 'uppercase', color: '#aab', marginBottom: 8 }}>
          {card.title}
        </div>
      )}
      {card.body && <div style={{ fontSize: 14, lineHeight: 1.5, marginBottom: card.actions?.length ? 14 : 0 }}>{card.body}</div>}
      {card.actions?.length ? (
        <div style={{ display: 'flex', gap: 8 }}>
          {card.actions.map(a => (
            <button
              key={a.id}
              onClick={() => {
                if (a.value) {
                  window.hermesDesktop?.glowOverlay?.cardAction({ type: 'submit', text: a.value })
                }
                dismiss()
              }}
              style={{
                flex: 1,
                background: a.id === 'primary' ? '#2b5bd0' : 'transparent',
                border: '0.5px solid rgba(255,255,255,0.2)',
                color: '#fff',
                borderRadius: 10,
                padding: '8px 0',
                fontSize: 13,
                cursor: 'pointer'
              }}
            >
              {a.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  )
}
```

- [ ] **Step 2: Mount it in the glow app**

In `apps/desktop/src/app/glow-overlay/glow-overlay-app.tsx`, import and render the capsule alongside the canvas:

```tsx
import { IslandCapsule } from './island-capsule'
```

Change the returned JSX from the single `<canvas .../>` to:

```tsx
  return (
    <>
      <canvas ref={canvasRef} style={{ display: 'block', width: '100vw', height: '100vh' }} />
      <IslandCapsule />
    </>
  )
```

- [ ] **Step 3: Typecheck**

Run: `cd apps/desktop && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 4: Commit**

```bash
git add apps/desktop/src/app/glow-overlay/island-capsule.tsx apps/desktop/src/app/glow-overlay/glow-overlay-app.tsx
git commit -m "feat(desktop): render island glass capsule in glow window"
```

---

### Task 5: Route `card.show` events + wire card actions

**Files:**
- Modify: `apps/desktop/src/app/session/hooks/use-message-stream.ts:1052` (the event router)
- Modify: `apps/desktop/src/app/desktop-controller.tsx` (wire submit handler + action listener)

- [ ] **Step 1: Add the `card.show` case in the event router**

In `use-message-stream.ts`, find the `else if (event.type === 'approval.request') {` branch (~line 1052) and add, immediately before or after it:

```ts
      } else if (event.type === 'card.show') {
        const p = (event.payload ?? {}) as {
          id?: string
          kind?: 'info' | 'result' | 'approval'
          title?: string
          body?: string
          duration?: number
          actions?: { id: string; label: string; value?: string }[]
        }
        showIslandCard({
          id: p.id ?? `card-${Date.now()}`,
          kind: p.kind ?? 'info',
          title: p.title,
          body: p.body,
          duration: p.duration,
          autoDismiss: typeof p.duration === 'number' && p.duration > 0,
          actions: p.actions
        })
```

Add the import at the top of the file:

```ts
import { showIslandCard } from '@/store/island-cards'
```

- [ ] **Step 2: Wire the submit handler + action listener once**

In `apps/desktop/src/app/desktop-controller.tsx`, where Plan A added `initGlowOverlayBridge()`, also register the card submit handler and listen for capsule actions. Add imports:

```tsx
import { dismissIslandCard, setIslandCardSubmitHandler } from '@/store/island-cards'
```

In the same init effect, after `initGlowOverlayBridge()`:

```tsx
    // A card action's text becomes a real user turn. Reuse the existing submit
    // path the composer/pet overlay already use (search this file for how
    // setPetOverlaySubmitHandler is wired and mirror its target).
    setIslandCardSubmitHandler(text => {
      void submitUserText(text)
    })

    const offCardAction = window.hermesDesktop?.glowOverlay?.onCardAction(payload => {
      if (payload.type === 'dismiss') {
        dismissIslandCard(payload.id)
      } else if (payload.type === 'submit') {
        void submitUserText(payload.text)
      }
    })
```

and add `offCardAction?.()` and `setIslandCardSubmitHandler(null)` to the effect cleanup.

Note: `submitUserText` is whatever this file already calls to submit a message (the same function `setPetOverlaySubmitHandler` is given). Step 1 of verification below confirms its exact name.

- [ ] **Step 3: Confirm the submit function name**

Run: `grep -n "setPetOverlaySubmitHandler" apps/desktop/src/app/desktop-controller.tsx`
Use the exact callback passed there as `submitUserText` above (rename to match). If pet overlay's submit handler lives elsewhere, wire the island submit handler in that same place instead.

- [ ] **Step 4: Typecheck**

Run: `cd apps/desktop && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src/app/session/hooks/use-message-stream.ts apps/desktop/src/app/desktop-controller.tsx
git commit -m "feat(desktop): route card.show events and wire capsule actions"
```

---

### Task 6: UI-event emit seam (Python)

**Files:**
- Create: `tools/ui_events.py`
- Test: `tests/tools/test_ui_events.py`

- [ ] **Step 1: Write the failing test**

```python
from tools import ui_events


def test_emit_routes_to_registered_session_callback():
    received = []
    ui_events.register_ui_event_notify("sess-1", lambda evt: received.append(evt))
    try:
        ok = ui_events.emit_ui_event("sess-1", {"event": "card.show", "payload": {"body": "hi"}})
    finally:
        ui_events.unregister_ui_event_notify("sess-1")

    assert ok is True
    assert received == [{"event": "card.show", "payload": {"body": "hi"}}]


def test_emit_is_noop_without_listener():
    assert ui_events.emit_ui_event("nobody", {"event": "card.show"}) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/tools/test_ui_events.py -v`
Expected: FAIL — module `tools.ui_events` not found.

- [ ] **Step 3: Write minimal implementation**

Modeled on `tools/approval.py`'s `_gateway_notify_cbs`, but fire-and-forget (no blocking Event).

```python
"""Fire-and-forget UI events from tools to the connected client.

Mirrors the per-session callback registry in tools/approval.py, but for
non-blocking UI surfacing (e.g. show_card). A platform adapter registers a
callback per session that forwards the event onto that run's event stream;
tools call emit_ui_event() to push a card to the user's presence overlay.
"""

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_ui_event_cbs: dict[str, Callable[[dict], None]] = {}


def register_ui_event_notify(session_key: str, cb: Callable[[dict], None]) -> None:
    """Register the per-session UI-event forwarder."""
    if not session_key:
        return
    with _lock:
        _ui_event_cbs[session_key] = cb


def unregister_ui_event_notify(session_key: str) -> None:
    """Remove the per-session UI-event forwarder."""
    if not session_key:
        return
    with _lock:
        _ui_event_cbs.pop(session_key, None)


def emit_ui_event(session_key: str, event: dict) -> bool:
    """Send a UI event to the session's client. Returns True if delivered.

    Never raises — a missing listener (CLI, cron, tests) is a no-op.
    """
    with _lock:
        cb = _ui_event_cbs.get(session_key)
    if cb is None:
        return False
    try:
        cb(event)
        return True
    except Exception as exc:
        logger.debug("emit_ui_event delivery failed: %s", exc)
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/tools/test_ui_events.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add tools/ui_events.py tests/tools/test_ui_events.py
git commit -m "feat(tools): add fire-and-forget UI-event emit seam"
```

---

### Task 7: The `show_card` tool

**Files:**
- Create: `tools/show_card.py`
- Test: `tests/tools/test_show_card.py`

- [ ] **Step 1: Confirm the registry.register signature**

Run: `grep -n "def register\b" tools/registry.py` and read that function. Confirm the keyword args (`name`, `toolset`, `schema`, `handler`, `check_fn`, `description`, `emoji`, ...) matching `ToolEntry.__init__` (lines 86-106). The step below uses `registry.register(name=..., toolset=..., schema=..., handler=..., description=...)`; adjust kwarg names to the exact signature if they differ.

- [ ] **Step 2: Write the failing test**

```python
from unittest.mock import patch

from tools import show_card


def test_show_card_emits_event_and_returns_ok():
    captured = {}

    def fake_emit(session_key, event):
        captured["session"] = session_key
        captured["event"] = event
        return True

    with patch("tools.show_card.get_current_session_key", return_value="s1"), patch(
        "tools.show_card.emit_ui_event", side_effect=fake_emit
    ):
        result = show_card.handle_show_card({"title": "Done", "body": "Shipped it", "kind": "result"})

    assert result["success"] is True
    assert captured["session"] == "s1"
    assert captured["event"]["event"] == "card.show"
    assert captured["event"]["payload"]["body"] == "Shipped it"
    assert captured["event"]["payload"]["kind"] == "result"


def test_show_card_reports_when_no_client():
    with patch("tools.show_card.get_current_session_key", return_value="s1"), patch(
        "tools.show_card.emit_ui_event", return_value=False
    ):
        result = show_card.handle_show_card({"body": "hi"})

    assert result["success"] is False
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/tools/test_show_card.py -v`
Expected: FAIL — module not found.

- [ ] **Step 4: Write minimal implementation**

```python
"""show_card tool: surface a compact card on the user's voice presence overlay.

Voice-first "show, don't say": when the agent wants to display a short result,
list, link, or a confirm prompt instead of speaking it, it calls show_card and
the desktop renders a glass capsule on the edge-glow presence.
"""

import uuid

from tools import registry
from tools.approval import get_current_session_key
from tools.ui_events import emit_ui_event

SHOW_CARD_SCHEMA = {
    "name": "show_card",
    "description": (
        "Show a compact card on the user's voice presence overlay (a small "
        "glass capsule). Use during voice interactions to SHOW something "
        "(a short result, a list, a link, or a confirm prompt) instead of "
        "speaking it aloud. Not for long text — keep body under ~200 chars."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "body": {"type": "string", "description": "The main line of the card (short)."},
            "title": {"type": "string", "description": "Optional small uppercase label."},
            "kind": {
                "type": "string",
                "enum": ["info", "result", "approval"],
                "description": "Card style. Default info.",
            },
            "duration_ms": {
                "type": "integer",
                "description": "Auto-dismiss after this many ms. Omit to keep until dismissed.",
            },
            "actions": {
                "type": "array",
                "description": "Optional buttons. Each action's value is sent back as a user message when clicked.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["id", "label"],
                },
            },
        },
        "required": ["body"],
    },
}


def handle_show_card(args: dict) -> dict:
    """Emit a card.show UI event to the connected client."""
    body = (args or {}).get("body", "")
    if not body:
        return {"success": False, "error": "body is required"}

    payload = {
        "id": str(uuid.uuid4()),
        "kind": args.get("kind", "info"),
        "title": args.get("title"),
        "body": body,
        "duration": args.get("duration_ms"),
        "actions": args.get("actions"),
    }

    session_key = get_current_session_key(default="")
    delivered = emit_ui_event(session_key, {"event": "card.show", "payload": payload})

    if not delivered:
        return {
            "success": False,
            "error": "No connected client to show the card (cards work in the desktop app voice presence).",
        }
    return {"success": True, "message": "Card shown."}


registry.register(
    name="show_card",
    toolset="voice",
    schema=SHOW_CARD_SCHEMA,
    handler=handle_show_card,
    description="Show a compact card on the voice presence overlay.",
)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/tools/test_show_card.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add tools/show_card.py tests/tools/test_show_card.py
git commit -m "feat(tools): add show_card tool"
```

---

### Task 8: Forward UI events from the gateway

**Files:**
- Modify: `gateway/platforms/api_server.py` (the `_approval_notify` / `_run_sync` region, ~line 3965-4019)

- [ ] **Step 1: Register a UI-event forwarder beside the approval one**

In `api_server.py`, immediately after the `_approval_notify` function definition (~line 3990), add a sibling that pushes onto the same run queue `q` (non-blocking, no status change):

```python
                def _ui_event_notify(ui_event: dict) -> None:
                    event = dict(ui_event or {})
                    event.setdefault("run_id", run_id)
                    event.setdefault("timestamp", time.time())
                    try:
                        loop.call_soon_threadsafe(q.put_nowait, event)
                    except Exception:
                        pass
```

- [ ] **Step 2: Register/unregister it in `_run_sync`**

In `_run_sync`, alongside the existing approval import and `register_gateway_notify(...)` (~line 3993-4019), add:

```python
                    from tools.ui_events import (
                        register_ui_event_notify,
                        unregister_ui_event_notify,
                    )
```

Register right after `register_gateway_notify(approval_session_key, _approval_notify)`:

```python
                        register_ui_event_notify(approval_session_key, _ui_event_notify)
```

And in the `finally` block, beside `unregister_gateway_notify(approval_session_key)`:

```python
                            unregister_ui_event_notify(approval_session_key)
```

- [ ] **Step 3: Verify the event passes through the stream's allowlist**

The `card.show` event carries `event: "card.show"` (like `event: "approval.request"`). Confirm the SSE/stream layer forwards arbitrary `event` keys (approval.request already rides this path, so a sibling event does too). Run the gateway and confirm no schema rejection for the new event key:

Run: `grep -n "approval.request\|event\[.event.\]\|allowed_events\|EVENT_ALLOWLIST" gateway/platforms/api_server.py`
Expected: confirm there is no explicit allowlist that would drop `card.show`. If one exists, add `card.show` to it.

- [ ] **Step 4: Sanity-check the file imports**

Run: `python -c "import ast; ast.parse(open('gateway/platforms/api_server.py').read())"`
Expected: no output (parses clean).

- [ ] **Step 5: Commit**

```bash
git add gateway/platforms/api_server.py
git commit -m "feat(gateway): forward show_card UI events to the client"
```

---

### Task 9: Mirror existing approvals onto the capsule (stream reuse)

**Files:**
- Modify: `apps/desktop/src/app/session/hooks/use-message-stream.ts` (the existing `approval.request` branch, ~line 1052)

- [ ] **Step 1: Surface an approval as an island card too**

The `approval.request` branch already drives the in-app approval row. Add, inside that same branch, a card so the approval also shows on the presence. Use the existing approval payload fields (command, description). Card actions reuse the existing approval resolution — for v1, the buttons send the gateway's approval choices as text via the same submit path is NOT correct for approvals; instead dismiss-on-resolve. Keep it minimal: show an info card mirroring the prompt, auto-dismissed when the approval resolves.

```ts
        // Mirror the approval onto the voice presence so it's visible while the
        // app is minimized. Resolution still happens through the existing
        // in-app approval flow; this card is informational + dismissed on
        // resolve.
        const ap = (event.payload ?? {}) as { command?: string; description?: string }
        showIslandCard({
          id: `approval-${event.payload && (event.payload as { run_id?: string }).run_id}`,
          kind: 'approval',
          title: 'Approval needed',
          body: ap.description || ap.command || 'A command needs your approval.',
          actions: [{ id: 'primary', label: 'Open app', value: '' }]
        })
```

(The "Open app" action with empty `value` simply dismisses + the user resolves in-app. Wiring island buttons directly to gateway approve/deny is a follow-up — out of scope here.)

- [ ] **Step 2: Dismiss the card when the approval resolves**

Find the event that clears an approval (search the file for where the `approval.request` row is removed/resolved, e.g. an `approval.resolved` or status change) and call `dismissIslandCard(\`approval-${runId}\`)` there. Add the import if missing:

```ts
import { dismissIslandCard, showIslandCard } from '@/store/island-cards'
```

Run: `grep -n "approval" apps/desktop/src/app/session/hooks/use-message-stream.ts`
Expected: locate the resolve/clear point; add the dismiss call there.

- [ ] **Step 3: Typecheck**

Run: `cd apps/desktop && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 4: Commit**

```bash
git add apps/desktop/src/app/session/hooks/use-message-stream.ts
git commit -m "feat(desktop): mirror approval prompts onto the voice presence"
```

---

### Task 10: End-to-end verification

**Files:** none (manual + suites).

- [ ] **Step 1: Run the test suites**

Run: `cd apps/desktop && npx vitest run src/lib/island-queue.test.ts` and `python -m pytest tests/tools/test_ui_events.py tests/tools/test_show_card.py -v`
Expected: all pass.

- [ ] **Step 2: Confirm `show_card` is registered in the voice toolset**

Run the agent and confirm `show_card` appears in available tools when the `voice` toolset is enabled (check via `hermes tools` or the tool list). If `voice` is not an existing toolset name, change `toolset=` in `tools/show_card.py` to an existing one (run `grep -rn "toolset=" tools/*.py | head` to see valid names) and re-commit.

- [ ] **Step 3: Manual: agent shows a card**

In the desktop app, in a voice session, prompt the agent to use `show_card` (e.g. "show me a card that says hello"). Confirm the glass capsule appears bottom-center inside the glow, is readable, and dismisses (auto if `duration_ms` set, or via its action).

- [ ] **Step 4: Manual: approval mirrors to the capsule**

Trigger a dangerous command in a voice session; confirm an "Approval needed" capsule appears on the presence and is cleared once you resolve the approval in-app.

- [ ] **Step 5: Commit any fixes; finish**

---

## Self-review notes

- Spec coverage: `show_card` tool (Task 7), island queue ported from Marvex (Task 1), capsule render (Task 4), event transport tool→gateway→desktop (Tasks 6, 8, 5), stream reuse of approvals (Task 9), card store + glow integration (Tasks 2, 3). Brand monochrome capsule (Task 4 styling).
- Type consistency: `IslandCard`/`IslandCardKind`/`IslandCardAction` defined in Task 1 are reused identically in Tasks 2-5. The `glowOverlay` additions (`pushCard`, `onCard`, `setIgnoreMouse`, `cardAction`, `onCardAction`) match across preload (Task 3), types (Task 3), store (Task 2), capsule (Task 4). `card.show` event shape is identical in the tool payload (Task 7), gateway passthrough (Task 8), and desktop router (Task 5).
- Flagged discovery steps (no fabricated identifiers): Task 5/9 confirm the submit-handler symbol via the existing `setPetOverlaySubmitHandler` wiring; Task 7 confirms `registry.register` kwargs; Task 8 confirms no event allowlist drops `card.show`; Task 10 confirms the `voice` toolset name.
- Dependency: requires Plan A's glow window, `glowOverlay` bridge, `initGlowOverlayBridge`, and `$voiceState`.
