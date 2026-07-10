import { Pill } from '../primitives'

import { useMarviKnowledge } from './use-marvi-knowledge'

const SOURCE_LABEL: Record<'presence' | 'subconscious', string> = {
  presence: 'Presence',
  subconscious: 'Subconscious'
}

/** Read-only viewer over distilled presence/subconscious memories ("What Marvi knows"). */
export function KnowledgeViewer() {
  const { entries, isAvailable, isLoading } = useMarviKnowledge()

  if (isLoading) {
    return <div className="px-3 py-6 text-center text-xs text-muted-foreground">Loading…</div>
  }

  if (!isAvailable) {
    return (
      <div className="rounded-md border border-dashed border-(--ui-stroke-secondary) px-3 py-6 text-center text-xs text-muted-foreground">
        Couldn't load what Marvi knows — the backend may be offline. It retries automatically.
      </div>
    )
  }

  if (entries.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-(--ui-stroke-secondary) px-3 py-6 text-center text-xs text-muted-foreground">
        Nothing distilled yet.
      </div>
    )
  }

  return (
    <ul className="divide-y divide-(--ui-stroke-secondary) rounded-md border border-(--ui-stroke-secondary)">
      {entries.map(entry => (
        <li className="flex items-start justify-between gap-3 px-3 py-2.5" key={entry.id}>
          <p className="min-w-0 text-xs text-foreground">{entry.summary}</p>
          <div className="flex shrink-0 items-center gap-2">
            <Pill>{SOURCE_LABEL[entry.source]}</Pill>
            <span className="text-[0.65rem] text-muted-foreground">{new Date(entry.createdAt).toLocaleDateString()}</span>
          </div>
        </li>
      ))}
    </ul>
  )
}
