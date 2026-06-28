import { useStore } from '@nanostores/react'
import type { ReactNode } from 'react'

import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { triggerHaptic } from '@/lib/haptics'
import { Mic, Settings2 } from '@/lib/icons'
import { cn } from '@/lib/utils'
import {
  $glowEnabled,
  $presenceCardsEnabled,
  $presenceEnabled,
  setGlowEnabled,
  setPresenceCardsEnabled,
  setPresenceEnabled
} from '@/store/voice-presence-settings'

import { ListRow, SectionHeading, SettingsContent } from './primitives'

const CAPTION = 'text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)'

function Caption({ children, className }: { children: ReactNode; className?: string }) {
  return <p className={cn(CAPTION, className)}>{children}</p>
}

function ToggleRow(props: {
  checked: boolean
  description: string
  disabled?: boolean
  label: string
  onChange: (on: boolean) => void
}) {
  return (
    <ListRow
      action={
        <Switch
          aria-label={props.label}
          checked={props.checked}
          disabled={props.disabled}
          onCheckedChange={on => {
            triggerHaptic('selection')
            props.onChange(on)
          }}
        />
      }
      description={props.description}
      title={props.label}
    />
  )
}

export function VoicePresenceSettings({ onOpenVoiceConfig }: { onOpenVoiceConfig: () => void }) {
  const presenceEnabled = useStore($presenceEnabled)
  const glowEnabled = useStore($glowEnabled)
  const cardsEnabled = useStore($presenceCardsEnabled)

  return (
    <SettingsContent>
      <SectionHeading icon={Mic} title="Voice presence" />
      <Caption className="mb-2 leading-(--conversation-caption-line-height)">
        An always-on presence for Marvi: speak the wake word from anywhere and an Apple-Intelligence-style glow lights the
        screen edge as it listens, thinks, and speaks. It keeps working while Marvi is minimized to the system tray.
      </Caption>

      <ToggleRow
        checked={presenceEnabled}
        description="Listen for the wake word whenever Marvi is running — even minimized to the tray — and send what you say to the active chat. Turn this off to stop all background listening."
        label="Always-on voice presence"
        onChange={setPresenceEnabled}
      />

      <div className="my-1 h-px bg-border/30" />

      <ToggleRow
        checked={glowEnabled}
        description="Show the colored edge glow while listening, thinking, and speaking."
        disabled={!presenceEnabled}
        label="Edge glow"
        onChange={setGlowEnabled}
      />

      <ToggleRow
        checked={cardsEnabled}
        description="Let Marvi surface short cards and approval prompts on the glow (from the show_card tool)."
        disabled={!presenceEnabled}
        label="Show cards on the presence"
        onChange={setPresenceCardsEnabled}
      />

      <div className="my-1 h-px bg-border/30" />

      <ListRow
        action={
          <Button className="gap-1.5" onClick={onOpenVoiceConfig} size="sm" type="button" variant="outline">
            <Settings2 className="size-3.5" />
            Open voice settings
          </Button>
        }
        description="Set the wake phrase, speech-to-text, and text-to-speech in the Voice settings."
        title="Wake phrase, speech & voice"
      />
    </SettingsContent>
  )
}
