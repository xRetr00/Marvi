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

  it('does not fire on sustained energy that is unconfirmed (speaker echo)', () => {
    const gate = createBargeInGate({ graceMs: 0, level: 0.3, sustainedMs: 350 })

    // Loud and sustained, but confirmed=false (believed to be Marvi's echo).
    expect(gate.update(0.8, 0, false)).toBe(false)
    expect(gate.update(0.8, 400, false)).toBe(false)
    expect(gate.state).toBe('rising')

    // Same energy, now confirmed as real user speech -> fires.
    expect(gate.update(0.8, 800, true)).toBe(true)
    expect(gate.state).toBe('triggered')
  })

  it('exposes state transitions for logging', () => {
    const gate = createBargeInGate({ graceMs: 100, level: 0.3, sustainedMs: 200 })

    gate.update(0.8, 50)
    expect(gate.state).toBe('idle') // still in grace window

    gate.update(0.8, 150)
    expect(gate.state).toBe('rising')

    gate.update(0.1, 200)
    expect(gate.state).toBe('idle') // energy dropped
  })
})
