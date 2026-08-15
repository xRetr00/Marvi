import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import {
  enrollVoiceSpeaker,
  getAuxiliaryModels,
  getGlobalModelOptions,
  getVoiceInstantStatus,
  getVoiceSpeakers,
  type ModelOptionProvider,
  removeVoiceSpeaker,
  setModelAssignment,
  type VoiceInstantStatusResponse,
  type VoiceSpeaker
} from '@/hermes'
import { triggerHaptic } from '@/lib/haptics'
import { Loader2, Mic, Settings2, Trash2 } from '@/lib/icons'
import { notifyError } from '@/store/notifications'
import {
  $islandEnabled,
  $islandPosition,
  $presenceCardsEnabled,
  $presenceEnabled,
  $voicePresenceDebug,
  setIslandEnabled,
  setIslandPosition,
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

const SPEAKER_ID_MODELS = [
  { id: 'wespeaker-en-voxceleb-cam++', label: 'WeSpeaker CAM++ (fast)' },
  { id: 'wespeaker-en-voxceleb-resnet293-lm', label: 'WeSpeaker ResNet293 (stronger)' },
  { id: '3dspeaker-eres2net-en-voxceleb', label: '3D-Speaker ERes2Net' }
] as const

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
  const islandPosition = useStore($islandPosition)
  const cardsEnabled = useStore($presenceCardsEnabled)
  const debugEnabled = useStore($voicePresenceDebug)
  const [speakerName, setSpeakerName] = useState('Owner')
  const [speakers, setSpeakers] = useState<VoiceSpeaker[]>([])
  const [enrolling, setEnrolling] = useState(false)
  const [instantProviders, setInstantProviders] = useState<ModelOptionProvider[]>([])
  const [instantProvider, setInstantProvider] = useState('')
  const [instantModel, setInstantModel] = useState('')
  const [instantStatus, setInstantStatus] = useState<VoiceInstantStatusResponse | null>(null)
  const [instantLoading, setInstantLoading] = useState(true)
  const [instantSaving, setInstantSaving] = useState(false)
  const [instantError, setInstantError] = useState('')
  const captureRef = useRef<DuplexMicCapture | null>(null)
  const speakerIdThreshold = marvi.get('voice.speaker_id.threshold', 0.45)
  const speakerIdModel = marvi.get<string>('voice.speaker_id.model', 'wespeaker-en-voxceleb-cam++')
  const voiceFocusMode = marvi.get<string>('voice.speaker_id.focus_mode', 'owner')

  const loadInstantModel = useCallback(async () => {
    setInstantLoading(true)
    setInstantError('')

    try {
      const [options, assignments, status] = await Promise.all([
        getGlobalModelOptions(),
        getAuxiliaryModels(),
        getVoiceInstantStatus()
      ])

      const ready = (options.providers ?? []).filter(
        provider => provider.authenticated !== false && (provider.models?.length ?? 0) > 0
      )

      const current = assignments.tasks.find(task => task.task === 'voice_instant')
      const provider = current?.provider && current.provider !== 'auto' ? current.provider : assignments.main.provider
      const model = current?.model || assignments.main.model

      setInstantProviders(ready)
      setInstantProvider(provider)
      setInstantModel(model)
      setInstantStatus(status)
    } catch (error) {
      setInstantError(error instanceof Error ? error.message : String(error))
    } finally {
      setInstantLoading(false)
    }
  }, [])

  const instantProviderModels = useMemo(() => {
    const models = instantProviders.find(provider => provider.slug === instantProvider)?.models ?? []

    return instantModel && !models.includes(instantModel) ? [instantModel, ...models] : models
  }, [instantModel, instantProvider, instantProviders])

  const applyInstantModel = async () => {
    if (!instantProvider || !instantModel || instantSaving) {
      return
    }

    setInstantSaving(true)
    setInstantError('')

    try {
      const providerRow = instantProviders.find(provider => provider.slug === instantProvider)

      const result = await setModelAssignment({
        model: instantModel,
        provider: instantProvider,
        scope: 'auxiliary',
        task: 'voice_instant',
        ...(providerRow?.api_url ? { base_url: providerRow.api_url } : {})
      })

      if (!result.ok) {
        throw new Error(result.confirm_message || 'The instant voice model was not saved.')
      }

      await loadInstantModel()
      triggerHaptic('success')
    } catch (error) {
      setInstantError(error instanceof Error ? error.message : String(error))
    } finally {
      setInstantSaving(false)
    }
  }

  useEffect(() => {
    void loadInstantModel()
    void Promise.resolve()
      .then(getVoiceSpeakers)
      .then(result => {
        setSpeakers(result.speakers)
        setSpeakerName(current =>
          current === 'Owner' ? (result.speakers.find(speaker => speaker.is_owner)?.name ?? current) : current
        )
      })
      .catch(() => undefined)

    return () => captureRef.current?.stop()
  }, [loadInstantModel])

  const enrollSpeaker = async (nameOverride?: string) => {
    const name = (nameOverride ?? speakerName).trim()

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
        appears at the top of the screen as it listens, thinks, and speaks. Runs through the duplex voice session when
        reachable, falling back to the classic pipeline otherwise. Keeps working while Marvi is minimized to the system
        tray.
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

      <ListRow
        action={
          <SegmentedControl
            className="w-full"
            onChange={position => {
              triggerHaptic('selection')
              setIslandPosition(position)
            }}
            options={[
              { id: 'left', label: 'Left' },
              { id: 'center', label: 'Center' },
              { id: 'right', label: 'Right' }
            ]}
            value={islandPosition}
          />
        }
        description="Choose where the island docks along the top edge of this display."
        title="Island position"
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
          <div className="flex max-w-full flex-wrap items-center justify-end gap-2">
            <Select
              disabled={instantLoading || instantSaving || !instantProviders.length}
              onValueChange={provider => {
                const models = instantProviders.find(row => row.slug === provider)?.models ?? []
                setInstantProvider(provider)
                setInstantModel(models[0] ?? '')
              }}
              value={instantProvider}
            >
              <SelectTrigger aria-label="Instant voice provider" className="min-w-36">
                <SelectValue placeholder="Provider" />
              </SelectTrigger>
              <SelectContent>
                {instantProviders.map(provider => (
                  <SelectItem key={provider.slug} value={provider.slug}>
                    {provider.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              disabled={instantLoading || instantSaving || !instantProviderModels.length}
              onValueChange={setInstantModel}
              value={instantModel}
            >
              <SelectTrigger aria-label="Instant voice model" className="min-w-52 max-w-72">
                <SelectValue placeholder="Model" />
              </SelectTrigger>
              <SelectContent>
                {instantProviderModels.map(model => (
                  <SelectItem key={model} value={model}>
                    {model}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button
              disabled={instantLoading || instantSaving || !instantProvider || !instantModel}
              onClick={() => void applyInstantModel()}
              size="sm"
              type="button"
            >
              {instantSaving && <Loader2 className="size-3.5 animate-spin" />}
              Apply
            </Button>
            <Button className="gap-1.5" onClick={onOpenModelConfig} size="sm" type="button" variant="outline">
              <Settings2 className="size-3.5" />
              All models
            </Button>
          </div>
        }
        description={
          <span className="flex flex-col gap-0.5">
            <span>
              Choose the fast auxiliary model that answers first in duplex voice mode. Applies to new sessions.
            </span>
            {instantStatus?.resolved && (
              <span className="font-mono text-[0.68rem] text-muted-foreground">
                Currently using: {instantStatus.provider} · {instantStatus.model}
                {instantStatus.is_fallback ? ' (fallback)' : ''}
              </span>
            )}
            {instantError && <span className="text-destructive">{instantError}</span>}
          </span>
        }
        title="Instant voice model"
      />

      <div className="my-1 h-px bg-border/30" />

      <SectionHeading icon={Mic} title="Speaker recognition" />
      <Caption>
        Record several independent five-second samples. Marvi marks a profile ready after three samples agree; use
        different sentences and normal speaking distance. The first enrolled speaker becomes the owner.
      </Caption>

      <ListRow
        action={
          <Pill tone={speakers.some(speaker => speaker.is_owner && speaker.ready) ? 'primary' : 'muted'}>
            {speakers.some(speaker => speaker.is_owner && speaker.ready)
              ? `Ready — ${speakers.length} enrolled`
              : speakers.length
                ? 'Learning voice'
                : 'Inactive — none enrolled'}
          </Pill>
        }
        description="Every duplex voice surface (island, hands-free overlay, composer) shows the resolved owner, guest, or unknown speaker badge."
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
      {speakers.some(speaker => speaker.model_mismatch) && (
        <div className="flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          Re-enroll needed — the speaker-ID model changed since these samples were captured. Old embeddings can&apos;t
          be compared against the new model; add a fresh sample for each speaker below.
        </div>
      )}
      {speakers.length ? (
        <div className="grid gap-1">
          {speakers.map(speaker => (
            <ListRow
              action={
                <div className="flex items-center gap-1">
                  <Button
                    aria-label={`Add voice sample for ${speaker.name}`}
                    disabled={enrolling}
                    onClick={() => void enrollSpeaker(speaker.name)}
                    size="sm"
                    type="button"
                    variant="outline"
                  >
                    <Mic className="size-3.5" />
                    Add sample
                  </Button>
                  <Button
                    aria-label={`Remove ${speaker.name}`}
                    onClick={() => void removeSpeaker(speaker.name)}
                    size="icon-sm"
                    type="button"
                    variant="ghost"
                  >
                    <Trash2 className="size-3.5" />
                  </Button>
                </div>
              }
              description={
                (speaker.ready
                  ? `Ready · ${speaker.embeddings} samples · ${Math.round((speaker.consistency ?? 0) * 100)}% consistency`
                  : speaker.samples_needed
                    ? `Needs ${speaker.samples_needed} more independent sample${speaker.samples_needed === 1 ? '' : 's'}`
                    : `Samples disagree (${Math.round((speaker.consistency ?? 0) * 100)}%) · add a clearer sample`) +
                (speaker.adaptive ? ` · +${speaker.adaptive} self-learned` : '') +
                (speaker.model_mismatch ? ' · re-enroll needed (model changed)' : '')
              }
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
          <Select onValueChange={model => void marvi.patch('voice.speaker_id.model', model)} value={speakerIdModel}>
            <SelectTrigger className="min-w-64">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {SPEAKER_ID_MODELS.map(model => (
                <SelectItem key={model.id} value={model.id}>
                  {model.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        }
        description="Choose the local voice-embedding model. Changing it requires fresh enrollment samples."
        title="Speaker-ID model"
      />

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
        checked={voiceFocusMode !== 'off'}
        description="Focus on the enrolled owner's voice when other people are talking nearby: other speakers' utterances are shown but ignored, and barge-in requires the owner's voice. Not a security or access control -- every speaker can still ask Marvi anything."
        label="Voice focus"
        onChange={value => void marvi.patch('voice.speaker_id.focus_mode', value ? 'owner' : 'off')}
      />
    </SettingsContent>
  )
}
