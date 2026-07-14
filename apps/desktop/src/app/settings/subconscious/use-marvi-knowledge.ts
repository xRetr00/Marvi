import { useQuery } from '@tanstack/react-query'

import type { KnowledgeEntry } from './types'

export interface MarviKnowledgeState {
  entries: KnowledgeEntry[]
  /** False when the backend surface can't be reached (fetch failed or no
   *  desktop bridge) — distinguishes "wired up, nothing distilled yet" from
   *  "couldn't load" in the viewer's empty state. */
  isAvailable: boolean
  isLoading: boolean
}

/** Raw row shape returned by `GET /api/marvi/knowledge` (hermes_cli/web_server.py). */
export interface MarviKnowledgeApiEntry {
  id: string
  text: string
  source: string
  timestamp: null | string
  topic?: string
}

interface MarviKnowledgeResponse {
  ok: boolean
  entries: MarviKnowledgeApiEntry[]
  /** Provenance caveat from the backend — the flat memory store has no true
   *  per-entry source/timestamp, so source/timestamp are best-effort. */
  note?: string
}

export const MARVI_KNOWLEDGE_KEY = ['marvi-knowledge'] as const

/** Map API rows onto the UI's KnowledgeEntry shape. Exported for tests. */
export function mapKnowledgeEntries(rows: MarviKnowledgeApiEntry[]): KnowledgeEntry[] {
  return rows
    .filter(row => Boolean(row?.id) && Boolean(row?.text))
    .map(row => ({
      id: row.id,
      summary: row.text,
      source: row.source === 'presence' ? 'presence' : 'subconscious',
      createdAt: row.timestamp ?? new Date(0).toISOString(),
      ...(row.topic ? { topic: row.topic } : {})
    }))
}

function fetchMarviKnowledge(): Promise<MarviKnowledgeResponse> {
  return window.hermesDesktop.api<MarviKnowledgeResponse>({ path: '/api/marvi/knowledge' })
}

/**
 * Distilled presence/subconscious memory entries for the "What Marvi knows"
 * viewer, read from `GET /api/marvi/knowledge` — which lists the entries the
 * presence distiller and subconscious tick wrote through the memory tool
 * (HERMES_HOME/memories/USER.md + MEMORY.md), newest first, capped at 100.
 * Same authenticated desktop transport as the config channel.
 */
export function useMarviKnowledge(): MarviKnowledgeState {
  const hasBridge = typeof window !== 'undefined' && typeof window.hermesDesktop?.api === 'function'

  const query = useQuery({
    enabled: hasBridge,
    queryKey: MARVI_KNOWLEDGE_KEY,
    queryFn: fetchMarviKnowledge,
    staleTime: 30_000
  })

  if (!hasBridge) {
    return { entries: [], isAvailable: false, isLoading: false }
  }

  return {
    entries: query.data ? mapKnowledgeEntries(query.data.entries ?? []) : [],
    isAvailable: !query.isError,
    isLoading: query.isLoading
  }
}
