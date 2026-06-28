import { atom } from 'nanostores'

export type VoicePhase = 'off' | 'wake' | 'listening' | 'transcribing' | 'thinking' | 'speaking'

export interface VoiceState {
  phase: VoicePhase
  /** Live mic amplitude 0..1 from the recorder; drives glow reactivity. */
  level: number
  muted: boolean
}

/** Conversation status from use-voice-conversation.ts. */
type VoiceStatus = 'idle' | 'listening' | 'transcribing' | 'thinking' | 'speaking'
/** Wake-word status from use-wake-word.ts. */
type WakeStatus = 'idle' | 'arming' | 'armed' | 'woken' | 'listening' | 'transcribing'

export const $voiceState = atom<VoiceState>({ phase: 'off', level: 0, muted: false })

/**
 * Collapse the two engines' statuses into one glow phase. Background hotword
 * listening is `'armed'` (glow dark) — the wake-word loop sits there waiting for
 * the phrase and must NOT light the glow. Once the hotword fires the engine
 * walks `'woken'` → `'listening'` → `'transcribing'`; those are the post-hotword
 * command-capture states and keep the glow lit as `'wake'`. An active
 * conversation's status maps straight through; anything else is `off`.
 */
export function deriveVoicePhase(args: {
  active: boolean
  voiceStatus: VoiceStatus
  wakeStatus: WakeStatus
}): VoicePhase {
  const { active, voiceStatus, wakeStatus } = args

  if (active && voiceStatus !== 'idle') {
    return voiceStatus
  }

  if (wakeStatus === 'woken' || wakeStatus === 'listening' || wakeStatus === 'transcribing') {
    return 'wake'
  }

  return 'off'
}

/** Publish the latest derived state for the glow overlay to mirror. */
export function publishVoiceState(next: VoiceState): void {
  $voiceState.set(next)
}
