import { describe, expect, it } from 'vitest'

import { voiceModeCaption, voiceModePresentation } from './voice-mode-stage'

describe('voiceModePresentation', () => {
  it('keeps every voice phase legible in the in-layout stage', () => {
    expect(voiceModePresentation('listening').label).toBe('Listening')
    expect(voiceModePresentation('thinking').label).toBe('Thinking it through')
    expect(voiceModePresentation('speaking').label).toBe('Speaking')
  })
})

describe('voiceModeCaption', () => {
  it('shows streaming assistant text while the instant lane replies', () => {
    expect(voiceModeCaption({ caption: 'Working on it', phase: 'thinking', userCaption: 'my request' })).toBe(
      'Working on it'
    )
  })
})
