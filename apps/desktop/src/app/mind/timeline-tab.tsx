import { useCallback, useEffect, useMemo, useState } from 'react'

import { Input } from '@/components/ui/input'
import { Clock } from '@/lib/icons'
import { notifyError } from '@/store/notifications'

import { Pill, SectionHeading } from '../settings/primitives'

interface Episode {
  id: number
  ts: string
  kind: string
  actor: string
  title: string
  summary: string
  source: string
}

interface EpisodesResponse {
  episodes: Episode[]
  note?: string
}

const KIND_LABELS: Record<string, string> = {
  conversation: 'Conversation',
  task: 'Task',
  room: 'Room',
  proactive: 'Proactive',
  device: 'Device',
  arrival: 'Arrival',
  learning: 'Learning'
}

const KINDS = Object.keys(KIND_LABELS)

/** "Marvi's diary" — a reverse-chronological, filterable stream over the episodic memory store. */
export function TimelineTab() {
  const [episodes, setEpisodes] = useState<Episode[]>([])
  const [note, setNote] = useState<null | string>(null)
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState<null | string>(null)
  const [since, setSince] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)

    try {
      const params = new URLSearchParams()

      if (query.trim()) {params.set('q', query.trim())}

      if (kind) {params.set('kind', kind)}

      if (since) {
        const parsed = new Date(since)

        if (!Number.isNaN(parsed.getTime())) {params.set('since', parsed.toISOString())}
      }

      params.set('limit', '100')

      const response = await window.hermesDesktop.api<EpisodesResponse>({
        path: `/api/memory/episodes?${params.toString()}`
      })

      setEpisodes(response.episodes ?? [])
      setNote(response.note ?? null)
      setError(false)
    } catch (err) {
      setError(true)
      notifyError(err, 'Failed to load Timeline')
    } finally {
      setLoading(false)
    }
  }, [kind, query, since])

  useEffect(() => {
    const handle = window.setTimeout(() => void load(), 250)

    return () => window.clearTimeout(handle)
  }, [load])

  const groups = useMemo(() => {
    const byDay = new Map<string, Episode[]>()

    for (const episode of episodes) {
      const day = episode.ts
        ? new Date(episode.ts).toLocaleDateString(undefined, {
            weekday: 'long',
            year: 'numeric',
            month: 'long',
            day: 'numeric'
          })
        : 'Unknown date'

      byDay.set(day, [...(byDay.get(day) ?? []), episode])
    }

    return Array.from(byDay.entries())
  }, [episodes])

  return (
    <div className="grid gap-5">
      <section>
        <SectionHeading icon={Clock} meta={`${episodes.length} episode${episodes.length === 1 ? '' : 's'}`} title="Timeline" />
        <p className="mb-3 text-xs text-muted-foreground">
          A reverse-chronological log of what Marvi has observed — conversations, tasks, room events, proactive
          nudges, arrivals, and learning proposals.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <Input
            className="max-w-xs"
            onChange={event => setQuery(event.target.value)}
            placeholder="Search episodes"
            value={query}
          />
          <Input className="max-w-[10rem]" onChange={event => setSince(event.target.value)} type="date" value={since} />
          <div className="flex flex-wrap gap-1.5">
            <button className={kindChipClass(kind === null)} onClick={() => setKind(null)} type="button">
              All
            </button>
            {KINDS.map(item => (
              <button className={kindChipClass(kind === item)} key={item} onClick={() => setKind(item)} type="button">
                {KIND_LABELS[item]}
              </button>
            ))}
          </div>
        </div>
      </section>

      {error && episodes.length === 0 ? (
        <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
          Timeline is unavailable while the backend is offline.{' '}
          <button className="underline" onClick={() => void load()} type="button">
            Retry
          </button>
        </div>
      ) : loading && episodes.length === 0 ? (
        <div className="px-3 py-6 text-center text-xs text-muted-foreground">Loading…</div>
      ) : episodes.length === 0 ? (
        <div className="rounded-md border border-dashed border-(--ui-stroke-secondary) px-3 py-8 text-center text-xs text-muted-foreground">
          {note || "Marvi's episodic memory starts filling as it observes your days."}
        </div>
      ) : (
        <div className="grid gap-5">
          {groups.map(([day, items]) => (
            <section key={day}>
              <h3 className="mb-1.5 text-[0.68rem] font-medium tracking-wide text-muted-foreground uppercase">{day}</h3>
              <ul className="divide-y divide-(--ui-stroke-secondary) rounded-md border border-(--ui-stroke-secondary)">
                {items.map(episode => (
                  <li className="flex items-start justify-between gap-3 px-3 py-2.5" key={episode.id}>
                    <div className="min-w-0">
                      <div className="text-xs font-medium text-foreground">{episode.title}</div>
                      {episode.summary && (
                        <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">{episode.summary}</p>
                      )}
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <Pill>{KIND_LABELS[episode.kind] ?? episode.kind}</Pill>
                      <span className="text-[0.65rem] text-muted-foreground">
                        {episode.ts
                          ? new Date(episode.ts).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
                          : ''}
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}
    </div>
  )
}

function kindChipClass(active: boolean) {
  return [
    'rounded-full border px-2.5 py-1 text-[0.65rem] font-medium transition',
    active
      ? 'border-primary/40 bg-primary/10 text-primary'
      : 'border-(--ui-stroke-secondary) text-muted-foreground hover:text-foreground'
  ].join(' ')
}
