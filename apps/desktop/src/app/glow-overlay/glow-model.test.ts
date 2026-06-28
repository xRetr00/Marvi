import { describe, expect, it } from 'vitest'

import { glowSpeedMs, targetAmplitude } from './glow-model'

describe('targetAmplitude', () => {
  it('is zero when off', () => {
    expect(targetAmplitude('off', 0)).toBe(0)
  })

  it('flares to full on wake', () => {
    expect(targetAmplitude('wake', 0)).toBe(1)
  })

  it('rises with mic level while listening and stays within 0..1', () => {
    expect(targetAmplitude('listening', 0)).toBeCloseTo(0.4)
    expect(targetAmplitude('listening', 1)).toBe(1)
  })

  it('has a steady baseline while thinking', () => {
    expect(targetAmplitude('thinking', 0)).toBeCloseTo(0.5)
  })
})

describe('glowSpeedMs', () => {
  it('flows fast when listening or speaking and slow when idle/thinking', () => {
    expect(glowSpeedMs('listening')).toBeLessThan(glowSpeedMs('thinking'))
    expect(glowSpeedMs('thinking')).toBeLessThan(glowSpeedMs('off'))
  })
})
