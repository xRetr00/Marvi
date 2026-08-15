import type { VoicePhase } from '@/store/voice-presence'

/** Keep the wake bloom visible while the duplex socket takes over the mic. */
export const WAKE_HANDOFF_MS = 2500

export function shouldHoldWakeHandoff(previous: VoicePhase, next: VoicePhase): boolean {
  return previous === 'wake' && next === 'off'
}

/** Target island amplitude (0..1) for a phase + current mic level. */
export function targetAmplitude(phase: VoicePhase, level: number): number {
  switch (phase) {
    case 'off':
      return 0

    case 'wake':
      return 1

    case 'listening':
      return Math.min(1, 0.4 + level * 0.9)

    case 'transcribing':
      return 0.45

    case 'thinking':
      return 0.5

    case 'speaking':
      return Math.min(1, 0.55 + level * 0.6)

    default:
      return 0
  }
}

/** Blob-drift animation duration per phase (ms). Lower = faster flow. */
// 'wake' is the fastest (a quick flare the instant the hotword fires); steady states flow slower.
export function islandFlowMs(phase: VoicePhase): number {
  switch (phase) {
    case 'listening':

    case 'speaking':
      return 3000

    case 'wake':
      return 2500

    case 'thinking':
      return 6000

    default:
      return 14000
  }
}
