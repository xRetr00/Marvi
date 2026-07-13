import { Tip } from '@/components/ui/tooltip'
import { relativeTime } from '@/lib/time'
import { cn } from '@/lib/utils'

import type { SubconsciousSurfaceStatus } from './activity-service'

const STATUS_DOT_CLASS: Record<SubconsciousSurfaceStatus['status'], string> = {
  ok: 'bg-emerald-500',
  'backing-off': 'bg-amber-500',
  error: 'bg-(--ui-red)'
}

const STATUS_LABEL: Record<SubconsciousSurfaceStatus['status'], string> = {
  ok: 'Synced',
  'backing-off': 'Backing off',
  error: 'Error'
}

function lastChangeLabel(surface: SubconsciousSurfaceStatus): string {
  if (!surface.last_success_at) {
    return 'never synced'
  }

  const at = new Date(surface.last_success_at)

  if (Number.isNaN(at.getTime())) {
    return 'never synced'
  }

  return `last change ${relativeTime(at.getTime())}`
}

function SurfaceRow({ surface }: { surface: SubconsciousSurfaceStatus }) {
  const row = (
    <div className="flex items-center justify-between gap-3 px-3 py-2">
      <div className="flex min-w-0 items-center gap-2">
        <span aria-hidden className={cn('size-2 shrink-0 rounded-full', STATUS_DOT_CLASS[surface.status])} />
        <span className="truncate text-xs font-medium text-foreground capitalize">{surface.surface}</span>
      </div>
      <div className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
        <span>{STATUS_LABEL[surface.status]}</span>
        <span className="text-muted-foreground/60">·</span>
        <span>{lastChangeLabel(surface)}</span>
      </div>
    </div>
  )

  if (surface.status !== 'ok' && surface.last_error) {
    return (
      <Tip label={surface.last_error}>
        <div>{row}</div>
      </Tip>
    )
  }

  return row
}

/**
 * Per-surface Composio sync health strip — one row per configured surface
 * (gmail, github, ...) from `GET /api/subconscious/surfaces`
 * (cron/scripts/subconscious/snapshot_store.py's `status_dict()`).
 */
export function SurfacesHealth({
  surfaces,
  isAvailable,
  isLoading
}: {
  surfaces: SubconsciousSurfaceStatus[]
  isAvailable: boolean
  isLoading: boolean
}) {
  if (isLoading) {
    return <div className="px-3 py-4 text-center text-xs text-muted-foreground">Loading surfaces…</div>
  }

  if (!isAvailable) {
    return (
      <div className="rounded-md border border-dashed border-(--ui-stroke-secondary) px-3 py-4 text-center text-xs text-muted-foreground">
        Couldn't load surface health — the backend may be offline.
      </div>
    )
  }

  if (surfaces.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-(--ui-stroke-secondary) px-3 py-4 text-center text-xs text-muted-foreground">
        No connected accounts yet — connect Gmail or GitHub below to give the subconscious tick something to watch.
      </div>
    )
  }

  return (
    <div className="divide-y divide-(--ui-stroke-secondary) rounded-md border border-(--ui-stroke-secondary)">
      {surfaces.map(surface => (
        <SurfaceRow key={surface.surface} surface={surface} />
      ))}
    </div>
  )
}
