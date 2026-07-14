/**
 * Types + parsing for the `WS /api/voice/duplex` protocol.
 *
 * This is a PINNED contract shared with the server workstream (V-A) building
 * the endpoint — see
 * docs/superpowers/specs/2026-07-10-marvi-duplex-voice-splitbrain-design.md.
 * Do not add fields or event types beyond what's documented there without
 * updating the spec first: the server and client are being built
 * independently against this same file.
 */

export type DuplexSpeaker = 'owner' | 'guest' | 'unknown'
export type DuplexWorkMode = 'thinking' | 'delegating'
export type DuplexActivityKind = 'web' | 'file' | 'memory' | 'session' | 'thinking' | 'delegation'

// --- Client -> Server --------------------------------------------------

export interface DuplexAudioMessage {
  type: 'audio'
  /** base64 pcm16, 16k mono. Sent continuously, including during TTS playback. */
  data: string
}

export interface DuplexPlaybackDoneMessage {
  type: 'playback_done'
}

export interface DuplexStopMessage {
  type: 'stop'
}

export type DuplexClientMessage = DuplexAudioMessage | DuplexPlaybackDoneMessage | DuplexStopMessage

// --- Server -> Client --------------------------------------------------

export interface DuplexReadyEvent {
  type: 'ready'
}

export interface DuplexPartialEvent {
  type: 'partial'
  text: string
  eou_prob?: number
}

export interface DuplexUtteranceEvent {
  type: 'utterance'
  text: string
  speaker: DuplexSpeaker
  speaker_name?: string
  /**
   * Voice focus (speaker ID repurpose, spec §4): set when the server
   * attributed this utterance to a non-owner speaker while focus mode is
   * active. The utterance is announced here for the badge/caption but was
   * never run through the instant lane/TTS and never joined the rolling
   * transcript -- additive field, absent (not just `false`) means "not
   * ignored" for clients built before this existed.
   */
  ignored?: boolean
}

export interface DuplexInstantDeltaEvent {
  type: 'instant_delta'
  text: string
}

export interface DuplexInstantDoneEvent {
  type: 'instant_done'
  text: string
}

export interface DuplexTtsStartEvent {
  type: 'tts_start'
  /** PCM sample rate of the chunks that follow; server falls back to 24000. */
  sample_rate?: number
}

export interface DuplexTtsChunkEvent {
  type: 'tts_chunk'
  /** base64 wav/pcm audio chunk. */
  data: string
  seq: number
}

export interface DuplexTtsEndEvent {
  type: 'tts_end'
}

export interface DuplexBargeInEvent {
  type: 'barge_in'
}

export interface DuplexConversationEndEvent {
  type: 'conversation_end'
}

export interface DuplexEscalatedEvent {
  type: 'escalated'
  task_id: string
  ack_text: string
  mode?: DuplexWorkMode
}

export interface DuplexActivityEvent {
  type: 'activity'
  status: 'started' | 'completed'
  kind: DuplexActivityKind
  label: string
  tool?: string
  task_id?: string
}

export interface DuplexCard {
  id: string
  kind: 'info' | 'result' | 'approval'
  title?: string
  body: string
  duration?: number
  actions?: Array<{ id: string; label: string; value?: string }>
}

export interface DuplexCardShowEvent {
  type: 'card_show'
  card: DuplexCard
}

export interface DuplexDeepResultEvent {
  type: 'deep_result'
  task_id: string
  text: string
}

export interface DuplexErrorEvent {
  type: 'error'
  error: string
}

export type DuplexServerEvent =
  | DuplexReadyEvent
  | DuplexPartialEvent
  | DuplexUtteranceEvent
  | DuplexInstantDeltaEvent
  | DuplexInstantDoneEvent
  | DuplexTtsStartEvent
  | DuplexTtsChunkEvent
  | DuplexTtsEndEvent
  | DuplexBargeInEvent
  | DuplexConversationEndEvent
  | DuplexEscalatedEvent
  | DuplexActivityEvent
  | DuplexCardShowEvent
  | DuplexDeepResultEvent
  | DuplexErrorEvent

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function asSpeaker(value: unknown): DuplexSpeaker {
  return value === 'owner' || value === 'guest' || value === 'unknown' ? value : 'unknown'
}

function asWorkMode(value: unknown): DuplexWorkMode {
  return value === 'delegating' ? 'delegating' : 'thinking'
}

function asActivityKind(value: unknown): DuplexActivityKind {
  return value === 'web' || value === 'file' || value === 'memory' || value === 'session' || value === 'delegation'
    ? value
    : 'thinking'
}

function asCard(value: unknown): DuplexCard | null {
  if (!isRecord(value) || typeof value.id !== 'string' || typeof value.body !== 'string') {
    return null
  }

  const kind = value.kind === 'result' || value.kind === 'approval' ? value.kind : 'info'

  const actions = Array.isArray(value.actions)
    ? value.actions.flatMap(action => {
        if (!isRecord(action) || typeof action.id !== 'string' || typeof action.label !== 'string') {
          return []
        }

        return [
          { id: action.id, label: action.label, ...(typeof action.value === 'string' ? { value: action.value } : {}) }
        ]
      })
    : undefined

  return {
    id: value.id,
    kind,
    body: value.body,
    ...(typeof value.title === 'string' ? { title: value.title } : {}),
    ...(typeof value.duration === 'number' && value.duration > 0 ? { duration: value.duration } : {}),
    ...(actions?.length ? { actions } : {})
  }
}

/**
 * Parse one raw (already `JSON.parse`d) server message into a typed event.
 * Returns null for anything that isn't a recognized, well-formed event of
 * the pinned protocol above — callers should treat that as "ignore silently"
 * rather than throw, since a malformed/unexpected payload must never take
 * down the voice session.
 */
export function parseDuplexServerEvent(raw: unknown): DuplexServerEvent | null {
  if (!isRecord(raw) || typeof raw.type !== 'string') {
    return null
  }

  switch (raw.type) {
    case 'ready':
      return { type: 'ready' }
    case 'partial': {
      if (typeof raw.text !== 'string') {
        return null
      }

      const event: DuplexPartialEvent = { type: 'partial', text: raw.text }

      if (typeof raw.eou_prob === 'number') {
        event.eou_prob = raw.eou_prob
      }

      return event
    }

    case 'utterance':
      return typeof raw.text === 'string'
        ? {
            type: 'utterance',
            text: raw.text,
            speaker: asSpeaker(raw.speaker),
            ...(typeof raw.speaker_name === 'string' ? { speaker_name: raw.speaker_name } : {}),
            ...(raw.ignored === true ? { ignored: true } : {})
          }
        : null

    case 'instant_delta':
      return typeof raw.text === 'string' ? { type: 'instant_delta', text: raw.text } : null

    case 'instant_done':
      return typeof raw.text === 'string' ? { type: 'instant_done', text: raw.text } : null

    case 'tts_start':
      return typeof raw.sample_rate === 'number' && raw.sample_rate > 0
        ? { type: 'tts_start', sample_rate: raw.sample_rate }
        : { type: 'tts_start' }

    case 'tts_chunk':
      return typeof raw.data === 'string' && typeof raw.seq === 'number'
        ? { type: 'tts_chunk', data: raw.data, seq: raw.seq }
        : null

    case 'tts_end':
      return { type: 'tts_end' }

    case 'barge_in':
      return { type: 'barge_in' }

    case 'conversation_end':
      return { type: 'conversation_end' }

    case 'escalated':
      return typeof raw.task_id === 'string' && typeof raw.ack_text === 'string'
        ? { type: 'escalated', task_id: raw.task_id, ack_text: raw.ack_text, mode: asWorkMode(raw.mode) }
        : null

    case 'activity':
      return (raw.status === 'started' || raw.status === 'completed') && typeof raw.label === 'string'
        ? {
            type: 'activity',
            status: raw.status,
            kind: asActivityKind(raw.kind),
            label: raw.label,
            ...(typeof raw.tool === 'string' ? { tool: raw.tool } : {}),
            ...(typeof raw.task_id === 'string' ? { task_id: raw.task_id } : {})
          }
        : null
    case 'card_show': {
      const card = asCard(raw.card)

      return card ? { type: 'card_show', card } : null
    }

    case 'deep_result':
      return typeof raw.task_id === 'string' && typeof raw.text === 'string'
        ? { type: 'deep_result', task_id: raw.task_id, text: raw.text }
        : null

    case 'error':
      return { type: 'error', error: typeof raw.error === 'string' ? raw.error : 'Unknown duplex error' }

    default:
      return null
  }
}
