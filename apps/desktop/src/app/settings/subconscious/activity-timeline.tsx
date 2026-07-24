import { useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { AlertTriangle, Bell, Brain, CheckCircle2, Clock, Cloud, MessageCircle, Moon, RefreshCw, Search, Zap } from '@/lib/icons'
import { relativeTime } from '@/lib/time'
import { cn } from '@/lib/utils'

import type { SubconsciousActivityRun, SubconsciousActivitySource } from './activity-service'

const OUTCOME_META: Record<
  NonNullable<SubconsciousActivityRun['outcome']>,
  { label: string; icon: typeof Clock; className: string }
> = {
  no_change: { label: 'Quiet', icon: Clock, className: 'text-muted-foreground' },
  diff_silent: { label: 'Quiet', icon: CheckCircle2, className: 'text-muted-foreground' },
  message: { label: 'Sent message', icon: MessageCircle, className: 'text-blue-500' },
  suggestion: { label: 'Suggested', icon: Zap, className: 'text-amber-500' },
  error: { label: 'Error', icon: AlertTriangle, className: 'text-(--ui-red)' }
}

const UNKNOWN_META = { label: 'Ran', icon: Clock, className: 'text-muted-foreground' }

const SOURCE_META: Record<SubconsciousActivitySource, { label: string; icon: typeof Clock }> = {
  tick: { label: 'Tick', icon: RefreshCw },
  idle_trigger: { label: 'Idle', icon: Moon },
  distiller: { label: 'Distiller', icon: Brain },
  reflection: { label: 'Reflection', icon: Brain },
  smart_room_alarm: { label: 'Room alarm', icon: Bell },
  goblin: { label: 'Goblin', icon: Bell },
  dreaming: { label: 'Dreaming', icon: Moon },
  world: { label: 'World', icon: Cloud },
  // tools/goal_tools.py::_handle_suggest_goal logs one of these when it
  // auto-creates an inferred goal (origin="inferred") -- see goals-panel.tsx
  // for where the goal itself shows up with its own "Inferred" badge.
  goal: { label: 'Goal', icon: Zap },
  // agent/autonomy/{budget,research,ask}.py -- self-directed research,
  // proactive questions, and other budgeted autonomy actions (Marvi freedom
  // spec Part 1). See mind/autonomy-panel.tsx for the dedicated Autonomy
  // section this same activity feed also backs.
  autonomy: { label: 'Autonomy', icon: Search }
}

type FilterKey = 'all' | 'distiller' | 'goblin' | 'ticks' | 'world'

const FILTERS: { key: FilterKey; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'ticks', label: 'Ticks' },
  { key: 'goblin', label: 'Goblin' },
  { key: 'distiller', label: 'Distiller' },
  { key: 'world', label: 'World' }
]

function matchesFilter(run: SubconsciousActivityRun, filter: FilterKey): boolean {
  if (filter === 'all') {
    return true
  }

  if (filter === 'ticks') {
    return run.source === 'tick' || run.source === 'idle_trigger'
  }

  return run.source === filter
}

function SourceChip({ source }: { source: SubconsciousActivitySource }) {
  const meta = SOURCE_META[source] ?? SOURCE_META.tick
  const Icon = meta.icon

  return (
    <span className="inline-flex items-center gap-1 text-[0.68rem] text-muted-foreground">
      <Icon className="size-3" />
      {meta.label}
    </span>
  )
}

function OutcomeChip({ outcome }: { outcome: SubconsciousActivityRun['outcome'] }) {
  const meta = outcome ? OUTCOME_META[outcome] : UNKNOWN_META
  const Icon = meta.icon

  return (
    <span className={cn('inline-flex items-center gap-1 text-xs font-medium', meta.className)}>
      <Icon className="size-3.5" />
      {meta.label}
    </span>
  )
}

function ActivityRow({ run }: { run: SubconsciousActivityRun }) {
  const [expanded, setExpanded] = useState(false)
  const hasDiff = Boolean(run.diff && run.diff.trim())
  const hasThought = Boolean(run.thought && run.thought.trim())
  const hasSummary = Boolean(run.summary && run.summary.trim())
  const canExpand = hasDiff || hasThought || hasSummary
  const at = run.at ? new Date(run.at) : null
  const when = at && !Number.isNaN(at.getTime()) ? relativeTime(at.getTime()) : 'unknown time'

  return (
    <li className="px-3 py-2.5">
      <button
        className={cn('flex w-full items-center justify-between gap-3 text-left', canExpand && 'cursor-pointer')}
        disabled={!canExpand}
        onClick={() => setExpanded(v => !v)}
        type="button"
      >
        <span className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">{when}</span>
          <SourceChip source={run.source} />
        </span>
        <OutcomeChip outcome={run.outcome} />
      </button>
      {expanded && (
        <div className="mt-2 grid gap-2 border-l-2 border-(--ui-stroke-secondary) pl-2.5">
          {hasDiff && (
            <div>
              <div className="text-[0.65rem] font-semibold tracking-wide text-muted-foreground uppercase">What changed</div>
              <p className="mt-0.5 whitespace-pre-wrap text-xs text-foreground/80">{run.diff}</p>
            </div>
          )}
          {hasThought && (
            <div>
              <div className="text-[0.65rem] font-semibold tracking-wide text-muted-foreground uppercase">
                What Marvi thought/did
              </div>
              <p className="mt-0.5 whitespace-pre-wrap text-xs text-foreground/80">{run.thought}</p>
            </div>
          )}
          {!hasDiff && !hasThought && hasSummary && <p className="text-xs text-foreground/80">{run.summary}</p>}
          {run.output_path && (
            <p className="truncate font-mono text-[0.6rem] text-muted-foreground/60" title={run.output_path}>
              {run.output_path}
            </p>
          )}
        </div>
      )}
    </li>
  )
}

/**
 * Compact tick timeline — the "is Marvi's subconscious actually doing
 * anything" surface. Backed by `GET /api/subconscious/activity`
 * (cron/scheduler.py's activity-log hooks), which merges every
 * background-thinking source into one feed: the subconscious tick, the
 * presence distiller, and goblin shoulder taps. Each row expands to show
 * the actual thinking — "What changed" (the stage-1 diff) and "What Marvi
 * thought/did" (the stage-2 agent's raw output, even a bare "[SILENT]") —
 * not just a bare outcome label. Empty states are always informative: a
 * quiet tick and a dead ticker must never look the same.
 */
export function ActivityTimeline({
  runs,
  note,
  isAvailable,
  isLoading
}: {
  runs: SubconsciousActivityRun[]
  note: null | string
  isAvailable: boolean
  isLoading: boolean
}) {
  const [filter, setFilter] = useState<FilterKey>('all')
  const filtered = useMemo(() => runs.filter(run => matchesFilter(run, filter)), [runs, filter])

  if (isLoading) {
    return <div className="px-3 py-6 text-center text-xs text-muted-foreground">Loading tick history…</div>
  }

  if (!isAvailable) {
    return (
      <div className="rounded-md border border-dashed border-(--ui-stroke-secondary) px-3 py-6 text-center text-xs text-muted-foreground">
        Couldn't load recent activity — the backend may be offline. It retries automatically.
      </div>
    )
  }

  if (runs.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-(--ui-stroke-secondary) px-3 py-6 text-center text-xs text-muted-foreground">
        Subconscious hasn't ticked yet. It runs on its configured interval — check back after the next tick.
      </div>
    )
  }

  const latest = runs[0]

  const latestWhen = (() => {
    if (!latest.at) {
      return null
    }

    const at = new Date(latest.at)

    return Number.isNaN(at.getTime()) ? null : relativeTime(at.getTime())
  })()

  return (
    <div className="grid gap-2">
      {latestWhen && !latest.outcome && (
        <p className="text-xs text-muted-foreground">Subconscious is running — last checked {latestWhen}, nothing new.</p>
      )}
      {note && <p className="text-[0.68rem] text-muted-foreground/70">{note}</p>}

      <div className="flex gap-1">
        {FILTERS.map(({ key, label }) => (
          <Button
            aria-pressed={filter === key}
            className={cn(filter === key && 'bg-(--chrome-action-hover) text-foreground')}
            key={key}
            onClick={() => setFilter(key)}
            size="xs"
            type="button"
            variant="ghost"
          >
            {label}
          </Button>
        ))}
      </div>

      {filtered.length === 0 ? (
        <div className="rounded-md border border-dashed border-(--ui-stroke-secondary) px-3 py-4 text-center text-xs text-muted-foreground">
          No activity for this filter yet.
        </div>
      ) : (
        <ul className="divide-y divide-(--ui-stroke-secondary) rounded-md border border-(--ui-stroke-secondary)">
          {filtered.map((run, idx) => (
            // Runs have no stable id — (at, idx) is unique within one response.
            <ActivityRow key={`${run.at ?? 'unknown'}-${idx}`} run={run} />
          ))}
        </ul>
      )}
    </div>
  )
}
