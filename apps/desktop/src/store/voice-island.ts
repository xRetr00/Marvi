import { vpLog } from '@/lib/voice-presence-log'

import { $islandCards } from './island-cards'
import { $voiceState } from './voice-presence'
import { $islandEnabled, $presenceEnabled } from './voice-presence-settings'

/**
 * Main-renderer controller for the voice presence island window. The island window
 * carries no gateway — this renderer is the single source of truth and pushes
 * $voiceState into it over IPC (mirrors the pet-overlay pattern). The window is
 * opened lazily on the first non-`off` phase and closed shortly after returning
 * to `off`, so idle costs nothing.
 */

let unsub: (() => void) | null = null
let unsubCards: (() => void) | null = null
let unsubIsland: (() => void) | null = null
let unsubPresence: (() => void) | null = null
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
  vpLog('window', 'open')
  void window.hermesDesktop?.islandOverlay
    ?.open()
    .then(() => {
      // The window may mount after the synchronous push in the subscriber, so
      // hand it a first frame once it actually exists.
      window.hermesDesktop?.islandOverlay?.pushState($voiceState.get())
      window.hermesDesktop?.islandOverlay?.pushCard($islandCards.get().active)
    })
    .catch(() => {
      // Open failed (IPC hiccup / window destroyed) — clear the flag so the
      // next non-off tick retries instead of pushing to a dead window.
      open = false
    })
}

function scheduleClose(): void {
  if (closeTimer) {
    return
  }

  closeTimer = setTimeout(() => {
    closeTimer = null
    open = false
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

// The window should be visible when the island is enabled AND there's something to
// show — an active voice phase or a card. The island toggle gates the whole window
// (the capsule lives in it too), so turning the island off hides the presence.
function shouldBeOpen(): boolean {
  // Master presence switch gates the whole window; the island toggle gates the
  // visual specifically.
  if (!$presenceEnabled.get() || !$islandEnabled.get()) {
    return false
  }

  return $voiceState.get().phase !== 'off' || $islandCards.get().active !== null
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
  }
}

/** Start mirroring $voiceState + cards into the island window. Idempotent. */
export function initVoiceIslandBridge(): () => void {
  if (unsub || !window.hermesDesktop?.islandOverlay) {
    return () => {}
  }

  unsub = $voiceState.subscribe(() => evaluate())
  unsubCards = $islandCards.subscribe(() => evaluate())
  unsubIsland = $islandEnabled.subscribe(() => evaluate())
  unsubPresence = $presenceEnabled.subscribe(() => evaluate())

  return () => {
    unsub?.()
    unsub = null
    unsubCards?.()
    unsubCards = null
    unsubIsland?.()
    unsubIsland = null
    unsubPresence?.()
    unsubPresence = null
    cancelClose()
    open = false
  }
}
