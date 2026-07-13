import { useQuery } from '@tanstack/react-query'

import { fetchSubconsciousActivity } from './activity-service'
import type { SubconsciousActivityRun } from './activity-service'

export const SUBCONSCIOUS_ACTIVITY_KEY = ['subconscious-activity'] as const

/** Poll cadence matching the tab-visible activity/suggestions refresh requirement. */
export const SUBCONSCIOUS_ACTIVITY_POLL_MS = 60_000

export interface SubconsciousActivityState {
  runs: SubconsciousActivityRun[]
  /** Backend-supplied caveat (e.g. "no per-run history yet") — render it,
   *  never silently show an empty list as if nothing has ever happened. */
  note: null | string
  /** False when the backend surface can't be reached at all. */
  isAvailable: boolean
  isLoading: boolean
  refetch: () => void
}

/**
 * Recent subconscious-tick runs, newest first, from
 * `GET /api/subconscious/activity` — the tick timeline's data source.
 * Polls every 60s while the tab is visible (TanStack Query's
 * `refetchIntervalInBackground` defaults to false).
 */
export function useSubconsciousActivity(limit = 30): SubconsciousActivityState {
  const hasBridge = typeof window !== 'undefined' && typeof window.hermesDesktop?.api === 'function'

  const query = useQuery({
    enabled: hasBridge,
    queryKey: [...SUBCONSCIOUS_ACTIVITY_KEY, limit],
    queryFn: () => fetchSubconsciousActivity(limit),
    staleTime: 30_000,
    refetchInterval: SUBCONSCIOUS_ACTIVITY_POLL_MS
  })

  if (!hasBridge) {
    return { runs: [], note: null, isAvailable: false, isLoading: false, refetch: () => {} }
  }

  return {
    runs: query.data?.runs ?? [],
    note: query.data?.note ?? null,
    isAvailable: !query.isError,
    isLoading: query.isLoading,
    refetch: () => void query.refetch()
  }
}
