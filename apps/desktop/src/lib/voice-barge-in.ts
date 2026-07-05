export type BargeInGateState = 'idle' | 'rising' | 'triggered'

interface BargeInGateOptions {
  graceMs: number
  level: number
  sustainedMs: number
}

// NOTE(duplex-phase2): shared barge-in tuning defaults for BOTH speak paths
// (hands-free conversation + read-aloud). See
// docs/design/2026-07-05-voice-duplex-design.md Tunables.
//   level: mic RMS 0..1 (normalized as rms/42 in use-mic-recorder). 0.22 is
//     deliberately lower than the old 0.32 because AEC + noiseSuppression +
//     autoGainControl shrink the user's voice during playback, so the old
//     threshold rarely triggered. If SPEAKER echo starts self-triggering,
//     raise this first (the `[voice-presence] barge-in level` logs show peaks).
//   graceMs: ignore the first N ms of playback (avoids the TTS onset click).
//   sustainedMs: how long speech must persist before it counts as an interrupt.
export const BARGE_IN_DEFAULTS = { graceMs: 500, level: 0.22, sustainedMs: 300 }

export interface BargeInGate {
  /** Last computed state — read after `update()` for logging/telemetry. */
  state: BargeInGateState
  /**
   * Feed one mic level sample. Returns true when barge-in should fire.
   *
   * `confirmed` is the echo-robustness hook (duplex phase 2): pass `false` while
   * the loud audio is believed to be Marvi's own voice leaking past AEC (e.g.
   * the streamed partial matches `isLikelySelfEchoTranscript`), so speaker echo
   * can't self-trigger a barge-in. Defaults to `true` for callers that only
   * have an energy signal — same behavior as before this param existed.
   */
  update(currentLevel: number, elapsedMs: number, confirmed?: boolean): boolean
}

export function createBargeInGate({ graceMs, level, sustainedMs }: BargeInGateOptions): BargeInGate {
  let speechStartedAt: number | null = null

  const gate: BargeInGate = {
    state: 'idle',
    update(currentLevel: number, elapsedMs: number, confirmed = true): boolean {
      if (elapsedMs < graceMs || currentLevel < level) {
        speechStartedAt = null
        gate.state = 'idle'
        return false
      }

      speechStartedAt ??= elapsedMs
      const sustained = elapsedMs - speechStartedAt >= sustainedMs
      const triggered = sustained && confirmed
      gate.state = triggered ? 'triggered' : 'rising'
      return triggered
    }
  }

  return gate
}
