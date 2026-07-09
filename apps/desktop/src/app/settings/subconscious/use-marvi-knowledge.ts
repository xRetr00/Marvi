import type { KnowledgeEntry } from './types'

export interface MarviKnowledgeState {
  entries: KnowledgeEntry[]
  /** False until a real backend surface exists — distinguishes "wired up,
   *  nothing distilled yet" from "not wired up at all" in the viewer's empty state. */
  isAvailable: boolean
  isLoading: boolean
}

// TODO(workstream-A/B): there is no backend endpoint yet that lists distilled
// presence/subconscious memory entries — the presence distiller and
// subconscious tick write summaries into the memory system, but nothing
// exposes them back out as a browsable list. Once that surface exists (e.g.
// a `GET /api/memory/entries?source=presence|subconscious`), swap this stub
// for a `useQuery` against it, following the same pattern as
// `useHermesConfigRecord`. Until then this intentionally returns an empty,
// `isAvailable: false` result — never fabricated placeholder memories.
export function useMarviKnowledge(): MarviKnowledgeState {
  return { entries: [], isAvailable: false, isLoading: false }
}
