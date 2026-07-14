import { describe, expect, it } from 'vitest'

import { shouldShowVoiceIsland } from './voice-island'
import { $islandPosition, setIslandPosition } from './voice-presence-settings'

describe('shouldShowVoiceIsland', () => {
  it('shows explicit voice mode even when background presence is off', () => {
    expect(shouldShowVoiceIsland(true, false, 'listening')).toBe(true)
    expect(shouldShowVoiceIsland(true, false, 'off')).toBe(false)
  })
})

describe('island position', () => {
  it('persists a left, center, or right dock choice', () => {
    const previous = $islandPosition.get()

    setIslandPosition('right')

    expect($islandPosition.get()).toBe('right')
    expect(window.localStorage.getItem('hermes.desktop.voice-presence.island-position.v1')).toBe('right')
    setIslandPosition(previous)
  })
})
