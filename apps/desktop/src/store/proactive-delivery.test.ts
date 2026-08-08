import { describe, expect, it } from 'vitest'

import {
  isEnglishTtsText,
  proactiveDeliveryAction,
  proactiveMessage,
  smartRoomGestureCommand,
  unseenProactiveRuns
} from './proactive-delivery'

describe('proactive delivery cursor', () => {
  const runs = [
    { at: '3', job_id: 'j', source: 'tick', outcome: 'message', thought: 'Newest' },
    { at: '2', job_id: 'j', source: 'tick', outcome: 'diff_silent', thought: '[SILENT]' },
    { at: '1', job_id: 'j', source: 'tick', outcome: 'message', thought: 'Already seen' }
  ]

  it('delivers only new user-facing messages in chronological order', () => {
    expect(unseenProactiveRuns(runs, '1:j:tick').map(proactiveMessage)).toEqual(['Newest'])
  })

  it('does not replay history before the first cursor is established', () => {
    expect(unseenProactiveRuns(runs, '')).toEqual([])
  })

  it('prefers the full thought over the capped activity summary', () => {
    expect(proactiveMessage({ thought: 'Full proactive answer', summary: 'Preview' })).toBe('Full proactive answer')
  })

  it('holds normal notices during protected activity but still surfaces urgent ones quietly', () => {
    const delivery = { mode: 'defer' as const, urgent_mode: 'quiet' as const }
    expect(proactiveDeliveryAction(delivery, 'normal')).toBe('defer')
    expect(proactiveDeliveryAction(delivery, 'urgent')).toBe('quiet')
  })

  it('never sends unsupported-language text to the English-only TTS lane', () => {
    expect(isEnglishTtsText('The room sensor briefly went offline.')).toBe(true)
    expect(isEnglishTtsText('Isıtma cihazı çevrimdışı oldu.')).toBe(false)
  })

  it('routes only explicit Smart Room voice gestures', () => {
    expect(smartRoomGestureCommand({ source: 'smart_room_gesture', thought: '__gesture__:voice_start' })).toBe('voice_start')
    expect(smartRoomGestureCommand({ source: 'smart_room_gesture', thought: '__gesture__:cancel' })).toBe('cancel')
    expect(smartRoomGestureCommand({ source: 'tick', thought: '__gesture__:voice_start' })).toBeNull()
  })
})
