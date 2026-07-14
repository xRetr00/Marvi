// REST calls for the local document Brain surface (hermes_cli/web_server.py
// — GET /api/brain/status|search, POST /api/brain/index, PUT
// /api/brain/config). Same authenticated desktop transport
// (`window.hermesDesktop.api`) as ../subconscious/activation-service.ts;
// split into its own file/folder because Brain is its own settings tab
// (Settings → Presence → Brain) rather than a subconscious sub-panel.
//
// PUT /api/brain/config is the single write path for every Brain config
// field (enabled/folders/exclude/schedule) — never a raw config patch —
// because the backend both validates folder paths exist on disk and, when
// enabling, creates/resumes the "Brain index" cron job (tools/brain/indexer.py
// ensure_index_job); a plain PUT /api/config write would silently skip both.

export interface BrainLastRun {
  at: null | string
  indexed: number
  skipped: number
  removed: number
  errors: number
}

/** `GET /api/brain/status` response shape. */
export interface BrainStatus {
  ok: boolean
  enabled: boolean
  folders: string[]
  exclude: string[]
  schedule: string
  files: number
  chunks: number
  indexed_at: null | string
  last_run: BrainLastRun
}

export interface BrainConfigPatch {
  enabled?: boolean
  folders?: string[]
  exclude?: string[]
  schedule?: string
}

export interface BrainConfigResponse {
  ok: boolean
  brain: {
    enabled: boolean
    folders: string[]
    exclude: string[]
    schedule: string
  }
}

export interface BrainSearchResult {
  path: string
  chunk_index: number
  snippet: string
  score: number
}

export interface BrainSearchResponse {
  ok: boolean
  results: BrainSearchResult[]
}

export interface BrainIndexResponse {
  ok: boolean
  indexed: number
  skipped: number
  removed: number
  errors: number
  files: number
  chunks: number
  indexed_at: null | string
}

export function fetchBrainStatus(): Promise<BrainStatus> {
  return window.hermesDesktop.api<BrainStatus>({ path: '/api/brain/status' })
}

export function updateBrainConfig(patch: BrainConfigPatch): Promise<BrainConfigResponse> {
  return window.hermesDesktop.api<BrainConfigResponse>({
    path: '/api/brain/config',
    method: 'PUT',
    body: patch
  })
}

export function indexBrainNow(): Promise<BrainIndexResponse> {
  return window.hermesDesktop.api<BrainIndexResponse>({
    path: '/api/brain/index',
    method: 'POST',
    body: {}
  })
}

export function searchBrain(query: string, limit = 8): Promise<BrainSearchResponse> {
  return window.hermesDesktop.api<BrainSearchResponse>({
    path: `/api/brain/search?q=${encodeURIComponent(query)}&limit=${limit}`
  })
}
