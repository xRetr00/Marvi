import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useSubconsciousSuggestions } from './use-subconscious-suggestions'

const fetchSubconsciousSuggestions = vi.fn()
const acceptSubconsciousSuggestion = vi.fn()
const dismissSubconsciousSuggestion = vi.fn()

vi.mock('./activity-service', () => ({
  fetchSubconsciousSuggestions: (...args: unknown[]) => fetchSubconsciousSuggestions(...args),
  acceptSubconsciousSuggestion: (...args: unknown[]) => acceptSubconsciousSuggestion(...args),
  dismissSubconsciousSuggestion: (...args: unknown[]) => dismissSubconsciousSuggestion(...args)
}))

const SUGGESTION = {
  id: 's1',
  title: 'Daily digest',
  summary: 'Summarize yesterday',
  source: 'subconscious',
  category: 'digest',
  tier: 'propose' as const,
  created: '2026-07-13T00:00:00Z'
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

beforeEach(() => {
  ;(window as unknown as { hermesDesktop: { api: unknown } }).hermesDesktop = { api: vi.fn() }
  fetchSubconsciousSuggestions.mockResolvedValue({ ok: true, suggestions: [SUGGESTION] })
})

afterEach(() => {
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  vi.clearAllMocks()
})

describe('useSubconsciousSuggestions', () => {
  it('reports unavailable when there is no desktop bridge', () => {
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop

    const { result } = renderHook(() => useSubconsciousSuggestions(), { wrapper })

    expect(result.current.isAvailable).toBe(false)
    expect(result.current.suggestions).toEqual([])
  })

  it('loads pending suggestions from the backend', async () => {
    const { result } = renderHook(() => useSubconsciousSuggestions(), { wrapper })

    await waitFor(() => expect(result.current.suggestions).toHaveLength(1))
    expect(result.current.suggestions[0].title).toBe('Daily digest')
  })

  it('accept removes the suggestion optimistically and keeps it gone on success', async () => {
    acceptSubconsciousSuggestion.mockResolvedValue({ ok: true, job: {} })
    const { result } = renderHook(() => useSubconsciousSuggestions(), { wrapper })
    await waitFor(() => expect(result.current.suggestions).toHaveLength(1))

    // Second fetch (post-invalidate) reflects the now-accepted suggestion gone server-side.
    fetchSubconsciousSuggestions.mockResolvedValue({ ok: true, suggestions: [] })

    await result.current.accept('s1')

    expect(acceptSubconsciousSuggestion).toHaveBeenCalledWith('s1')
    await waitFor(() => expect(result.current.suggestions).toEqual([]))
    expect(result.current.busyId).toBeNull()
  })

  it('dismiss rolls back and keeps the card when the backend call fails', async () => {
    dismissSubconsciousSuggestion.mockRejectedValue(new Error('boom'))
    const { result } = renderHook(() => useSubconsciousSuggestions(), { wrapper })
    await waitFor(() => expect(result.current.suggestions).toHaveLength(1))

    await result.current.dismiss('s1')

    // Rolled back: the suggestion is still there after the failed call settles.
    await waitFor(() => expect(result.current.suggestions).toHaveLength(1))
    expect(result.current.busyId).toBeNull()
  })
})
