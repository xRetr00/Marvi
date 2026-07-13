import { describe, expect, it, vi } from 'vitest'

import { createPerfMonitor, type PerfMonitorDeps } from './perf-monitor'

/** Minimal fake PerformanceObserver that captures its callback and lets tests drive it directly. */
function fakeObserverCtor() {
  let capturedCallback: ((list: { getEntries: () => unknown[] }) => void) | null = null
  const observeCalls: unknown[] = []
  let disconnected = false

  class FakeObserver {
    constructor(callback: (list: { getEntries: () => unknown[] }) => void) {
      capturedCallback = callback
    }

    observe(options: unknown) {
      observeCalls.push(options)
    }

    disconnect() {
      disconnected = true
    }
  }

  return {
    Ctor: FakeObserver as unknown as NonNullable<PerfMonitorDeps['PerformanceObserverCtor']>,
    emit: (entries: unknown[]) => capturedCallback?.({ getEntries: () => entries }),
    observeCalls,
    isDisconnected: () => disconnected
  }
}

/** Fake interval registry: tests advance time by directly invoking a given interval's handler. */
function fakeIntervals() {
  const handlers = new Map<number, { handler: () => void; ms: number }>()
  let nextId = 1

  const setIntervalFn = (handler: () => void, ms: number) => {
    const id = nextId
    nextId += 1
    handlers.set(id, { handler, ms })

    return id as unknown as ReturnType<typeof setInterval>
  }

  const clearIntervalFn = (id: ReturnType<typeof setInterval>) => {
    handlers.delete(id as unknown as number)
  }

  const fire = (ms: number) => {
    for (const { handler, ms: intervalMs } of handlers.values()) {
      if (intervalMs === ms) {
        handler()
      }
    }
  }

  return { setIntervalFn, clearIntervalFn, fire, handlers }
}

describe('createPerfMonitor', () => {
  describe('longtask observer', () => {
    it('logs each longtask entry over the 100ms threshold with its duration and attribution', () => {
      const observer = fakeObserverCtor()
      const intervals = fakeIntervals()
      const log = vi.fn()

      createPerfMonitor({
        PerformanceObserverCtor: observer.Ctor,
        setIntervalFn: intervals.setIntervalFn,
        clearIntervalFn: intervals.clearIntervalFn,
        log,
        now: () => 0
      })

      observer.emit([
        { duration: 142, attribution: [{ containerType: 'window' }] },
        { duration: 50 } // under threshold, ignored
      ])

      expect(log).toHaveBeenCalledTimes(1)
      expect(log).toHaveBeenCalledWith('[UI-PERF]', 'longtask dur=142ms attr=window')
    })

    it('falls back to "unknown" attribution when the entry has none', () => {
      const observer = fakeObserverCtor()
      const intervals = fakeIntervals()
      const log = vi.fn()

      createPerfMonitor({
        PerformanceObserverCtor: observer.Ctor,
        setIntervalFn: intervals.setIntervalFn,
        clearIntervalFn: intervals.clearIntervalFn,
        log,
        now: () => 0
      })

      observer.emit([{ duration: 120 }])

      expect(log).toHaveBeenCalledWith('[UI-PERF]', 'longtask dur=120ms attr=unknown')
    })

    it('stops logging individual entries past the chatty threshold and aggregates the rest into one burst line per window', () => {
      const observer = fakeObserverCtor()
      const intervals = fakeIntervals()
      const log = vi.fn()

      createPerfMonitor({
        PerformanceObserverCtor: observer.Ctor,
        setIntervalFn: intervals.setIntervalFn,
        clearIntervalFn: intervals.clearIntervalFn,
        log,
        now: () => 0
      })

      // 8 qualifying long tasks in one window: chatty threshold is 5.
      const entries = Array.from({ length: 8 }, (_, i) => ({ duration: 100 + i * 10 }))
      observer.emit(entries)

      // First 5 logged individually, the burst line comes at the 10s window flush.
      const individualCalls = log.mock.calls.filter(([, fields]) => (fields as string).startsWith('longtask dur='))
      expect(individualCalls).toHaveLength(5)

      intervals.fire(10_000)

      const burstCalls = log.mock.calls.filter(([, fields]) => (fields as string).startsWith('longtask-burst'))
      expect(burstCalls).toHaveLength(1)
      expect(burstCalls[0][1]).toContain('count=8')
      expect(burstCalls[0][1]).toContain('suppressed=3')
    })

    it('does not log a burst line when the window never exceeded the chatty threshold', () => {
      const observer = fakeObserverCtor()
      const intervals = fakeIntervals()
      const log = vi.fn()

      createPerfMonitor({
        PerformanceObserverCtor: observer.Ctor,
        setIntervalFn: intervals.setIntervalFn,
        clearIntervalFn: intervals.clearIntervalFn,
        log,
        now: () => 0
      })

      observer.emit([{ duration: 110 }, { duration: 130 }])
      intervals.fire(10_000)

      const burstCalls = log.mock.calls.filter(([, fields]) => (fields as string).startsWith('longtask-burst'))
      expect(burstCalls).toHaveLength(0)
    })

    it('skips wiring the observer entirely when longtask is not in supportedEntryTypes', () => {
      const observer = fakeObserverCtor()
      const intervals = fakeIntervals()
      const log = vi.fn()

      createPerfMonitor({
        PerformanceObserverCtor: observer.Ctor,
        supportedEntryTypes: ['paint', 'resource'],
        setIntervalFn: intervals.setIntervalFn,
        clearIntervalFn: intervals.clearIntervalFn,
        log,
        now: () => 0
      })

      expect(observer.observeCalls).toHaveLength(0)
    })

    it('stop() disconnects the observer and clears its flush timer', () => {
      const observer = fakeObserverCtor()
      const intervals = fakeIntervals()

      const handle = createPerfMonitor({
        PerformanceObserverCtor: observer.Ctor,
        setIntervalFn: intervals.setIntervalFn,
        clearIntervalFn: intervals.clearIntervalFn,
        log: vi.fn(),
        now: () => 0
      })

      expect(intervals.handlers.size).toBeGreaterThan(0)
      handle.stop()

      expect(observer.isDisconnected()).toBe(true)
      expect(intervals.handlers.size).toBe(0)
    })
  })

  describe('freeze probe', () => {
    it('does not log when the heartbeat fires on schedule (no drift)', () => {
      const intervals = fakeIntervals()
      const log = vi.fn()
      let clock = 0

      createPerfMonitor({
        setIntervalFn: intervals.setIntervalFn,
        clearIntervalFn: intervals.clearIntervalFn,
        log,
        now: () => clock
      })

      clock = 50
      intervals.fire(50)

      expect(log).not.toHaveBeenCalled()
    })

    it('logs a freeze line when drift exceeds 200ms', () => {
      const intervals = fakeIntervals()
      const log = vi.fn()
      let clock = 0

      createPerfMonitor({
        setIntervalFn: intervals.setIntervalFn,
        clearIntervalFn: intervals.clearIntervalFn,
        log,
        now: () => clock
      })

      // Heartbeat scheduled every 50ms, but the tick doesn't actually fire
      // until 400ms later — a 350ms drift, comfortably past the 200ms bar.
      clock = 400
      intervals.fire(50)

      expect(log).toHaveBeenCalledWith('[UI-PERF]', 'freeze drift=350ms')
    })

    it('throttles to at most one freeze line per second even under sustained lag', () => {
      const intervals = fakeIntervals()
      const log = vi.fn()
      let clock = 0

      createPerfMonitor({
        setIntervalFn: intervals.setIntervalFn,
        clearIntervalFn: intervals.clearIntervalFn,
        log,
        now: () => clock
      })

      clock = 400
      intervals.fire(50) // first freeze: logged
      clock = 700
      intervals.fire(50) // 300ms later, still within the 1s throttle window: suppressed
      clock = 1500
      intervals.fire(50) // past the 1s window: logged again

      const freezeCalls = log.mock.calls.filter(([, fields]) => (fields as string).startsWith('freeze'))
      expect(freezeCalls).toHaveLength(2)
    })
  })
})
