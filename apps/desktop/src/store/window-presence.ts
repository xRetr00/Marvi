import { atom } from 'nanostores'

// Live state of the MAIN app window, mirrored from the main process. Used to
// decide whether the voice-island should surface cards — we don't want to show
// island cards when the user is already looking at the main Marvi window.
export const $mainWindowFocused = atom(false)
export const $mainWindowVisible = atom(false)

let started = false

/** Subscribe to main-process window-state broadcasts. Call once at app boot. */
export function initWindowPresence(): void {
  if (started || !window.hermesDesktop?.onWindowStateChanged) {
    return
  }

  started = true
  window.hermesDesktop.onWindowStateChanged(state => {
    $mainWindowFocused.set(Boolean(state?.focused))
    $mainWindowVisible.set(Boolean(state?.visible))
  })
}
