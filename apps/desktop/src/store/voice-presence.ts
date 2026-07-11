import { atom, computed } from 'nanostores'

import { vpLog } from '@/lib/voice-presence-log'

import { $voicePlayback } from './voice-playback'

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
  /**
   * True while Marvi is speaking AND barge-in is available — the island shows a
   * "tap/talk to interrupt" affordance. duplex phase 3: this must be true in
   * EVERY mode, so it derives from the shared TTS playback state (covers the
   * read-aloud / wake-word path) not just the hands-free conversation.
   */
  bargeable: boolean
  /**
   * Duplex-only: overrides the generic per-phase label (e.g. "Replying" /
   * "Answering" instead of the generic "Thinking") when a duplex session
   * (composer hands-free or ambient wake-word) is driving this state. Null
   * uses the default label for `phase`.
   */
  label: string | null
  /**
   * Duplex-only (speaker ID, spec section 4): set when the duplex server
   * attributes the current utterance to someone other than the enrolled
   * owner. Every voice surface shows the same small, unobtrusive badge for
   * this — see components/voice-speaker-badge.tsx.
   */
  speakerBadge: 'guest' | 'unknown' | null
  /** Duplex-only: true while an escalated background task hasn't resolved yet. */
  deepWorking: boolean
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
  /** TTS is playing (any mode). Lights the island as `speaking` for the
   * read-aloud / wake-word / auto-speak paths that don't run the hands-free
   * conversation loop. duplex phase 3. */
  playbackSpeaking?: boolean
}): VoicePhase {
  const { active, voiceStatus, wakeStatus, playbackSpeaking } = args

  if (active && voiceStatus !== 'idle') {
    return voiceStatus
  }

  if (wakeStatus === 'woken' || wakeStatus === 'listening' || wakeStatus === 'transcribing') {
    return 'wake'
  }

  if (playbackSpeaking) {
    return 'speaking'
  }

  return 'off'
}

/** Duplex-only extras a conversation/ambient slice can carry (see VoiceState). */
export interface DuplexExtras {
  label: string | null
  speakerBadge: 'guest' | 'unknown' | null
  deepWorking: boolean
}

const NO_DUPLEX_EXTRAS: DuplexExtras = { label: null, speakerBadge: null, deepWorking: false }

/** Conversation inputs, published by the composer (owns useVoiceConversation / the hands-free duplex session). */
export const $conversation = atom<
  { active: boolean; status: VoiceStatus; level: number; muted: boolean; caption: string | null } & DuplexExtras
>({
  active: false,
  status: 'idle',
  level: 0,
  muted: false,
  caption: null,
  ...NO_DUPLEX_EXTRAS
})

/** Wake-word status, published by chat/index.tsx (owns useWakeWord). */
export const $wakeStatus = atom<WakeStatus>('idle')

/** The user's live/final speech transcript, published by the voice loops (streaming partials + final flash). */
export const $userCaption = atom<string | null>(null)

/**
 * Whether barge-in is currently available, published by the composer. Both
 * speak paths (conversation + read-aloud) arm their gates from the same
 * `bargeInEnabled` prop, so one flag drives the island's "interrupt" affordance
 * for every mode. duplex phase 3.
 */
export const $bargeInEnabled = atom<boolean>(true)

/**
 * Ambient (wake-word / presence) duplex session, published by desktop-controller.tsx
 * (owns the ambient `useDuplexVoice` alongside `useWakeWord`). While `active`,
 * this fully drives `$voiceState` — the presence/island path's duplex session
 * takes priority over the legacy wake-status derivation below, exactly like
 * the composer's hands-free `$conversation.active` already does for its own
 * duplex session. Falls back to the legacy wake-word-driven phase the moment
 * the duplex endpoint proves unavailable.
 */
export const $ambientDuplex = atom<{ active: boolean; phase: VoicePhase; level: number; caption: string | null; userCaption: string | null; bargeable: boolean } & DuplexExtras>({
  active: false,
  phase: 'off',
  level: 0,
  caption: null,
  userCaption: null,
  bargeable: false,
  ...NO_DUPLEX_EXTRAS
})

/** The single derived presence state the island overlay mirrors. */
export const $voiceState = computed(
  [$conversation, $wakeStatus, $userCaption, $voicePlayback, $bargeInEnabled, $ambientDuplex],
  (conv, wakeStatus, userCaption, playback, bargeInEnabled, ambient): VoiceState => {
    // Ambient duplex (wake-word/presence path) takes over the presentation the
    // same way the composer's hands-free duplex session already does via
    // `conv` below — see $ambientDuplex's doc comment.
    if (ambient.active) {
      return {
        bargeable: ambient.bargeable,
        caption: ambient.caption,
        deepWorking: ambient.deepWorking,
        label: ambient.label,
        level: ambient.level,
        muted: false,
        phase: ambient.phase,
        speakerBadge: ambient.speakerBadge,
        userCaption: ambient.userCaption
      }
    }

    const playbackSpeaking = playback.status === 'speaking'
    const phase = deriveVoicePhase({ active: conv.active, voiceStatus: conv.status, wakeStatus, playbackSpeaking })

    return {
      bargeable: phase === 'speaking' && bargeInEnabled,
      caption: conv.caption,
      deepWorking: conv.active ? conv.deepWorking : false,
      label: conv.active ? conv.label : null,
      level: conv.level,
      muted: conv.muted,
      phase,
      speakerBadge: conv.active ? conv.speakerBadge : null,
      userCaption
    }
  }
)

/** Publish the conversation slice (called from the composer). */
export function publishConversation(
  next: { active: boolean; status: VoiceStatus; level: number; muted: boolean; caption: string | null } & Partial<DuplexExtras>
): void {
  $conversation.set({ ...NO_DUPLEX_EXTRAS, ...next })
}

/** Publish the ambient (wake-word/presence) duplex slice (called from desktop-controller.tsx). */
export function publishAmbientDuplex(
  next: { active: boolean; phase: VoicePhase; level: number; caption: string | null; userCaption: string | null; bargeable: boolean } & Partial<DuplexExtras>
): void {
  $ambientDuplex.set({ ...NO_DUPLEX_EXTRAS, ...next })
}

/** Publish the wake-word slice (called from chat/index.tsx). */
export function publishWakeStatus(status: WakeStatus): void {
  $wakeStatus.set(status)
}

/** Publish whether barge-in is available (called from the composer). */
export function publishBargeInEnabled(enabled: boolean): void {
  $bargeInEnabled.set(enabled)
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
