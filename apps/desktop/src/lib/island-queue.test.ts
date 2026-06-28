import { describe, expect, it, vi } from 'vitest'

import { createIslandQueue } from './island-queue'

describe('createIslandQueue', () => {
  it('shows the first card as active and queues the rest', () => {
    const q = createIslandQueue()
    expect(q.show({ id: 'a', kind: 'info' })).toBe('a')
    q.show({ id: 'b', kind: 'info' })
    const snap = q.snapshot()
    expect(snap.active?.id).toBe('a')
    expect(snap.queued.map(c => c.id)).toEqual(['b'])
  })

  it('promotes the next card on dismiss', () => {
    const q = createIslandQueue()
    q.show({ id: 'a', kind: 'info' })
    q.show({ id: 'b', kind: 'info' })
    q.dismiss('a')
    expect(q.snapshot().active?.id).toBe('b')
  })

  it('force replaces the active card immediately', () => {
    const q = createIslandQueue()
    q.show({ id: 'a', kind: 'info' })
    q.show({ id: 'urgent', kind: 'approval' }, { force: true })
    expect(q.snapshot().active?.id).toBe('urgent')
  })

  it('auto-dismisses after the duration', () => {
    vi.useFakeTimers()
    const q = createIslandQueue()
    q.show({ id: 'a', kind: 'info', duration: 1000, autoDismiss: true })
    expect(q.snapshot().active?.id).toBe('a')
    vi.advanceTimersByTime(1001)
    expect(q.snapshot().active).toBeNull()
    vi.useRealTimers()
  })

  it('trims the queue to maxQueue', () => {
    const q = createIslandQueue({ maxQueue: 1 })
    q.show({ id: 'a', kind: 'info' })
    q.show({ id: 'b', kind: 'info' })
    q.show({ id: 'c', kind: 'info' })
    expect(q.snapshot().queued.map(c => c.id)).toEqual(['c'])
  })
})
