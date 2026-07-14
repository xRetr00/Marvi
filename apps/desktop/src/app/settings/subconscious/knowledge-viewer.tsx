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

  const groups = Object.entries(
    entries.reduce<Record<string, typeof entries>>((byTopic, entry) => {
      const topic = entry.topic || 'Uncategorized'
      byTopic[topic] = [...(byTopic[topic] ?? []), entry]
      return byTopic
    }, {})
  )

  return (
    <div className="grid gap-3">
      {groups.map(([topic, topicEntries]) => (
        <section key={topic}>
          <h3 className="mb-1.5 text-[0.68rem] font-medium tracking-wide text-muted-foreground uppercase">{topic}</h3>
          <ul className="divide-y divide-(--ui-stroke-secondary) rounded-md border border-(--ui-stroke-secondary)">
          {(topicEntries ?? []).map(entry => (
            <li className="flex items-start justify-between gap-3 px-3 py-2.5" key={entry.id}>
          <p className="min-w-0 text-xs text-foreground">{entry.summary}</p>
          <div className="flex shrink-0 items-center gap-2">
            <Pill>{SOURCE_LABEL[entry.source]}</Pill>
            <span className="text-[0.65rem] text-muted-foreground">{new Date(entry.createdAt).toLocaleDateString()}</span>
          </div>
            </li>
          ))}
          </ul>
        </section>
      ))}
    </div>
  )
}
