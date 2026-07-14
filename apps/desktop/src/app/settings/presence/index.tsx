import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Activity, Brain, FolderOpen, type IconComponent, Mic } from '@/lib/icons'

import { useRouteEnumParam } from '../../hooks/use-route-enum-param'
import { BrainSettings } from '../brain'
import { LoadingState, SettingsContent } from '../primitives'
import { SubconsciousSettings } from '../subconscious'
import { DesktopPresenceSettings } from '../subconscious/core-settings'
import { useMarviConfig } from '../subconscious/use-marvi-config'
import { VoicePresenceSettings } from '../voice-presence-settings'

import { WakeWordSettings } from './wake-word-settings'

export const PRESENCE_TABS = ['subconscious', 'desktop', 'brain', 'voice', 'wake-word'] as const
export type PresenceTab = (typeof PRESENCE_TABS)[number]

const TAB_META: Record<PresenceTab, { label: string; icon: IconComponent }> = {
  subconscious: { icon: Brain, label: 'Subconscious' },
  desktop: { icon: Activity, label: 'Desktop Presence' },
  // FolderOpen (not Brain) so this tab reads distinctly from "Subconscious"
  // in the tab strip — Brain is a local-folder index, not the tick/reflection.
  brain: { icon: FolderOpen, label: 'Brain' },
  voice: { icon: Mic, label: 'Voice' },
  'wake-word': { icon: Mic, label: 'Wake Word' }
}

/**
 * Settings → Presence: the single home for everything that makes Marvi feel
 * present without being asked — the subconscious tick + goals + knowledge +
 * accounts, desktop context (ActivityWatch), always-on voice presence +
 * speaker ID, and wake-word detection. Previously three separate nav entries
 * (Subconscious, Voice presence, plus wake word buried in the generic Voice
 * config page); consolidated here as tabs per
 * docs/superpowers/specs/2026-07-10-marvi-duplex-voice-splitbrain-design.md's
 * UI restructuring ask. Every tab reuses its existing component unchanged —
 * this is reorganization, not a rewrite.
 */
export function PresenceSettings({
  onOpenModelConfig,
  onOpenVoiceConfig
}: {
  onOpenModelConfig: () => void
  onOpenVoiceConfig: () => void
}) {
  const [tab, setTab] = useRouteEnumParam<PresenceTab>('ptab', PRESENCE_TABS, 'subconscious')
  const marvi = useMarviConfig()
  const loading = marvi.isLoading && !marvi.config

  return (
    <Tabs className="h-full min-h-0 gap-0" onValueChange={value => setTab(value as PresenceTab)} value={tab}>
      <TabsList className="mx-4 mt-3 w-fit shrink-0">
        {PRESENCE_TABS.map(id => {
          const meta = TAB_META[id]
          const Icon = meta.icon

          return (
            <TabsTrigger key={id} value={id}>
              <Icon className="size-3.5" />
              {meta.label}
            </TabsTrigger>
          )
        })}
      </TabsList>

      <div className="min-h-0 flex-1">
        {tab === 'subconscious' ? (
          <SubconsciousSettings />
        ) : tab === 'brain' ? (
          <BrainSettings />
        ) : loading ? (
          <LoadingState label="Loading Marvi settings" />
        ) : tab === 'desktop' ? (
          <SettingsContent>
            <DesktopPresenceSettings marvi={marvi} />
          </SettingsContent>
        ) : tab === 'voice' ? (
          <VoicePresenceSettings marvi={marvi} onOpenModelConfig={onOpenModelConfig} onOpenVoiceConfig={onOpenVoiceConfig} />
        ) : (
          <SettingsContent>
            <WakeWordSettings marvi={marvi} />
          </SettingsContent>
        )}
      </div>
    </Tabs>
  )
}
