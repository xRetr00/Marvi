import { atom } from 'nanostores'

// Startup warmup state of the voice engines, mirrored from the backend
// /api/audio/voice-warmup endpoint (warming runs in the background while the app
// connects — this just surfaces progress in the status bar; it never gates use).
export type WarmupPhase = 'pending' | 'warming' | 'ready' | 'skipped' | 'failed'

export interface VoiceWarmup {
  tts: WarmupPhase
  stt: WarmupPhase
  wake: WarmupPhase
  done: boolean
  started: boolean
}

const INITIAL: VoiceWarmup = { tts: 'pending', stt: 'pending', wake: 'pending', done: false, started: false }

export const $voiceWarmup = atom<VoiceWarmup>(INITIAL)

let timer: number | null = null
let polling = false

async function pollOnce(): Promise<void> {
  if (!polling) {
    return
  }

  try {
    const conn = await window.hermesDesktop?.getConnection?.().catch(() => null)

    if (conn?.token) {
      const res = await fetch(`${conn.baseUrl.replace(/\/+$/, '')}/api/audio/voice-warmup`, {
        headers: { 'X-Hermes-Session-Token': conn.token }
      })

      if (res.ok) {
        const next = (await res.json()) as VoiceWarmup
        $voiceWarmup.set(next)

        if (next.done) {
          polling = false

          return
        }
      }
    }
  } catch {
    // transient (still connecting) — keep polling
  }

  timer = window.setTimeout(() => void pollOnce(), 1500)
}

/** Begin polling warmup state until all engines resolve. Idempotent. */
export function startVoiceWarmupPolling(): void {
  if (polling) {
    return
  }

  polling = true
  void pollOnce()
}

export function stopVoiceWarmupPolling(): void {
  polling = false

  if (timer !== null) {
    window.clearTimeout(timer)
    timer = null
  }
}
