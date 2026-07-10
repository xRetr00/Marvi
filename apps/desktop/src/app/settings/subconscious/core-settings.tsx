import type { ReactNode } from 'react'

import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { triggerHaptic } from '@/lib/haptics'
import { Activity, Brain } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'

import { CONTROL_TEXT } from '../constants'
import { ListRow, Pill, SectionHeading } from '../primitives'

import { disableSubconscious, enableSubconscious, pausePresence, setupPresence } from './activation-service'
import { StringListEditor } from './string-list-editor'
import { TierMatrix } from './tier-matrix'
import type { TierMap } from './types'
import { useActivityWatchStatus } from './use-activitywatch-status'
import type { useMarviConfig } from './use-marvi-config'

const CAPTION = 'text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)'

function Caption({ children }: { children: ReactNode }) {
  return <p className={cn(CAPTION, 'mb-2 leading-(--conversation-caption-line-height)')}>{children}</p>
}

function ToggleRow(props: { checked: boolean; description: string; disabled?: boolean; label: string; onChange: (on: boolean) => void }) {
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

/** Debounced-on-blur text field for config strings that shouldn't save per keystroke (interval, minutes). */
function DebouncedField({
  value,
  onCommit,
  placeholder,
  type = 'text',
  disabled
}: {
  value: string
  onCommit: (next: string) => void
  placeholder?: string
  type?: 'number' | 'text'
  disabled?: boolean
}) {
  return (
    <Input
      className={cn('max-w-32', CONTROL_TEXT)}
      defaultValue={value}
      disabled={disabled}
      key={value}
      onBlur={e => onCommit(e.target.value)}
      onKeyDown={e => {
        if (e.key === 'Enter') {
          e.currentTarget.blur()
        }
      }}
      placeholder={placeholder}
      type={type}
    />
  )
}

export function SubconsciousCoreSettings({ marvi }: { marvi: ReturnType<typeof useMarviConfig> }) {
  const enabled = marvi.get('subconscious.enabled', false)
  const interval = marvi.get('subconscious.interval', '20m')
  const idleTrigger = marvi.get('subconscious.idle_trigger_minutes', 15)
  const tiers = marvi.get<TierMap>('subconscious.tiers', {})

  return (
    <>
      <SectionHeading icon={Brain} title="Subconscious" />
      <Caption>
        Marvi diffs your world on a cron tick — goals, overnight changes, calendar — and acts or suggests without
        waiting for a prompt. No LLM call unless something actually changed.
      </Caption>

      <ToggleRow
        checked={enabled}
        description="Run a periodic tick that reasons over what changed and proactively messages or suggests."
        label="Enable subconscious"
        // Not a raw config patch: the tick's cron job only exists via the
        // enable/disable endpoints (cron/subconscious.py), which flip
        // `subconscious.enabled` themselves as part of creating/pausing it.
        onChange={value =>
          void marvi.activate(
            'subconscious.enabled',
            value,
            () => (value ? enableSubconscious(String(interval).trim() || undefined) : disableSubconscious()),
            value ? 'Failed to enable the subconscious tick' : 'Failed to disable the subconscious tick'
          )
        }
      />

      <ListRow
        action={
          <DebouncedField
            disabled={!enabled}
            onCommit={value => void marvi.patch('subconscious.interval', value.trim() || '20m')}
            placeholder="20m"
            value={String(interval)}
          />
        }
        description="Tick cadence, e.g. 20m, 1h. Backs off automatically when quiet."
        title="Tick interval"
      />

      <ListRow
        action={
          <DebouncedField
            disabled={!enabled}
            onCommit={value => {
              const n = Number.parseInt(value, 10)
              void marvi.patch('subconscious.idle_trigger_minutes', Number.isFinite(n) && n >= 0 ? n : 15)
            }}
            type="number"
            value={String(idleTrigger)}
          />
        }
        description="Fire one tick after this many minutes of silence following an active session."
        title="Idle trigger"
      />

      <ListRow
        below={
          <div className="mt-2">
            <TierMatrix
              disabled={!enabled}
              onChange={next => void marvi.patch('subconscious.tiers', next)}
              tiers={tiers}
            />
          </div>
        }
        description="Per-category proactivity: notify only, propose (one-tap accept), or auto (pre-approved)."
        title="Proactivity tiers"
      />
    </>
  )
}

export function PresenceSettings({ marvi }: { marvi: ReturnType<typeof useMarviConfig> }) {
  const enabled = marvi.get('presence.enabled', false)
  const flowGating = marvi.get('presence.flow_gating', true)
  const shoulderTaps = marvi.get('presence.goblin.shoulder_taps', false)
  const sessionPriming = marvi.get('presence.goblin.session_priming', false)
  const denylist = marvi.get<string[]>('presence.denylist', [])
  const aw = useActivityWatchStatus()

  return (
    <>
      <SectionHeading icon={Activity} title="Presence" />
      <Caption>
        Marvi can see what you're doing on this desktop — foreground app, window titles, now-playing — via
        ActivityWatch, a local collector. Raw data never leaves this machine; only distilled summaries reach memory.
      </Caption>

      <ListRow
        action={
          <Pill tone={aw.reachable ? 'primary' : 'muted'}>
            {aw.checking && !aw.checked ? 'Checking…' : aw.reachable ? 'ActivityWatch reachable' : 'ActivityWatch not running'}
          </Pill>
        }
        description="Marvi's desktop collector, expected at localhost:5600."
        title="ActivityWatch"
      />

      <ToggleRow
        checked={enabled}
        description="Let Marvi read local desktop activity for context and distillation."
        label="Enable presence"
        // Not a raw config patch: presence activation (media watcher,
        // distiller cron job) only happens via the setup/pause endpoints
        // (hermes_cli/presence_cmd.py), which flip `presence.enabled`
        // themselves. A resolved-but-not-ok result means the backend still
        // flipped the config but one step degraded (e.g. distiller job
        // creation failed) — keep the toggle, surface the detail.
        onChange={value =>
          void marvi.activate(
            'presence.enabled',
            value,
            async () => {
              const result = value ? await setupPresence() : await pausePresence()

              if (!result.ok) {
                const detail = ('job_message' in result ? result.job_message : result.message) || 'Unknown error'
                notifyError(new Error(detail), value ? 'Presence setup reported a problem' : 'Failed to stop the presence watcher')
              }
            },
            value ? 'Failed to set up presence' : 'Failed to pause presence'
          )
        }
      />

      <ToggleRow
        checked={flowGating}
        description="Hold proactive messages while you're heads-down in a focus app; flush on idle or context switch."
        disabled={!enabled}
        label="Flow-aware delivery"
        onChange={value => void marvi.patch('presence.flow_gating', value)}
      />

      <ToggleRow
        checked={shoulderTaps}
        description="Opt-in: detect being stuck (same file 45+ min, error-ish titles, rapid tab-switching) and offer to help."
        disabled={!enabled}
        label="Goblin mode — shoulder taps"
        onChange={value => void marvi.patch('presence.goblin.shoulder_taps', value)}
      />

      <ToggleRow
        checked={sessionPriming}
        description="Opt-in: prime new chat sessions with a summary of the last hour's activity — zero cold start."
        disabled={!enabled}
        label="Goblin mode — session priming"
        onChange={value => void marvi.patch('presence.goblin.session_priming', value)}
      />

      <ListRow
        below={
          <div className="mt-2">
            <StringListEditor
              disabled={!enabled}
              emptyLabel="Empty — nothing is filtered by default."
              onChange={next => void marvi.patch('presence.denylist', next)}
              placeholder="Title substring to strip"
              values={denylist}
            />
          </div>
        }
        description="Window/app titles matching these are stripped before reaching the LLM or memory."
        title="Denylist"
      />
    </>
  )
}
