import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { DisclosureCaret } from '@/components/ui/disclosure-caret'

import { Pill } from '../primitives'

import { useMarviKnowledge } from './use-marvi-knowledge'
import { useMemoryArchive } from './use-memory-archive'

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

  const groups = Object.entries(
    entries.reduce<Record<string, typeof entries>>((byTopic, entry) => {
      const topic = entry.topic || 'Uncategorized'
      byTopic[topic] = [...(byTopic[topic] ?? []), entry]

      return byTopic
    }, {})
  )

  return (
    <div className="grid gap-3">
      {entries.length === 0 ? (
        <div className="rounded-md border border-dashed border-(--ui-stroke-secondary) px-3 py-6 text-center text-xs text-muted-foreground">
          Nothing distilled yet.
        </div>
      ) : (
        groups.map(([topic, topicEntries]) => (
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
        ))
      )}
      <ArchivedSection />
    </div>
  )
}

/**
 * Collapsible "Archived" section (Loop 3, memory-maturity spec) — entries
 * the decay pass moved out of the hot store instead of deleting. Closed by
 * default so it doesn't compete with the live knowledge list; a Restore
 * button puts an entry back.
 */
function ArchivedSection() {
  const [open, setOpen] = useState(false)
  const { entries, isAvailable, isLoading, restoringId, restore } = useMemoryArchive()

  return (
    <section className="rounded-md border border-(--ui-stroke-secondary)">
      <button
        className="flex w-full items-center justify-between gap-2 px-3 py-2.5 text-left"
        onClick={() => setOpen(prev => !prev)}
        type="button"
      >
        <span className="text-xs font-medium text-foreground">
          Archived{entries.length > 0 ? ` (${entries.length})` : ''}
        </span>
        <DisclosureCaret open={open} />
      </button>
      {open && (
        <div className="border-t border-(--ui-stroke-secondary)">
          {isLoading ? (
            <div className="px-3 py-4 text-center text-xs text-muted-foreground">Loading…</div>
          ) : !isAvailable ? (
            <div className="px-3 py-4 text-center text-xs text-muted-foreground">Couldn't load archived memory.</div>
          ) : entries.length === 0 ? (
            <div className="px-3 py-4 text-center text-xs text-muted-foreground">
              Nothing archived yet. Stale or superseded entries land here instead of being deleted.
            </div>
          ) : (
            <ul className="divide-y divide-(--ui-stroke-secondary)">
              {entries.map(entry => (
                <li className="flex items-start justify-between gap-3 px-3 py-2.5" key={entry.id}>
                  <div className="min-w-0">
                    <p className="text-xs text-foreground">{entry.text}</p>
                    <p className="mt-1 text-[0.65rem] text-muted-foreground">
                      Archived {new Date(entry.archived_at).toLocaleDateString()}
                      {entry.topic ? ` · ${entry.topic}` : ''}
                    </p>
                  </div>
                  <Button
                    disabled={restoringId === entry.id}
                    onClick={() => void restore(entry.id)}
                    size="sm"
                    variant="outline"
                  >
                    Restore
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}
