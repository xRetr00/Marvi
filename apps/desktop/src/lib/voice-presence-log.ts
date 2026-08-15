import { $voicePresenceDebug } from '@/store/voice-presence-settings'

// Dedicated, separable logging for the voice-presence mode (Dynamic Island +
// wake word + cards). Off by default; enable it in Settings -> Voice presence ->
// "Debug logs". Everything is prefixed so it's easy to filter in devtools:
//   [voice-presence] <scope>: <message>
// A small ring buffer keeps the most recent entries for optional surfacing.

export interface VoicePresenceLogEntry {
  at: number
  scope: string
  message: string
  detail?: unknown
}

const RING_MAX = 200
const ring: VoicePresenceLogEntry[] = []

export function vpLog(scope: string, message: string, detail?: unknown): void {
  const entry: VoicePresenceLogEntry = { at: Date.now(), scope, message, detail }
  ring.push(entry)

  if (ring.length > RING_MAX) {
    ring.shift()
  }

  if (!$voicePresenceDebug.get()) {
    return
  }

  if (detail !== undefined) {
    console.info(`[voice-presence] ${scope}: ${message}`, detail)
  } else {
    console.info(`[voice-presence] ${scope}: ${message}`)
  }

  // Mirror to logs/voice-presence.log so it's reviewable as a file, not only in
  // DevTools. Fire-and-forget; absent on the island window / non-desktop shells.
  try {
    ;(window as unknown as { hermesDesktop?: { logVoicePresence?: (e: unknown) => void } }).hermesDesktop?.logVoicePresence?.({
      scope,
      message,
      detail
    })
  } catch {
    // never let logging throw
  }
}

/** Snapshot of recent voice-presence log entries (most recent last). */
export function voicePresenceLogTail(): VoicePresenceLogEntry[] {
  return ring.slice()
}
