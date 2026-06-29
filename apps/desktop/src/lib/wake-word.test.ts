import { describe, expect, it } from 'vitest'

import { normalizeWakeWordConfig, stripWakePhrase } from './wake-word'

describe('normalizeWakeWordConfig', () => {
  it('defaults to disabled sherpa wake-word mode with Marvi variants', () => {
    const config = normalizeWakeWordConfig(undefined)

    expect(config.enabled).toBe(false)
    expect(config.debug).toBe(false)
    expect(config.provider).toBe('sherpa_onnx')
    expect(config.phrases).toContain('hey marvi')
    expect(config.phrases).toContain('marvi')
    expect(config.phrases).toContain('marve')
    expect(config.phrases).toContain('marfe')
    expect(config.phrases).toContain('marfi')
  })

  it('normalizes configured phrases and timing', () => {
    const config = normalizeWakeWordConfig({
      enabled: true,
      debug: true,
      phrases: ['Hey Marvi', 'marfe', '', 'hey marvi'],
      command_timeout_ms: 9000,
      cooldown_ms: 500
    })

    expect(config.enabled).toBe(true)
    expect(config.debug).toBe(true)
    expect(config.phrases).toEqual(['hey marvi', 'marfe'])
    expect(config.commandTimeoutMs).toBe(9000)
    expect(config.cooldownMs).toBe(500)
  })
})

describe('stripWakePhrase', () => {
  const phrases = ['hey marvi', 'marvi', 'marfe', 'marfi']

  it('removes the wake phrase from the start of the command', () => {
    expect(stripWakePhrase('Hey Marvi, summarize this file', phrases)).toBe('summarize this file')
    expect(stripWakePhrase('marfi open settings', phrases)).toBe('open settings')
  })

  it('leaves text alone when the wake phrase is not at the start', () => {
    expect(stripWakePhrase('please say hey marvi back', phrases)).toBe('please say hey marvi back')
  })
})
