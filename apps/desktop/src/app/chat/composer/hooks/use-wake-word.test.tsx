import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useWakeWord } from './use-wake-word'

const openWakeWordSession = vi.fn()
const openStreamingSttSession = vi.fn()
const notifyError = vi.fn()
const cancelMic = vi.fn()
const startMic = vi.fn()
const stopMic = vi.fn()

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

vi.mock('@/lib/streaming-stt', () => ({
  openStreamingSttSession: (...args: unknown[]) => openStreamingSttSession(...args)
}))

vi.mock('@/lib/wake-word', () => ({
  normalizeWakeWordConfig: () => ({
    boost: 2,
    commandTimeoutMs: 8000,
    cooldownMs: 1000,
    enabled: false,
    model: 'kws-en-3.3m',
    phrases: ['hey marvi'],
    provider: 'sherpa_onnx',
    sampleRate: 16000,
    threshold: 0.35
  }),
  openWakeWordSession: (...args: unknown[]) => openWakeWordSession(...args),
  stripWakePhrase: (text: string) => text
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
    let recorderOptions: { onSilence: () => void } | null = null
    const wakeSession = { sendFrame: vi.fn(), stop: vi.fn() }
    const streamingSession = { finish: vi.fn(), sendFrame: vi.fn(), stop: vi.fn() }
    openWakeWordSession.mockResolvedValue(wakeSession)
    openStreamingSttSession.mockResolvedValue(streamingSession)
    startMic.mockImplementation(async options => {
      recorderOptions = options
    })
    stopMic.mockResolvedValue(null)

    renderHook(() =>
      useWakeWord({
        busy: false,
        config: {
          boost: 2,
          commandTimeoutMs: 8000,
          cooldownMs: 1000,
          enabled: true,
          phrases: ['hey marvi'],
          provider: 'sherpa_onnx',
          sampleRate: 16000,
          threshold: 0.35
        },
        enabled: true,
        onSubmit: vi.fn(),
        onTranscribeAudio: vi.fn(),
        sttStreamingEnabled: true
      })
    )

    await waitFor(() => expect(startMic).toHaveBeenCalled())

    await act(async () => {
      recorderOptions?.onSilence()
      await Promise.resolve()
    })

    expect(stopMic).not.toHaveBeenCalled()
    expect(wakeSession.stop).not.toHaveBeenCalled()
    expect(streamingSession.stop).not.toHaveBeenCalled()
    expect(openWakeWordSession).toHaveBeenCalledTimes(1)
  })
})
