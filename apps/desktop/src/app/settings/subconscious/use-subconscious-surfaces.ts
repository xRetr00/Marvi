import { useQuery } from '@tanstack/react-query'

import { fetchSubconsciousSurfaces } from './activity-service'
import type { SubconsciousSurfaceStatus } from './activity-service'

export const SUBCONSCIOUS_SURFACES_KEY = ['subconscious-surfaces'] as const

export interface SubconsciousSurfacesState {
  surfaces: SubconsciousSurfaceStatus[]
  isAvailable: boolean
  isLoading: boolean
}

/**
 * Per-Composio-surface sync health from `GET /api/subconscious/surfaces`
 * (cron/scripts/subconscious/snapshot_store.py's `status_dict()` per
 * configured surface). Polls every 60s while the tab is visible.
 */
export function useSubconsciousSurfaces(): SubconsciousSurfacesState {
  const hasBridge = typeof window !== 'undefined' && typeof window.hermesDesktop?.api === 'function'

  const query = useQuery({
    enabled: hasBridge,
    queryKey: SUBCONSCIOUS_SURFACES_KEY,
    queryFn: fetchSubconsciousSurfaces,
    staleTime: 30_000,
    refetchInterval: 60_000
  })

  if (!hasBridge) {
    return { surfaces: [], isAvailable: false, isLoading: false }
  }

  return {
    surfaces: query.data?.surfaces ?? [],
    isAvailable: !query.isError,
    isLoading: query.isLoading
  }
}
