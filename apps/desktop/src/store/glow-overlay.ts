import { $voiceState } from './voice-presence'
import { $islandCards } from './island-cards'
import { $glowEnabled } from './voice-presence-settings'

/**
 * Main-renderer controller for the voice presence glow window. The glow window
 * carries no gateway — this renderer is the single source of truth and pushes
 * $voiceState into it over IPC (mirrors the pet-overlay pattern). The window is
 * opened lazily on the first non-`off` phase and closed shortly after returning
 * to `off`, so idle costs nothing.
 */

let unsub: (() => void) | null = null
let unsubCards: (() => void) | null = null
let unsubGlow: (() => void) | null = null
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
  void window.hermesDesktop?.glowOverlay
    ?.open()
    .then(() => {
      // The window may mount after the synchronous push in the subscriber, so
      // hand it a first frame once it actually exists.
      window.hermesDesktop?.glowOverlay?.pushState($voiceState.get())
      window.hermesDesktop?.glowOverlay?.pushCard($islandCards.get().active)
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
    void window.hermesDesktop?.glowOverlay?.close()
  }, CLOSE_LINGER_MS)
}

function cancelClose(): void {
  if (closeTimer) {
    clearTimeout(closeTimer)
    closeTimer = null
  }
}

// The window should be visible when the glow is enabled AND there's something to
// show — an active voice phase or a card. The glow toggle gates the whole window
// (the capsule lives in it too), so turning the glow off hides the presence.
function shouldBeOpen(): boolean {
  if (!$glowEnabled.get()) {
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
    window.hermesDesktop?.glowOverlay?.pushState($voiceState.get())
    window.hermesDesktop?.glowOverlay?.pushCard($islandCards.get().active)
  }
}

/** Start mirroring $voiceState + cards into the glow window. Idempotent. */
export function initGlowOverlayBridge(): () => void {
  if (unsub || !window.hermesDesktop?.glowOverlay) {
    return () => {}
  }

  unsub = $voiceState.subscribe(() => evaluate())
  unsubCards = $islandCards.subscribe(() => evaluate())
  unsubGlow = $glowEnabled.subscribe(() => evaluate())

  return () => {
    unsub?.()
    unsub = null
    unsubCards?.()
    unsubCards = null
    unsubGlow?.()
    unsubGlow = null
    cancelClose()
    open = false
  }
}
