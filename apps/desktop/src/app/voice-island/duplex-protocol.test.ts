import { describe, expect, it } from 'vitest'

import { parseDuplexServerEvent } from './duplex-protocol'

describe('parseDuplexServerEvent', () => {
  it('parses every documented event type', () => {
    expect(parseDuplexServerEvent({ type: 'ready' })).toEqual({ type: 'ready' })
    expect(parseDuplexServerEvent({ type: 'partial', text: 'hi' })).toEqual({ type: 'partial', text: 'hi' })
    expect(parseDuplexServerEvent({ type: 'partial', text: 'hi', eou_prob: 0.9 })).toEqual({
      type: 'partial',
      text: 'hi',
      eou_prob: 0.9
    })
    expect(parseDuplexServerEvent({ type: 'utterance', text: 'hi', speaker: 'guest' })).toEqual({
      type: 'utterance',
      text: 'hi',
      speaker: 'guest'
    })
    expect(parseDuplexServerEvent({ type: 'instant_delta', text: 'a' })).toEqual({ type: 'instant_delta', text: 'a' })
    expect(parseDuplexServerEvent({ type: 'instant_done', text: 'a' })).toEqual({ type: 'instant_done', text: 'a' })
    expect(parseDuplexServerEvent({ type: 'tts_start' })).toEqual({ type: 'tts_start' })
    expect(parseDuplexServerEvent({ type: 'tts_chunk', data: 'AA', seq: 1 })).toEqual({
      type: 'tts_chunk',
      data: 'AA',
      seq: 1
    })
    expect(parseDuplexServerEvent({ type: 'tts_end' })).toEqual({ type: 'tts_end' })
    expect(parseDuplexServerEvent({ type: 'barge_in' })).toEqual({ type: 'barge_in' })
    expect(parseDuplexServerEvent({ type: 'escalated', task_id: 't1', ack_text: 'ack' })).toEqual({
      type: 'escalated',
      task_id: 't1',
      ack_text: 'ack',
      mode: 'thinking'
    })
    expect(parseDuplexServerEvent({ type: 'activity', status: 'started', kind: 'web', label: 'Searching' })).toEqual({
      type: 'activity',
      status: 'started',
      kind: 'web',
      label: 'Searching'
    })
    expect(parseDuplexServerEvent({ type: 'deep_result', task_id: 't1', text: 'result' })).toEqual({
      type: 'deep_result',
      task_id: 't1',
      text: 'result'
    })
    expect(parseDuplexServerEvent({ type: 'error', error: 'boom' })).toEqual({ type: 'error', error: 'boom' })
  })

  it('normalizes an unrecognized speaker on utterance to unknown', () => {
    expect(parseDuplexServerEvent({ type: 'utterance', text: 'hi', speaker: 'martian' })).toEqual({
      type: 'utterance',
      text: 'hi',
      speaker: 'unknown'
    })
    expect(parseDuplexServerEvent({ type: 'utterance', text: 'hi' })).toEqual({
      type: 'utterance',
      text: 'hi',
      speaker: 'unknown'
    })
  })

  it('falls back to a generic message when error.error is missing/non-string', () => {
    expect(parseDuplexServerEvent({ type: 'error' })).toEqual({ type: 'error', error: 'Unknown duplex error' })
    expect(parseDuplexServerEvent({ type: 'error', error: 42 })).toEqual({
      type: 'error',
      error: 'Unknown duplex error'
    })
  })

  it('returns null for non-object, missing-type, and unknown-type payloads', () => {
    expect(parseDuplexServerEvent(null)).toBeNull()
    expect(parseDuplexServerEvent(undefined)).toBeNull()
    expect(parseDuplexServerEvent('ready')).toBeNull()
    expect(parseDuplexServerEvent(42)).toBeNull()
    expect(parseDuplexServerEvent([])).toBeNull()
    expect(parseDuplexServerEvent({})).toBeNull()
    expect(parseDuplexServerEvent({ type: 42 })).toBeNull()
    expect(parseDuplexServerEvent({ type: 'not_a_real_event' })).toBeNull()
  })

  it('returns null when required fields are missing or the wrong type', () => {
    expect(parseDuplexServerEvent({ type: 'partial' })).toBeNull()
    expect(parseDuplexServerEvent({ type: 'partial', text: 5 })).toBeNull()
    expect(parseDuplexServerEvent({ type: 'utterance' })).toBeNull()
    expect(parseDuplexServerEvent({ type: 'instant_delta' })).toBeNull()
    expect(parseDuplexServerEvent({ type: 'instant_done' })).toBeNull()
    expect(parseDuplexServerEvent({ type: 'tts_chunk', data: 'AA' })).toBeNull()
    expect(parseDuplexServerEvent({ type: 'tts_chunk', seq: 1 })).toBeNull()
    expect(parseDuplexServerEvent({ type: 'tts_chunk', data: 5, seq: 1 })).toBeNull()
    expect(parseDuplexServerEvent({ type: 'escalated', task_id: 't1' })).toBeNull()
    expect(parseDuplexServerEvent({ type: 'escalated', ack_text: 'ack' })).toBeNull()
    expect(parseDuplexServerEvent({ type: 'deep_result', task_id: 't1' })).toBeNull()
    expect(parseDuplexServerEvent({ type: 'deep_result', text: 'x' })).toBeNull()
  })
})
