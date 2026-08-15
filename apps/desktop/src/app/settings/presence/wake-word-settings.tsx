import { Mic } from '@/lib/icons'

import { Caption, DebouncedField, ListRow, Pill, SectionHeading, ToggleRow } from '../primitives'
import { StringListEditor } from '../subconscious/string-list-editor'
import type { useMarviConfig } from '../subconscious/use-marvi-config'

const DEFAULT_PHRASES = ['hey marvi', 'marvi', 'marve', 'marvy', 'marvie', 'marfi', 'marfe', 'marvey']

function clampFloat(value: string, fallback: number, min: number, max: number): number {
  const n = Number.parseFloat(value)

  return Number.isFinite(n) ? Math.max(min, Math.min(max, n)) : fallback
}

function clampInt(value: string, fallback: number, min: number, max: number): number {
  const n = Number.parseInt(value, 10)

  return Number.isFinite(n) ? Math.max(min, Math.min(max, n)) : fallback
}

/**
 * Settings → Presence → Wake Word tab. Say-the-phrase detection that lights
 * the Dynamic Island and starts a turn; unrelated to speaker ID (Presence → Voice).
 * that actually carries the conversation once woken (desktop-controller.tsx).
 */
export function WakeWordSettings({ marvi }: { marvi: ReturnType<typeof useMarviConfig> }) {
  const enabled = marvi.get('voice.wake_word.enabled', false)
  const phrases = marvi.get<string[]>('voice.wake_word.phrases', DEFAULT_PHRASES)
  const threshold = marvi.get('voice.wake_word.threshold', 0.5)
  const boost = marvi.get('voice.wake_word.boost', 4)
  const debug = marvi.get('voice.wake_word.debug', false)
  const commandTimeoutMs = marvi.get('voice.wake_word.command_timeout_ms', 8000)
  const cooldownMs = marvi.get('voice.wake_word.cooldown_ms', 1200)

  const setEnabled = (value: boolean) => {
    void marvi.patch('voice.wake_word.enabled', value)

    if (value) {
      void marvi.patch('voice.wake_word.provider', 'livekit')
      void marvi.patch('voice.wake_word.model', 'livekit-marvi')
    }
  }

  return (
    <>
      <SectionHeading icon={Mic} title="Wake word" />
      <Caption>
        Say the wake phrase from anywhere to summon Marvi — on-device detection via LiveKit, separate from speaker
        ID and the conversation itself.
      </Caption>

      <ToggleRow
        checked={enabled}
        description="Listen for the wake phrase whenever voice presence is on."
        label="Enable wake word"
        onChange={setEnabled}
      />

      <ListRow
        action={<Pill tone="primary">LiveKit</Pill>}
        description="Marvi's on-device wake-word detector."
        title="Provider"
      />

      <ListRow
        below={
          <div className="mt-2">
            <StringListEditor
              disabled={!enabled}
              emptyLabel="No phrases configured — falls back to the built-in defaults."
              onChange={next => void marvi.patch('voice.wake_word.phrases', next)}
              placeholder="Phrase or common misrecognition"
              values={phrases}
            />
          </div>
        }
        description="Phrases (and common misrecognitions) that wake the one-shot command pipeline."
        title="Wake phrases"
      />

      <ListRow
        action={
          <DebouncedField
            disabled={!enabled}
            onCommit={value => void marvi.patch('voice.wake_word.threshold', clampFloat(value, 0.5, 0, 1))}
            type="number"
            value={String(threshold)}
          />
        }
        description="Higher values reduce false wakes but may miss valid wake phrases."
        title="Threshold"
      />

      <ListRow
        action={
          <DebouncedField
            disabled={!enabled}
            onCommit={value => void marvi.patch('voice.wake_word.boost', clampFloat(value, 4, 0, 20))}
            type="number"
            value={String(boost)}
          />
        }
        description="Keyword score boost. Increase only if valid wake phrases are missed."
        title="Boost"
      />

      <ListRow
        action={
          <DebouncedField
            disabled={!enabled}
            onCommit={value => void marvi.patch('voice.wake_word.command_timeout_ms', clampInt(value, 8000, 1000, 30000))}
            type="number"
            value={String(commandTimeoutMs)}
          />
        }
        description="Maximum time to keep listening for the command after the wake phrase."
        title="Command timeout (ms)"
      />

      <ListRow
        action={
          <DebouncedField
            disabled={!enabled}
            onCommit={value => void marvi.patch('voice.wake_word.cooldown_ms', clampInt(value, 1200, 0, 10000))}
            type="number"
            value={String(cooldownMs)}
          />
        }
        description="Delay before wake listening arms again after one command finishes."
        title="Cooldown (ms)"
      />

      <ToggleRow
        checked={debug}
        description="Write extra wake-word tuning logs for frames, energy, starts, and detections."
        label="Debug logs"
        onChange={value => void marvi.patch('voice.wake_word.debug', value)}
      />
    </>
  )
}
