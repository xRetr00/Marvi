import { logPersisted } from './perf-log'

/**
 * [CONN-PERF] instrumentation for the renderer's gateway connection path —
 * see apps/desktop/src/app/gateway/hooks/use-gateway-boot.ts (connect/
 * reconnect/soft-switch timing) and apps/desktop/src/store/profile.ts
 * (touchActiveGatewayBackend, the existing periodic keepalive "ping" reused
 * here as the round-trip probe rather than inventing a new one). Lines are
 * persisted via lib/perf-log.ts's console.error bridge — see that module's
 * doc comment for why.
 */

const PREFIX = '[CONN-PERF]'

// >1s to go from "dialing" to "ready" is the threshold called out for the
// desktop app's reported slow/frozen connection.
const SLOW_CONNECT_MS = 1000
// No explicit threshold was specified for the keepalive round-trip probe;
// reusing the same 1s bar as connect keeps the two "is the gateway path
// healthy" signals consistent.
const SLOW_PING_MS = 1000
// More than 3 reconnects within a rolling 60s window is the threshold called
// out for flagging connection instability.
const RECONNECTS_PER_MIN_WARN = 3
const RECONNECT_RATE_WINDOW_MS = 60_000

/** Log a connect attempt's start -> ready (or start -> failed) duration. `label` identifies the call site (boot/reconnect/soft-switch). */
export function logConnectDuration(label: string, durationMs: number, ok: boolean, error?: string): void {
  const rounded = Math.round(durationMs)
  const slow = durationMs > SLOW_CONNECT_MS
  const level = ok && !slow ? 'INFO' : 'WARN'
  const status = ok ? 'ok' : `failed error=${JSON.stringify(error ?? 'unknown')}`

  logPersisted(PREFIX, `connect label=${label} durationMs=${rounded} level=${level} ${status}`)
}

/** Log the start of one reconnect attempt with why it was triggered. */
export function logReconnectAttempt(attempt: number, reason: string): void {
  logPersisted(PREFIX, `reconnect-attempt n=${attempt} reason=${reason} level=INFO`)
}

// Rolling reconnect-rate tracker, module-scoped for the life of this
// renderer (a fresh boot/reload naturally resets it).
const reconnectTimestamps: number[] = []

/**
 * Record one reconnect attempt for rate tracking and, if the rolling-60s
 * count crosses the threshold, emit a WARN line naming the reason of the
 * attempt that tipped it over.
 */
export function noteReconnectRate(reason: string, nowMs: number): void {
  reconnectTimestamps.push(nowMs)

  while (reconnectTimestamps.length && nowMs - reconnectTimestamps[0] > RECONNECT_RATE_WINDOW_MS) {
    reconnectTimestamps.shift()
  }

  if (reconnectTimestamps.length > RECONNECTS_PER_MIN_WARN) {
    logPersisted(
      PREFIX,
      `reconnect-rate count=${reconnectTimestamps.length} windowMs=${RECONNECT_RATE_WINDOW_MS} reason=${reason} level=WARN`
    )
  }
}

/** Log one keepalive round-trip probe (touchActiveGatewayBackend). */
export function logPingRoundTrip(durationMs: number, ok: boolean, profile: string): void {
  const rounded = Math.round(durationMs)
  const level = ok && durationMs <= SLOW_PING_MS ? 'INFO' : 'WARN'

  logPersisted(PREFIX, `ping-rtt durationMs=${rounded} ok=${ok} profile=${profile} level=${level}`)
}
