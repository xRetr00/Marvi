/**
 * Shared plumbing for the renderer's lightweight performance / connection log
 * lines ([UI-PERF] from app/perf-monitor.ts, [CONN-PERF] from the gateway
 * boot hooks + profile keepalive ping). Both funnel through here so there's
 * one place documenting how a renderer line ends up in desktop.log.
 *
 * There is no dedicated renderer -> desktop.log IPC channel for arbitrary log
 * lines. The only two paths a renderer has into the main process's log
 * plumbing today are:
 *
 *  1. `hermesDesktop.logVoicePresence` (electron/preload.ts) -> writes into
 *     the SEPARATE logs/voice-presence.log file, not desktop.log.
 *  2. `mainWindow.webContents.on('console-message', ...)` in
 *     electron/main.ts, which mirrors renderer console messages into
 *     desktop.log via `rememberLog` (prefixed `[renderer console] `) — but
 *     ONLY level-3/error messages (`if (level !== 3) return`); console.warn/
 *     info/log never reach it.
 *
 * This task's ownership is scoped to apps/desktop/src/** — electron/main.ts
 * and electron/preload.ts are explicitly off limits, so a purpose-built
 * `hermesDesktop.logPerf(...)` channel mirroring logVoicePresence's pattern
 * straight into desktop.log (the "correct" long-term fix) isn't done here;
 * it's flagged as a handoff instead. Until that lands, every line this
 * module emits goes through `console.error` so it actually persists to
 * desktop.log; real severity is carried as a `level=INFO|WARN` token inside
 * the message text rather than the console method, since only the error
 * level survives the mirror.
 */

const SESSION_START =
  typeof performance !== 'undefined' && typeof performance.now === 'function' ? performance.now() : Date.now()

function now(): number {
  return typeof performance !== 'undefined' && typeof performance.now === 'function' ? performance.now() : Date.now()
}

/** Session-relative seconds since this renderer loaded, e.g. "+12.4s". */
export function sessionRelativeTimestamp(): string {
  return `+${((now() - SESSION_START) / 1000).toFixed(1)}s`
}

/**
 * Best-effort current view/route. Cheap: reads the HashRouter hash straight
 * off `window.location` rather than going through React Router context, so
 * it's safe to call from plain modules with no component tree available.
 */
export function currentViewLabel(): string {
  try {
    const hash = window.location.hash.replace(/^#\/?/, '/')

    return hash || '/'
  } catch {
    return '?'
  }
}

/**
 * Emit one grep-friendly, single-line perf/connection log entry of the form
 * `<prefix> <fields> view=<route> t=<+Ns>`. Routed through console.error so
 * it persists into desktop.log via the existing renderer-console mirror (see
 * module doc). Never throws — logging must never take down the caller.
 */
export function logPersisted(prefix: string, fields: string): void {
  try {
    // Deliberate use of console.error, not a stray debug log — this IS the
    // desktop.log bridge, see module doc above.
    console.error(`${prefix} ${fields} view=${currentViewLabel()} t=${sessionRelativeTimestamp()}`)
  } catch {
    // never let logging throw
  }
}
