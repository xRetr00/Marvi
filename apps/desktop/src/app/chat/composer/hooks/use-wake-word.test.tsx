import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useWakeWord } from './use-wake-word'

const openWakeWordSession = vi.fn()
const openStreamingTranscription = vi.fn()
const notifyError = vi.fn()
const cancelMic = vi.fn()
const startMic = vi.fn()
const stopMic = vi.fn()

interface RecorderOptionsForTest {
  onAudioFrame?: (samples: Float32Array) => void
  onSilence?: () => void
}

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      notifications: {
        voice: {
          microphoneFailed: 'Microphone failed',
          noSpeechDetected: 'No speech detected',
          streamingUnavailable: 'Wake word unavailable',
          transcriptionFailed: 'Transcription failed',
          tryRecordingAgain: 'Try recording again'
        }
      }
    }
  })
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: (...args: unknown[]) => notifyError(...args)
}))

vi.mock('@/lib/streaming-transcription', () => ({
  openStreamingTranscription: (...args: unknown[]) => openStreamingTranscription(...args)
}))

vi.mock('@/lib/wake-word', () => ({
  normalizeWakeWordConfig: () => ({
    boost: 2,
    commandTimeoutMs: 8000,
    cooldownMs: 1000,
    debug: false,
    enabled: false,
    model: 'kws-en-3.3m',
    phrases: ['hey marvi'],
    provider: 'sherpa_onnx',
    sampleRate: 16000,
    threshold: 0.35
  }),
  openWakeWordSession: (...args: unknown[]) => openWakeWordSession(...args),
  stripWakePhrase: (text: string, phrases: string[]) => {
    const lower = text.toLowerCase()
    const phrase = phrases.find(item => lower.startsWith(item))
    return phrase ? text.slice(phrase.length).trim() : text
  }
}))

vi.mock('./use-mic-recorder', () => ({
  useMicRecorder: () => ({
    handle: {
      cancel: cancelMic,
      start: (...args: unknown[]) => startMic(...args),
      stop: (...args: unknown[]) => stopMic(...args)
    }
  })
}))

describe('useWakeWord', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('does not retry wake startup continuously after an unavailable backend error', async () => {
    openWakeWordSession.mockRejectedValue(new Error('sherpa missing'))

    const { result } = renderHook(() =>
      useWakeWord({
        busy: false,
        config: {
          boost: 2,
          commandTimeoutMs: 8000,
          cooldownMs: 1000,
          debug: false,
          enabled: true,
          phrases: ['hey marvi'],
          provider: 'sherpa_onnx',
          sampleRate: 16000,
          threshold: 0.35
        },
        enabled: true,
        onSubmit: vi.fn(),
        onTranscribeAudio: vi.fn()
      })
    )

    await waitFor(() => expect(openWakeWordSession).toHaveBeenCalled())
    await new Promise(resolve => window.setTimeout(resolve, 50))

    expect(openWakeWordSession).toHaveBeenCalledTimes(1)
    expect(notifyError).toHaveBeenCalledTimes(1)
    expect(result.current.status).toBe('idle')
  })

  it('keeps the idle wake listener open when silence fires before wake detection', async () => {
    const recorderState: { options?: RecorderOptionsForTest } = {}
    const wakeSession = { sendFrame: vi.fn(), stop: vi.fn() }
    openWakeWordSession.mockResolvedValue(wakeSession)
    startMic.mockImplementation(async options => {
      recorderState.options = options
    })
    stopMic.mockResolvedValue(null)

    renderHook(() =>
      useWakeWord({
        busy: false,
        config: {
          boost: 2,
          commandTimeoutMs: 8000,
          cooldownMs: 1000,
          debug: false,
          enabled: true,
          phrases: ['hey marvi'],
          provider: 'sherpa_onnx',
          sampleRate: 16000,
          threshold: 0.35
        },
        enabled: true,
        onSubmit: vi.fn(),
        onTranscribeAudio: vi.fn()
      })
    )

    await waitFor(() => expect(startMic).toHaveBeenCalled())

    await act(async () => {
      recorderState.options?.onSilence?.()
      await Promise.resolve()
    })

    expect(stopMic).not.toHaveBeenCalled()
    expect(wakeSession.stop).not.toHaveBeenCalled()
    expect(openWakeWordSession).toHaveBeenCalledTimes(1)
  })

  it('transcribes only post-wake command audio with batch STT', async () => {
    let wakeOptions: { debug?: boolean; onDetected: (phrase: string) => void } | null = null
    const recorderState: { options?: RecorderOptionsForTest } = {}
    const wakeSession = { sendFrame: vi.fn(), stop: vi.fn() }
    const onTranscribeAudio = vi.fn().mockResolvedValue('hey marvi how are you')
    const onSubmit = vi.fn()
    openWakeWordSession.mockImplementation(async options => {
      wakeOptions = options
      return wakeSession
    })
    startMic.mockImplementation(async options => {
      recorderState.options = options
    })
    stopMic.mockResolvedValue({ audio: new Blob(['idle'], { type: 'audio/webm' }), durationMs: 1200, heardSpeech: true })

    renderHook(() =>
      useWakeWord({
        busy: false,
        config: {
          boost: 2,
          commandTimeoutMs: 8000,
          cooldownMs: 1000,
          debug: false,
          enabled: true,
          phrases: ['hey marvi'],
          provider: 'sherpa_onnx',
          sampleRate: 16000,
          threshold: 0.35
        },
        enabled: true,
        onSubmit,
        onTranscribeAudio
      })
    )

    await waitFor(() => expect(startMic).toHaveBeenCalled())

    const beforeWake = new Float32Array([0.1, 0.2])
    recorderState.options?.onAudioFrame?.(beforeWake)

    expect(wakeSession.sendFrame).toHaveBeenCalledWith(beforeWake)

    await act(async () => {
      wakeOptions?.onDetected('hey marvi')
      await Promise.resolve()
    })

    const commandFrame = new Float32Array([0.3, 0.4])
    recorderState.options?.onAudioFrame?.(commandFrame)

    await act(async () => {
      recorderState.options?.onSilence?.()
      await waitFor(() => expect(onSubmit).toHaveBeenCalledWith('how are you'))
    })

    expect(onTranscribeAudio).toHaveBeenCalledTimes(1)
    const audio = onTranscribeAudio.mock.calls[0][0] as Blob
    expect(audio.type).toBe('audio/wav')
    expect(audio.size).toBeGreaterThan(44)
  })

  it('streams post-wake command audio when streaming STT is enabled', async () => {
    let wakeOptions: { debug?: boolean; onDetected: (phrase: string) => void } | null = null
    const recorderState: { options?: RecorderOptionsForTest } = {}
    const wakeSession = { sendFrame: vi.fn(), stop: vi.fn() }
    const streamSession = { finish: vi.fn().mockResolvedValue('hey marvi stream this'), sendFrame: vi.fn() }
    const onSubmit = vi.fn()
    openWakeWordSession.mockImplementation(async options => {
      wakeOptions = options
      return wakeSession
    })
    openStreamingTranscription.mockResolvedValue(streamSession)
    startMic.mockImplementation(async options => {
      recorderState.options = options
    })
    stopMic.mockResolvedValue({ audio: new Blob(['idle'], { type: 'audio/webm' }), durationMs: 1200, heardSpeech: true })

    renderHook(() =>
      useWakeWord({
        busy: false,
        config: {
          boost: 2,
          commandTimeoutMs: 8000,
          cooldownMs: 1000,
          debug: false,
          enabled: true,
          phrases: ['hey marvi'],
          provider: 'sherpa_onnx',
          sampleRate: 16000,
          threshold: 0.35
        },
        enabled: true,
        onSubmit,
        onTranscribeAudio: vi.fn(),
        streamingSttEnabled: true
      })
    )

    await waitFor(() => expect(startMic).toHaveBeenCalled())
    await act(async () => {
      wakeOptions?.onDetected('hey marvi')
      await Promise.resolve()
    })

    const commandFrame = new Float32Array([0.3, 0.4])
    recorderState.options?.onAudioFrame?.(commandFrame)

    await act(async () => {
      recorderState.options?.onSilence?.()
      await waitFor(() => expect(onSubmit).toHaveBeenCalledWith('stream this'))
    })

    expect(openStreamingTranscription).toHaveBeenCalledTimes(1)
    expect(streamSession.sendFrame).toHaveBeenCalledWith(commandFrame)
    expect(streamSession.finish).toHaveBeenCalledTimes(1)
  })

  it('falls back to batch STT when streaming STT is enabled but unavailable', async () => {
    let wakeOptions: { debug?: boolean; onDetected: (phrase: string) => void } | null = null
    const recorderState: { options?: RecorderOptionsForTest } = {}
    const wakeSession = { sendFrame: vi.fn(), stop: vi.fn() }
    const onTranscribeAudio = vi.fn().mockResolvedValue('hey marvi fallback text')
    const onSubmit = vi.fn()
    openWakeWordSession.mockImplementation(async options => {
      wakeOptions = options
      return wakeSession
    })
    openStreamingTranscription.mockRejectedValue(new Error('stream failed'))
    startMic.mockImplementation(async options => {
      recorderState.options = options
    })

    renderHook(() =>
      useWakeWord({
        busy: false,
        config: {
          boost: 2,
          commandTimeoutMs: 8000,
          cooldownMs: 1000,
          debug: false,
          enabled: true,
          phrases: ['hey marvi'],
          provider: 'sherpa_onnx',
          sampleRate: 16000,
          threshold: 0.35
        },
        enabled: true,
        onSubmit,
        onTranscribeAudio,
        streamingSttEnabled: true
      })
    )

    await waitFor(() => expect(startMic).toHaveBeenCalled())

    await act(async () => {
      wakeOptions?.onDetected('hey marvi')
      await Promise.resolve()
    })

    recorderState.options?.onAudioFrame?.(new Float32Array([0.3, 0.4]))

    await act(async () => {
      recorderState.options?.onSilence?.()
      await waitFor(() => expect(onSubmit).toHaveBeenCalledWith('fallback text'))
    })

    expect(notifyError).not.toHaveBeenCalled()
    expect(onTranscribeAudio).toHaveBeenCalledTimes(1)
  })

  it('passes debug mode to the wake-word session', async () => {
    const wakeSession = { sendFrame: vi.fn(), stop: vi.fn() }
    openWakeWordSession.mockResolvedValue(wakeSession)
    startMic.mockResolvedValue(undefined)

    renderHook(() =>
      useWakeWord({
        busy: false,
        config: {
          boost: 2,
          commandTimeoutMs: 8000,
          cooldownMs: 1000,
          debug: true,
          enabled: true,
          phrases: ['hey marvi'],
          provider: 'sherpa_onnx',
          sampleRate: 16000,
          threshold: 0.35
        },
        enabled: true,
        onSubmit: vi.fn(),
        onTranscribeAudio: vi.fn()
      })
    )

    await waitFor(() => expect(openWakeWordSession).toHaveBeenCalled())

    expect(openWakeWordSession.mock.calls.at(-1)?.[0]).toMatchObject({ debug: true })
  })
})
