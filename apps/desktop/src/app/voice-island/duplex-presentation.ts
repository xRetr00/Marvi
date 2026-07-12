import type { VoicePhase } from '@/store/voice-presence'

import type { DuplexActivityKind, DuplexWorkMode } from './duplex-protocol'
import type { DuplexSessionState } from './duplex-session'

/**
 * Maps a `DuplexSessionState` onto the shared display vocabulary
 * (`VoicePhase`, labels, the `who`-tagged caption convention, `VoiceState`'s
 * `label`/`speakerBadge`/`deepWorking` extras) that every voice surface reads
 * from `$voiceState`. Both duplex sessions — the composer's hands-free
 * overlay (use-composer-voice.ts) and the ambient wake-word/island path
 * (desktop-controller.tsx) — funnel their `DuplexSessionState` through this
 * one pure function before publishing into the store, so the island,
 * voice-mode overlay, and composer status all render duplex turns identically
 * without needing to know about duplex's internals. Pure + unit-testable.
 */
export interface DuplexPresentation {
  phase: VoicePhase
  label: string
  caption: { text: string; who: 'marvi' | 'you' } | null
  bargeable: boolean
  /** Small unobtrusive badge when the server attributes speech to someone other than the owner. */
  speakerBadge: 'owner' | 'guest' | 'unknown' | null
  speakerName: string | null
  /** True while an escalated background task hasn't resolved yet. */
  deepWorking: boolean
  deepMode: DuplexWorkMode | null
  activity: { kind: DuplexActivityKind; label: string } | null
}

const PHASE_MAP: Record<DuplexSessionState['phase'], VoicePhase> = {
  closed: 'off',
  connecting: 'off',
  listening: 'listening',
  replying: 'thinking',
  speaking: 'speaking'
}

function resolveLabel(state: DuplexSessionState): string {
  if (state.activity) {
    return state.activity.label
  }

  if (state.phase === 'speaking') {
    return 'Speaking'
  }

  if (state.phase === 'replying') {
    return state.replySource === 'deep' ? 'Answering' : 'Replying'
  }

  return 'Listening'
}

function resolveCaption(state: DuplexSessionState): DuplexPresentation['caption'] {
  if ((state.phase === 'speaking' || state.phase === 'replying') && state.replyText) {
    return { text: state.replyText, who: 'marvi' }
  }

  if (state.partialCaption) {
    return { text: state.partialCaption, who: 'you' }
  }

  if (state.utteranceCaption) {
    return { text: state.utteranceCaption, who: 'you' }
  }

  return null
}

export function resolveDuplexPresentation(state: DuplexSessionState): DuplexPresentation {
  return {
    bargeable: state.bargeable,
    caption: resolveCaption(state),
    deepWorking: Boolean(state.deepWork),
    deepMode: state.deepWork?.mode ?? null,
    activity: state.activity,
    label: resolveLabel(state),
    phase: PHASE_MAP[state.phase],
    speakerBadge: state.speaker,
    speakerName: state.speakerName
  }
}
