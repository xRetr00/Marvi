import { describe, expect, it } from 'vitest'

import { deriveVoicePhase } from './voice-presence'

describe('deriveVoicePhase', () => {
  it('is off when nothing is active', () => {
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'armed' })).toBe('off')
  })

  it('lights as wake the moment the hotword is detected', () => {
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'woken' })).toBe('wake')
  })

  it('does not light for background hotword listening', () => {
    expect(deriveVoicePhase({ active: false, voiceStatus: 'idle', wakeStatus: 'listening' })).toBe('off')
  })

  it('maps an active conversation status straight through', () => {
    expect(deriveVoicePhase({ active: true, voiceStatus: 'listening', wakeStatus: 'idle' })).toBe('listening')
    expect(deriveVoicePhase({ active: true, voiceStatus: 'speaking', wakeStatus: 'idle' })).toBe('speaking')
  })

  it('is off when a conversation is active but idle between turns', () => {
    expect(deriveVoicePhase({ active: true, voiceStatus: 'idle', wakeStatus: 'idle' })).toBe('off')
  })
})
