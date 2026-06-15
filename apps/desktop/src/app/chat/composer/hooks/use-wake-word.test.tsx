import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useWakeWord } from './use-wake-word'

const openWakeWordSession = vi.fn()
const notifyError = vi.fn()
const cancelMic = vi.fn()

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
  openStreamingSttSession: vi.fn()
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
      start: vi.fn(),
      stop: vi.fn()
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
})
