import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $voicePlayback } from '@/store/voice-playback'

import { useVoiceConversation } from './use-voice-conversation'

const mocks = vi.hoisted(() => {
  let onSilence: null | (() => void) = null
  let resolveSpeech: null | ((played: boolean) => void) = null

  const stopVoicePlayback = vi.fn(() => {
    const current = $voicePlayback.get()
    $voicePlayback.set({ ...current, sequence: current.sequence + 1, status: 'idle' })
  })

  const handle = {
    cancel: vi.fn(),
    start: vi.fn(async (options: { onSilence: () => void }) => {
      onSilence = options.onSilence
    }),
    stop: vi.fn(async () => ({
      audio: new Blob(['voice'], { type: 'audio/webm' }),
      heardSpeech: true
    }))
  }

  return {
    enqueueSpeech: vi.fn(),
    finishPlayback(played = true) {
      resolveSpeech?.(played)
      resolveSpeech = null
    },
    finishSpeech: vi.fn(
      () =>
        new Promise<boolean>(resolve => {
          resolveSpeech = resolve
        })
    ),
    handle,
    resetSpeechMocks() {
      resolveSpeech = null
    },
    startSpeechSession: vi.fn(() => {
      const current = $voicePlayback.get()
      $voicePlayback.set({ ...current, sequence: current.sequence + 1, status: 'preparing' })
    }),
    stopVoicePlayback,
    triggerSilence() {
      onSilence?.()
    }
  }
})

vi.mock('./use-mic-recorder', () => ({
  useMicRecorder: () => ({ handle: mocks.handle, level: 0 })
}))

vi.mock('@/lib/barge-in-detector', () => ({
  startBargeInDetector: () => vi.fn()
}))

vi.mock('@/lib/voice-playback', () => ({
  enqueueSpeech: mocks.enqueueSpeech,
  finishSpeech: mocks.finishSpeech,
  startSpeechSession: mocks.startSpeechSession,
  stopVoicePlayback: mocks.stopVoicePlayback
}))

vi.mock('@/lib/thinking-sound', () => ({
  startThinkingSound: vi.fn(),
  stopThinkingSound: vi.fn()
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      notifications: {
        voice: {
          configureSpeechToText: '',
          couldNotStartSession: '',
          microphoneFailed: '',
          playbackFailed: '',
          transcriptionFailed: '',
          unavailable: ''
        }
      }
    }
  })
}))

function renderRearmConversation(responseId: string, responseText: string) {
  let response: null | { id: string; pending: boolean; text: string } = null

  return renderHook(
    ({ enabled }) =>
      useVoiceConversation({
        busy: false,
        consumePendingResponse: vi.fn(),
        enabled,
        onSubmit: async () => {
          response = { id: responseId, pending: false, text: responseText }
        },
        onTranscribeAudio: async () => 'Hello',
        pendingResponse: () => response
      }),
    { initialProps: { enabled: false } }
  )
}

async function beginReply(hook: ReturnType<typeof renderRearmConversation>) {
  hook.rerender({ enabled: true })
  await waitFor(() => expect(mocks.handle.start).toHaveBeenCalledTimes(1))

  await act(async () => {
    mocks.triggerSilence()
  })
}

describe('useVoiceConversation playback rearm', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    mocks.resetSpeechMocks()
    $voicePlayback.set({
      audioElement: null,
      caption: null,
      level: 0,
      messageId: null,
      sequence: 0,
      source: null,
      status: 'idle'
    })
  })

  it('re-arms the microphone after normal streaming playback completes', async () => {
    $voicePlayback.set({
      audioElement: null,
      caption: null,
      level: 0,
      messageId: null,
      sequence: 7,
      source: null,
      status: 'idle'
    })
    const hook = renderRearmConversation('reply-1', 'Hello back')

    await beginReply(hook)
    await waitFor(() => expect(mocks.startSpeechSession).toHaveBeenCalled())
    expect($voicePlayback.get().sequence).toBeGreaterThan(7)

    await act(async () => {
      mocks.finishPlayback()
    })

    await waitFor(() => expect(mocks.handle.start).toHaveBeenCalledTimes(2))
    expect(hook.result.current.status).toBe('listening')
  })

  it('does not re-arm after voice conversation is disabled during playback', async () => {
    const hook = renderRearmConversation('reply-disabled', 'Playing now')

    await beginReply(hook)
    await waitFor(() => expect(mocks.startSpeechSession).toHaveBeenCalled())

    hook.rerender({ enabled: false })
    await act(async () => {
      mocks.finishPlayback(false)
    })

    await waitFor(() => expect(hook.result.current.status).toBe('idle'))
    expect(mocks.handle.start).toHaveBeenCalledTimes(1)
  })
})
