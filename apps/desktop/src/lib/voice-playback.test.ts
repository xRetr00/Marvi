import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { playSpeechText, stopVoicePlayback } from './voice-playback'

const speakText = vi.fn()

vi.mock('@/hermes', () => ({
  speakText: (text: string) => speakText(text)
}))

vi.mock('@/store/voice-playback', () => ({
  $voicePlayback: { get: () => ({ status: 'idle' }) },
  setVoicePlaybackState: vi.fn()
}))

class FakeAudio {
  src = ''
  addEventListener(event: string, callback: () => void) {
    if (event === 'ended') {
      setTimeout(callback, 0)
    }
  }
  load() {}
  pause() {}
  play() {
    return Promise.resolve()
  }
  removeEventListener() {}
}

describe('playSpeechText', () => {
  beforeEach(() => {
    speakText.mockClear()
    vi.stubGlobal('Audio', FakeAudio)
    vi.stubGlobal(
      'AudioContext',
      class {
        currentTime = 0
        destination = {}
        createBuffer(_channels: number, length: number) {
          return {
            getChannelData: () => new Float32Array(length)
          }
        }
        createBufferSource() {
          return {
            connect: vi.fn(),
            start: vi.fn()
          }
        }
      }
    )
    speakText.mockResolvedValue({
      data_url: 'data:audio/mpeg;base64,AA==',
      mime_type: 'audio/mpeg',
      ok: true,
      provider: 'test'
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('sends markdown-cleaned speech text through every playback path', async () => {
    await playSpeechText('## Result\n\nUse `PocketTTS` for **voice**.', { source: 'read-aloud' })

    expect(speakText).toHaveBeenCalledWith('Result. Use PocketTTS for voice.')
  })

  it('uses the chunked TTS stream when the backend supports token auth', async () => {
    const encoder = new TextEncoder()
    const fetch = vi.fn().mockResolvedValue({
      body: new ReadableStream({
        start(controller) {
          controller.enqueue(
            encoder.encode(
              [
                JSON.stringify({ type: 'start', sample_rate: 24000 }),
                JSON.stringify({ type: 'chunk', audio: 'AAE=' }),
                JSON.stringify({ type: 'end' }),
                ''
              ].join('\n')
            )
          )
          controller.close()
        }
      }),
      ok: true
    })
    vi.stubGlobal('fetch', fetch)
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        getConnection: vi.fn().mockResolvedValue({
          authMode: 'token',
          baseUrl: 'http://127.0.0.1:9119',
          token: 'secret'
        })
      }
    })

    await playSpeechText('Hello', { source: 'read-aloud' })

    expect(fetch).toHaveBeenCalledWith(
      'http://127.0.0.1:9119/api/audio/speak/stream',
      expect.objectContaining({
        body: JSON.stringify({ text: 'Hello.' }),
        headers: expect.objectContaining({ 'X-Hermes-Session-Token': 'secret' }),
        method: 'POST',
        signal: expect.any(AbortSignal)
      })
    )
    expect(speakText).not.toHaveBeenCalled()
  })

  it('aborts the streaming TTS request when playback is stopped', async () => {
    let signal: AbortSignal | undefined
    const fetch = vi.fn().mockImplementation((_url, options: RequestInit) => {
      signal = options.signal instanceof AbortSignal ? options.signal : undefined

      return Promise.resolve({
        body: new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(`${JSON.stringify({ type: 'start', sample_rate: 24000 })}\n`))
          }
        }),
        ok: true
      })
    })
    vi.stubGlobal('fetch', fetch)
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        getConnection: vi.fn().mockResolvedValue({
          authMode: 'token',
          baseUrl: 'http://127.0.0.1:9119',
          token: 'secret'
        })
      }
    })

    const playback = playSpeechText('Hello', { source: 'read-aloud' })
    await vi.waitFor(() => expect(signal).toBeDefined())
    const abortSignal = signal

    stopVoicePlayback()

    expect(abortSignal?.aborted).toBe(true)
    await expect(playback).resolves.toBe(false)
  })

  it('prebuffers streaming chunks from current audio time instead of cutting them off', async () => {
    const starts: number[] = []
    const resume = vi.fn().mockResolvedValue(undefined)
    const encoder = new TextEncoder()
    const fetch = vi.fn().mockResolvedValue({
      body: new ReadableStream({
        start(controller) {
          controller.enqueue(
            encoder.encode(
              [
                JSON.stringify({ type: 'start', sample_rate: 24000 }),
                JSON.stringify({ type: 'chunk', audio: 'AAE=' }),
                JSON.stringify({ type: 'end' }),
                ''
              ].join('\n')
            )
          )
          controller.close()
        }
      }),
      ok: true
    })
    vi.stubGlobal('fetch', fetch)
    vi.stubGlobal(
      'AudioContext',
      class {
        currentTime = 5
        destination = {}
        sampleRate = 24000
        createBuffer(_channels: number, length: number) {
          return {
            getChannelData: () => new Float32Array(length)
          }
        }
        createBufferSource() {
          return {
            connect: vi.fn(),
            start: (time: number) => starts.push(time)
          }
        }
        close() {
          return Promise.resolve()
        }
        resume() {
          return resume()
        }
      }
    )
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        getConnection: vi.fn().mockResolvedValue({
          authMode: 'token',
          baseUrl: 'http://127.0.0.1:9119',
          token: 'secret'
        })
      }
    })

    await playSpeechText('Hello', { source: 'read-aloud' })

    expect(resume).toHaveBeenCalled()
    expect(starts[0]).toBe(5)
    expect(Math.max(...starts)).toBeGreaterThanOrEqual(5.9)
    expect(speakText).not.toHaveBeenCalled()
  })

  it('falls back to normal speech synthesis when the stream endpoint reports unavailable', async () => {
    const encoder = new TextEncoder()
    const fetch = vi.fn().mockResolvedValue({
      body: new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode(`${JSON.stringify({ type: 'error', error: 'not qwen3' })}\n`))
          controller.close()
        }
      }),
      ok: true
    })
    vi.stubGlobal('fetch', fetch)
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        getConnection: vi.fn().mockResolvedValue({
          authMode: 'token',
          baseUrl: 'http://127.0.0.1:9119',
          token: 'secret'
        })
      }
    })

    await playSpeechText('Hello', { source: 'read-aloud' })

    expect(fetch).toHaveBeenCalled()
    expect(speakText).toHaveBeenCalledWith('Hello.')
  })
})
