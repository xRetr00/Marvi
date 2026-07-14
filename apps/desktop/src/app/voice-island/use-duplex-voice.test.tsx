// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { connectDuplexVoice, type DuplexConnectOptions } from './duplex-client'
import { INITIAL_DUPLEX_STATE } from './duplex-session'
import { useDuplexVoice } from './use-duplex-voice'

vi.mock('./duplex-client', () => ({ connectDuplexVoice: vi.fn() }))

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('useDuplexVoice', () => {
  it('reconnects a previously live session after a background transport interruption', async () => {
    vi.useFakeTimers()
    const attempts: DuplexConnectOptions[] = []
    vi.mocked(connectDuplexVoice).mockImplementation(async options => {
      attempts.push(options)

      return { stop: vi.fn() }
    })

    const { result } = renderHook(() => useDuplexVoice(true))
    await act(async () => Promise.resolve())

    act(() => attempts[0].onState({ ...INITIAL_DUPLEX_STATE, phase: 'listening' }))
    expect(result.current.status).toBe('active')

    act(() => attempts[0].onUnavailable('duplex websocket closed'))
    expect(result.current.status).toBe('connecting')

    await act(async () => vi.advanceTimersByTimeAsync(400))
    expect(connectDuplexVoice).toHaveBeenCalledTimes(2)
  })
})
