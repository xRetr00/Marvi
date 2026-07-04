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

  it('shows Qwen3 TTS fields only when Qwen3 is selected', () => {
    expect(voiceFieldVisible('tts.qwen3.model', cfg())).toBe(false)
    expect(voiceFieldVisible('tts.qwen3.ref_audio', cfg({ tts: { provider: 'qwen3', qwen3: {} } }))).toBe(true)
    expect(voiceFieldVisible('tts.qwen3.instruct', cfg({ tts: { provider: 'qwen3', qwen3: {} } }))).toBe(true)
  })
})
