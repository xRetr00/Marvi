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
