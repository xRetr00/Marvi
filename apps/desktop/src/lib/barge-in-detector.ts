import { openStreamingTranscription, type StreamingTranscriptionSession } from './streaming-transcription'
import { BARGE_IN_DEFAULTS, createBargeInGate } from './voice-barge-in'
import { isLikelySelfEchoTranscript } from './voice-echo-guard'
import { vpLog } from './voice-presence-log'

// Two-stage barge-in (see docs/design/2026-07-06-barge-in-and-aec.md):
//   1. Energy PRE-GATE (free, CPU): only feed the STT while the echo-cancelled
//      mic rises above the post-AEC echo floor, so the model isn't transcribing
//      during Marvi-only playback (keeps the GPU free).
//   2. STT WORD confirmation (Parakeet EOU): the interrupt fires on transcribed
//      words that are NOT Marvi's own echo (rejected via isLikelySelfEchoTranscript,
//      which compares against her known TTS text). This works even when AEC
//      crushes the user's voice to ~0.02, because it keys on CONTENT, not energy.
// Energy-only is kept as a fallback when streaming STT is unavailable.
const ECHO_FLOOR = 0.025
const MIN_CONFIRM_CHARS = 4
const BARGE_GRACE_MS = 300

interface BargeInMicHandle {
  start: (options: {
    onAudioFrame?: (samples: Float32Array) => void
    onError?: (error: Error) => void
    onLevel?: (level: number) => void
  }) => Promise<void>
  cancel: () => void
}

interface BargeInDetectorOptions {
  handle: BargeInMicHandle
  streamingSttEnabled: boolean
  /** Fired once when a real (non-echo) interruption is detected. */
  onInterrupt: () => void
}

/**
 * Arm barge-in detection for the duration of a playback. Returns a stop()
 * function; call it when playback ends or the detector is torn down.
 */
export function startBargeInDetector({ handle, streamingSttEnabled, onInterrupt }: BargeInDetectorOptions): () => void {
  const startedAt = Date.now()
  const gate = createBargeInGate(BARGE_IN_DEFAULTS) // energy fallback only
  let streaming: StreamingTranscriptionSession | null = null
  let latestLevel = 0
  let peak = 0
  let lastLogAt = 0
  let stopped = false
  let fired = false

  const stop = () => {
    if (stopped) {
      return
    }

    stopped = true
    void streaming?.finish().catch(() => '')
    streaming = null
    handle.cancel()
  }

  const fire = (reason: string, detail: Record<string, unknown>) => {
    if (fired || stopped) {
      return
    }

    fired = true
    vpLog('voice', 'barge-in accepted', { reason, ...detail })
    onInterrupt()
    stop()
  }

  if (streamingSttEnabled) {
    void openStreamingTranscription({
      onPartial: text => {
        if (stopped || fired) {
          return
        }

        const trimmed = text.trim()

        if (!trimmed) {
          return
        }

        const echo = isLikelySelfEchoTranscript(trimmed)
        vpLog('voice', 'barge-in partial', { chars: trimmed.length, echo })

        if (!echo && trimmed.length >= MIN_CONFIRM_CHARS && Date.now() - startedAt > BARGE_GRACE_MS) {
          fire('stt-words', { text: trimmed })
        }
      }
    })
      .then(session => {
        if (stopped) {
          void session.finish().catch(() => '')
        } else {
          streaming = session
        }
      })
      .catch(err => vpLog('voice', 'barge-in stt open failed', { error: String(err) }))
  }

  // useMicRecorder.start() silently no-ops if a recorder is still active, which
  // would arm NOTHING — cancel first for a guaranteed clean start.
  handle.cancel()
  vpLog('voice', 'barge-in armed', { streaming: streamingSttEnabled, floor: ECHO_FLOOR })
  void handle
    .start({
      onError: err => vpLog('voice', 'barge-in mic error', { error: String(err) }),
      onAudioFrame: samples => {
        // Stage 1: only feed the STT while there's sound above the echo floor.
        if (streaming && !stopped && !fired && latestLevel >= ECHO_FLOOR) {
          streaming.sendFrame(samples)
        }
      },
      onLevel: level => {
        if (stopped || fired) {
          return
        }

        latestLevel = level
        peak = Math.max(peak, level)

        if (Date.now() - lastLogAt > 1000) {
          vpLog('voice', 'barge-in level', { peak: Number(peak.toFixed(3)), floor: ECHO_FLOOR, confirmBy: streamingSttEnabled ? 'stt-words' : 'energy' })
          lastLogAt = Date.now()
          peak = 0
        }

        // Fallback: energy gate ONLY when streaming STT isn't available.
        if (!streamingSttEnabled && gate.update(level, Date.now() - startedAt)) {
          fire('energy', { level })
        }
      }
    })
    .catch(err => vpLog('voice', 'barge-in mic start failed', { error: String(err) }))

  return stop
}
