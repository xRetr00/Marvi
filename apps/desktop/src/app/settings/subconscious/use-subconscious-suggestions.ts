import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { notifyError } from '@/store/notifications'

import {
  acceptSubconsciousSuggestion,
  dismissSubconsciousSuggestion,
  fetchSubconsciousSuggestions
} from './activity-service'
import type { SubconsciousSuggestion, SubconsciousSuggestionsResponse } from './activity-service'

export const SUBCONSCIOUS_SUGGESTIONS_KEY = ['subconscious-suggestions'] as const

export interface SubconsciousSuggestionsState {
  suggestions: SubconsciousSuggestion[]
  isAvailable: boolean
  isLoading: boolean
  /** Suggestion id currently mid-flight (accept or dismiss) — disables its buttons. */
  busyId: null | string
  accept: (id: string) => Promise<void>
  dismiss: (id: string) => Promise<void>
}

/**
 * Pending-suggestion inbox backed by `GET /api/subconscious/suggestions`
 * (cron/suggestions.py's consent-first store) plus the accept/dismiss
 * actions. Both actions remove the card immediately (optimistic) and put it
 * back — with an error toast — if the backend call fails. Polls every 60s
 * while the tab is visible.
 */
export function useSubconsciousSuggestions(): SubconsciousSuggestionsState {
  const hasBridge = typeof window !== 'undefined' && typeof window.hermesDesktop?.api === 'function'
  const queryClient = useQueryClient()
  const [busyId, setBusyId] = useState<null | string>(null)

  const query = useQuery({
    enabled: hasBridge,
    queryKey: SUBCONSCIOUS_SUGGESTIONS_KEY,
    queryFn: fetchSubconsciousSuggestions,
    staleTime: 30_000,
    refetchInterval: 60_000
  })

  async function resolve(id: string, action: (id: string) => Promise<unknown>, errorLabel: string): Promise<void> {
    const previous = queryClient.getQueryData<SubconsciousSuggestionsResponse>(SUBCONSCIOUS_SUGGESTIONS_KEY)

    queryClient.setQueryData<SubconsciousSuggestionsResponse | undefined>(SUBCONSCIOUS_SUGGESTIONS_KEY, current =>
      current ? { ...current, suggestions: current.suggestions.filter(s => s.id !== id) } : current
    )
    setBusyId(id)

    try {
      await action(id)
      await queryClient.invalidateQueries({ queryKey: SUBCONSCIOUS_SUGGESTIONS_KEY })
    } catch (err) {
      if (previous) {
        queryClient.setQueryData(SUBCONSCIOUS_SUGGESTIONS_KEY, previous)
      }

      notifyError(err, errorLabel)
    } finally {
      setBusyId(null)
    }
  }

  const accept = (id: string) => resolve(id, acceptSubconsciousSuggestion, 'Failed to accept suggestion')
  const dismiss = (id: string) => resolve(id, dismissSubconsciousSuggestion, 'Failed to dismiss suggestion')

  if (!hasBridge) {
    return { suggestions: [], isAvailable: false, isLoading: false, busyId: null, accept, dismiss }
  }

  return {
    suggestions: query.data?.suggestions ?? [],
    isAvailable: !query.isError,
    isLoading: query.isLoading,
    busyId,
    accept,
    dismiss
  }
}
