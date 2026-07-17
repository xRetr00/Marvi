import { afterEach, describe, expect, it } from 'vitest'

import { $islandActivity } from './island-activity'
import { $activeSessionId, $busy } from './session'
import { $todosBySession } from './todos'
import { currentIslandWork, shouldShowVoiceIsland } from './voice-island'
import { $islandPosition, setIslandPosition } from './voice-presence-settings'

afterEach(() => {
  $activeSessionId.set(null)
  $busy.set(false)
  $islandActivity.set(null)
  $todosBySession.set({})
})

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

describe('currentIslandWork', () => {
  it('summarizes observable task state without inventing reasoning text', () => {
    $activeSessionId.set('s1')
    $busy.set(true)
    $islandActivity.set('Reading the requested file')
    $todosBySession.set({
      s1: [
        { content: 'Inspect the card reference', id: 'a', status: 'completed' },
        { content: 'Wire the island surface', id: 'b', status: 'in_progress' },
        { content: 'Verify the renderer', id: 'c', status: 'pending' }
      ]
    })

    expect(currentIslandWork()).toEqual({
      active: true,
      items: [
        { id: 'current-activity', meta: 'tool', state: 'running', title: 'Reading the requested file' },
        { id: 'todo:a', meta: 'completed', state: 'done', title: 'Inspect the card reference' },
        { id: 'todo:b', meta: 'in_progress', state: 'running', title: 'Wire the island surface' },
        { id: 'todo:c', meta: 'pending', state: 'pending', title: 'Verify the renderer' }
      ],
      title: 'Working through the plan'
    })
  })
})
