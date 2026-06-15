import { describe, expect, it, vi } from 'vitest'

import { downsampleFloat32, resumeAudioContextIfSuspended } from './use-mic-recorder'

describe('downsampleFloat32', () => {
  it('averages source samples into the requested output rate', () => {
    const input = new Float32Array([0, 2, 4, 6, 8, 10])

    expect(Array.from(downsampleFloat32(input, 48000, 16000))).toEqual([2, 8])
  })

  it('copies samples when the rate already matches', () => {
    const input = new Float32Array([0.1, 0.2])
    const output = downsampleFloat32(input, 16000, 16000)

    expect(output).not.toBe(input)
    expect(output[0]).toBeCloseTo(0.1)
    expect(output[1]).toBeCloseTo(0.2)
  })
})

describe('resumeAudioContextIfSuspended', () => {
  it('resumes a suspended recording audio context', () => {
    const resume = vi.fn()

    resumeAudioContextIfSuspended({ resume, state: 'suspended' })

    expect(resume).toHaveBeenCalledTimes(1)
  })

  it('does not resume an already running recording audio context', () => {
    const resume = vi.fn()

    resumeAudioContextIfSuspended({ resume, state: 'running' })

    expect(resume).not.toHaveBeenCalled()
  })
})
