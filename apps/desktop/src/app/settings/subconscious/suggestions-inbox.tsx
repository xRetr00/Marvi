import { Button } from '@/components/ui/button'

import { Pill } from '../primitives'

import type { SubconsciousSuggestion } from './activity-service'

function SuggestionCard({
  suggestion,
  busy,
  onAccept,
  onDismiss
}: {
  suggestion: SubconsciousSuggestion
  busy: boolean
  onAccept: () => void
  onDismiss: () => void
}) {
  return (
    <div className="flex items-start justify-between gap-3 px-3 py-2.5">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="truncate text-xs font-medium text-foreground">{suggestion.title}</span>
          <Pill>{suggestion.category}</Pill>
        </div>
        {suggestion.summary && <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">{suggestion.summary}</p>}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        <Button disabled={busy} onClick={onDismiss} size="sm" variant="ghost">
          Dismiss
        </Button>
        <Button disabled={busy} onClick={onAccept} size="sm" variant="outline">
          Accept
        </Button>
      </div>
    </div>
  )
}

/**
 * Pending-suggestion inbox — consent-first automations the subconscious tick
 * proposed via `suggest_automation` (cron/suggestions.py), surfaced from
 * `GET /api/subconscious/suggestions`. Accept/dismiss are optimistic: the
 * card disappears immediately and reappears (with a toast) if the backend
 * call fails.
 */
export function SuggestionsInbox({
  suggestions,
  isAvailable,
  isLoading,
  busyId,
  onAccept,
  onDismiss
}: {
  suggestions: SubconsciousSuggestion[]
  isAvailable: boolean
  isLoading: boolean
  busyId: null | string
  onAccept: (id: string) => void
  onDismiss: (id: string) => void
}) {
  if (isLoading) {
    return <div className="px-3 py-4 text-center text-xs text-muted-foreground">Loading suggestions…</div>
  }

  if (!isAvailable) {
    return (
      <div className="rounded-md border border-dashed border-(--ui-stroke-secondary) px-3 py-4 text-center text-xs text-muted-foreground">
        Couldn't load suggestions — the backend may be offline.
      </div>
    )
  }

  if (suggestions.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-(--ui-stroke-secondary) px-3 py-4 text-center text-xs text-muted-foreground">
        No pending suggestions right now.
      </div>
    )
  }

  return (
    <div className="divide-y divide-(--ui-stroke-secondary) rounded-md border border-(--ui-stroke-secondary)">
      {suggestions.map(suggestion => (
        <SuggestionCard
          busy={busyId === suggestion.id}
          key={suggestion.id}
          onAccept={() => onAccept(suggestion.id)}
          onDismiss={() => onDismiss(suggestion.id)}
          suggestion={suggestion}
        />
      ))}
    </div>
  )
}
