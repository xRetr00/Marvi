import { Activity } from '@/lib/icons'

import { Caption, SectionHeading } from '../primitives'

import { ActivityTimeline } from './activity-timeline'
import { SuggestionsInbox } from './suggestions-inbox'
import { SurfacesHealth } from './surfaces-health'
import { useSubconsciousActivity } from './use-subconscious-activity'
import { useSubconsciousSuggestions } from './use-subconscious-suggestions'
import { useSubconsciousSurfaces } from './use-subconscious-surfaces'

/**
 * "Is Marvi's subconscious actually doing anything" — the visibility surface
 * this settings page was missing. A quiet tick (nothing changed) and a dead
 * ticker otherwise look identical: nothing renders either way. Rendered at
 * the TOP of the Subconscious tab, before the config controls, so the first
 * thing the user sees is evidence of activity (or an honest "nothing new
 * yet" — never a blank void).
 */
export function ActivitySection() {
  const activity = useSubconsciousActivity()
  const surfaces = useSubconsciousSurfaces()
  const suggestions = useSubconsciousSuggestions()

  return (
    <>
      <SectionHeading icon={Activity} title="Activity" />
      <Caption>What the subconscious tick has actually been doing — recent runs, connected-account sync health, and any suggestions waiting on you.</Caption>

      <ActivityTimeline
        isAvailable={activity.isAvailable}
        isLoading={activity.isLoading}
        note={activity.note}
        runs={activity.runs}
      />

      {(surfaces.isLoading || surfaces.surfaces.length > 0 || !surfaces.isAvailable) && (
        <div className="mt-3">
          <SurfacesHealth isAvailable={surfaces.isAvailable} isLoading={surfaces.isLoading} surfaces={surfaces.surfaces} />
        </div>
      )}

      {(suggestions.isLoading || suggestions.suggestions.length > 0) && (
        <div className="mt-3">
          <SuggestionsInbox
            busyId={suggestions.busyId}
            isAvailable={suggestions.isAvailable}
            isLoading={suggestions.isLoading}
            onAccept={id => void suggestions.accept(id)}
            onDismiss={id => void suggestions.dismiss(id)}
            suggestions={suggestions.suggestions}
          />
        </div>
      )}
    </>
  )
}
