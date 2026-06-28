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
 * Collapse the two engines' statuses into one glow phase. The wake-word loop is
 * always listening in the background for the hotword — that must NOT light the
 * glow, so only `woken` (hotword just fired) counts. An active conversation's
 * status maps straight through; anything else is `off` (glow dark).
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

  if (wakeStatus === 'woken') {
    return 'wake'
  }

  return 'off'
}

/** Publish the latest derived state for the glow overlay to mirror. */
export function publishVoiceState(next: VoiceState): void {
  $voiceState.set(next)
}
