import { describe, expect, it } from 'vitest'

import { voiceModePresentation } from './voice-mode-overlay'

describe('voiceModePresentation', () => {
  it('keeps every voice phase legible in the full-screen overlay', () => {
    expect(voiceModePresentation('listening').label).toBe('Listening')
    expect(voiceModePresentation('thinking').label).toBe('Thinking deeper')
    expect(voiceModePresentation('speaking').label).toBe('Speaking')
  })
})
