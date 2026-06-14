import { afterEach, describe, expect, it, vi } from 'vitest'

import { speakText } from '@/hermes'

import { playSpeechTextQueue, stopVoicePlayback } from './voice-playback'

vi.mock('@/hermes', () => ({
  speakText: vi.fn()
}))

class FakeAudio extends EventTarget {
  static instances: FakeAudio[] = []

  src = ''

  constructor(src: string) {
    super()
    this.src = src
    FakeAudio.instances.push(this)
  }

  load(): void {}
  pause(): void {}

  play(): Promise<void> {
    return Promise.resolve()
  }

  end(): void {
    this.dispatchEvent(new Event('ended'))
  }
}

describe('playSpeechTextQueue', () => {
  const originalAudio = globalThis.Audio

  afterEach(() => {
    stopVoicePlayback()
    FakeAudio.instances = []
    ;(globalThis as { Audio: unknown }).Audio = originalAudio
    vi.mocked(speakText).mockReset()
  })

  it('starts playback from the first sentence instead of waiting for the full response audio', async () => {
    ;(globalThis as { Audio: unknown }).Audio = FakeAudio
    vi.mocked(speakText).mockResolvedValue({
      data_url: 'data:audio/wav;base64,AAAA',
      mime_type: 'audio/wav',
      ok: true,
      provider: 'pockettts'
    })

    const promise = playSpeechTextQueue('First sentence. Second sentence.', { source: 'voice-conversation' })

    await vi.waitFor(() => expect(FakeAudio.instances).toHaveLength(1))

    expect(speakText).toHaveBeenNthCalledWith(1, 'First sentence.')
    expect(speakText).not.toHaveBeenCalledWith('First sentence. Second sentence.')

    FakeAudio.instances[0].end()
    await vi.waitFor(() => expect(FakeAudio.instances).toHaveLength(2))
    FakeAudio.instances[1].end()

    await expect(promise).resolves.toBe(true)
    expect(speakText).toHaveBeenNthCalledWith(2, 'Second sentence.')
  })

  it('prefetches the next sentence while the current sentence is playing', async () => {
    ;(globalThis as { Audio: unknown }).Audio = FakeAudio
    vi.mocked(speakText).mockResolvedValue({
      data_url: 'data:audio/wav;base64,AAAA',
      mime_type: 'audio/wav',
      ok: true,
      provider: 'pockettts'
    })

    const promise = playSpeechTextQueue('First sentence. Second sentence.', { source: 'voice-conversation' })

    await vi.waitFor(() => expect(FakeAudio.instances).toHaveLength(1))
    await vi.waitFor(() => expect(speakText).toHaveBeenCalledTimes(2))

    expect(speakText).toHaveBeenNthCalledWith(1, 'First sentence.')
    expect(speakText).toHaveBeenNthCalledWith(2, 'Second sentence.')

    FakeAudio.instances[0].end()
    await vi.waitFor(() => expect(FakeAudio.instances).toHaveLength(2))
    FakeAudio.instances[1].end()

    await expect(promise).resolves.toBe(true)
  })
})
