import { useStore } from '@nanostores/react'
import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  enrollVoiceSpeaker,
  getVoiceSpeakers,
  removeVoiceSpeaker,
  type VoiceSpeaker
} from '@/hermes'
import { triggerHaptic } from '@/lib/haptics'
import { Loader2, Mic, Settings2, Trash2 } from '@/lib/icons'
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

import { Caption, DebouncedField, ListRow, Pill, SectionHeading, SettingsContent, ToggleRow } from './primitives'
import type { useMarviConfig } from './subconscious/use-marvi-config'

function clampThreshold(value: string, fallback: number): number {
  const n = Number.parseFloat(value)

  return Number.isFinite(n) ? Math.max(0, Math.min(1, n)) : fallback
}

export function VoicePresenceSettings({
  marvi,
  onOpenModelConfig,
  onOpenVoiceConfig
}: {
  marvi: ReturnType<typeof useMarviConfig>
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
  const speakerIdThreshold = marvi.get('voice.speaker_id.threshold', 0.45)
  const requireOwnerForEscalation = marvi.get('voice.speaker_id.require_owner_for_escalation', true)

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
        An always-on presence for Marvi: talk from anywhere (wake word — see the Wake Word tab) and a Dynamic Island
        appears at the top of the screen as it listens, thinks, and speaks. Runs through the duplex voice session
        when reachable, falling back to the classic pipeline otherwise. Keeps working while Marvi is minimized to
        the system tray.
      </Caption>

      <ToggleRow
        checked={presenceEnabled}
        description="Listen whenever Marvi is running — even minimized to the tray — and send what you say to the active chat. Turn this off to stop all background listening."
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
        description="Set speech-to-text and text-to-speech in the Voice settings."
        title="Speech & voice"
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
      <Caption>
        Passive — active the moment a speaker is enrolled below, with no separate switch. Record five seconds of
        clear speech; the first enrolled speaker becomes the owner.
      </Caption>

      <ListRow
        action={
          <Pill tone={speakers.length ? 'primary' : 'muted'}>
            {speakers.length ? `Active — ${speakers.length} enrolled` : 'Inactive — none enrolled'}
          </Pill>
        }
        description="Every voice surface (island, hands-free overlay, composer) shows a small badge when speech is attributed to a guest or unknown voice."
        title="Speaker ID"
      />

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

      <ListRow
        action={
          <DebouncedField
            onCommit={value => void marvi.patch('voice.speaker_id.threshold', clampThreshold(value, 0.45))}
            type="number"
            value={String(speakerIdThreshold)}
          />
        }
        description="Minimum enrolled-speaker similarity. Raise it to reduce false owner matches."
        title="Match threshold"
      />

      <ToggleRow
        checked={requireOwnerForEscalation}
        description="Only an enrolled owner may hand a voice request to the tool-enabled reasoning model. Non-owner speech still gets instant-lane answers."
        label="Require owner for escalation"
        onChange={value => void marvi.patch('voice.speaker_id.require_owner_for_escalation', value)}
      />
    </SettingsContent>
  )
}
