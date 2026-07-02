import { vpLog } from '@/lib/voice-presence-log'

import { $islandCards } from './island-cards'
import { $voiceState } from './voice-presence'
import { $islandEnabled, $presenceEnabled } from './voice-presence-settings'

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

function shouldBeOpen(): boolean {
  // Ambient: the island is a persistent layer while enabled — it rests as a
  // seed and morphs on activity, rather than opening/closing per turn.
  return $presenceEnabled.get() && $islandEnabled.get()
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
