import { describe, expect, it } from 'vitest'

import { voiceOrbPalette } from './voice-orb'

describe('voiceOrbPalette', () => {
  it('changes with the voice state', () => {
    expect(voiceOrbPalette('listening')).not.toEqual(voiceOrbPalette('thinking'))
    expect(voiceOrbPalette('thinking')).not.toEqual(voiceOrbPalette('speaking'))
  })
})
