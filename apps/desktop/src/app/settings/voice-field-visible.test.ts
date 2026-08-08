import { describe, expect, it } from 'vitest'

import type { HermesConfigRecord } from '@/types/hermes'

import { voiceFieldVisible } from './config-settings'

const cfg = (over: Record<string, unknown> = {}): HermesConfigRecord =>
  ({
    tts: { provider: 'edge', edge: {}, openai: {} },
    stt: { enabled: true, provider: 'local', local: {}, groq: {} },
    ...over
  }) as unknown as HermesConfigRecord

describe('voiceFieldVisible', () => {
  it('always shows top-level + non-provider keys', () => {
    const config = cfg()

    for (const key of [
      'tts.provider',
      'stt.enabled',
      'stt.provider',
      'stt.streaming.provider',
      'voice.auto_tts',
      'voice.barge_in',
      'voice.record_key',
      'voice.semantic_turn'
    ]) {
      expect(voiceFieldVisible(key, config)).toBe(true)
    }
  })

  it('shows only the selected TTS provider sub-fields', () => {
    const config = cfg()
    expect(voiceFieldVisible('tts.edge.voice', config)).toBe(true)
    expect(voiceFieldVisible('tts.openai.voice', config)).toBe(false)
    expect(voiceFieldVisible('tts.elevenlabs.voice_id', config)).toBe(false)
  })

  it('shows only the selected STT provider sub-fields', () => {
    const config = cfg()
    expect(voiceFieldVisible('stt.local.model', config)).toBe(true)
    expect(voiceFieldVisible('stt.groq.model', config)).toBe(false)
  })

  it('hides every STT provider sub-field when STT is disabled', () => {
    const config = cfg({ stt: { enabled: false, provider: 'local', local: {} } })
    expect(voiceFieldVisible('stt.local.model', config)).toBe(false)
    // ...but the enable/provider toggles themselves stay visible.
    expect(voiceFieldVisible('stt.enabled', config)).toBe(true)
    expect(voiceFieldVisible('stt.provider', config)).toBe(true)
  })

  it('tracks a provider switch', () => {
    expect(voiceFieldVisible('tts.openai.voice', cfg({ tts: { provider: 'openai', openai: {} } }))).toBe(true)
    expect(voiceFieldVisible('tts.edge.voice', cfg({ tts: { provider: 'openai', openai: {} } }))).toBe(false)
  })

  it('shows PocketTTS fields only when PocketTTS is selected', () => {
    expect(voiceFieldVisible('tts.pockettts.voice', cfg())).toBe(false)
    expect(voiceFieldVisible('tts.pockettts.voice', cfg({ tts: { provider: 'pockettts', pockettts: {} } }))).toBe(true)
    expect(voiceFieldVisible('tts.pockettts.device', cfg({ tts: { provider: 'pockettts', pockettts: {} } }))).toBe(true)
  })

  it('hides legacy streaming sidecar fields when Parakeet is selected', () => {
    const config = cfg({ stt: { enabled: true, provider: 'local', streaming: { provider: 'parakeet' } } })

    expect(voiceFieldVisible('stt.streaming.provider', config)).toBe(true)
    expect(voiceFieldVisible('stt.streaming.parakeet.model', config)).toBe(true)
    expect(voiceFieldVisible('stt.streaming.parakeet.engine', config)).toBe(true)
    expect(voiceFieldVisible('stt.streaming.parakeet.device', config)).toBe(true)
    expect(voiceFieldVisible('stt.streaming.eou_token', config)).toBe(true)
    expect(voiceFieldVisible('stt.streaming.model', config)).toBe(false)
    expect(voiceFieldVisible('stt.streaming.backend', config)).toBe(false)
    expect(voiceFieldVisible('stt.streaming.host', config)).toBe(false)
    expect(voiceFieldVisible('stt.streaming.port', config)).toBe(false)
    expect(voiceFieldVisible('stt.streaming.max_clients', config)).toBe(false)
  })

  it('shows only Moonshine streaming settings when Moonshine is selected', () => {
    const config = cfg({ stt: { enabled: true, provider: 'local', streaming: { provider: 'moonshine' } } })

    expect(voiceFieldVisible('stt.streaming.moonshine.language', config)).toBe(true)
    expect(voiceFieldVisible('stt.streaming.moonshine.model', config)).toBe(true)
    expect(voiceFieldVisible('stt.streaming.model', config)).toBe(false)
    expect(voiceFieldVisible('stt.streaming.eou_token', config)).toBe(false)
  })
})
