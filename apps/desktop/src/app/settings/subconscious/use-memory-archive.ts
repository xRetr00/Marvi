import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { notifyError } from '@/store/notifications'

import { MARVI_KNOWLEDGE_KEY } from './use-marvi-knowledge'

/** Raw row shape returned by `GET /api/memory/archived` (hermes_cli/web_server.py, Loop 3). */
export interface ArchivedMemoryEntry {
  id: string
  target: 'memory' | 'user'
  text: string
  topic?: string
  archived_at: string
  reason?: string
}

interface ArchivedMemoryResponse {
  ok: boolean
  entries: ArchivedMemoryEntry[]
}

export const MEMORY_ARCHIVE_KEY = ['memory-archive'] as const

function fetchArchivedMemory(): Promise<ArchivedMemoryResponse> {
  return window.hermesDesktop.api<ArchivedMemoryResponse>({ path: '/api/memory/archived' })
}

function restoreArchivedMemory(id: string): Promise<unknown> {
  return window.hermesDesktop.api({ path: `/api/memory/restore/${encodeURIComponent(id)}`, method: 'POST', body: {} })
}

export interface MemoryArchiveState {
  entries: ArchivedMemoryEntry[]
  isAvailable: boolean
  isLoading: boolean
  /** Archived-entry id currently mid-restore — disables its Restore button. */
  restoringId: null | string
  restore: (id: string) => Promise<void>
}

/**
 * Archived semantic-memory entries for the "What Marvi knows" viewer's
 * Archived collapsible — entries the decay pass (agent/memory/decay.py,
 * Loop 3) moved out of the hot USER.md/MEMORY.md store instead of deleting
 * them. Restoring one puts it back in the live store and invalidates both
 * this list and the main knowledge viewer.
 */
export function useMemoryArchive(): MemoryArchiveState {
  const hasBridge = typeof window !== 'undefined' && typeof window.hermesDesktop?.api === 'function'
  const queryClient = useQueryClient()
  const [restoringId, setRestoringId] = useState<null | string>(null)

  const query = useQuery({
    enabled: hasBridge,
    queryKey: MEMORY_ARCHIVE_KEY,
    queryFn: fetchArchivedMemory,
    staleTime: 30_000
  })

  async function restore(id: string): Promise<void> {
    setRestoringId(id)

    try {
      await restoreArchivedMemory(id)
      await queryClient.invalidateQueries({ queryKey: MEMORY_ARCHIVE_KEY })
      await queryClient.invalidateQueries({ queryKey: MARVI_KNOWLEDGE_KEY })
    } catch (err) {
      notifyError(err, 'Failed to restore memory entry')
    } finally {
      setRestoringId(null)
    }
  }

  if (!hasBridge) {
    return { entries: [], isAvailable: false, isLoading: false, restoringId: null, restore }
  }

  return {
    entries: query.data?.entries ?? [],
    isAvailable: !query.isError,
    isLoading: query.isLoading,
    restoringId,
    restore
  }
}
