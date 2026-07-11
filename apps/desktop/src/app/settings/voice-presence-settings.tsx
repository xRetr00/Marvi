import { useStore } from '@nanostores/react'
import { type ReactNode, useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import {
  enrollVoiceSpeaker,
  getVoiceSpeakers,
  removeVoiceSpeaker,
  type VoiceSpeaker
} from '@/hermes'
import { triggerHaptic } from '@/lib/haptics'
import { Loader2, Mic, Settings2, Trash2 } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'
import {
  $islandEnabled,
  $presenceCardsEnabled,
  $presenceEnabled,
  $voicePresenceDebug,
  setIslandEnabled,
  setPresenceCardsEnabled,
  setPresenceEnabled,
  setVoicePresenceDebug
} from '@/store/voice-presence-settings'

import { type DuplexMicCapture, startDuplexMicCapture } from '../voice-island/duplex-audio'

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

export function VoicePresenceSettings({
  onOpenModelConfig,
  onOpenVoiceConfig
}: {
  onOpenModelConfig: () => void
  onOpenVoiceConfig: () => void
}) {
  const presenceEnabled = useStore($presenceEnabled)
  const islandEnabled = useStore($islandEnabled)
  const cardsEnabled = useStore($presenceCardsEnabled)
  const debugEnabled = useStore($voicePresenceDebug)
  const [speakerName, setSpeakerName] = useState('Owner')
  const [speakers, setSpeakers] = useState<VoiceSpeaker[]>([])
  const [enrolling, setEnrolling] = useState(false)
  const captureRef = useRef<DuplexMicCapture | null>(null)

  useEffect(() => {
    void Promise.resolve()
      .then(getVoiceSpeakers)
      .then(result => setSpeakers(result.speakers))
      .catch(() => undefined)

    return () => captureRef.current?.stop()
  }, [])

  const enrollSpeaker = async () => {
    const name = speakerName.trim()

    if (!name || enrolling) {
      return
    }

    setEnrolling(true)
    const audio: string[] = []

    try {
      captureRef.current = await startDuplexMicCapture({ onFrame: chunk => audio.push(chunk) })
      await new Promise(resolve => window.setTimeout(resolve, 5_000))
      captureRef.current.stop()
      captureRef.current = null
      const result = await enrollVoiceSpeaker(name, audio)
      setSpeakers(result.speakers)
      triggerHaptic('success')
    } catch (error) {
      captureRef.current?.stop()
      captureRef.current = null
      notifyError(error, 'Speaker enrollment failed')
    } finally {
      setEnrolling(false)
    }
  }

  const removeSpeaker = async (name: string) => {

    try {
      const result = await removeVoiceSpeaker(name)
      setSpeakers(result.speakers)
    } catch (error) {
      notifyError(error, 'Could not remove speaker')
    }
  }

  return (
    <SettingsContent>
      <SectionHeading icon={Mic} title="Voice presence" />
      <Caption className="mb-2 leading-(--conversation-caption-line-height)">
        An always-on presence for Marvi: speak the wake word from anywhere and a Dynamic Island appears at the top of the
        screen as it listens, thinks, and speaks. It keeps working while Marvi is minimized to the system tray.
      </Caption>

      <ToggleRow
        checked={presenceEnabled}
        description="Listen for the wake word whenever Marvi is running — even minimized to the tray — and send what you say to the active chat. Turn this off to stop all background listening."
        label="Always-on voice presence"
        onChange={setPresenceEnabled}
      />

      <div className="my-1 h-px bg-border/30" />

      <ToggleRow
        checked={islandEnabled}
        description="Show the Dynamic Island during wake-word presence and explicit voice conversations."
        label="Show island"
        onChange={setIslandEnabled}
      />

      <ToggleRow
        checked={cardsEnabled}
        description="Let Marvi surface short cards and approval prompts on the island (from the show_card tool)."
        disabled={!presenceEnabled}
        label="Show cards on the presence"
        onChange={setPresenceCardsEnabled}
      />

      <ToggleRow
        checked={debugEnabled}
        description="Print detailed [voice-presence] logs to the developer console for troubleshooting the wake word, island, and cards."
        label="Debug logs"
        onChange={setVoicePresenceDebug}
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

      <ListRow
        action={
          <Button className="gap-1.5" onClick={onOpenModelConfig} size="sm" type="button" variant="outline">
            <Settings2 className="size-3.5" />
            Choose model
          </Button>
        }
        description="Choose the fast auxiliary model that answers first in duplex voice mode."
        title="Instant voice model"
      />

      <div className="my-1 h-px bg-border/30" />

      <SectionHeading icon={Mic} title="Speaker recognition" />
      <Caption>Record five seconds of clear speech. The first enrolled speaker becomes the owner.</Caption>
      <div className="flex items-center gap-2">
        <Input
          aria-label="Speaker name"
          disabled={enrolling}
          onChange={event => setSpeakerName(event.target.value)}
          placeholder="Speaker name"
          value={speakerName}
        />
        <Button disabled={enrolling || !speakerName.trim()} onClick={() => void enrollSpeaker()} type="button">
          {enrolling ? <Loader2 className="size-3.5 animate-spin" /> : <Mic className="size-3.5" />}
          {enrolling ? 'Recording…' : 'Enroll'}
        </Button>
      </div>
      {speakers.length ? (
        <div className="grid gap-1">
          {speakers.map(speaker => (
            <ListRow
              action={
                <Button
                  aria-label={`Remove ${speaker.name}`}
                  onClick={() => void removeSpeaker(speaker.name)}
                  size="icon-sm"
                  type="button"
                  variant="ghost"
                >
                  <Trash2 className="size-3.5" />
                </Button>
              }
              description={`${speaker.embeddings} voice sample${speaker.embeddings === 1 ? '' : 's'}`}
              key={speaker.name}
              title={`${speaker.name}${speaker.is_owner ? ' · owner' : ''}`}
            />
          ))}
        </div>
      ) : (
        <Caption>No speakers enrolled.</Caption>
      )}
    </SettingsContent>
  )
}
