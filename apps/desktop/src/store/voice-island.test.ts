import { describe, expect, it } from 'vitest'

import { shouldShowVoiceIsland } from './voice-island'

describe('shouldShowVoiceIsland', () => {
  it('shows explicit voice mode even when background presence is off', () => {
    expect(shouldShowVoiceIsland(true, false, 'listening')).toBe(true)
    expect(shouldShowVoiceIsland(true, false, 'off')).toBe(false)
  })
})
