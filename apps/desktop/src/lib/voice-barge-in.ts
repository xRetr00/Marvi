export type BargeInGateState = 'idle' | 'rising' | 'triggered'

interface BargeInGateOptions {
  graceMs: number
  level: number
  sustainedMs: number
}

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
