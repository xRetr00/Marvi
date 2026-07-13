import { logPersisted } from '@/lib/perf-log'

/**
 * Renderer performance instrumentation. Started once from src/main.tsx (the
 * primary window branch only — the pet-overlay/island windows are
 * deliberately cheap and skip this). Two independent probes, both funneling
 * [UI-PERF] lines into desktop.log via lib/perf-log.ts's console.error
 * bridge:
 *
 *  (a) A PerformanceObserver on `longtask` entries >100ms. These are tasks
 *      that block the main thread long enough to be visibly janky (dropped
 *      frames, unresponsive clicks/keys). A slow React commit IS a long
 *      task, so this covers commit-phase stalls for free — no separate
 *      Profiler wiring is added.
 *  (b) A setInterval heartbeat that measures its own scheduling drift.
 *      Event-loop lag beyond 200ms means the renderer was frozen/starved for
 *      that long regardless of whether it shows up as a `longtask` entry
 *      (e.g. a GC pause or native call with no JS task boundary to
 *      attribute).
 *
 * Both probes are built by `createPerfMonitor()` against injectable deps so
 * they're unit-testable without a real PerformanceObserver/timers (see
 * perf-monitor.test.ts) — `startPerfMonitor()` just wires that to the real
 * globals and guards against being started twice.
 */

const LONGTASK_THRESHOLD_MS = 100
const FREEZE_DRIFT_THRESHOLD_MS = 200
const HEARTBEAT_INTERVAL_MS = 50
const FREEZE_LOG_MIN_INTERVAL_MS = 1000
// Once a rolling window sees more than this many qualifying long tasks, stop
// logging them one-by-one (a janky drag/scroll/paste can throw dozens) and
// fold the rest into a single aggregate line for that window instead.
const LONGTASK_WINDOW_MS = 10_000
const LONGTASK_CHATTY_THRESHOLD = 5

interface LongtaskEntryLike {
  duration: number
  attribution?: Array<{ containerType?: string; containerName?: string }>
}

interface LongtaskObserverEntryList {
  getEntries: () => LongtaskEntryLike[]
}

interface PerformanceObserverLike {
  observe: (options: { type: string; buffered?: boolean }) => void
  disconnect: () => void
}

export interface PerfMonitorDeps {
  now?: () => number
  setIntervalFn?: (handler: () => void, ms: number) => ReturnType<typeof setInterval>
  clearIntervalFn?: (id: ReturnType<typeof setInterval>) => void
  /** Test seam for PerformanceObserver. Defaults to the real global when available. */
  PerformanceObserverCtor?: new (callback: (list: LongtaskObserverEntryList) => void) => PerformanceObserverLike
  /** Test seam for PerformanceObserver.supportedEntryTypes. */
  supportedEntryTypes?: string[]
  /** Test seam for the log sink. Defaults to lib/perf-log.ts's logPersisted. */
  log?: (prefix: string, fields: string) => void
}

export interface PerfMonitorHandle {
  stop: () => void
}

function describeAttribution(entry: LongtaskEntryLike): string {
  const first = entry.attribution?.[0]

  if (!first?.containerType) {
    return 'unknown'
  }

  return first.containerName ? `${first.containerType}:${first.containerName}` : first.containerType
}

/**
 * Build the two probes against `deps` (falling back to real globals). Kept
 * separate from `startPerfMonitor()` below so tests can exercise the logic
 * with fake timers/observers/log sink without a singleton guard in the way.
 */
export function createPerfMonitor(deps: PerfMonitorDeps = {}): PerfMonitorHandle {
  const now = deps.now ?? (() => performance.now())
  const setIntervalFn = deps.setIntervalFn ?? ((handler, ms) => setInterval(handler, ms))
  const clearIntervalFn = deps.clearIntervalFn ?? (id => clearInterval(id))
  const log = deps.log ?? logPersisted
  const timers: Array<ReturnType<typeof setInterval>> = []

  // --- (a) longtask observer ---------------------------------------------
  const usingRealGlobal = !deps.PerformanceObserverCtor

  const ObserverCtor =
    deps.PerformanceObserverCtor ??
    ((typeof PerformanceObserver !== 'undefined' ? PerformanceObserver : undefined) as
      | PerfMonitorDeps['PerformanceObserverCtor']
      | undefined)

  // supportedEntryTypes is a static property of the REAL PerformanceObserver
  // class, so it only makes sense to consult when we're actually using that
  // global (not an injected fake) — otherwise fall back to "assume
  // supported" unless the caller explicitly overrides it via deps.
  const supported =
    deps.supportedEntryTypes ??
    (usingRealGlobal && typeof PerformanceObserver !== 'undefined' ? PerformanceObserver.supportedEntryTypes : undefined)

  let longtaskObserver: PerformanceObserverLike | undefined
  let windowCount = 0
  let windowTotalMs = 0
  let windowMaxMs = 0

  const flushLongtaskWindow = () => {
    if (windowCount > LONGTASK_CHATTY_THRESHOLD) {
      const suppressed = windowCount - LONGTASK_CHATTY_THRESHOLD

      log(
        '[UI-PERF]',
        `longtask-burst count=${windowCount} suppressed=${suppressed} totalMs=${Math.round(windowTotalMs)} ` +
          `maxMs=${Math.round(windowMaxMs)} avgMs=${Math.round(windowTotalMs / windowCount)}`
      )
    }

    windowCount = 0
    windowTotalMs = 0
    windowMaxMs = 0
  }

  if (ObserverCtor && (!supported || supported.includes('longtask'))) {
    try {
      const observer = new ObserverCtor(list => {
        for (const entry of list.getEntries()) {
          if (entry.duration < LONGTASK_THRESHOLD_MS) {
            continue
          }

          windowCount += 1
          windowTotalMs += entry.duration
          windowMaxMs = Math.max(windowMaxMs, entry.duration)

          // Log individually up to the chatty threshold; beyond that this
          // window's tasks are folded into flushLongtaskWindow()'s summary.
          if (windowCount <= LONGTASK_CHATTY_THRESHOLD) {
            log('[UI-PERF]', `longtask dur=${Math.round(entry.duration)}ms attr=${describeAttribution(entry)}`)
          }
        }
      })

      observer.observe({ type: 'longtask', buffered: true })
      longtaskObserver = observer
      timers.push(setIntervalFn(flushLongtaskWindow, LONGTASK_WINDOW_MS))
    } catch {
      // longtask observation isn't available in this Chromium build — skip silently.
    }
  }

  // --- (b) event-loop lag / freeze probe ----------------------------------
  let lastBeat = now()
  // -Infinity (not 0) so the very first qualifying freeze always logs even
  // when `now()` itself starts near zero (e.g. under fake timers in tests).
  let lastFreezeLoggedAt = -Infinity

  timers.push(
    setIntervalFn(() => {
      const at = now()
      const drift = at - lastBeat - HEARTBEAT_INTERVAL_MS
      lastBeat = at

      if (drift <= FREEZE_DRIFT_THRESHOLD_MS) {
        return
      }

      // Throttle: at most one freeze line per second even under sustained lag.
      if (at - lastFreezeLoggedAt < FREEZE_LOG_MIN_INTERVAL_MS) {
        return
      }

      lastFreezeLoggedAt = at
      log('[UI-PERF]', `freeze drift=${Math.round(drift)}ms`)
    }, HEARTBEAT_INTERVAL_MS)
  )

  return {
    stop: () => {
      timers.forEach(id => clearIntervalFn(id))
      longtaskObserver?.disconnect()
    }
  }
}

let singleton: PerfMonitorHandle | null = null

/**
 * Idempotent: call once from the app root (src/main.tsx, primary window
 * only). No-op outside a browser/renderer context (SSR/tests importing this
 * module incidentally) or if already started.
 */
export function startPerfMonitor(): void {
  if (singleton || typeof window === 'undefined' || typeof performance === 'undefined') {
    return
  }

  singleton = createPerfMonitor()
}
