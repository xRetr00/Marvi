import { useEffect, useRef, useState } from 'react'

type BrowserAudioContext = typeof AudioContext

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

    output[i] = count > 0 ? sum / count : input[Math.min(start, input.length - 1)] ?? 0
  }

  return output
}

interface ResumableAudioContext {
  state: AudioContextState
  resume: () => Promise<void> | void
}

export function resumeAudioContextIfSuspended(audioContext: ResumableAudioContext): void {
  if (audioContext.state !== 'suspended') {
    return
  }

  const resumeResult = audioContext.resume()

  if (resumeResult && typeof resumeResult.catch === 'function') {
    void resumeResult.catch(() => undefined)
  }
}

export interface MicRecorderOptions {
  onAudioFrame?: (samples: Float32Array) => void
  onLevel?: (level: number) => void
  onError?: (error: Error) => void
  onSilence?: () => boolean | void | Promise<boolean | void>
  silenceLevel?: number
  silenceMs?: number
  idleSilenceMs?: number
}

export interface MicRecording {
  audio: Blob
  durationMs: number
  heardSpeech: boolean
}

export interface MicRecorderErrorCopy {
  microphoneAccessDenied: string
  microphoneConstraintsUnsupported: string
  microphoneInUse: string
  microphonePermissionDenied: string
  microphoneStartFailed: string
  microphoneUnsupported: string
  noMicrophone: string
}

interface MicRecorderHandle {
  start: (options?: MicRecorderOptions) => Promise<void>
  stop: () => Promise<MicRecording | null>
  cancel: () => void
}

function micError(error: unknown, copy: MicRecorderErrorCopy): Error {
  const name = error instanceof DOMException ? error.name : ''

  if (name === 'NotAllowedError' || name === 'SecurityError') {
    return new Error(copy.microphonePermissionDenied)
  }

  if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
    return new Error(copy.noMicrophone)
  }

  if (name === 'NotReadableError' || name === 'TrackStartError') {
    return new Error(copy.microphoneInUse)
  }

  if (name === 'OverconstrainedError') {
    return new Error(copy.microphoneConstraintsUnsupported)
  }

  if (error instanceof Error) {
    return error
  }

  return new Error(copy.microphoneStartFailed)
}

function shouldRetryPlainAudio(error: unknown): boolean {
  const name = error instanceof DOMException ? error.name : ''

  return name === 'NotFoundError' || name === 'DevicesNotFoundError' || name === 'OverconstrainedError'
}

export async function getMicrophoneStream(mediaDevices: MediaDevices): Promise<MediaStream> {
  try {
    return await mediaDevices.getUserMedia({
      audio: { autoGainControl: true, echoCancellation: true, noiseSuppression: true }
    })
  } catch (error) {
    if (!shouldRetryPlainAudio(error)) {
      throw error
    }

    return mediaDevices.getUserMedia({ audio: true })
  }
}

export function useMicRecorder(copy: MicRecorderErrorCopy): {
  handle: MicRecorderHandle
  level: number
  recording: boolean
} {
  const [level, setLevel] = useState(0)
  const [recording, setRecording] = useState(false)

  const recorderRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const audioContextRef = useRef<AudioContext | null>(null)
  const processorRef = useRef<ScriptProcessorNode | null>(null)
  const animationRef = useRef<number | null>(null)
  const startedAtRef = useRef(0)
  const heardSpeechRef = useRef(false)
  const silenceTriggeredRef = useRef(false)
  const silencePendingRef = useRef(false)
  const silenceStartedAtRef = useRef<number | null>(null)
  const stopResolverRef = useRef<((recording: MicRecording | null) => void) | null>(null)
  const methodsRef = useRef<MicRecorderHandle | null>(null)

  const handleRef = useRef<MicRecorderHandle>({
    start: options => methodsRef.current!.start(options),
    stop: () => methodsRef.current!.stop(),
    cancel: () => methodsRef.current!.cancel()
  })

  const cleanup = () => {
    if (animationRef.current) {
      window.cancelAnimationFrame(animationRef.current)
      animationRef.current = null
    }

    void audioContextRef.current?.close()
    audioContextRef.current = null
    processorRef.current = null
    streamRef.current?.getTracks().forEach(track => track.stop())
    streamRef.current = null
    recorderRef.current = null
    setLevel(0)
    setRecording(false)
    silenceTriggeredRef.current = false
    silencePendingRef.current = false
  }

  useEffect(() => () => cleanup(), [])

  const startMeter = (stream: MediaStream, options: MicRecorderOptions) => {
    const audioWindow = window as Window & { webkitAudioContext?: BrowserAudioContext }
    const AudioContextCtor = window.AudioContext || audioWindow.webkitAudioContext

    if (!AudioContextCtor) {
      return
    }

    try {
      const audioContext = new AudioContextCtor()
      resumeAudioContextIfSuspended(audioContext)
      const analyser = audioContext.createAnalyser()
      const source = audioContext.createMediaStreamSource(stream)

      analyser.fftSize = 256
      const data = new Uint8Array(analyser.fftSize)

      source.connect(analyser)

      if (options.onAudioFrame) {
        const processor = audioContext.createScriptProcessor(4096, 1, 1)

        processor.onaudioprocess = event => {
          const input = event.inputBuffer.getChannelData(0)
          options.onAudioFrame?.(downsampleFloat32(input, audioContext.sampleRate, 16000))

          const output = event.outputBuffer.getChannelData(0)
          output.fill(0)
        }

        source.connect(processor)
        processor.connect(audioContext.destination)
        processorRef.current = processor
      }

      audioContextRef.current = audioContext

      const tick = () => {
        analyser.getByteTimeDomainData(data)

        let sum = 0

        for (const value of data) {
          const centered = value - 128
          sum += centered * centered
        }

        const rms = Math.sqrt(sum / data.length)
        const normalized = Math.min(1, rms / 42)
        const now = Date.now()

        setLevel(normalized)
        options.onLevel?.(normalized)

        const speechThreshold = options.silenceLevel ?? 0
        const silenceMs = options.silenceMs ?? 0
        const idleSilenceMs = options.idleSilenceMs ?? 0

        if (speechThreshold > 0 && options.onSilence && !silenceTriggeredRef.current && !silencePendingRef.current) {
          if (normalized >= speechThreshold) {
            heardSpeechRef.current = true
            silenceStartedAtRef.current = null
          } else if (heardSpeechRef.current && silenceMs > 0) {
            silenceStartedAtRef.current ??= now

            if (now - silenceStartedAtRef.current >= silenceMs) {
              silenceTriggeredRef.current = true
              silencePendingRef.current = true
              Promise.resolve(options.onSilence()).then(shouldStop => {
                silencePendingRef.current = false

                if (shouldStop === false) {
                  silenceTriggeredRef.current = false
                  silenceStartedAtRef.current = null
                  animationRef.current = window.requestAnimationFrame(tick)
                }
              })

              return
            }
          } else if (!heardSpeechRef.current && idleSilenceMs > 0 && now - startedAtRef.current >= idleSilenceMs) {
            silenceTriggeredRef.current = true
            silencePendingRef.current = true
            Promise.resolve(options.onSilence()).then(shouldStop => {
              silencePendingRef.current = false

              if (shouldStop === false) {
                silenceTriggeredRef.current = false
                silenceStartedAtRef.current = null
                startedAtRef.current = Date.now()
                animationRef.current = window.requestAnimationFrame(tick)
              }
            })

            return
          }
        }

        animationRef.current = window.requestAnimationFrame(tick)
      }

      tick()
    } catch {
      setLevel(0)
    }
  }

  const start: MicRecorderHandle['start'] = async (options = {}) => {
    if (recorderRef.current) {
      return
    }

    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      throw new Error(copy.microphoneUnsupported)
    }

    const permitted = await window.hermesDesktop?.requestMicrophoneAccess?.()

    if (permitted === false) {
      throw new Error(copy.microphoneAccessDenied)
    }

    let stream: MediaStream

    try {
      stream = await getMicrophoneStream(navigator.mediaDevices)
    } catch (error) {
      throw micError(error, copy)
    }

    const mimeType =
      ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus', 'audio/ogg', 'audio/wav'].find(
        type => MediaRecorder.isTypeSupported(type)
      ) ?? ''

    let recorder: MediaRecorder

    try {
      recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
    } catch (error) {
      stream.getTracks().forEach(track => track.stop())
      throw micError(error, copy)
    }

    chunksRef.current = []
    streamRef.current = stream
    recorderRef.current = recorder
    heardSpeechRef.current = false
    silenceTriggeredRef.current = false
    silenceStartedAtRef.current = null
    startedAtRef.current = Date.now()

    recorder.ondataavailable = event => {
      if (event.data.size > 0) {
        chunksRef.current.push(event.data)
      }
    }

    recorder.onstop = () => {
      const chunks = chunksRef.current
      const recordingType = recorder.mimeType || mimeType || 'audio/webm'
      const durationMs = Date.now() - startedAtRef.current
      const heardSpeech = heardSpeechRef.current

      chunksRef.current = []
      cleanup()

      const resolver = stopResolverRef.current
      stopResolverRef.current = null

      if (!chunks.length) {
        resolver?.(null)

        return
      }

      resolver?.({
        audio: new Blob(chunks, { type: recordingType }),
        durationMs,
        heardSpeech
      })
    }

    recorder.onerror = event => {
      const error = micError((event as Event & { error?: unknown }).error, copy)
      const resolver = stopResolverRef.current
      stopResolverRef.current = null
      cleanup()
      options.onError?.(error)
      resolver?.(null)
    }

    recorder.start()
    setRecording(true)
    startMeter(stream, options)
  }

  const stop: MicRecorderHandle['stop'] = () =>
    new Promise<MicRecording | null>(resolve => {
      const recorder = recorderRef.current

      if (!recorder || recorder.state === 'inactive') {
        cleanup()
        resolve(null)

        return
      }

      stopResolverRef.current = resolve
      recorder.stop()
    })

  const cancel: MicRecorderHandle['cancel'] = () => {
    const recorder = recorderRef.current
    const resolver = stopResolverRef.current
    stopResolverRef.current = null

    if (recorder && recorder.state !== 'inactive') {
      recorder.ondataavailable = null
      recorder.onerror = null
      recorder.onstop = null
      recorder.stop()
    }

    cleanup()
    resolver?.(null)
  }

  methodsRef.current = { start, stop, cancel }

  return { handle: handleRef.current, level, recording }
}
