import { atom, computed } from 'nanostores'

import { vpLog } from '@/lib/voice-presence-log'

export type VoicePhase = 'off' | 'wake' | 'listening' | 'transcribing' | 'thinking' | 'speaking'

export interface VoiceState {
  phase: VoicePhase
  /** Live mic amplitude 0..1 from the recorder; drives island reactivity. */
  level: number
  muted: boolean
  /** The TTS text currently being spoken, for the island's live caption. Null when not speaking. */
  caption: string | null
  /** The user's live/final speech transcript, for the island's "what you said" caption. Null when none. */
  userCaption: string | null
}

/** Conversation status from use-voice-conversation.ts. */
export type VoiceStatus = 'idle' | 'listening' | 'transcribing' | 'thinking' | 'speaking'
/** Wake-word status from use-wake-word.ts. */
export type WakeStatus = 'idle' | 'arming' | 'armed' | 'woken' | 'listening' | 'transcribing'

/**
 * Collapse the two engines' statuses into one island phase. Background hotword
 * listening is `'armed'` (island dark) — the wake-word loop sits there waiting for
 * the phrase and must NOT light the island. Once the hotword fires the engine
 * walks `'woken'` → `'listening'` → `'transcribing'`; those are the post-hotword
 * command-capture states and keep the island lit as `'wake'`. An active
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

/** Conversation inputs, published by the composer (owns useVoiceConversation). */
export const $conversation = atom<{ active: boolean; status: VoiceStatus; level: number; muted: boolean; caption: string | null }>({
  active: false,
  status: 'idle',
  level: 0,
  muted: false,
  caption: null
})

/** Wake-word status, published by chat/index.tsx (owns useWakeWord). */
export const $wakeStatus = atom<WakeStatus>('idle')

/** The user's live/final speech transcript, published by the voice loops (streaming partials + final flash). */
export const $userCaption = atom<string | null>(null)

/** The single derived presence state the island overlay mirrors. */
export const $voiceState = computed(
  [$conversation, $wakeStatus, $userCaption],
  (conv, wakeStatus, userCaption): VoiceState => ({
    phase: deriveVoicePhase({ active: conv.active, voiceStatus: conv.status, wakeStatus }),
    level: conv.level,
    muted: conv.muted,
    caption: conv.caption,
    userCaption
  })
)

/** Publish the conversation slice (called from the composer). */
export function publishConversation(next: { active: boolean; status: VoiceStatus; level: number; muted: boolean; caption: string | null }): void {
  $conversation.set(next)
}

/** Publish the wake-word slice (called from chat/index.tsx). */
export function publishWakeStatus(status: WakeStatus): void {
  $wakeStatus.set(status)
}

/** Publish the user's live/final speech transcript (called from the voice loops). */
export function setUserCaption(v: string | null): void {
  $userCaption.set(v)
}

// Log phase transitions only (not every level/tick) so the debug log stays
// readable. Subscribed once at module load.
let _lastPhase: VoicePhase | null = null
$voiceState.subscribe(s => {
  if (s.phase !== _lastPhase) {
    _lastPhase = s.phase
    vpLog('phase', s.phase)
  }
})
