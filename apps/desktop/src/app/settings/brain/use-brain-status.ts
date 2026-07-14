import { useQuery, useQueryClient } from '@tanstack/react-query'

import { fetchBrainStatus } from './brain-service'
import type { BrainStatus } from './brain-service'

export const BRAIN_STATUS_KEY = ['brain-status'] as const

export interface BrainStatusState {
  status: BrainStatus | null
  /** False when the backend surface can't be reached — distinguishes "wired
   *  up, Brain is just off/empty" from "couldn't load" in the tab's empty state. */
  isAvailable: boolean
  isLoading: boolean
  refetch: () => Promise<unknown>
}

/**
 * Brain index status — folders/exclude/schedule config plus index stats
 * (files/chunks/last run/errors) from `GET /api/brain/status`
 * (tools/brain/store.py + tools/brain/indexer.py's read_last_run). Polled
 * every 30s so a background "Brain index" cron run updates the tab without
 * requiring a manual refresh.
 */
export function useBrainStatus(): BrainStatusState {
  const hasBridge = typeof window !== 'undefined' && typeof window.hermesDesktop?.api === 'function'
  const queryClient = useQueryClient()

  const query = useQuery({
    enabled: hasBridge,
    queryKey: BRAIN_STATUS_KEY,
    queryFn: fetchBrainStatus,
    staleTime: 15_000,
    refetchInterval: 30_000
  })

  if (!hasBridge) {
    return { status: null, isAvailable: false, isLoading: false, refetch: async () => undefined }
  }

  return {
    status: query.data ?? null,
    isAvailable: !query.isError,
    isLoading: query.isLoading,
    refetch: () => queryClient.invalidateQueries({ queryKey: BRAIN_STATUS_KEY })
  }
}
