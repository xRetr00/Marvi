import { renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { downsampleFloat32, getMicrophoneStream, resumeAudioContextIfSuspended, useMicRecorder } from './use-mic-recorder'

const errorCopy = {
  microphoneAccessDenied: '',
  microphoneConstraintsUnsupported: '',
  microphoneInUse: '',
  microphonePermissionDenied: '',
  microphoneStartFailed: '',
  microphoneUnsupported: '',
  noMicrophone: ''
}

it('keeps the recorder handle stable across audio-level renders', () => {
  const { result, rerender } = renderHook(() => useMicRecorder(errorCopy))
  const handle = result.current.handle

  rerender()

  expect(result.current.handle).toBe(handle)
})

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

describe('getMicrophoneStream', () => {
  it('retries with plain audio when device constraints fail', async () => {
    const stream = {} as MediaStream

    const getUserMedia = vi
      .fn()
      .mockRejectedValueOnce(new DOMException('No device matched constraints', 'NotFoundError'))
      .mockResolvedValueOnce(stream)

    await expect(getMicrophoneStream({ getUserMedia } as unknown as MediaDevices)).resolves.toBe(stream)
    expect(getUserMedia).toHaveBeenNthCalledWith(1, {
      audio: { autoGainControl: true, echoCancellation: true, noiseSuppression: true }
    })
    expect(getUserMedia).toHaveBeenNthCalledWith(2, { audio: true })
  })
})
