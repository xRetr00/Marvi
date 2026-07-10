// REST calls for the Marvi subconscious/presence activation endpoints
// (hermes_cli/web_server.py). Flipping `subconscious.enabled` /
// `presence.enabled` via a raw PUT /api/config does nothing by itself —
// the tick cron job is only created by cron.subconscious.enable() and the
// presence stack (media watcher, distiller job) only by presence_cmd's
// setup/pause functions — so the toggles must go through these endpoints,
// which flip the config keys themselves as part of activation.
//
// Uses the same authenticated desktop transport (`window.hermesDesktop.api`,
// routed through Electron main with the dashboard session token) as
// src/hermes.ts; the functions live here rather than there because they are
// private to this settings surface.

export interface SubconsciousStatus {
  ok: boolean
  enabled: boolean
  interval: string
  idle_trigger_minutes: number
  tiers: Record<string, string>
  job_id: null | string
  job_state: null | string
  last_run_at: null | number
  next_run_at: null | number
}

export interface PresenceActionResult {
  ok: boolean
  enabled: boolean
  message?: string
}

export interface PresenceSetupResult {
  ok: boolean
  enabled: boolean
  activitywatch_available: boolean
  watcher_ok: boolean
  watcher_message: string
  job_ok: boolean
  job_message: string
}

export function enableSubconscious(interval?: string): Promise<SubconsciousStatus> {
  return window.hermesDesktop.api<SubconsciousStatus>({
    path: '/api/subconscious/enable',
    method: 'POST',
    body: interval ? { interval } : {}
  })
}

export function disableSubconscious(): Promise<SubconsciousStatus> {
  return window.hermesDesktop.api<SubconsciousStatus>({
    path: '/api/subconscious/disable',
    method: 'POST',
    body: {}
  })
}

export function setupPresence(): Promise<PresenceSetupResult> {
  return window.hermesDesktop.api<PresenceSetupResult>({
    path: '/api/presence/setup',
    method: 'POST',
    body: {}
  })
}

export function pausePresence(): Promise<PresenceActionResult> {
  return window.hermesDesktop.api<PresenceActionResult>({
    path: '/api/presence/pause',
    method: 'POST',
    body: {}
  })
}
