import { afterEach, describe, expect, it, vi } from 'vitest'

import { createVoiceFillerController, type VoiceFillerConfig } from './voice-filler'

class FakeFillerSound {
  starts = 0
  stops = 0

  start(): void {
    this.starts += 1
  }

  stop(): void {
    this.stops += 1
  }
}

describe('createVoiceFillerController', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  const enabledConfig: VoiceFillerConfig = {
    enabled: true,
    minimumPlayDurationMs: 1200,
    responseDeliveryDelayMs: 200,
    startDelayMs: 1000,
    type: 'typing'
  }

  it('does not start filler before the start delay elapses', () => {
    vi.useFakeTimers()
    const sound = new FakeFillerSound()
    const filler = createVoiceFillerController(enabledConfig, { createSound: () => sound })

    filler.waiting()
    vi.advanceTimersByTime(999)

    expect(sound.starts).toBe(0)

    filler.stopNow()
  })

  it('starts after delay and holds the minimum duration before response playback', async () => {
    vi.useFakeTimers()
    const sound = new FakeFillerSound()
    const filler = createVoiceFillerController(enabledConfig, { createSound: () => sound })

    filler.waiting()
    vi.advanceTimersByTime(1000)
    expect(sound.starts).toBe(1)

    const ready = filler.beforeResponse()
    vi.advanceTimersByTime(1199)
    await Promise.resolve()
    expect(sound.stops).toBe(0)

    vi.advanceTimersByTime(1)
    await Promise.resolve()
    vi.advanceTimersByTime(200)
    await expect(ready).resolves.toBeUndefined()
    expect(sound.stops).toBe(1)

    filler.stopNow()
  })

  it('does nothing when disabled', async () => {
    vi.useFakeTimers()
    const sound = new FakeFillerSound()
    const filler = createVoiceFillerController({ ...enabledConfig, enabled: false }, { createSound: () => sound })

    filler.waiting()
    vi.advanceTimersByTime(5000)
    await filler.beforeResponse()

    expect(sound.starts).toBe(0)
    expect(sound.stops).toBe(0)
  })
})
