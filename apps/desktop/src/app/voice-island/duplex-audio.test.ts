// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'

import { createDuplexAudioPlayer, downsampleFloat32, floatToPcm16Base64, pcm16Base64ToFloat32 } from './duplex-audio'

describe('pcm16 <-> base64 round trip', () => {
  it('round-trips silence, full-scale, and mid-range samples within pcm16 precision', () => {
    const samples = new Float32Array([0, 1, -1, 0.5, -0.5, 0.25])
    const encoded = floatToPcm16Base64(samples)
    const decoded = pcm16Base64ToFloat32(encoded)

    expect(decoded.length).toBe(samples.length)

    for (let i = 0; i < samples.length; i += 1) {
      expect(decoded[i]).toBeCloseTo(samples[i], 3)
    }
  })

  it('clamps out-of-range samples instead of wrapping', () => {
    const samples = new Float32Array([2, -2])
    const decoded = pcm16Base64ToFloat32(floatToPcm16Base64(samples))

    expect(decoded[0]).toBeCloseTo(1, 3)
    expect(decoded[1]).toBeCloseTo(-1, 3)
  })

  it('handles an empty buffer', () => {
    expect(pcm16Base64ToFloat32(floatToPcm16Base64(new Float32Array()))).toEqual(new Float32Array())
  })
})

describe('downsampleFloat32', () => {
  it('returns the input unchanged when rates match', () => {
    const input = new Float32Array([0.1, 0.2, 0.3])
    expect(downsampleFloat32(input, 16000, 16000)).toEqual(input)
  })

  it('halves the length when downsampling 2:1', () => {
    const input = new Float32Array([0, 1, 0, 1, 0, 1, 0, 1])
    const output = downsampleFloat32(input, 32000, 16000)
    expect(output.length).toBe(4)
  })

  it('returns empty for empty input', () => {
    expect(downsampleFloat32(new Float32Array(), 48000, 16000)).toEqual(new Float32Array())
  })
})

// Minimal fakes standing in for the Web Audio API surface the player touches.
// Not a full AudioContext — just enough for enqueue/reset/expectEnd/onDrained
// wiring to be exercised without a real audio device.
function createFakeSource() {
  const source = {
    buffer: null as unknown,
    connect: vi.fn(),
    onended: null as (() => void) | null,
    start: vi.fn(),
    stop: vi.fn(function (this: { onended: (() => void) | null }) {
      this.onended?.()
    })
  }

  return source
}

function createFakeAudioContext() {
  const sources: Array<ReturnType<typeof createFakeSource>> = []

  const ctx = {
    currentTime: 0,
    destination: {},
    createBuffer: (_channels: number, length: number) => ({
      getChannelData: () => new Float32Array(length)
    }),
    createBufferSource: () => {
      const source = createFakeSource()
      sources.push(source)

      return source
    },
    close: vi.fn().mockResolvedValue(undefined),
    resume: vi.fn().mockResolvedValue(undefined)
  }

  return { ctx, sources }
}

describe('createDuplexAudioPlayer', () => {
  it('fires onDrained once queued chunks finish playing after expectEnd', () => {
    const { ctx, sources } = createFakeAudioContext()
    const player = createDuplexAudioPlayer({ createAudioContext: () => ctx as unknown as AudioContext })

    const drained = vi.fn()
    player.onDrained(drained)

    const chunk = floatToPcm16Base64(new Float32Array([0.1, 0.2]))
    player.enqueueChunk(chunk, 0)
    player.expectEnd()

    expect(drained).not.toHaveBeenCalled()

    // Simulate the scheduled source finishing.
    sources[0].onended?.()

    expect(drained).toHaveBeenCalledTimes(1)
  })

  it('does not fire onDrained until expectEnd is called even after chunks finish', () => {
    const { ctx, sources } = createFakeAudioContext()
    const player = createDuplexAudioPlayer({ createAudioContext: () => ctx as unknown as AudioContext })

    const drained = vi.fn()
    player.onDrained(drained)

    player.enqueueChunk(floatToPcm16Base64(new Float32Array([0.1])), 0)
    sources[0].onended?.()

    expect(drained).not.toHaveBeenCalled()
  })

  it('reset() stops all pending sources and clears the queue (barge-in kill)', () => {
    const { ctx, sources } = createFakeAudioContext()
    const player = createDuplexAudioPlayer({ createAudioContext: () => ctx as unknown as AudioContext })

    player.enqueueChunk(floatToPcm16Base64(new Float32Array([0.1])), 0)
    player.enqueueChunk(floatToPcm16Base64(new Float32Array([0.2])), 1)
    player.expectEnd()

    const drained = vi.fn()
    player.onDrained(drained)

    player.reset()

    for (const source of sources) {
      expect(source.stop).toHaveBeenCalled()
    }

    // expectEnd's watch is cancelled by reset — no further drained callback
    // should fire even though stop() synchronously invokes onended above.
    expect(drained).not.toHaveBeenCalled()
  })

  it('ignores a malformed chunk instead of throwing', () => {
    const { ctx } = createFakeAudioContext()
    const player = createDuplexAudioPlayer({ createAudioContext: () => ctx as unknown as AudioContext })

    expect(() => player.enqueueChunk('not-valid-base64!!!', 0)).not.toThrow()
  })

  it('destroy() tears down the context and stops sources', () => {
    const { ctx, sources } = createFakeAudioContext()
    const player = createDuplexAudioPlayer({ createAudioContext: () => ctx as unknown as AudioContext })

    player.enqueueChunk(floatToPcm16Base64(new Float32Array([0.1])), 0)
    player.destroy()

    expect(sources[0].stop).toHaveBeenCalled()
    expect(ctx.close).toHaveBeenCalled()
  })
})
