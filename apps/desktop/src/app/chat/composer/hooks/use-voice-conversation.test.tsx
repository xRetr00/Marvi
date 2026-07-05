import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { clearRecentSpokenText, rememberSpokenText } from '@/lib/voice-echo-guard'

import { useVoiceConversation } from './use-voice-conversation'

const openStreamingTranscription = vi.fn()
const playSpeechText = vi.fn()
const stopVoicePlayback = vi.fn()
const startMic = vi.fn()
const stopMic = vi.fn()
const cancelMic = vi.fn()

interface RecorderOptionsForTest {
  onSilence?: () => void
}

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      notifications: {
        voice: {
          configureSpeechToText: 'Configure STT',
          couldNotStartSession: 'Could not start',
          microphoneFailed: 'Microphone failed',
          playbackFailed: 'Playback failed',
          transcriptionFailed: 'Transcription failed',
          unavailable: 'Unavailable'
        }
      }
    }
  })
}))

vi.mock('@/lib/streaming-transcription', () => ({
  openStreamingTranscription: (...args: unknown[]) => openStreamingTranscription(...args)
}))

vi.mock('@/lib/voice-barge-in', () => ({
  createBargeInGate: () => ({ update: (level: number) => level > 0 })
}))

vi.mock('@/lib/voice-playback', () => ({
  playSpeechText: (...args: unknown[]) => playSpeechText(...args),
  stopVoicePlayback: () => stopVoicePlayback()
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('@/store/voice-presence', () => ({
  setUserCaption: vi.fn()
}))

vi.mock('./use-mic-recorder', () => ({
  useMicRecorder: () => ({
    handle: {
      cancel: cancelMic,
      start: (...args: unknown[]) => startMic(...args),
      stop: stopMic
    },
    level: 0
  })
}))

describe('useVoiceConversation', () => {
  afterEach(() => {
    vi.clearAllMocks()
    clearRecentSpokenText()
  })

  it('keeps listening when semantic turn detection says the user turn is incomplete', async () => {
    const recorderState: { options?: RecorderOptionsForTest } = {}
    const streamSession = { checkTurn: vi.fn().mockResolvedValue(false), finish: vi.fn(), sendFrame: vi.fn() }
    openStreamingTranscription.mockResolvedValue(streamSession)
    startMic.mockImplementation(async options => {
      recorderState.options = options
    })

    const { result } = renderHook(() =>
      useVoiceConversation({
        busy: false,
        consumePendingResponse: vi.fn(),
        enabled: true,
        onSubmit: vi.fn(),
        onTranscribeAudio: vi.fn(),
        pendingResponse: () => null,
        streamingSttEnabled: true
      })
    )

    await act(async () => {
      await result.current.start()
    })
    await waitFor(() => expect(startMic).toHaveBeenCalled())

    await act(async () => {
      await recorderState.options?.onSilence?.()
    })

    expect(streamSession.checkTurn).toHaveBeenCalledTimes(1)
    expect(stopMic).not.toHaveBeenCalled()
    expect(streamSession.finish).not.toHaveBeenCalled()
    expect(result.current.status).toBe('listening')
  })

  it('drops transcripts that are self-echo from recent TTS', async () => {
    const recorderState: { options?: RecorderOptionsForTest } = {}
    const onSubmit = vi.fn()
    rememberSpokenText('The deployment succeeded and the logs are green.', Date.now())
    startMic.mockImplementation(async options => {
      recorderState.options = options
    })
    stopMic.mockResolvedValue({ audio: new Blob(['voice']), durationMs: 1000, heardSpeech: true })

    const { result } = renderHook(() =>
      useVoiceConversation({
        busy: false,
        consumePendingResponse: vi.fn(),
        enabled: true,
        onSubmit,
        onTranscribeAudio: vi.fn().mockResolvedValue('deployment succeeded and logs are green'),
        pendingResponse: () => null,
        streamingSttEnabled: false
      })
    )

    await act(async () => {
      await result.current.start()
    })
    await waitFor(() => expect(startMic).toHaveBeenCalled())

    await act(async () => {
      await recorderState.options?.onSilence?.()
    })

    expect(onSubmit).not.toHaveBeenCalled()
    expect(result.current.status).toBe('listening')
  })

  it('interrupts the active assistant turn on barge-in and stops speaking stale chunks', async () => {
    const recorderState: { options?: RecorderOptionsForTest & { onLevel?: (level: number) => void } } = {}
    const onSubmit = vi.fn()
    const onInterrupt = vi.fn()
    const consumePendingResponse = vi.fn()
    let response = null as null | { id: string; pending: boolean; text: string }
    let releasePlayback!: () => void

    startMic.mockImplementation(async options => {
      recorderState.options = options
    })
    stopMic.mockResolvedValue({ audio: new Blob(['voice']), durationMs: 1000, heardSpeech: true })
    playSpeechText.mockImplementation(
      () =>
        new Promise<void>(resolve => {
          releasePlayback = resolve
        })
    )

    const { result, rerender } = renderHook(() =>
      useVoiceConversation({
        busy: false,
        consumePendingResponse,
        enabled: true,
        onInterrupt,
        onSubmit,
        onTranscribeAudio: vi.fn().mockResolvedValue('hello'),
        pendingResponse: () => response,
        streamingSttEnabled: false
      })
    )

    response = { id: 'assistant-1', pending: true, text: 'This is a spoken sentence.' }
    await act(async () => {
      await result.current.start()
      await recorderState.options?.onSilence?.()
    })
    rerender()
    await waitFor(() => expect(playSpeechText).toHaveBeenCalledTimes(1))

    act(() => {
      recorderState.options?.onLevel?.(0.5)
    })

    expect(stopVoicePlayback).toHaveBeenCalledTimes(1)
    expect(onInterrupt).toHaveBeenCalledTimes(1)
    expect(consumePendingResponse).toHaveBeenCalled()

    response = { id: 'assistant-1', pending: true, text: 'This is a spoken sentence. More stale text.' }
    act(() => {
      releasePlayback()
    })
    rerender()

    expect(playSpeechText).toHaveBeenCalledTimes(1)
  })
})
