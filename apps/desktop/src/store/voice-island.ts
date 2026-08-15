import type { IslandWorkState } from '@/lib/island-work'
import { vpLog } from '@/lib/voice-presence-log'

import { $statusItemsBySession } from './composer-status'
import { $islandActivity } from './island-activity'
import { $islandCards } from './island-cards'
import { $activeSessionId, $busy } from './session'
import { $voiceState, type VoicePhase } from './voice-presence'
import { $islandEnabled, $islandPosition, $presenceEnabled } from './voice-presence-settings'

/**
 * Main-renderer controller for the voice presence island window. The island window
 * carries no gateway — this renderer is the single source of truth and pushes
 * $voiceState into it over IPC (mirrors the pet-overlay pattern). The window is an
 * always-present ambient layer while presence + island are enabled: it opens on
 * enable and rests as a tiny seed, morphing to idle/expanded on activity. It only
 * closes when a toggle turns off.
 */

let unsub: (() => void) | null = null
let unsubCards: (() => void) | null = null
let unsubActivity: (() => void) | null = null
let unsubIsland: (() => void) | null = null
let unsubPosition: (() => void) | null = null
let unsubPresence: (() => void) | null = null
let unsubStatus: (() => void) | null = null
let unsubSession: (() => void) | null = null
let unsubBusy: (() => void) | null = null
let open = false
let retryAfter = 0
let closeTimer: ReturnType<typeof setTimeout> | null = null

// ponytail: 1.2s linger before closing so a brief idle gap between a turn and
// the next wake doesn't tear the window down and respawn it.
const CLOSE_LINGER_MS = 1200

export function currentIslandWork(): IslandWorkState | null {
  const sid = $activeSessionId.get()
  const activity = $islandActivity.get()
  const source = sid ? ($statusItemsBySession.get()[sid] ?? []) : []

  const items = source.map(item => ({
    id: item.id,
    meta: item.currentTool || item.todoStatus || item.type,
    state: item.todoStatus === 'pending' ? ('pending' as const) : item.state,
    title: item.title
  }))

  if (activity && !items.some(item => item.state === 'running' && item.title === activity)) {
    items.unshift({ id: 'current-activity', meta: 'tool', state: 'running', title: activity })
  }

  if ($busy.get() && items.length === 0) {
    items.push({ id: 'thinking', meta: 'agent', state: 'running', title: 'Thinking' })
  }

  const active = $busy.get() || items.some(item => item.state === 'running')

  if (!active && items.length === 0) {
    return null
  }

  return {
    active,
    items,
    title: items.length > 1 ? 'Working through the plan' : activity || (active ? 'Marvi is working' : 'Work complete')
  }
}

function ensureOpen(): void {
  if (open || Date.now() < retryAfter) {
    return
  }

  const overlay = window.hermesDesktop?.islandOverlay

  if (!overlay) {
    vpLog('window', 'open failed', { error: 'island overlay preload API is unavailable' })
    retryAfter = Date.now() + 2000

    return
  }

  open = true
  vpLog('window', 'open')
  overlay.setPosition($islandPosition.get())
  void overlay
    .open()
    .then(() => {
      retryAfter = 0
      // The window may mount after the synchronous push in the subscriber, so
      // hand it a first frame once it actually exists.
      window.hermesDesktop?.islandOverlay?.pushState($voiceState.get())
      window.hermesDesktop?.islandOverlay?.pushCard($islandCards.get().active)
      window.hermesDesktop?.islandOverlay?.pushActivity($islandActivity.get())
      window.hermesDesktop?.islandOverlay?.pushWork(currentIslandWork())
    })
    .catch(error => {
      // Open failed (IPC hiccup / window destroyed) — clear the flag so the
      // next non-off tick retries instead of pushing to a dead window.
      open = false
      retryAfter = Date.now() + 2000
      vpLog('window', 'open failed', { error: error instanceof Error ? error.message : String(error) })
    })
}

function scheduleClose(): void {
  if (closeTimer) {
    return
  }

  closeTimer = setTimeout(() => {
    closeTimer = null
    open = false
    retryAfter = 0
    vpLog('window', 'close')
    void window.hermesDesktop?.islandOverlay?.close()
  }, CLOSE_LINGER_MS)
}

function cancelClose(): void {
  if (closeTimer) {
    clearTimeout(closeTimer)
    closeTimer = null
  }
}

export function shouldShowVoiceIsland(islandEnabled: boolean, presenceEnabled: boolean, phase: VoicePhase): boolean {
  return islandEnabled && (presenceEnabled || phase !== 'off')
}

function shouldBeOpen(): boolean {
  // Wake-word presence keeps the ambient seed alive. Explicit voice mode must
  // still show the island when background wake listening is disabled.
  return (
    $islandEnabled.get() &&
    ($presenceEnabled.get() || $voiceState.get().phase !== 'off' || currentIslandWork() !== null)
  )
}

// Re-evaluate open/closed from the three inputs and push the latest frame.
function evaluate(): void {
  if (shouldBeOpen()) {
    cancelClose()
    ensureOpen()
  } else {
    scheduleClose()
  }

  if (open) {
    window.hermesDesktop?.islandOverlay?.pushState($voiceState.get())
    window.hermesDesktop?.islandOverlay?.pushCard($islandCards.get().active)
    window.hermesDesktop?.islandOverlay?.pushActivity($islandActivity.get())
    window.hermesDesktop?.islandOverlay?.pushWork(currentIslandWork())
  }
}

/** Start mirroring $voiceState + cards + activity into the island window. Idempotent. */
export function initVoiceIslandBridge(): () => void {
  if (unsub || !window.hermesDesktop?.islandOverlay) {
    return () => {}
  }

  unsub = $voiceState.subscribe(() => evaluate())
  unsubCards = $islandCards.subscribe(() => evaluate())
  unsubActivity = $islandActivity.subscribe(() => evaluate())
  unsubIsland = $islandEnabled.subscribe(() => evaluate())
  unsubPosition = $islandPosition.subscribe(position => window.hermesDesktop?.islandOverlay?.setPosition(position))
  unsubPresence = $presenceEnabled.subscribe(() => evaluate())
  unsubStatus = $statusItemsBySession.subscribe(() => evaluate())
  unsubSession = $activeSessionId.subscribe(() => evaluate())
  unsubBusy = $busy.subscribe(() => evaluate())

  return () => {
    unsub?.()
    unsub = null
    unsubCards?.()
    unsubCards = null
    unsubActivity?.()
    unsubActivity = null
    unsubIsland?.()
    unsubIsland = null
    unsubPosition?.()
    unsubPosition = null
    unsubPresence?.()
    unsubPresence = null
    unsubStatus?.()
    unsubStatus = null
    unsubSession?.()
    unsubSession = null
    unsubBusy?.()
    unsubBusy = null
    cancelClose()
    open = false
  }
}
