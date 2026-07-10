import { describe, expect, it } from 'vitest'

import { mapKnowledgeEntries } from './use-marvi-knowledge'

describe('mapKnowledgeEntries', () => {
  it('maps API rows onto the KnowledgeEntry viewer shape', () => {
    const mapped = mapKnowledgeEntries([
      { id: 'USER.md:0', text: 'Prefers dark mode', source: 'presence', timestamp: '2026-07-09T00:00:00+00:00' },
      { id: 'MEMORY.md:0', text: 'Project uses pytest', source: 'subconscious', timestamp: '2026-07-08T00:00:00+00:00' }
    ])

    expect(mapped).toEqual([
      { id: 'USER.md:0', summary: 'Prefers dark mode', source: 'presence', createdAt: '2026-07-09T00:00:00+00:00' },
      { id: 'MEMORY.md:0', summary: 'Project uses pytest', source: 'subconscious', createdAt: '2026-07-08T00:00:00+00:00' }
    ])
  })

  it('coerces unknown sources to subconscious and null timestamps to the epoch', () => {
    const [entry] = mapKnowledgeEntries([{ id: 'MEMORY.md:1', text: 'x', source: 'mystery', timestamp: null }])

    expect(entry.source).toBe('subconscious')
    expect(entry.createdAt).toBe(new Date(0).toISOString())
  })

  it('drops malformed rows without id or text', () => {
    const mapped = mapKnowledgeEntries([
      { id: '', text: 'no id', source: 'presence', timestamp: null },
      { id: 'MEMORY.md:2', text: '', source: 'presence', timestamp: null },
      { id: 'MEMORY.md:3', text: 'kept', source: 'presence', timestamp: null }
    ])

    expect(mapped).toHaveLength(1)
    expect(mapped[0].summary).toBe('kept')
  })
})
