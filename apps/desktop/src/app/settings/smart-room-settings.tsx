import { useCallback, useEffect, useId, useRef, useState } from 'react'

import { Switch } from '@/components/ui/switch'
import {
  acknowledgeSmartRoomAlarm,
  applySmartRoomConfig,
  cancelSmartRoomSleep,
  deleteSmartRoomAlarm,
  deleteSmartRoomFace,
  enrollSmartRoomFace,
  getHermesConfigRecord,
  getSmartRoomFaces,
  getSmartRoomStatus,
  getSmartRoomVisionPreview,
  observeSmartRoomVision,
  reviewSmartRoomFace,
  saveHermesConfig,
  saveSmartRoomAlarm,
  saveSmartRoomSecrets,
  setSmartRoomLight,
  setSmartRoomMode,
  setSmartRoomOverride,
  type SmartRoomAlarm,
  type SmartRoomFaces,
  type SmartRoomStatus,
  type SmartRoomVisionPreview,
  testSmartRoomWelcome
} from '@/hermes'
import { useI18n } from '@/i18n'
import {
  AlertTriangle,
  AudioLines,
  Box,
  Brain,
  Cloud,
  Monitor,
  Moon,
  RefreshCw,
  Settings2,
  SlidersHorizontal,
  Zap
} from '@/lib/icons'
import { notify, notifyError } from '@/store/notifications'

import { CONTROL_TEXT } from './constants'
import { SettingsContent } from './primitives'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface SmartRoomConfig {
  enabled: boolean
  owner: string
  welcome: {
    enabled: boolean
    identity_grace_seconds: number
    reset_after_seconds: number
    owner_evidence_window_seconds: number
  }
  sound_events: {
    enabled: boolean
    sleep_enabled: boolean
    confidence: number
    min_peak: number
    input_device: null | string
  }
  mqtt: { broker: string; port: number }
  context: { enabled: boolean }
  subconscious: { enabled: boolean }
  vision: {
    enabled: boolean
    camera_index: number
    camera_name: string
    width: number
    height: number
    standby_fps: number
    active_fps: number
    dark_brightness: number
    gestures: {
      enabled: boolean
      wake_gesture: string
      armed_seconds: number
      hold_seconds: number
      confidence: number
      mapping: Record<string, { command: string; [key: string]: string | number }>
    }
    faces: { min_enrollment_samples: number; match_threshold: number }
    sleep: { settling_seconds: number; likely_sleeping_seconds: number }
    deep: { enabled: boolean; provider: string; model: string; timeout: number }
    history: { retention_hours: number; max_events: number }
  }
  tuya: {
    worker: { timeout_seconds: number; retries: number; queue_size: number }
    bulb: {
      ip: string
      device_id: string
      protocol: string
      brightness_max: number
      color_temp_max: number
      dps: { switch: number; brightness: number; color_temp: number; color: number }
    }
    he20: { ip: string; device_id: string; protocol: string; presence_dp: number; occupied_values: string[] }
  }
  esp32: {
    ip: string
    room_id: string
    owner_device_id: string
    presence_topic: string
    status_topic: string
    rssi_enter_threshold: number
    rssi_exit_threshold: number
    enter_debounce_seconds: number
    exit_timeout: number
    missing_timeout_seconds: number
  }
  presence: {
    wifi_ping: { enabled: boolean; ip: string; interval_seconds: number }
  }
  owntracks: {
    topic: string
    zones: string[]
  }
  automations: {
    adaptive_light: { enabled: boolean; auto_off: boolean; debounce: number }
    evening_sleep: { enabled: boolean; time: string }
    work_return: { enabled: boolean; work_hours_start: string; work_hours_end: string; settle_delay: number }
    daily_reset: string
  }
  scenes: Record<
    string,
    {
      color_temp?: number
      brightness?: number
      transition?: number
      rgb?: number[]
      flash?: boolean
      flash_interval?: number
    }
  >
}

const DEFAULT_CONFIG: SmartRoomConfig = {
  enabled: false,
  owner: 'Shereef',
  welcome: {
    enabled: true,
    identity_grace_seconds: 4,
    reset_after_seconds: 3600,
    owner_evidence_window_seconds: 3600
  },
  sound_events: { enabled: false, sleep_enabled: false, confidence: 0.15, min_peak: 0.04, input_device: null },
  mqtt: { broker: '127.0.0.1', port: 1883 },
  context: { enabled: true },
  subconscious: { enabled: true },
  vision: {
    enabled: false,
    camera_index: 0,
    camera_name: 'Smart Room camera',
    width: 1280,
    height: 720,
    standby_fps: 1.5,
    active_fps: 8,
    dark_brightness: 28,
    gestures: {
      enabled: true,
      wake_gesture: 'Open_Palm',
      armed_seconds: 8,
      hold_seconds: 0.65,
      confidence: 0.65,
      mapping: {
        Thumb_Up: { command: 'brightness_up', step: 15 },
        Thumb_Down: { command: 'brightness_down', step: 15 },
        Closed_Fist: { command: 'cancel' },
        Victory: { command: 'voice_mode' },
        Pointing_Up: { command: 'toggle_light' },
        ILoveYou: { command: 'set_mode', mode: 'relax' }
      }
    },
    faces: { min_enrollment_samples: 8, match_threshold: 0.42 },
    sleep: { settling_seconds: 120, likely_sleeping_seconds: 600 },
    deep: { enabled: true, provider: '', model: '', timeout: 30 },
    history: { retention_hours: 72, max_events: 2000 }
  },
  tuya: {
    worker: { timeout_seconds: 4, retries: 1, queue_size: 16 },
    bulb: {
      ip: '',
      device_id: '',
      protocol: '3.3',
      brightness_max: 255,
      color_temp_max: 255,
      dps: { switch: 1, brightness: 2, color_temp: 3, color: 5 }
    },
    he20: {
      ip: '',
      device_id: '',
      protocol: '3.3',
      presence_dp: 1,
      occupied_values: ['true', '1', 'presence', 'occupied', 'pir', 'human']
    }
  },
  esp32: {
    ip: '192.168.1.172',
    room_id: 'smart_room',
    owner_device_id: '',
    presence_topic: 'espresense/devices/+/smart_room',
    status_topic: 'espresense/rooms/smart_room/#',
    rssi_enter_threshold: -70,
    rssi_exit_threshold: -85,
    enter_debounce_seconds: 3,
    exit_timeout: 300,
    missing_timeout_seconds: 30
  },
  presence: { wifi_ping: { enabled: false, ip: '', interval_seconds: 60 } },
  owntracks: { topic: 'owntracks/shereef/#', zones: ['home', 'university', 'bakery'] },
  automations: {
    adaptive_light: { enabled: true, auto_off: true, debounce: 3 },
    evening_sleep: { enabled: true, time: '18:00' },
    work_return: { enabled: true, work_hours_start: '06:00', work_hours_end: '10:00', settle_delay: 300 },
    daily_reset: '00:00'
  },
  scenes: {
    normal: { color_temp: 4000, brightness: 70, transition: 2 },
    reading: { color_temp: 3000, brightness: 70, transition: 2 },
    focus: { color_temp: 5000, brightness: 100, transition: 2 },
    relax: { color_temp: 2700, rgb: [255, 180, 80], brightness: 40, transition: 3 },
    night: { color_temp: 2200, rgb: [255, 120, 40], brightness: 15, transition: 3 },
    alarm: { color_temp: 6500, brightness: 100, flash: true, flash_interval: 500 }
  }
}

function mergeDefaults<T>(defaults: T, value: unknown): T {
  if (Array.isArray(defaults)) {
    return (Array.isArray(value) ? value : defaults) as T
  }

  if (defaults && typeof defaults === 'object') {
    const incoming = value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
    const fallbackObject = defaults as Record<string, unknown>
    const keys = new Set([...Object.keys(incoming), ...Object.keys(fallbackObject)])

    return Object.fromEntries(
      [...keys].map(key => [
        key,
        key in fallbackObject ? mergeDefaults(fallbackObject[key], incoming[key]) : incoming[key]
      ])
    ) as T
  }

  return (value === undefined || value === null ? defaults : value) as T
}

const MODES = [
  { id: 'normal', label: 'Normal', icon: '💡', desc: 'White 4000K @ 70%' },
  { id: 'reading', label: 'Reading', icon: '📖', desc: 'Warm 3000K @ 70%' },
  { id: 'focus', label: 'Focus', icon: '🧠', desc: 'Cool 5000K @ 100%' },
  { id: 'relax', label: 'Relax', icon: '😌', desc: 'Amber 2700K @ 40%' },
  { id: 'night', label: 'Night', icon: '🌙', desc: 'Dim warm light @ 15%' },
  { id: 'sleep', label: 'Sleep', icon: '😴', desc: 'Lights off, darkness' },
  { id: 'alarm', label: 'Alarm', icon: '🚨', desc: 'Flash bright white' },
  { id: 'off', label: 'Off', icon: '⏻', desc: 'Lights off' }
] as const

const AUTOMATIONS = [
  { key: 'adaptive_light', label: 'Adaptive Light (Presence)', desc: 'Turn on/off based on room presence' },
  { key: 'evening_sleep', label: 'Evening Sleep', desc: 'Auto sleep mode at 6 PM' },
  { key: 'work_return', label: 'Work Return Sleep', desc: 'Auto sleep when arriving home from work' }
] as const

const LIGHT_COLORS = ['#ff8c2a', '#ffd0a0', '#fff1dc', '#ffffff', '#7ca9ff', '#ad72ff', '#ed78d1', '#ff6d55']

function rgbToHex(rgb: number[] | null | undefined) {
  return rgb?.length === 3
    ? `#${rgb.map(value => Math.max(0, Math.min(255, value)).toString(16).padStart(2, '0')).join('')}`
    : '#ffffff'
}

function hexToRgb(hex: string) {
  return [1, 3, 5].map(offset => parseInt(hex.slice(offset, offset + 2), 16))
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function StatusDot({ online }: { online: boolean }) {
  return <span className={`inline-block h-2 w-2 rounded-full ${online ? 'bg-emerald-500' : 'bg-red-500/60'}`} />
}

function Toggle({
  checked,
  onChange,
  label = 'Toggle setting'
}: {
  checked: boolean
  onChange: (v: boolean) => void
  label?: string
}) {
  return <Switch aria-label={label} checked={checked} onCheckedChange={onChange} />
}

function ToggleRow({
  label,
  description,
  checked,
  onChange
}: {
  label: string
  description: string
  checked: boolean
  onChange: (value: boolean) => void
}) {
  return (
    <div className="mb-3 flex items-center justify-between gap-4">
      <div>
        <p className="text-sm text-zinc-300">{label}</p>
        <p className={`${CONTROL_TEXT} text-zinc-500`}>{description}</p>
      </div>
      <Toggle checked={checked} label={label} onChange={onChange} />
    </div>
  )
}

function SectionCard({ title, icon: Icon, children }: { title: string; icon: any; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
      <div className="mb-3 flex items-center gap-2">
        <Icon className="h-4 w-4 text-zinc-400" />
        <h3 className="text-sm font-medium text-zinc-200">{title}</h3>
      </div>
      {children}
    </div>
  )
}

function TextField({
  label,
  value,
  onChange,
  placeholder,
  type = 'text',
  hint,
  min,
  max,
  step
}: {
  label: string
  value: string | number
  onChange: (v: string) => void
  placeholder?: string
  type?: string
  hint?: string
  min?: number
  max?: number
  step?: number
}) {
  const id = useId()

  return (
    <div className="mb-3">
      <label className={`mb-1 block ${CONTROL_TEXT} text-zinc-400`} htmlFor={id}>
        {label}
      </label>
      <input
        className="w-full rounded-md border border-zinc-800 bg-zinc-950 px-3 py-1.5 text-sm text-zinc-200 placeholder:text-zinc-600 focus:border-primary focus:outline-none"
        id={id}
        max={max}
        min={min}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        step={step}
        type={type}
        value={value}
      />
      {hint && <p className={`mt-1 ${CONTROL_TEXT} text-zinc-500`}>{hint}</p>}
    </div>
  )
}

function AlarmEditor({
  alarm,
  onDelete,
  onSaved
}: {
  alarm: SmartRoomAlarm
  onDelete: (id: string) => Promise<void>
  onSaved: () => Promise<void>
}) {
  const [draft, setDraft] = useState(alarm)
  const [busy, setBusy] = useState(false)

  useEffect(() => setDraft(alarm), [alarm])

  const patch = <K extends keyof SmartRoomAlarm>(key: K, value: SmartRoomAlarm[K]) =>
    setDraft(current => ({ ...current, [key]: value }))

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-3">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-6">
        <TextField label="Name" onChange={value => patch('name', value)} value={draft.name} />
        <TextField label="Time" onChange={value => patch('time', value)} type="time" value={draft.time} />
        <div className="mb-3">
          <label className={`mb-1 block ${CONTROL_TEXT} text-zinc-400`}>Repeat</label>
          <select
            className="w-full rounded-md border border-zinc-800 bg-zinc-950 px-3 py-1.5 text-sm text-zinc-200"
            onChange={event => patch('recurrence', event.target.value as SmartRoomAlarm['recurrence'])}
            value={draft.recurrence}
          >
            <option value="once">One day</option>
            <option value="daily">Every day</option>
          </select>
        </div>
        {draft.recurrence === 'once' ? (
          <TextField label="Date" onChange={value => patch('date', value)} type="date" value={draft.date || ''} />
        ) : (
          <div />
        )}
        <TextField
          label="Duration (minutes)"
          max={180}
          min={1}
          onChange={value => patch('duration_minutes', Math.max(1, parseInt(value) || 30))}
          type="number"
          value={draft.duration_minutes}
        />
        <div className="flex items-center justify-between gap-2 pb-3 pt-5">
          <Toggle checked={draft.enabled} label={`${draft.name} enabled`} onChange={value => patch('enabled', value)} />
          <button
            className="rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50"
            disabled={busy || !draft.name.trim() || !draft.time || (draft.recurrence === 'once' && !draft.date)}
            onClick={() => {
              setBusy(true)
              void saveSmartRoomAlarm(draft)
                .then(onSaved)
                .catch(error => notifyError(error, 'Failed to save alarm'))
                .finally(() => setBusy(false))
            }}
            type="button"
          >
            Save
          </button>
          {draft.id ? (
            <button
              className="rounded-md border border-red-900/60 px-3 py-1.5 text-xs text-red-400 disabled:opacity-50"
              disabled={busy}
              onClick={() => {
                setBusy(true)
                void onDelete(draft.id).finally(() => setBusy(false))
              }}
              type="button"
            >
              Delete
            </button>
          ) : null}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function SmartRoomSettings() {
  const { t } = useI18n()
  const [config, setConfig] = useState<SmartRoomConfig>(DEFAULT_CONFIG)
  const [saving, setSaving] = useState(false)
  const [liveStatus, setLiveStatus] = useState<SmartRoomStatus | null>(null)
  const [secrets, setSecrets] = useState({ bulb_key: '', he20_key: '', mqtt_username: '', mqtt_password: '' })
  const [loading, setLoading] = useState(true)
  const [testingWelcome, setTestingWelcome] = useState<'owner' | 'guest' | null>(null)
  const [lightBusy, setLightBusy] = useState(false)
  const [lightDraft, setLightDraft] = useState({ brightness: 70, colorTemp: 3000, color: '#ffffff' })
  const [visionPreview, setVisionPreview] = useState<SmartRoomVisionPreview | null>(null)
  const [faces, setFaces] = useState<SmartRoomFaces | null>(null)
  const [faceName, setFaceName] = useState('Shereef')
  const [visionBusy, setVisionBusy] = useState(false)

  const [newAlarm, setNewAlarm] = useState<SmartRoomAlarm>({
    id: '',
    name: 'Alarm',
    time: '08:00',
    recurrence: 'once',
    date: new Date().toISOString().slice(0, 10),
    enabled: true,
    duration_minutes: 30
  })

  const saveQueue = useRef<Promise<void>>(Promise.resolve())

  const refreshStatus = useCallback(async () => setLiveStatus(await getSmartRoomStatus()), [])

  // Load config
  useEffect(() => {
    getHermesConfigRecord()
      .then((cfg: any) => {
        const sr = cfg?.smart_room

        if (sr) {
          const migrated = mergeDefaults(DEFAULT_CONFIG, sr)

          delete (migrated.automations as any).alarm
          setConfig(migrated)

          if (sr.automations?.alarm) {
            cfg.smart_room = migrated
            void saveHermesConfig(cfg).catch(() => undefined)
          }
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  // Poll the authenticated backend API; the renderer never connects to the
  // runtime's private TCP socket directly.
  useEffect(() => {
    const poll = async () => {
      try {
        await refreshStatus()
      } catch {
        setLiveStatus(null)
      }
    }

    void poll()
    const interval = setInterval(poll, 10000)

    return () => clearInterval(interval)
  }, [refreshStatus])

  // Camera frames stay local and are fetched only while this settings page is
  // mounted. A slow preview heartbeat is enough for enrollment/gesture review
  // without turning the settings UI into a second video pipeline.
  useEffect(() => {
    if (!config.vision.enabled || !liveStatus?.runtime?.alive) {
      setVisionPreview(null)

      return
    }

    const refreshVision = async () => {
      try {
        const [{ preview }, faceResult] = await Promise.all([
          getSmartRoomVisionPreview(),
          getSmartRoomFaces()
        ])

        setVisionPreview(preview)
        setFaces(faceResult.faces)
      } catch {
        // The runtime status card carries the actionable error. Keep the last
        // good frame instead of flashing the preview on transient restarts.
      }
    }

    void refreshVision()
    const interval = setInterval(refreshVision, 2000)

    return () => clearInterval(interval)
  }, [config.vision.enabled, liveStatus?.runtime?.alive])

  useEffect(() => {
    const light = liveStatus?.state?.light

    if (light) {
      setLightDraft({
        brightness: light.brightness ?? 70,
        colorTemp: light.color_temp ?? 3000,
        color: rgbToHex(light.rgb)
      })
    }
  }, [liveStatus])

  const updateConfig = useCallback((newConfig: SmartRoomConfig) => {
    setConfig(newConfig)
    setSaving(true)

    const queued = saveQueue.current
      .catch(() => {})
      .then(async () => {
        const cfg = await getHermesConfigRecord()
        cfg.smart_room = newConfig
        await saveHermesConfig(cfg)
      })

    saveQueue.current = queued
    void queued
      .catch(err => notifyError(err, 'Failed to save smart room config'))
      .finally(() => {
        if (saveQueue.current === queued) {
          setSaving(false)
        }
      })
  }, [])

  const updatePath = (path: string, value: any) => {
    const next = JSON.parse(JSON.stringify(config)) // deep clone
    const parts = path.split('.')
    let obj = next

    for (let i = 0; i < parts.length - 1; i++) {
      obj = obj[parts[i]]
    }

    obj[parts[parts.length - 1]] = value
    void updateConfig(next)
  }

  const persistSecrets = async () => {
    const values = Object.fromEntries(Object.entries(secrets).filter(([, value]) => value))
    await saveSmartRoomSecrets(values)
    setSecrets({ bulb_key: '', he20_key: '', mqtt_username: '', mqtt_password: '' })
  }

  const testWelcome = async (audience: 'owner' | 'guest') => {
    setTestingWelcome(audience)

    try {
      await saveQueue.current
      await testSmartRoomWelcome(audience)
      notify({ kind: 'success', message: `${audience === 'owner' ? 'Owner' : 'Guest'} greeting is playing` })
    } catch (err) {
      notifyError(err, `Failed to test ${audience} greeting`)
    } finally {
      setTestingWelcome(null)
    }
  }

  const controlLight = async (values: { on?: boolean; brightness?: number; color_temp?: number; rgb?: number[] }) => {
    setLightBusy(true)

    try {
      await setSmartRoomLight(values)
      await refreshStatus()
    } catch (error) {
      notifyError(error, 'Failed to control the room light')
    } finally {
      setLightBusy(false)
    }
  }

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-zinc-500">
        Loading smart room settings...
      </div>
    )
  }

  const liveState = liveStatus?.state
  const runtimeUp = !!liveStatus?.runtime?.alive && liveStatus.runtime.ready !== false

  const devices = (liveStatus?.health?.devices || liveState?.devices || {}) as Record<
    string,
    { online?: boolean; stale?: boolean }
  >

  const light = liveState?.light
  const presence = liveState?.presence || {}

  const soundEvents = liveStatus?.health?.sound_events as
    | {
        enabled?: boolean
        running?: boolean
        microphone?: string | null
        dataset?: { confirmed: number; pending: number; rejected: number; target: number }
      }
    | undefined

  const activeMode = liveState?.modes?.active_mode || null
  const overrideMode = (liveState?.modes?.manual_override || 'none') as 'hold_off' | 'hold_on' | 'none'
  const alarms = (liveState?.alarms || []) as SmartRoomAlarm[]
  const activeAlarm = liveState?.active_alarm as { id: string; name: string; phase: string } | null | undefined

  const locationHistory = (liveState?.location_history || []) as Array<{
    accuracy_m?: number
    event?: string
    latitude?: number
    longitude?: number
    reported_at: string
    type: string
    zone?: string
  }>

  return (
    <SettingsContent>
      <div className="space-y-4 px-4 pb-8">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Box className="h-5 w-5 text-primary" />
            <h2 className="text-lg font-semibold text-zinc-100">Smart Room</h2>
            <span
              className={`ml-2 rounded-full px-2 py-0.5 ${CONTROL_TEXT} ${runtimeUp ? 'bg-emerald-500/20 text-emerald-400' : 'bg-zinc-800 text-zinc-500'}`}
            >
              {runtimeUp ? 'Runtime Online' : 'Runtime Offline'}
            </span>
          </div>
          <div className="flex items-center gap-2">
            {saving && <span className={`${CONTROL_TEXT} text-zinc-500`}>Saving...</span>}
            <button
              aria-label="Apply settings and restart Smart Room"
              className="rounded-md p-1.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
              onClick={() => {
                void saveQueue.current
                  .then(() => applySmartRoomConfig())
                  .then(() => getSmartRoomStatus())
                  .then(setLiveStatus)
                  .catch(err => notifyError(err, 'Failed to apply smart room config'))
              }}
              title="Apply settings and restart runtime"
            >
              <RefreshCw className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* Enable toggle */}
        <SectionCard icon={Settings2} title="Plugin">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-zinc-200">Smart Room Engine</p>
              <p className={`${CONTROL_TEXT} text-zinc-500`}>Native presence fusion + Tuya control + automations</p>
            </div>
            <Toggle
              checked={config.enabled}
              label="Enable Smart Room plugin"
              onChange={v => updatePath('enabled', v)}
            />
          </div>
        </SectionCard>

        <SectionCard icon={Monitor} title="Vision, Face & Hand Controls">
          <ToggleRow
            checked={config.vision.enabled}
            description="Local camera perception feeds Smart Room cognition, face identity, sleep state, and gestures"
            label="Vision service"
            onChange={value => updatePath('vision.enabled', value)}
          />

          <div className="grid gap-4 lg:grid-cols-[minmax(0,1.5fr)_minmax(280px,1fr)]">
            <div className="overflow-hidden rounded-lg border border-zinc-800 bg-black">
              {visionPreview?.available && visionPreview.image ? (
                <img
                  alt="Live Smart Room camera preview"
                  className="aspect-video w-full object-contain"
                  src={visionPreview.image}
                />
              ) : (
                <div className="flex aspect-video items-center justify-center px-6 text-center text-xs text-zinc-500">
                  {visionPreview?.error || liveStatus?.health?.vision?.last_error || 'Waiting for a camera frame…'}
                </div>
              )}
              <div className="flex flex-wrap gap-x-3 gap-y-1 border-t border-zinc-800 bg-zinc-950 px-3 py-2 text-[11px] text-zinc-400">
                <span className={liveState?.vision?.camera_online ? 'text-emerald-400' : 'text-red-400'}>
                  ● {liveState?.vision?.camera_online ? 'Camera online' : 'Camera offline'}
                </span>
                <span>{liveState?.vision?.visibility || 'unavailable'} light</span>
                <span>{liveState?.vision?.person_count ?? 0} people</span>
                <span>{liveState?.vision?.owner_visible ? `${faces?.owner || 'Owner'} visible` : 'Owner not visible'}</span>
                <span>{liveState?.vision?.sleep_state || 'sleep unknown'}</span>
                <span>{liveState?.vision?.active_gesture || 'no gesture'}</span>
              </div>
            </div>

            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-x-3">
                <TextField label="Camera name" onChange={value => updatePath('vision.camera_name', value)} value={config.vision.camera_name} />
                <TextField label="Camera index" min={0} onChange={value => updatePath('vision.camera_index', Math.max(0, parseInt(value) || 0))} type="number" value={config.vision.camera_index} />
                <TextField label="Width" min={320} onChange={value => updatePath('vision.width', Math.max(320, parseInt(value) || 1280))} type="number" value={config.vision.width} />
                <TextField label="Height" min={240} onChange={value => updatePath('vision.height', Math.max(240, parseInt(value) || 720))} type="number" value={config.vision.height} />
                <TextField label="Idle FPS" max={10} min={0.2} onChange={value => updatePath('vision.standby_fps', parseFloat(value) || 1.5)} step={0.1} type="number" value={config.vision.standby_fps} />
                <TextField label="Active FPS" max={30} min={1} onChange={value => updatePath('vision.active_fps', parseFloat(value) || 8)} step={1} type="number" value={config.vision.active_fps} />
              </div>
              <button
                className="w-full rounded-md border border-zinc-700 px-3 py-2 text-xs text-zinc-200 disabled:opacity-40"
                disabled={!runtimeUp || visionBusy}
                onClick={() => {
                  setVisionBusy(true)
                  void observeSmartRoomVision(true, 'Describe what is happening in the room now.')
                    .then(refreshStatus)
                    .catch(error => notifyError(error, 'Vision inspection failed'))
                    .finally(() => setVisionBusy(false))
                }}
                type="button"
              >
                {visionBusy ? 'Inspecting scene…' : 'Inspect scene with vision model'}
              </button>
              {liveState?.vision?.scene_analysis?.summary ? (
                <p className="rounded-md bg-zinc-950 p-2 text-xs text-zinc-300">{liveState.vision.scene_analysis.summary}</p>
              ) : null}
            </div>
          </div>

          <details className="mt-4 border-t border-zinc-800 pt-3 text-xs text-zinc-400">
            <summary className="cursor-pointer text-zinc-300">Advanced vision reasoning</summary>
            <div className="mt-3 grid grid-cols-2 gap-x-3 lg:grid-cols-4">
              <TextField label="Dark threshold" max={100} min={0} onChange={value => updatePath('vision.dark_brightness', parseFloat(value) || 28)} step={1} type="number" value={config.vision.dark_brightness} />
              <TextField label="Face match threshold" max={1} min={0.1} onChange={value => updatePath('vision.faces.match_threshold', parseFloat(value) || 0.42)} step={0.01} type="number" value={config.vision.faces.match_threshold} />
              <TextField label="Enrollment samples" max={30} min={3} onChange={value => updatePath('vision.faces.min_enrollment_samples', Math.max(3, parseInt(value) || 8))} type="number" value={config.vision.faces.min_enrollment_samples} />
              <TextField label="Gesture confidence" max={1} min={0.1} onChange={value => updatePath('vision.gestures.confidence', parseFloat(value) || 0.65)} step={0.05} type="number" value={config.vision.gestures.confidence} />
              <TextField label="Sleep settling (seconds)" min={10} onChange={value => updatePath('vision.sleep.settling_seconds', parseInt(value) || 120)} type="number" value={config.vision.sleep.settling_seconds} />
              <TextField label="Likely sleeping (seconds)" min={30} onChange={value => updatePath('vision.sleep.likely_sleeping_seconds', parseInt(value) || 600)} type="number" value={config.vision.sleep.likely_sleeping_seconds} />
              <TextField label="History retention (hours)" min={1} onChange={value => updatePath('vision.history.retention_hours', parseInt(value) || 72)} type="number" value={config.vision.history.retention_hours} />
              <TextField label="History event limit" min={100} onChange={value => updatePath('vision.history.max_events', parseInt(value) || 2000)} type="number" value={config.vision.history.max_events} />
              <TextField label="Scene model provider" onChange={value => updatePath('vision.deep.provider', value)} placeholder="openrouter" value={config.vision.deep.provider} />
              <div className="col-span-2">
                <TextField label="Scene vision model" onChange={value => updatePath('vision.deep.model', value)} placeholder="google/gemma-4-26b-a4b-it:free" value={config.vision.deep.model} />
              </div>
              <ToggleRow checked={config.vision.deep.enabled} description="Use the configured multimodal model only for requested or uncertain scenes" label="Deep scene analysis" onChange={value => updatePath('vision.deep.enabled', value)} />
            </div>
          </details>

          <div className="mt-4 grid gap-4 border-t border-zinc-800 pt-4 lg:grid-cols-2">
            <div>
              <div className="mb-2 flex items-center justify-between">
                <div>
                  <p className="text-sm text-zinc-200">Face recognition</p>
                  <p className={`${CONTROL_TEXT} text-zinc-500`}>Reviewed local embeddings only; enrollment uses the live preview.</p>
                </div>
                <span className={`${CONTROL_TEXT} text-zinc-400`}>{faces?.owner ? `Owner: ${faces.owner}` : 'No owner enrolled'}</span>
              </div>
              <div className="flex gap-2">
                <input
                  aria-label="Face name"
                  className="min-w-0 flex-1 rounded-md border border-zinc-800 bg-zinc-950 px-3 py-1.5 text-sm text-zinc-200"
                  onChange={event => setFaceName(event.target.value)}
                  placeholder="Person name"
                  value={faceName}
                />
                <button
                  className="rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-40"
                  disabled={!runtimeUp || visionBusy || !faceName.trim()}
                  onClick={() => {
                    setVisionBusy(true)
                    void enrollSmartRoomFace(faceName.trim(), true, config.vision.faces.min_enrollment_samples)
                      .then(result => setFaces(result.faces))
                      .then(() => notify({ kind: 'success', message: `${faceName.trim()} enrolled as owner` }))
                      .catch(error => notifyError(error, 'Face enrollment failed'))
                      .finally(() => setVisionBusy(false))
                  }}
                  type="button"
                >
                  Enroll current face
                </button>
              </div>
              <div className="mt-2 space-y-1">
                {Object.entries(faces?.people || {}).map(([name, person]) => (
                  <div className="flex items-center justify-between rounded-md bg-zinc-950 px-3 py-2 text-xs" key={name}>
                    <span className="text-zinc-300">{name}{faces?.owner === name ? ' · owner' : ''} · {person.samples} samples</span>
                    <button className="text-red-400" onClick={() => void deleteSmartRoomFace(name).then(result => setFaces(result.faces)).catch(error => notifyError(error, 'Could not delete face'))} type="button">Delete</button>
                  </div>
                ))}
                {(faces?.pending_items || []).map(item => (
                  <div className="rounded-md border border-amber-900/40 bg-amber-950/20 px-3 py-2 text-xs" key={item.event_id}>
                    <p className="mb-2 text-amber-300">Unknown face needs review · {item.event_id}</p>
                    <div className="flex gap-2">
                      <button className="rounded border border-zinc-700 px-2 py-1 text-zinc-300" onClick={() => void reviewSmartRoomFace(item.event_id, faceName.trim(), false, false).then(result => setFaces(result.faces)).catch(error => notifyError(error, 'Face review failed'))} type="button">Accept as {faceName || 'name'}</button>
                      <button className="rounded border border-red-900 px-2 py-1 text-red-400" onClick={() => void reviewSmartRoomFace(item.event_id, '', false, true).then(result => setFaces(result.faces)).catch(error => notifyError(error, 'Face review failed'))} type="button">Reject</button>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div>
              <ToggleRow
                checked={config.vision.gestures.enabled}
                description={`Hold ${config.vision.gestures.wake_gesture} to arm controls for ${config.vision.gestures.armed_seconds} seconds`}
                label="Hand gesture controls"
                onChange={value => updatePath('vision.gestures.enabled', value)}
              />
              <div className="grid grid-cols-2 gap-x-3">
                <TextField label="Arm gesture" onChange={value => updatePath('vision.gestures.wake_gesture', value)} value={config.vision.gestures.wake_gesture} />
                <TextField label="Hold seconds" max={3} min={0.2} onChange={value => updatePath('vision.gestures.hold_seconds', parseFloat(value) || 0.65)} step={0.05} type="number" value={config.vision.gestures.hold_seconds} />
              </div>
              <div className="space-y-1 text-xs">
                {Object.entries(config.vision.gestures.mapping).map(([gesture, action]) => (
                  <div className="flex items-center justify-between rounded-md bg-zinc-950 px-3 py-1.5" key={gesture}>
                    <span className={liveState?.vision?.active_gesture === gesture ? 'text-emerald-400' : 'text-zinc-300'}>{gesture}</span>
                    <select
                      aria-label={`${gesture} action`}
                      className="rounded border border-zinc-800 bg-zinc-900 px-2 py-1 text-zinc-300"
                      onChange={event => updatePath(`vision.gestures.mapping.${gesture}.command`, event.target.value)}
                      value={action.command}
                    >
                      <option value="toggle_light">Toggle light</option>
                      <option value="brightness_up">Brightness up</option>
                      <option value="brightness_down">Brightness down</option>
                      <option value="voice_mode">Voice mode</option>
                      <option value="cancel">Cancel / wake</option>
                      <option value="set_mode">Relax mode</option>
                    </select>
                  </div>
                ))}
              </div>
            </div>
          </div>
          <p className={`mt-3 ${CONTROL_TEXT} text-zinc-500`}>Camera and model changes apply after pressing the refresh button at the top.</p>
        </SectionCard>

        <SectionCard icon={Brain} title="Voice Welcome">
          <ToggleRow
            checked={config.welcome.enabled}
            description="Generate a short personality-aware TTS greeting after a genuine arrival"
            label="Welcome on arrival"
            onChange={v => updatePath('welcome.enabled', v)}
          />
          <div className="grid grid-cols-1 gap-x-4 sm:grid-cols-4">
            <TextField label="Owner name" onChange={v => updatePath('owner', v)} value={config.owner} />
            <TextField
              hint="Wait for ESPresense identity."
              label="Identity wait (seconds)"
              onChange={v => updatePath('welcome.identity_grace_seconds', parseInt(v) || 4)}
              type="number"
              value={config.welcome.identity_grace_seconds}
            />
            <TextField
              hint="Default: 60 minutes."
              label="Empty reset (minutes)"
              onChange={v => updatePath('welcome.reset_after_seconds', (parseInt(v) || 60) * 60)}
              type="number"
              value={Math.round(config.welcome.reset_after_seconds / 60)}
            />
            <TextField
              hint="Keeps BLE/OwnTracks identity through iPhone sleep."
              label="Owner evidence (minutes)"
              onChange={v => updatePath('welcome.owner_evidence_window_seconds', (parseInt(v) || 60) * 60)}
              type="number"
              value={Math.round(config.welcome.owner_evidence_window_seconds / 60)}
            />
          </div>
          <div className="flex gap-2">
            {(['owner', 'guest'] as const).map(audience => (
              <button
                className="rounded-md border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800 disabled:cursor-wait disabled:opacity-50"
                disabled={testingWelcome !== null || !runtimeUp}
                key={audience}
                onClick={() => void testWelcome(audience)}
                type="button"
              >
                {testingWelcome === audience ? 'Generating…' : `Test ${audience} greeting`}
              </button>
            ))}
          </div>
        </SectionCard>

        <SectionCard icon={AudioLines} title={t.settings.soundEvents.title}>
          <ToggleRow
            checked={config.sound_events.enabled}
            description={t.settings.soundEvents.description}
            label={t.settings.soundEvents.enabled}
            onChange={value => updatePath('sound_events.enabled', value)}
          />
          <ToggleRow
            checked={config.sound_events.sleep_enabled}
            description="Off by default: prevents ambient/startup audio from putting the room to sleep."
            label="Triple clap Sleep (experimental)"
            onChange={value => updatePath('sound_events.sleep_enabled', value)}
          />
          <div className="mb-4 flex items-center gap-2 text-xs text-zinc-400">
            <StatusDot online={!!soundEvents?.running} />
            <span>{soundEvents?.running ? t.settings.soundEvents.listening : t.common.off}</span>
            {soundEvents?.microphone && (
              <span className="text-zinc-500">
                · {t.settings.soundEvents.microphone}: {soundEvents.microphone}
              </span>
            )}
          </div>
          {soundEvents?.dataset && (
            <div className="mb-4 rounded-md border border-zinc-800 bg-zinc-950/40 px-3 py-2">
              <div className="flex items-center justify-between text-xs text-zinc-300">
                <span>Human-confirmed clap dataset</span>
                <span>
                  {soundEvents.dataset.confirmed} / {soundEvents.dataset.target}
                </span>
              </div>
              <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-zinc-800">
                <div
                  className="h-full rounded-full bg-primary transition-[width]"
                  style={{
                    width: `${Math.min(100, (soundEvents.dataset.confirmed / soundEvents.dataset.target) * 100)}%`
                  }}
                />
              </div>
              <p className={`${CONTROL_TEXT} mt-1.5 text-zinc-500`}>
                {soundEvents.dataset.pending} awaiting review · {soundEvents.dataset.rejected} saved as hard negatives
              </p>
            </div>
          )}
          <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <p className="text-sm text-zinc-200">{t.settings.soundEvents.doubleClap}</p>
              <p className={`${CONTROL_TEXT} text-zinc-500`}>{t.settings.soundEvents.doubleClapDesc}</p>
            </div>
            <div>
              <p className="text-sm text-zinc-200">{t.settings.soundEvents.tripleClap}</p>
              <p className={`${CONTROL_TEXT} text-zinc-500`}>{t.settings.soundEvents.tripleClapDesc}</p>
            </div>
          </div>
          <div className="grid grid-cols-1 gap-x-4 sm:grid-cols-3">
            <TextField
              hint={t.settings.soundEvents.confidenceHint}
              label={t.settings.soundEvents.confidence}
              max={1}
              min={0}
              onChange={value => {
                const parsed = Number(value)

                if (value !== '' && Number.isFinite(parsed)) {
                  updatePath('sound_events.confidence', Math.min(1, Math.max(0, parsed)))
                }
              }}
              step={0.05}
              type="number"
              value={config.sound_events.confidence}
            />
            <TextField
              hint={t.settings.soundEvents.minimumPeakHint}
              label={t.settings.soundEvents.minimumPeak}
              max={1}
              min={0}
              onChange={value => {
                const parsed = Number(value)

                if (value !== '' && Number.isFinite(parsed)) {
                  updatePath('sound_events.min_peak', Math.min(1, Math.max(0, parsed)))
                }
              }}
              step={0.01}
              type="number"
              value={config.sound_events.min_peak}
            />
            <TextField
              hint="Leave empty for the Windows default recording device."
              label="Microphone device"
              onChange={value => updatePath('sound_events.input_device', value.trim() || null)}
              placeholder={soundEvents?.microphone || 'Default input'}
              value={config.sound_events.input_device || ''}
            />
          </div>
          <p className={`${CONTROL_TEXT} text-zinc-500`}>{t.settings.soundEvents.applyHint}</p>
        </SectionCard>

        {/* Connection Status */}
        <SectionCard icon={Cloud} title="Connection Status">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div className="flex items-center gap-2">
              <StatusDot online={!!liveStatus?.health?.mqtt?.connected} />
              <span className={`${CONTROL_TEXT} text-zinc-400`}>MQTT</span>
            </div>
            <div className="flex items-center gap-2">
              <StatusDot online={!!devices.esp32?.online && !devices.esp32?.stale} />
              <span className={`${CONTROL_TEXT} text-zinc-400`}>ESP32</span>
            </div>
            <div className="flex items-center gap-2">
              <StatusDot online={!!devices.tuya_bulb?.online} />
              <span className={`${CONTROL_TEXT} text-zinc-400`}>Bulb</span>
            </div>
            <div className="flex items-center gap-2">
              <StatusDot online={!!devices.tuya_he20?.online} />
              <span className={`${CONTROL_TEXT} text-zinc-400`}>HE20</span>
            </div>
          </div>
        </SectionCard>

        {/* Current Room State */}
        <SectionCard icon={Brain} title="Room State">
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-5">
            <div>
              <p className={`${CONTROL_TEXT} text-zinc-500`}>Owner</p>
              <p className="text-sm text-zinc-200">
                {presence.detected ? `Detected (${(presence.confidence * 100).toFixed(0)}%)` : 'Not identified'}
              </p>
            </div>
            <div>
              <p className={`${CONTROL_TEXT} text-zinc-500`}>HE20</p>
              <p className="text-sm text-zinc-200">{liveState?.mmwave?.occupied ? 'Occupied' : 'Clear'}</p>
            </div>
            <div>
              <p className={`${CONTROL_TEXT} text-zinc-500`}>Mode</p>
              <p className="text-sm capitalize text-zinc-200">{activeMode || 'off'}</p>
            </div>
            <div>
              <p className={`${CONTROL_TEXT} text-zinc-500`}>Light</p>
              <p className="text-sm text-zinc-200">{light?.on ? `${light.brightness || 0}%` : 'Off'}</p>
            </div>
            <div>
              <p className={`${CONTROL_TEXT} text-zinc-500`}>Location</p>
              <p className="text-sm capitalize text-zinc-200">{liveState?.location?.zone || 'unknown'}</p>
            </div>
          </div>
        </SectionCard>

        <SectionCard icon={SlidersHorizontal} title="Light Control">
          <div className="grid gap-5 md:grid-cols-[180px_1fr]">
            <div className="flex flex-col items-center justify-center rounded-xl border border-zinc-800 bg-zinc-950/50 p-4">
              <span className="text-3xl font-semibold text-zinc-100">{lightDraft.brightness}%</span>
              <span className={`${CONTROL_TEXT} mt-1 text-zinc-500`}>{light?.on ? 'Powered on' : 'Powered off'}</span>
              <div className="mt-4 flex gap-2">
                <button
                  className={`rounded-md px-4 py-2 text-xs ${light?.on ? 'bg-primary text-primary-foreground' : 'border border-zinc-700 text-zinc-300'}`}
                  disabled={!runtimeUp || lightBusy}
                  onClick={() => void controlLight({ on: true })}
                  type="button"
                >
                  On
                </button>
                <button
                  className={`rounded-md px-4 py-2 text-xs ${!light?.on ? 'bg-zinc-200 text-zinc-950' : 'border border-zinc-700 text-zinc-300'}`}
                  disabled={!runtimeUp || lightBusy}
                  onClick={() => void controlLight({ on: false })}
                  type="button"
                >
                  Off
                </button>
              </div>
            </div>
            <div className="space-y-4">
              <label className="block text-xs text-zinc-400">
                <span className="mb-1 flex justify-between">
                  <span>Brightness</span>
                  <span>{lightDraft.brightness}%</span>
                </span>
                <input
                  className="w-full accent-primary"
                  disabled={!runtimeUp || lightBusy}
                  max={100}
                  min={1}
                  onChange={event => setLightDraft(current => ({ ...current, brightness: Number(event.target.value) }))}
                  onKeyUp={event => void controlLight({ on: true, brightness: Number(event.currentTarget.value) })}
                  onPointerUp={event => void controlLight({ on: true, brightness: Number(event.currentTarget.value) })}
                  type="range"
                  value={lightDraft.brightness}
                />
              </label>
              <label className="block text-xs text-zinc-400">
                <span className="mb-1 flex justify-between">
                  <span>White temperature</span>
                  <span>{lightDraft.colorTemp}K</span>
                </span>
                <input
                  className="w-full accent-amber-300"
                  disabled={!runtimeUp || lightBusy}
                  max={6500}
                  min={2200}
                  onChange={event => setLightDraft(current => ({ ...current, colorTemp: Number(event.target.value) }))}
                  onKeyUp={event => void controlLight({ on: true, color_temp: Number(event.currentTarget.value) })}
                  onPointerUp={event => void controlLight({ on: true, color_temp: Number(event.currentTarget.value) })}
                  step={100}
                  type="range"
                  value={lightDraft.colorTemp}
                />
              </label>
              <div>
                <p className="mb-2 text-xs text-zinc-400">Color</p>
                <div className="flex flex-wrap items-center gap-2">
                  <input
                    aria-label="Custom light color"
                    className="h-10 w-10 cursor-pointer rounded-full border-0 bg-transparent p-0"
                    disabled={!runtimeUp || lightBusy}
                    onChange={event => {
                      const color = event.target.value
                      setLightDraft(current => ({ ...current, color }))
                      void controlLight({ on: true, rgb: hexToRgb(color) })
                    }}
                    type="color"
                    value={lightDraft.color}
                  />
                  {LIGHT_COLORS.map(color => (
                    <button
                      aria-label={`Set light color ${color}`}
                      className="h-8 w-8 rounded-full border border-white/20 disabled:opacity-40"
                      disabled={!runtimeUp || lightBusy}
                      key={color}
                      onClick={() => {
                        setLightDraft(current => ({ ...current, color }))
                        void controlLight({ on: true, rgb: hexToRgb(color) })
                      }}
                      style={{ backgroundColor: color }}
                      type="button"
                    />
                  ))}
                </div>
              </div>
            </div>
          </div>
          <p className={`${CONTROL_TEXT} mt-3 text-zinc-500`}>
            Manual changes cancel a stale Sleep state. Sliders apply when released.
          </p>
        </SectionCard>

        {/* Mode Buttons */}
        <SectionCard icon={Zap} title="Modes">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
            {MODES.map(mode => (
              <button
                className={`flex flex-col items-center rounded-lg border p-3 transition-colors ${
                  activeMode === mode.id
                    ? 'border-primary bg-primary/10 text-primary'
                    : 'border-zinc-800 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200'
                }`}
                disabled={!runtimeUp}
                key={mode.id}
                onClick={() => {
                  void setSmartRoomMode(mode.id)
                    .then(() => getSmartRoomStatus())
                    .then(setLiveStatus)
                    .catch(err => notifyError(err, `Failed to set ${mode.label} mode`))
                }}
              >
                <span className="text-lg">{mode.icon}</span>
                <span className="mt-1 text-xs font-medium">{mode.label}</span>
                <span className={`mt-0.5 text-[10px] text-zinc-600`}>{mode.desc}</span>
              </button>
            ))}
          </div>
          <p className={`mt-2 ${CONTROL_TEXT} text-zinc-500`}>
            Save changes automatically, then use the refresh button above to apply them to the runtime.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <label className="flex items-center gap-2 text-xs text-zinc-400">
              Presence override
              <select
                className="rounded-md border border-zinc-700 bg-zinc-950 px-2 py-1.5 text-zinc-300"
                disabled={!runtimeUp}
                onChange={event => {
                  void setSmartRoomOverride(event.target.value as typeof overrideMode)
                    .then(refreshStatus)
                    .catch(err => notifyError(err, 'Failed to change manual override'))
                }}
                value={overrideMode}
              >
                <option value="none">Automatic</option>
                <option value="hold_on">Keep light on</option>
                <option value="hold_off">Keep light off</option>
              </select>
            </label>
            <button
              className="rounded-md border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 disabled:opacity-40"
              disabled={!runtimeUp || activeMode !== 'sleep'}
              onClick={() =>
                void cancelSmartRoomSleep()
                  .then(() => getSmartRoomStatus())
                  .then(setLiveStatus)
                  .catch(err => notifyError(err, 'Failed to cancel sleep'))
              }
            >
              Cancel sleep
            </button>
          </div>
        </SectionCard>

        {/* Automations */}
        <SectionCard icon={RefreshCw} title="Automations">
          <div className="space-y-3">
            {AUTOMATIONS.map(auto => {
              const enabled = (config.automations as any)?.[auto.key]?.enabled ?? false

              return (
                <div className="flex items-center justify-between" key={auto.key}>
                  <div>
                    <p className="text-sm text-zinc-200">{auto.label}</p>
                    <p className={`${CONTROL_TEXT} text-zinc-500`}>{auto.desc}</p>
                  </div>
                  <Toggle
                    checked={enabled}
                    label={auto.label}
                    onChange={v => updatePath(`automations.${auto.key}.enabled`, v)}
                  />
                </div>
              )
            })}
            <div className="flex items-center justify-between border-t border-zinc-800 pt-3">
              <div>
                <p className="text-sm text-zinc-200">Auto-off when room clears</p>
                <p className={`${CONTROL_TEXT} text-zinc-500`}>
                  Turns off after the exit timeout in every mode except Focus.
                </p>
              </div>
              <Toggle
                checked={config.automations.adaptive_light.auto_off}
                label="Auto-off when room clears"
                onChange={value => updatePath('automations.adaptive_light.auto_off', value)}
              />
            </div>
            <details className="border-t border-zinc-800 pt-3 text-xs text-zinc-400">
              <summary className="cursor-pointer">Automation timing</summary>
              <div className="mt-3 grid grid-cols-1 gap-x-4 sm:grid-cols-2">
                <TextField
                  label="Evening Sleep Time"
                  onChange={v => updatePath('automations.evening_sleep.time', v)}
                  type="time"
                  value={config.automations.evening_sleep.time}
                />
                <TextField
                  label="Work Return Settle (seconds)"
                  onChange={v => updatePath('automations.work_return.settle_delay', parseInt(v) || 300)}
                  type="number"
                  value={config.automations.work_return.settle_delay}
                />
                <TextField
                  label="Arrival Window Start"
                  onChange={v => updatePath('automations.work_return.work_hours_start', v)}
                  type="time"
                  value={config.automations.work_return.work_hours_start}
                />
                <TextField
                  label="Arrival Window End"
                  onChange={v => updatePath('automations.work_return.work_hours_end', v)}
                  type="time"
                  value={config.automations.work_return.work_hours_end}
                />
              </div>
            </details>
            <div className="flex items-center justify-between border-t border-zinc-800 pt-3">
              <div>
                <p className="text-sm text-zinc-200">Daily Reset</p>
                <p className={`${CONTROL_TEXT} text-zinc-500`}>Reset mode flags at midnight</p>
              </div>
              <input
                aria-label="Daily reset time"
                className="rounded-md border border-zinc-800 bg-zinc-950 px-2 py-1 text-sm text-zinc-200"
                onChange={e => updatePath('automations.daily_reset', e.target.value)}
                type="time"
                value={config.automations.daily_reset}
              />
            </div>
          </div>
        </SectionCard>

        <SectionCard icon={Moon} title="Alarms">
          <p className={`mb-3 ${CONTROL_TEXT} text-zinc-500`}>
            Named one-day or every-day alarms flash for one minute, stay bright, start Voice Instant, and restore the
            previous room state when acknowledged.
          </p>
          {activeAlarm ? (
            <div className="mb-3 flex items-center justify-between rounded-lg border border-amber-500/40 bg-amber-500/10 p-3">
              <div>
                <p className="text-sm font-medium text-amber-300">{activeAlarm.name}</p>
                <p className={`${CONTROL_TEXT} capitalize text-amber-200/70`}>{activeAlarm.phase}</p>
              </div>
              <button
                className="rounded-md bg-amber-300 px-3 py-1.5 text-sm font-medium text-zinc-950"
                onClick={() =>
                  void acknowledgeSmartRoomAlarm()
                    .then(refreshStatus)
                    .catch(error => notifyError(error, 'Failed to stop alarm'))
                }
                type="button"
              >
                I&apos;m awake
              </button>
            </div>
          ) : null}
          <div className="space-y-3">
            {alarms.map(alarm => (
              <AlarmEditor
                alarm={alarm}
                key={alarm.id}
                onDelete={async id => {
                  await deleteSmartRoomAlarm(id)
                  await refreshStatus()
                }}
                onSaved={refreshStatus}
              />
            ))}
            <details className="rounded-lg border border-dashed border-zinc-700 p-3">
              <summary className="cursor-pointer text-sm text-zinc-300">Add alarm</summary>
              <div className="mt-3">
                <AlarmEditor
                  alarm={newAlarm}
                  onDelete={async () => undefined}
                  onSaved={async () => {
                    await refreshStatus()
                    setNewAlarm(current => ({ ...current, id: '', name: 'Alarm' }))
                  }}
                />
              </div>
            </details>
          </div>
        </SectionCard>

        {/* Scene Presets */}
        <SectionCard icon={SlidersHorizontal} title="Scene Presets">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
            {Object.entries(config.scenes).map(([name, scene]) => (
              <div className="rounded-lg border border-zinc-800 bg-zinc-950 p-3" key={name}>
                <p className="mb-1 text-sm font-medium capitalize text-zinc-200">{name}</p>
                <div className={`space-y-0.5 ${CONTROL_TEXT} text-zinc-500`}>
                  <p>{scene.brightness ? `${scene.brightness}% brightness` : '—'}</p>
                  <p>{scene.color_temp ? `${scene.color_temp}K` : scene.rgb ? 'RGB mode' : '—'}</p>
                  {scene.flash && <p className="text-amber-500">Flash mode</p>}
                </div>
              </div>
            ))}
          </div>
        </SectionCard>

        {/* Tuya Device Config */}
        <SectionCard icon={Cloud} title="Tuya Devices">
          <div className="mb-3 flex items-start gap-2 rounded-md bg-amber-500/10 p-2 text-amber-400">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <p className={`${CONTROL_TEXT}`}>
              Local keys are needed <strong>once</strong>. Get them from the Tuya IoT Portal (free). After that, all
              control is LAN-only — no cloud. Run{' '}
              <code className="text-amber-300">python plugins/smart_room/scripts/discover_tuya.py</code> to find device
              IPs.
            </p>
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <p className="mb-2 text-sm font-medium text-zinc-300">RGBCW Bulb</p>
              <TextField
                label="IP Address"
                onChange={v => updatePath('tuya.bulb.ip', v)}
                placeholder="192.168.1.x"
                value={config.tuya.bulb.ip}
              />
              <TextField
                label="Device ID"
                onChange={v => updatePath('tuya.bulb.device_id', v)}
                placeholder="Tuya device ID"
                value={config.tuya.bulb.device_id}
              />
              <TextField
                label="Local Key"
                onChange={v => setSecrets(current => ({ ...current, bulb_key: v }))}
                placeholder="Stored securely; enter to replace"
                type="password"
                value={secrets.bulb_key}
              />
              <TextField
                label="Protocol"
                onChange={v => updatePath('tuya.bulb.protocol', v)}
                placeholder="3.3"
                value={config.tuya.bulb.protocol}
              />
              <details className="rounded-md border border-zinc-800 p-2 text-xs text-zinc-400">
                <summary className="cursor-pointer">Advanced DPS mapping</summary>
                <div className="mt-3 grid grid-cols-2 gap-x-3">
                  <TextField
                    label="Switch DP"
                    onChange={v => updatePath('tuya.bulb.dps.switch', parseInt(v) || 1)}
                    type="number"
                    value={config.tuya.bulb.dps.switch}
                  />
                  <TextField
                    label="Brightness DP"
                    onChange={v => updatePath('tuya.bulb.dps.brightness', parseInt(v) || 2)}
                    type="number"
                    value={config.tuya.bulb.dps.brightness}
                  />
                  <TextField
                    label="Color Temp DP"
                    onChange={v => updatePath('tuya.bulb.dps.color_temp', parseInt(v) || 3)}
                    type="number"
                    value={config.tuya.bulb.dps.color_temp}
                  />
                  <TextField
                    label="Color DP"
                    onChange={v => updatePath('tuya.bulb.dps.color', parseInt(v) || 5)}
                    type="number"
                    value={config.tuya.bulb.dps.color}
                  />
                  <TextField
                    label="Brightness Max"
                    onChange={v => updatePath('tuya.bulb.brightness_max', parseInt(v) || 255)}
                    type="number"
                    value={config.tuya.bulb.brightness_max}
                  />
                  <TextField
                    label="Color Temp Max"
                    onChange={v => updatePath('tuya.bulb.color_temp_max', parseInt(v) || 255)}
                    type="number"
                    value={config.tuya.bulb.color_temp_max}
                  />
                </div>
              </details>
            </div>
            <div>
              <p className="mb-2 text-sm font-medium text-zinc-300">HE20 Presence Sensor</p>
              <TextField
                label="IP Address"
                onChange={v => updatePath('tuya.he20.ip', v)}
                placeholder="192.168.1.x"
                value={config.tuya.he20.ip}
              />
              <TextField
                label="Device ID"
                onChange={v => updatePath('tuya.he20.device_id', v)}
                placeholder="Tuya device ID"
                value={config.tuya.he20.device_id}
              />
              <TextField
                label="Local Key"
                onChange={v => setSecrets(current => ({ ...current, he20_key: v }))}
                placeholder="Stored securely; enter to replace"
                type="password"
                value={secrets.he20_key}
              />
              <TextField
                label="Protocol"
                onChange={v => updatePath('tuya.he20.protocol', v)}
                placeholder="3.3"
                value={config.tuya.he20.protocol}
              />
              <details className="rounded-md border border-zinc-800 p-2 text-xs text-zinc-400">
                <summary className="cursor-pointer">Advanced presence mapping</summary>
                <div className="mt-3">
                  <TextField
                    label="Presence DP"
                    onChange={v => updatePath('tuya.he20.presence_dp', parseInt(v) || 1)}
                    type="number"
                    value={config.tuya.he20.presence_dp}
                  />
                  <TextField
                    hint="Comma-separated values that mean occupied."
                    label="Occupied Values"
                    onChange={v =>
                      updatePath(
                        'tuya.he20.occupied_values',
                        v
                          .split(',')
                          .map(value => value.trim())
                          .filter(Boolean)
                      )
                    }
                    value={config.tuya.he20.occupied_values.join(', ')}
                  />
                </div>
              </details>
            </div>
          </div>
          <details className="mt-3 rounded-md border border-zinc-800 p-2 text-xs text-zinc-400">
            <summary className="cursor-pointer">Reliability worker</summary>
            <div className="mt-3 grid grid-cols-1 gap-x-3 sm:grid-cols-3">
              <TextField
                label="Socket timeout (seconds)"
                min={1}
                onChange={value => updatePath('tuya.worker.timeout_seconds', Math.max(1, parseInt(value) || 4))}
                type="number"
                value={config.tuya.worker.timeout_seconds}
              />
              <TextField
                label="Retries"
                max={3}
                min={0}
                onChange={value => updatePath('tuya.worker.retries', Math.max(0, Math.min(3, parseInt(value) || 0)))}
                type="number"
                value={config.tuya.worker.retries}
              />
              <TextField
                label="Queue capacity"
                min={4}
                onChange={value => updatePath('tuya.worker.queue_size', Math.max(4, parseInt(value) || 16))}
                type="number"
                value={config.tuya.worker.queue_size}
              />
            </div>
          </details>
          <button
            className="mt-3 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground"
            onClick={() => {
              void persistSecrets().catch(err => notifyError(err, 'Failed to save Smart Room secrets'))
            }}
          >
            Save secrets
          </button>
        </SectionCard>

        {/* ESP32 Config */}
        <SectionCard icon={Monitor} title="ESP32 (ESPresense)">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <TextField
                label="Node IP"
                onChange={v => updatePath('esp32.ip', v)}
                placeholder="192.168.1.172"
                value={config.esp32.ip}
              />
              <TextField
                label="Room ID"
                onChange={v => updatePath('esp32.room_id', v)}
                placeholder="smart_room"
                value={config.esp32.room_id}
              />
              <TextField
                hint="Copy the enrolled ID shown in ESPresense/MQTT."
                label="Owner Device ID"
                onChange={v => updatePath('esp32.owner_device_id', v)}
                placeholder="ESPresense enrolled device ID"
                value={config.esp32.owner_device_id}
              />
              <TextField
                label="Presence Topic"
                onChange={v => updatePath('esp32.presence_topic', v)}
                placeholder="espresense/devices/+/smart_room"
                value={config.esp32.presence_topic}
              />
              <TextField
                label="Status Topic"
                onChange={v => updatePath('esp32.status_topic', v)}
                placeholder="espresense/rooms/smart_room/#"
                value={config.esp32.status_topic}
              />
            </div>
            <div>
              <TextField
                hint="dBm — closer = higher. -70 is typical."
                label="RSSI Enter Threshold"
                onChange={v => updatePath('esp32.rssi_enter_threshold', parseInt(v) || -70)}
                type="number"
                value={config.esp32.rssi_enter_threshold}
              />
              <TextField
                hint="dBm — must drop below this to leave."
                label="RSSI Exit Threshold"
                onChange={v => updatePath('esp32.rssi_exit_threshold', parseInt(v) || -85)}
                type="number"
                value={config.esp32.rssi_exit_threshold}
              />
              <TextField
                hint="Require sustained BLE signal before entering."
                label="BLE Entry Debounce (seconds)"
                onChange={v => updatePath('esp32.enter_debounce_seconds', Math.max(0, parseInt(v) || 0))}
                type="number"
                value={config.esp32.enter_debounce_seconds}
              />
              <TextField
                hint="Keep this above brief HE20 false-clears; 300 seconds is recommended."
                label="Exit Timeout (seconds)"
                onChange={v => updatePath('esp32.exit_timeout', Math.max(30, parseInt(v) || 300))}
                type="number"
                value={config.esp32.exit_timeout}
              />
              <TextField
                hint="BLE silence alone never clears presence; this only enters sticky fusion."
                label="BLE Missing Timeout (seconds)"
                onChange={v => updatePath('esp32.missing_timeout_seconds', parseInt(v) || 30)}
                type="number"
                value={config.esp32.missing_timeout_seconds}
              />
            </div>
          </div>
          <div className="mt-4 border-t border-zinc-800 pt-4">
            <ToggleRow
              checked={config.presence.wifi_ping.enabled}
              description="Ping a reserved iPhone IP. A reply helps identity fusion; a timeout never means away."
              label="Positive-only Wi-Fi presence fallback"
              onChange={v => updatePath('presence.wifi_ping.enabled', v)}
            />
            {config.presence.wifi_ping.enabled && (
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <TextField
                  label="Reserved iPhone IP"
                  onChange={v => updatePath('presence.wifi_ping.ip', v)}
                  placeholder="192.168.1.x"
                  value={config.presence.wifi_ping.ip}
                />
                <TextField
                  label="Probe Interval (seconds)"
                  onChange={v => updatePath('presence.wifi_ping.interval_seconds', parseInt(v) || 60)}
                  type="number"
                  value={config.presence.wifi_ping.interval_seconds}
                />
              </div>
            )}
          </div>
        </SectionCard>

        {/* Phone location */}
        <SectionCard icon={Moon} title="iPhone Location">
          <div className="mb-4 rounded-md border border-zinc-800 bg-zinc-950 p-3 text-xs text-zinc-400">
            <p className="font-medium text-zinc-200">Primary: OwnTracks over authenticated MQTT</p>
            <p className="mt-1">
              Use User ID <code>smart_room</code> and Device ID <code>iphone</code>. OwnTracks transitions update
              home/away; iOS Shortcuts is not required.
            </p>
            <p className="mt-1 text-amber-300/80">
              Use the PC's private Tailscale address with port 1883 and TLS off. Tailscale encrypts the route; never
              expose port 1883 publicly.
            </p>
          </div>
          <TextField
            label="MQTT Topic"
            onChange={v => updatePath('owntracks.topic', v)}
            placeholder="owntracks/shereef/#"
            value={config.owntracks.topic}
          />
          <div className="mb-4 max-h-52 overflow-y-auto rounded-md border border-zinc-800 bg-zinc-950 p-3">
            <p className="mb-2 text-xs font-medium text-zinc-300">Recent reports</p>
            {locationHistory.length ? (
              [...locationHistory].reverse().map((report, index) => (
                <div
                  className="border-t border-zinc-800 py-2 text-xs text-zinc-400 first:border-0"
                  key={`${report.reported_at}-${index}`}
                >
                  <span className="text-zinc-200">{new Date(report.reported_at).toLocaleString()}</span>
                  {' · '}
                  {report.event || report.type}
                  {report.zone ? ` ${report.zone}` : ''}
                  {report.latitude != null && report.longitude != null
                    ? ` · ${report.latitude.toFixed(5)}, ${report.longitude.toFixed(5)} ±${report.accuracy_m ?? '?'}m`
                    : ''}
                </div>
              ))
            ) : (
              <p className="text-xs text-zinc-500">No OwnTracks reports recorded yet.</p>
            )}
          </div>
          <div>
            <label className={`mb-1 block ${CONTROL_TEXT} text-zinc-400`}>Geofence Zones</label>
            <div className="flex flex-wrap gap-2">
              {config.owntracks.zones.map((zone, i) => (
                <span className="rounded-full bg-zinc-800 px-3 py-1 text-xs text-zinc-300" key={`${zone}-${i}`}>
                  {zone}
                  <button
                    aria-label={`Remove ${zone} zone`}
                    className="ml-2 text-zinc-500 hover:text-red-400"
                    onClick={() => {
                      const zones = config.owntracks.zones.filter((_, idx) => idx !== i)
                      updatePath('owntracks.zones', zones)
                    }}
                  >
                    ×
                  </button>
                </span>
              ))}
              <input
                aria-label="Add geofence zone"
                className="w-24 rounded-full border border-zinc-800 bg-zinc-950 px-3 py-1 text-xs text-zinc-200"
                onKeyDown={e => {
                  if (e.key === 'Enter') {
                    const val = (e.target as HTMLInputElement).value.trim()

                    if (val && !config.owntracks.zones.includes(val)) {
                      updatePath('owntracks.zones', [...config.owntracks.zones, val])
                      ;(e.target as HTMLInputElement).value = ''
                    }
                  }
                }}
                placeholder="add zone..."
                type="text"
              />
            </div>
          </div>
        </SectionCard>

        {/* MQTT Broker Config */}
        <SectionCard icon={Cloud} title="MQTT Broker">
          <div className="grid grid-cols-2 gap-4">
            <TextField
              label="Broker IP"
              onChange={v => updatePath('mqtt.broker', v)}
              placeholder="127.0.0.1"
              value={config.mqtt.broker}
            />
            <TextField
              label="Port"
              onChange={v => updatePath('mqtt.port', parseInt(v) || 1883)}
              type="number"
              value={config.mqtt.port}
            />
            <TextField
              label="Username"
              onChange={v => setSecrets(current => ({ ...current, mqtt_username: v }))}
              placeholder="Stored securely; enter to replace"
              value={secrets.mqtt_username}
            />
            <TextField
              label="Password"
              onChange={v => setSecrets(current => ({ ...current, mqtt_password: v }))}
              placeholder="Stored securely; enter to replace"
              type="password"
              value={secrets.mqtt_password}
            />
          </div>
          <button
            className="mt-3 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground"
            onClick={() => void persistSecrets().catch(err => notifyError(err, 'Failed to save MQTT credentials'))}
          >
            Save MQTT credentials
          </button>
        </SectionCard>

        {/* Context & Subconscious */}
        <SectionCard icon={Brain} title="Marvi Integration">
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-zinc-200">World Context</p>
                <p className={`${CONTROL_TEXT} text-zinc-500`}>Inject room state into session context</p>
              </div>
              <Toggle
                checked={config.context.enabled}
                label="World context"
                onChange={v => updatePath('context.enabled', v)}
              />
            </div>
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-zinc-200">Subconscious Surface</p>
                <p className={`${CONTROL_TEXT} text-zinc-500`}>Meaningful transitions appear in subconscious</p>
              </div>
              <Toggle
                checked={config.subconscious.enabled}
                label="Subconscious awareness"
                onChange={v => updatePath('subconscious.enabled', v)}
              />
            </div>
          </div>
        </SectionCard>
      </div>
    </SettingsContent>
  )
}
