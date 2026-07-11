import type { VoicePhase } from '@/store/voice-presence'

import type { DuplexSessionState } from './duplex-session'

/**
 * Maps a `DuplexSessionState` onto the island's existing display vocabulary
 * (VoicePhase colors/labels, caption speaker convention) so `dynamic-island.tsx`
 * can render the duplex-driven states with the same visual language as the
 * legacy IPC-pushed `VoiceState`, without either side needing to know about
 * the other's internals. Pure + unit-testable.
 */
export interface DuplexPresentation {
  phase: VoicePhase
  label: string
  caption: { text: string; who: 'marvi' | 'you' } | null
  bargeable: boolean
  /** Small unobtrusive badge when the server attributes speech to someone other than the owner. */
  speakerBadge: 'guest' | 'unknown' | null
  /** True while an escalated background task hasn't resolved yet. */
  deepWorking: boolean
}

const PHASE_MAP: Record<DuplexSessionState['phase'], VoicePhase> = {
  closed: 'off',
  connecting: 'off',
  listening: 'listening',
  replying: 'thinking',
  speaking: 'speaking'
}

function resolveLabel(state: DuplexSessionState): string {
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

/** Only call for `connecting`/`closed` — those phases mean "not actually active"; callers should fall back to the legacy presentation instead. */
export function isDuplexPhaseActive(phase: DuplexSessionState['phase']): boolean {
  return phase !== 'connecting' && phase !== 'closed'
}

export function resolveDuplexPresentation(state: DuplexSessionState): DuplexPresentation {
  return {
    bargeable: state.bargeable,
    caption: resolveCaption(state),
    deepWorking: Boolean(state.deepWork),
    label: resolveLabel(state),
    phase: PHASE_MAP[state.phase],
    speakerBadge: state.speaker === 'guest' || state.speaker === 'unknown' ? state.speaker : null
  }
}
