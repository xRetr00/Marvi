import { describe, expect, it } from 'vitest'

import { createBargeInGate } from './voice-barge-in'

describe('createBargeInGate', () => {
  it('requires sustained speech after the playback grace window', () => {
    const gate = createBargeInGate({ graceMs: 700, level: 0.3, sustainedMs: 350 })

    expect(gate.update(0.8, 500)).toBe(false)
    expect(gate.update(0.8, 800)).toBe(false)
    expect(gate.update(0.8, 1149)).toBe(false)
    expect(gate.update(0.8, 1150)).toBe(true)
  })

  it('resets when speech confidence drops', () => {
    const gate = createBargeInGate({ graceMs: 0, level: 0.3, sustainedMs: 350 })

    expect(gate.update(0.8, 0)).toBe(false)
    expect(gate.update(0.1, 200)).toBe(false)
    expect(gate.update(0.8, 300)).toBe(false)
    expect(gate.update(0.8, 649)).toBe(false)
    expect(gate.update(0.8, 650)).toBe(true)
  })
})
