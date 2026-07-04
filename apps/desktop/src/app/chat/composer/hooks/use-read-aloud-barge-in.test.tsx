import { cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setVoicePlaybackState } from '@/store/voice-playback'

import { useReadAloudBargeIn } from './use-read-aloud-barge-in'

const startMic = vi.fn()
const cancelMic = vi.fn()
const stopVoicePlayback = vi.fn()

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      notifications: {
        voice: {}
      }
    }
  })
}))

vi.mock('@/lib/voice-playback', () => ({
  stopVoicePlayback: () => stopVoicePlayback()
}))

vi.mock('./use-mic-recorder', () => ({
  useMicRecorder: () => ({
    handle: {
      cancel: cancelMic,
      start: (...args: unknown[]) => startMic(...args)
    }
  })
}))

describe('useReadAloudBargeIn', () => {
  beforeEach(() => {
    setVoicePlaybackState({ audioElement: null, messageId: null, sequence: 0, source: null, status: 'idle' })
    vi.clearAllMocks()
  })

  afterEach(() => {
    cleanup()
    setVoicePlaybackState({ audioElement: null, messageId: null, sequence: 0, source: null, status: 'idle' })
    vi.clearAllMocks()
  })

  it('stops read-aloud playback after sustained speech', async () => {
    let onLevel: ((level: number) => void) | undefined
    startMic.mockImplementation(async options => {
      onLevel = options.onLevel
    })

    renderHook(() => useReadAloudBargeIn({ enabled: true, blocked: false }))

    setVoicePlaybackState({ audioElement: null, messageId: 'm1', sequence: 1, source: 'read-aloud', status: 'speaking' })
    await waitFor(() => expect(startMic).toHaveBeenCalled())

    onLevel?.(0.8)
    await new Promise(resolve => window.setTimeout(resolve, 1_100))
    onLevel?.(0.8)
    await new Promise(resolve => window.setTimeout(resolve, 400))
    onLevel?.(0.8)

    expect(stopVoicePlayback).toHaveBeenCalledTimes(1)
    expect(cancelMic).toHaveBeenCalled()
  })

  it('does not listen while another voice mode is active', async () => {
    renderHook(() => useReadAloudBargeIn({ enabled: true, blocked: true }))

    setVoicePlaybackState({ audioElement: null, messageId: 'm1', sequence: 1, source: 'read-aloud', status: 'speaking' })
    await new Promise(resolve => window.setTimeout(resolve, 0))

    expect(startMic).not.toHaveBeenCalled()
  })
})
