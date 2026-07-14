import { LoadingState, SettingsContent } from '../primitives'

import { SubconsciousCoreSettings } from './core-settings'
import { useMarviConfig } from './use-marvi-config'

// Marvi's proactive-agent settings surface. Composio account/tool management
// lives in its own Mind tab so this page stays focused on activation policy.
// It is rendered as the "Subconscious" tab of Settings → Presence (see
// ../presence/index.tsx). Desktop Presence (ActivityWatch/flow-gating) and
// Voice presence live in their own sibling tabs there instead of here.
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
    </SettingsContent>
  )
}
