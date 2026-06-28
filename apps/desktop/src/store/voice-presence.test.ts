import { afterEach, describe, expect, it } from 'vitest'

import { $voiceState, deriveVoicePhase, publishVoiceState } from './voice-presence'

describe('deriveVoicePhase', () => {
  it('is off when nothing is active', () => {
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'armed' })).toBe('off')
  })

  it('lights as wake the moment the hotword is detected', () => {
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'woken' })).toBe('wake')
  })

  it('does not light while only armed for the hotword', () => {
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'armed' })).toBe('off')
  })

  it('keeps the glow lit through the post-hotword capture states', () => {
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'woken' })).toBe('wake')
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'listening' })).toBe('wake')
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'transcribing' })).toBe('wake')
  })

  it('maps an active conversation status straight through', () => {
    expect(deriveVoicePhase({ active: true, voiceStatus: 'listening', wakeStatus: 'idle' })).toBe('listening')
    expect(deriveVoicePhase({ active: true, voiceStatus: 'speaking', wakeStatus: 'idle' })).toBe('speaking')
  })

  it('is off when a conversation is active but idle between turns', () => {
    expect(deriveVoicePhase({ active: true, voiceStatus: 'idle', wakeStatus: 'idle' })).toBe('off')
  })
})

describe('publishVoiceState', () => {
  afterEach(() => $voiceState.set({ phase: 'off', level: 0, muted: false }))

  it('updates the atom', () => {
    const next = { phase: 'listening', level: 0.5, muted: false } as const
    publishVoiceState(next)
    expect($voiceState.get()).toEqual(next)
  })
})
