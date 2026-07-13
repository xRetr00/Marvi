import {
  type DuplexActivityKind,
  type DuplexServerEvent,
  type DuplexSpeaker,
  type DuplexWorkMode,
  parseDuplexServerEvent
} from './duplex-protocol'

/**
 * Pure state machine over the `WS /api/voice/duplex` protocol (see
 * duplex-protocol.ts and
 * docs/superpowers/specs/2026-07-10-marvi-duplex-voice-splitbrain-design.md).
 *
 * Deliberately has NO WebSocket and NO Web Audio in it — callers feed it
 * server events (or raw, unparsed JSON) and drain the `DuplexCommand[]` it
 * returns into a real transport (see duplex-client.ts / duplex-audio.ts).
 * That split is what makes the conversational logic (barge-in kill,
 * escalation bookkeeping, instant vs deep replies) unit-testable without
 * mocking browser audio APIs.
 */

export type DuplexPhase = 'closed' | 'connecting' | 'listening' | 'replying' | 'speaking'

export interface DuplexDeepWork {
  taskId: string
  ackText: string
  mode: DuplexWorkMode
}

export interface DuplexActivity {
  kind: DuplexActivityKind
  label: string
}

export interface DuplexSessionState {
  phase: DuplexPhase
  /** Live partial transcript of the user's current utterance, or null. */
  partialCaption: string | null
  /** The last utterance the server finalized (cleared by the next `utterance`). */
  utteranceCaption: string | null
  /** Who the server attributed the last `utterance` to. */
  speaker: DuplexSpeaker | null
  speakerName: string | null
  /** Accumulated instant/deep reply text for the in-flight turn. */
  replyText: string | null
  replySource: 'deep' | 'instant' | null
  /** Set while a background deep task is outstanding (escalated, no deep_result/error yet). */
  deepWork: DuplexDeepWork | null
  activity: DuplexActivity | null
  /** True while Marvi is speaking and a barge-in would be honored. */
  bargeable: boolean
  /** Last error message from the server, for surfacing/logging. Not fatal. */
  lastError: string | null
}

export type DuplexCommand =
  /** Stop any playing/queued audio immediately (barge-in, tts_start reset, close).
   *  sampleRate (from tts_start) applies to the chunks that follow the reset. */
  | { type: 'reset_playback'; sampleRate?: number }
  /** Schedule one TTS chunk for playback. */
  | { type: 'enqueue_audio'; data: string; seq: number }
  /**
   * No more chunks are coming for the current tts_end. The audio transport
   * should call `notifyPlaybackFinished()` once every enqueued chunk has
   * actually finished playing.
   */
  | { type: 'expect_playback_end' }
  /** Tell the server we finished playing the last tts_end's audio. */
  | { type: 'send_playback_done' }
  /** End the session. */
  | { type: 'send_stop' }

export const INITIAL_DUPLEX_STATE: DuplexSessionState = {
  phase: 'connecting',
  partialCaption: null,
  utteranceCaption: null,
  speaker: null,
  speakerName: null,
  replyText: null,
  replySource: null,
  deepWork: null,
  activity: null,
  bargeable: false,
  lastError: null
}

export class DuplexSessionMachine {
  private _state: DuplexSessionState = { ...INITIAL_DUPLEX_STATE }

  // True once a tts_end has been received for the in-flight utterance and
  // we're waiting for the audio layer to report the queued chunks actually
  // finished playing, so we know when it's safe to send playback_done.
  private awaitingPlaybackEnd = false

  get state(): DuplexSessionState {
    return this._state
  }

  private patch(next: Partial<DuplexSessionState>): void {
    this._state = { ...this._state, ...next }
  }

  /**
   * Feed one raw server message (already `JSON.parse`d). Unknown/malformed
   * payloads are ignored — state is left untouched and no commands are
   * produced. Never throws.
   */
  applyRawEvent(raw: unknown): DuplexCommand[] {
    const event = parseDuplexServerEvent(raw)

    return event ? this.applyEvent(event) : []
  }

  applyEvent(event: DuplexServerEvent): DuplexCommand[] {
    switch (event.type) {
      case 'ready':
        this.patch({ phase: 'listening', lastError: null })

        return []

      case 'partial':
        this.patch({ partialCaption: event.text })

        return []

      case 'utterance':
        // A new utterance closes out whatever the previous turn was saying,
        // but deliberately does NOT touch `deepWork` — the owner can keep
        // talking while an earlier escalation is still being worked on in
        // the background (spec section 2/3).
        this.patch({
          utteranceCaption: event.text,
          speaker: event.speaker,
          speakerName: event.speaker_name ?? null,
          partialCaption: null,
          replyText: null,
          replySource: null,
          phase: 'listening'
        })

        return []

      case 'instant_delta':
        // Guard against the "phase flapping" bug: once tts_start has moved us
        // to 'speaking', an instant_delta for the NEXT sentence (multi-sentence
        // replies stream reply-text and audio concurrently, so text for the
        // next chunk can easily arrive before this chunk's audio has drained)
        // must not knock the presented phase back down to 'replying' while
        // audio is still playing/queued. Text keeps accumulating either way —
        // only the phase (and therefore the `[phase]` log + island UI) holds
        // at 'speaking' until playback actually drains (notifyPlaybackFinished)
        // or a barge_in tears it down.
        this.patch({
          phase: this._state.phase === 'speaking' ? 'speaking' : 'replying',
          replySource: this._state.replySource ?? 'instant',
          replyText: (this._state.replyText ?? '') + event.text
        })

        return []

      case 'instant_done':
        // Same guard as instant_delta above — see comment there.
        this.patch({
          phase: this._state.phase === 'speaking' ? 'speaking' : 'replying',
          replySource: 'instant',
          replyText: event.text,
          activity: null
        })

        return []

      case 'tts_start':
        this.awaitingPlaybackEnd = false
        this.patch({ phase: 'speaking', bargeable: true })

        return [{ type: 'reset_playback', sampleRate: event.sample_rate }]

      case 'tts_chunk':
        // A chunk without an active speaking session (arrived early/late,
        // or after a barge_in already tore the session down) has nowhere
        // safe to schedule into — drop it rather than resurrect playback.
        if (this._state.phase !== 'speaking') {
          return []
        }

        return [{ type: 'enqueue_audio', data: event.data, seq: event.seq }]

      case 'tts_end':
        if (this._state.phase !== 'speaking') {
          return []
        }

        this.awaitingPlaybackEnd = true

        return [{ type: 'expect_playback_end' }]

      case 'barge_in':
        // Kill playback immediately and flush the queue. Explicitly leaves
        // `deepWork` alone: barge-in during an escalation ack must not
        // cancel the background deep task (spec "Error handling").
        this.awaitingPlaybackEnd = false
        this.patch({ phase: 'listening', bargeable: false, replyText: null, replySource: null })

        return [{ type: 'reset_playback' }]

      case 'escalated':
        // The ack is spoken through the normal instant-reply + TTS cycle, so
        // treat it as this turn's reply text while ALSO raising the
        // "thinking deeper" flag for the outstanding background task.
        {
          const mode = event.mode ?? 'thinking'
          this.patch({
            phase: 'replying',
            replyText: event.ack_text,
            replySource: 'instant',
            deepWork: { taskId: event.task_id, ackText: event.ack_text, mode },
            activity: {
              kind: mode === 'delegating' ? 'delegation' : 'thinking',
              label: mode === 'delegating' ? 'Sub-agent is working' : 'Thinking deeper'
            }
          })
        }

        return []

      case 'deep_result':
        // Clear deepWork whenever a result comes back, whether or not its
        // task_id matches what we had outstanding — the protocol only ever
        // tracks one escalation's ack client-side, and leaving the
        // "thinking deeper" indicator stuck on a stale/mismatched id is
        // worse than clearing it a beat early.
        this.patch({
          phase: 'replying',
          replyText: event.text,
          replySource: 'deep',
          deepWork: null,
          activity: null
        })

        return []

      case 'error':
        // No task_id on `error`, so an escalated-task failure can't be
        // distinguished from an unrelated one. Clear deepWork defensively:
        // the alternative (never clearing on error) risks the "thinking
        // deeper" indicator sticking forever when the failure WAS the deep
        // task, which is the worse user-facing outcome.
        this.patch({ lastError: event.error, deepWork: null, activity: null })

        return []

      case 'activity':
        this.patch({
          activity:
            event.status === 'started'
              ? { kind: event.kind, label: event.label }
              : this._state.deepWork
                ? {
                    kind: this._state.deepWork.mode === 'delegating' ? 'delegation' : 'thinking',
                    label: this._state.deepWork.mode === 'delegating' ? 'Sub-agent is working' : 'Thinking deeper'
                  }
                : null
        })

        return []

      default:
        return []
    }
  }

  /**
   * Call once the audio transport finishes playing every chunk queued for
   * the current tts_end (or immediately if nothing was queued). No-op if a
   * playback end isn't currently expected (e.g. it was already interrupted
   * by a barge_in, or called more than once).
   */
  notifyPlaybackFinished(): DuplexCommand[] {
    if (!this.awaitingPlaybackEnd) {
      return []
    }

    this.awaitingPlaybackEnd = false
    this.patch({ phase: 'listening', bargeable: false })

    return [{ type: 'send_playback_done' }]
  }

  /** End the session (component unmount / connection torn down). */
  close(): DuplexCommand[] {
    this.awaitingPlaybackEnd = false
    this.patch({ phase: 'closed', bargeable: false })

    return [{ type: 'send_stop' }, { type: 'reset_playback' }]
  }
}
