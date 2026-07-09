import { CheckCircle2, Eye, Link as LinkIcon } from '@/lib/icons'

import { LoadingState, SectionHeading, SettingsContent } from '../primitives'

import { ConnectedAccounts } from './connected-accounts'
import { PresenceSettings, SubconsciousCoreSettings } from './core-settings'
import { GoalsPanel } from './goals-panel'
import { KnowledgeViewer } from './knowledge-viewer'
import { useMarviConfig } from './use-marvi-config'

// Marvi's proactive-agent surface: subconscious tick + presence settings,
// goals, a read-only memory viewer, and Composio accounts — all one scrolling
// settings page, matching the flat single-page pattern of neighboring
// settings surfaces (VoicePresenceSettings, NotificationsSettings) rather
// than introducing new nested sub-nav routing.
export function SubconsciousSettings() {
  const marvi = useMarviConfig()

  if (marvi.isLoading && !marvi.config) {
    return <LoadingState label="Loading Marvi settings" />
  }

  if (marvi.isError && !marvi.config) {
    return (
      <SettingsContent>
        <div className="grid min-h-48 place-items-center text-center text-sm text-muted-foreground">
          Couldn't load Marvi settings.{' '}
          <button className="underline" onClick={() => void marvi.refetch()} type="button">
            Retry
          </button>
        </div>
      </SettingsContent>
    )
  }

  return (
    <SettingsContent>
      <SubconsciousCoreSettings marvi={marvi} />

      <div className="my-4 h-px bg-border/30" />

      <PresenceSettings marvi={marvi} />

      <div className="my-4 h-px bg-border/30" />

      <SectionHeading icon={CheckCircle2} title="Goals" />
      <GoalsPanel />

      <div className="my-4 h-px bg-border/30" />

      <SectionHeading icon={Eye} title="What Marvi knows" />
      <KnowledgeViewer />

      <div className="my-4 h-px bg-border/30" />

      <SectionHeading icon={LinkIcon} title="Connected accounts" />
      <ConnectedAccounts marvi={marvi} />
    </SettingsContent>
  )
}
