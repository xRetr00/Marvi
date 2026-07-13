// REST calls for the subconscious tick's visibility surface
// (hermes_cli/web_server.py — GET /api/subconscious/activity|surfaces|suggestions,
// POST /api/subconscious/suggestions/{id}/accept|dismiss).
//
// Same authenticated desktop transport (`window.hermesDesktop.api`) as
// activation-service.ts; split into its own file because these are read
// surfaces (+ two suggestion actions) rather than config-flipping toggles.

/** Which background-thinking surface produced an activity row. */
export type SubconsciousActivitySource = 'distiller' | 'goblin' | 'idle_trigger' | 'tick'

/** One row from `GET /api/subconscious/activity` — a single background-thinking run. */
export interface SubconsciousActivityRun {
  at: null | string
  source: SubconsciousActivitySource
  job_id?: null | string
  outcome: 'diff_silent' | 'error' | 'message' | 'no_change' | 'suggestion' | null
  summary: null | string
  /** Stage-1 "what changed" — the world-diff/script output that woke the run
   *  (capped at 4000 chars server-side; the untruncated original is at `output_path`). */
  diff?: null | string
  /** Stage-2 "what Marvi thought/did" — the agent's raw final response, even
   *  a bare "[SILENT]" when that's literally the entire output produced. */
  thought?: null | string
  /** Path to the full, untruncated cron output .md file for deep-dive. */
  output_path?: null | string
  duration_ms?: null | number
}

export interface SubconsciousActivityResponse {
  ok: boolean
  runs: SubconsciousActivityRun[]
  /** Present when the response is a degraded fallback (e.g. no activity-log
   *  history yet) — surfaced so the UI can explain the limitation instead of
   *  silently pretending the list is complete. */
  note?: string
}

/** One row from `GET /api/subconscious/surfaces` — a Composio surface's sync health. */
export interface SubconsciousSurfaceStatus {
  surface: string
  status: 'backing-off' | 'error' | 'ok'
  cursor_age_seconds: null | number
  quiet_streak: null | number
  effective_interval_seconds: null | number
  consecutive_failures: null | number
  last_error: null | string
  last_success_at: null | string
  next_retry_at: null | string
}

export interface SubconsciousSurfacesResponse {
  ok: boolean
  surfaces: SubconsciousSurfaceStatus[]
}

/** One row from `GET /api/subconscious/suggestions` — a pending automation proposal. */
export interface SubconsciousSuggestion {
  id: string
  title: string
  summary: string
  source: string
  category: string
  tier: 'auto' | 'notify' | 'propose'
  created: null | string
}

export interface SubconsciousSuggestionsResponse {
  ok: boolean
  suggestions: SubconsciousSuggestion[]
}

export function fetchSubconsciousActivity(limit = 30): Promise<SubconsciousActivityResponse> {
  return window.hermesDesktop.api<SubconsciousActivityResponse>({
    path: `/api/subconscious/activity?limit=${limit}`
  })
}

export function fetchSubconsciousSurfaces(): Promise<SubconsciousSurfacesResponse> {
  return window.hermesDesktop.api<SubconsciousSurfacesResponse>({ path: '/api/subconscious/surfaces' })
}

export function fetchSubconsciousSuggestions(): Promise<SubconsciousSuggestionsResponse> {
  return window.hermesDesktop.api<SubconsciousSuggestionsResponse>({ path: '/api/subconscious/suggestions' })
}

export function acceptSubconsciousSuggestion(id: string): Promise<{ job: Record<string, unknown>; ok: boolean }> {
  return window.hermesDesktop.api({
    path: `/api/subconscious/suggestions/${encodeURIComponent(id)}/accept`,
    method: 'POST',
    body: {}
  })
}

export function dismissSubconsciousSuggestion(id: string): Promise<{ ok: boolean }> {
  return window.hermesDesktop.api({
    path: `/api/subconscious/suggestions/${encodeURIComponent(id)}/dismiss`,
    method: 'POST',
    body: {}
  })
}
