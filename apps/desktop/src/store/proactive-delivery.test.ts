import { describe, expect, it } from 'vitest'

import { proactiveMessage, unseenProactiveRuns } from './proactive-delivery'

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
})
