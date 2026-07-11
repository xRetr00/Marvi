/**
 * Browser audio I/O for the duplex voice client: mic capture (continuous,
 * base64 pcm16 16k mono frames) and a scheduled playback queue for TTS
 * chunks. Impure by nature (Web Audio / getUserMedia) — kept separate from
 * duplex-session.ts so the conversational state machine stays unit-testable
 * without mocking these APIs.
 */

const DUPLEX_SAMPLE_RATE = 16000

// The duplex protocol doesn't carry a sample-rate field on tts_start/
// tts_chunk. This matches the existing streaming TTS pipeline's default
// (see GaplessPlayer in src/lib/voice-playback.ts) since the spec says the
// duplex server reuses that same streaming TTS backend.
const DEFAULT_TTS_SAMPLE_RATE = 24000

// --- Pure pcm16 <-> base64 helpers (exported for testing) ------------------

export function floatToPcm16Base64(samples: Float32Array): string {
  const pcm = new Int16Array(samples.length)

  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i] ?? 0))
    pcm[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff
  }

  const bytes = new Uint8Array(pcm.buffer)
  let binary = ''

  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i])
  }

  return btoa(binary)
}

export function pcm16Base64ToFloat32(encoded: string): Float32Array {
  const raw = atob(encoded)
  const bytes = new Uint8Array(raw.length)

  for (let i = 0; i < raw.length; i += 1) {
    bytes[i] = raw.charCodeAt(i)
  }

  const pcm = new Int16Array(bytes.buffer)
  const samples = new Float32Array(pcm.length)

  for (let i = 0; i < pcm.length; i += 1) {
    samples[i] = Math.max(-1, pcm[i] / 0x8000)
  }

  return samples
}

export function downsampleFloat32(input: Float32Array, inputRate: number, outputRate: number): Float32Array {
  if (!input.length || inputRate <= 0 || outputRate <= 0) {
    return new Float32Array()
  }

  if (inputRate === outputRate) {
    return new Float32Array(input)
  }

  const ratio = inputRate / outputRate
  const outputLength = Math.max(1, Math.floor(input.length / ratio))
  const output = new Float32Array(outputLength)

  for (let i = 0; i < outputLength; i += 1) {
    const start = Math.floor(i * ratio)
    const end = Math.min(input.length, Math.floor((i + 1) * ratio))
    let sum = 0
    let count = 0

    for (let j = start; j < end; j += 1) {
      sum += input[j] ?? 0
      count += 1
    }

    output[i] = count > 0 ? sum / count : (input[Math.min(start, input.length - 1)] ?? 0)
  }

  return output
}

// --- Mic capture -------------------------------------------------------

export interface DuplexMicCaptureOptions {
  /** Called with each ~256ms base64 pcm16 16k mono frame, continuously — including while TTS is playing. */
  onFrame: (base64Pcm16: string) => void
  onLevel?: (level: number) => void
  onError?: (error: Error) => void
}

export interface DuplexMicCapture {
  stop: () => void
}

type AudioContextLike = typeof AudioContext

/**
 * Opens the mic with `echoCancellation: true` (plus noise suppression + AGC)
 * and keeps streaming frames for as long as `stop()` isn't called — including
 * through TTS playback. That's what makes server-side barge-in detection
 * possible (spec section 3): the mic must never be paused/closed just
 * because Marvi is talking.
 */
export async function startDuplexMicCapture(options: DuplexMicCaptureOptions): Promise<DuplexMicCapture> {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error('Microphone capture is not supported in this environment')
  }

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { autoGainControl: true, echoCancellation: true, noiseSuppression: true }
  })

  const audioWindow = window as Window & { webkitAudioContext?: AudioContextLike }
  const AudioContextCtor = window.AudioContext || audioWindow.webkitAudioContext

  if (!AudioContextCtor) {
    stream.getTracks().forEach(track => track.stop())
    throw new Error('Web Audio is not supported in this environment')
  }

  const audioContext = new AudioContextCtor()

  if (audioContext.state === 'suspended') {
    void audioContext.resume?.()?.catch?.(() => undefined)
  }

  const source = audioContext.createMediaStreamSource(stream)
  const analyser = audioContext.createAnalyser()
  analyser.fftSize = 256
  const levelData = new Uint8Array(analyser.fftSize)
  source.connect(analyser)

  const processor = audioContext.createScriptProcessor(4096, 1, 1)

  processor.onaudioprocess = event => {
    try {
      const input = event.inputBuffer.getChannelData(0)
      const downsampled = downsampleFloat32(input, audioContext.sampleRate, DUPLEX_SAMPLE_RATE)

      if (downsampled.length) {
        options.onFrame(floatToPcm16Base64(downsampled))
      }
    } catch (error) {
      options.onError?.(error instanceof Error ? error : new Error('Duplex mic capture failed'))
    }

    // Keep the processor node alive without echoing input to the speakers.
    event.outputBuffer.getChannelData(0).fill(0)
  }

  source.connect(processor)
  processor.connect(audioContext.destination)

  let levelRaf: null | number = null

  if (options.onLevel) {
    const tick = () => {
      analyser.getByteTimeDomainData(levelData)
      let sum = 0

      for (const value of levelData) {
        const centered = value - 128
        sum += centered * centered
      }

      const rms = Math.sqrt(sum / levelData.length)
      options.onLevel?.(Math.min(1, rms / 42))
      levelRaf = window.requestAnimationFrame(tick)
    }

    levelRaf = window.requestAnimationFrame(tick)
  }

  return {
    stop: () => {
      if (levelRaf !== null) {
        window.cancelAnimationFrame(levelRaf)
        levelRaf = null
      }

      processor.onaudioprocess = null

      try {
        processor.disconnect()
      } catch {
        // already disconnected
      }

      try {
        source.disconnect()
      } catch {
        // already disconnected
      }

      void audioContext.close?.()?.catch?.(() => undefined)
      stream.getTracks().forEach(track => track.stop())
    }
  }
}

// --- TTS playback queue --------------------------------------------------

export interface DuplexAudioPlayer {
  /** Schedule one TTS chunk for gapless playback. */
  enqueueChunk: (data: string, seq: number) => void
  /** No more chunks are coming for the current utterance; fire onDrained once queued audio finishes. */
  expectEnd: () => void
  /** Barge-in kill: stop everything playing/queued immediately. An optional
   *  sampleRate (from tts_start) applies to chunks enqueued after the reset. */
  reset: (sampleRate?: number) => void
  /** Register (single) callback for "all queued audio finished playing" after expectEnd(). */
  onDrained: (callback: (() => void) | null) => void
  /** Tear down the underlying AudioContext entirely. */
  destroy: () => void
}

export interface DuplexAudioPlayerDeps {
  createAudioContext?: () => AudioContext
}

export function createDuplexAudioPlayer(deps: DuplexAudioPlayerDeps = {}): DuplexAudioPlayer {
  const createAudioContext = deps.createAudioContext ?? (() => new AudioContext())

  let ctx: AudioContext | null = null
  let nextTime = 0
  let sampleRate = DEFAULT_TTS_SAMPLE_RATE
  const sources = new Set<AudioBufferSourceNode>()
  let endRequested = false
  let drainedCallback: (() => void) | null = null
  let destroyed = false

  const ensureContext = (): AudioContext | null => {
    if (destroyed) {
      return null
    }

    if (!ctx) {
      try {
        ctx = createAudioContext()
        void ctx.resume?.()?.catch?.(() => undefined)
        nextTime = ctx.currentTime
      } catch {
        ctx = null
      }
    }

    return ctx
  }

  const maybeDrained = () => {
    if (endRequested && sources.size === 0) {
      endRequested = false
      drainedCallback?.()
    }
  }

  return {
    destroy: () => {
      destroyed = true
      endRequested = false

      for (const source of sources) {
        try {
          source.stop()
        } catch {
          // already stopped
        }
      }

      sources.clear()
      void ctx?.close?.()?.catch?.(() => undefined)
      ctx = null
    },

    enqueueChunk: (data: string) => {
      const context = ensureContext()

      if (!context) {
        return
      }

      let samples: Float32Array

      try {
        samples = pcm16Base64ToFloat32(data)
      } catch {
        // Malformed chunk — drop it rather than throw and take down playback.
        return
      }

      if (!samples.length) {
        return
      }

      const buffer = context.createBuffer(1, samples.length, sampleRate)
      buffer.getChannelData(0).set(samples)
      const source = context.createBufferSource()
      source.buffer = buffer
      source.connect(context.destination)

      const startAt = Math.max(nextTime, context.currentTime)
      source.start(startAt)
      nextTime = startAt + samples.length / sampleRate

      sources.add(source)

      source.onended = () => {
        sources.delete(source)
        maybeDrained()
      }
    },

    expectEnd: () => {
      endRequested = true
      maybeDrained()
    },

    onDrained: callback => {
      drainedCallback = callback
    },

    reset: (nextSampleRate?: number) => {
      endRequested = false

      if (typeof nextSampleRate === 'number' && nextSampleRate > 0) {
        sampleRate = nextSampleRate
      }

      for (const source of sources) {
        try {
          source.stop()
        } catch {
          // already stopped
        }
      }

      sources.clear()
      nextTime = ctx?.currentTime ?? 0
    }
  }
}
