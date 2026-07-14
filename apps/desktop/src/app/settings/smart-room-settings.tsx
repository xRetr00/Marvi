import { useCallback, useEffect, useState } from 'react'
import { getHermesConfigRecord, saveHermesConfig } from '@/hermes'
import { useI18n } from '@/i18n'
import { notifyError } from '@/store/notifications'
import {
  Box,
  Brain,
  Moon,
  RefreshCw,
  Cloud,
  Zap,
  AlertTriangle,
  Settings2,
  Monitor,
  SlidersHorizontal
} from '@/lib/icons'

import { CONTROL_TEXT } from './constants'
import { OverlayMain } from '../overlays/overlay-split-layout'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface SmartRoomConfig {
  enabled: boolean
  mqtt: { broker: string; port: number }
  context: { enabled: boolean }
  subconscious: { enabled: boolean }
  tuya: {
    bulb: { ip: string; local_key: string; protocol: string }
    he20: { ip: string; local_key: string; protocol: string }
  }
  esp32: {
    room_id: string
    presence_topic: string
    rssi_enter_threshold: number
    rssi_exit_threshold: number
    exit_timeout: number
  }
  owntracks: {
    topic: string
    zones: string[]
  }
  automations: {
    adaptive_light: { enabled: boolean; debounce: number; exit_timeout: number }
    alarm: { enabled: boolean; daily_time: string; duration_minutes: number; flash_interval_ms: number }
    evening_sleep: { enabled: boolean; time: string }
    work_return: { enabled: boolean; work_hours_start: string; work_hours_end: string; settle_delay: number }
    daily_reset: string
  }
  scenes: Record<string, {
    color_temp?: number
    brightness?: number
    transition?: number
    rgb?: number[]
    flash?: boolean
    flash_interval?: number
  }>
}

const DEFAULT_CONFIG: SmartRoomConfig = {
  enabled: true,
  mqtt: { broker: '127.0.0.1', port: 1883 },
  context: { enabled: true },
  subconscious: { enabled: true },
  tuya: {
    bulb: { ip: '', local_key: '', protocol: '3.3' },
    he20: { ip: '', local_key: '', protocol: '3.3' },
  },
  esp32: {
    room_id: 'smart_room',
    presence_topic: 'espresense/rooms/smart_room/#',
    rssi_enter_threshold: -70,
    rssi_exit_threshold: -85,
    exit_timeout: 60,
  },
  owntracks: { topic: 'owntracks/shereef/#', zones: ['home', 'university', 'bakery'] },
  automations: {
    adaptive_light: { enabled: true, debounce: 3, exit_timeout: 60 },
    alarm: { enabled: false, daily_time: '23:30', duration_minutes: 30, flash_interval_ms: 500 },
    evening_sleep: { enabled: true, time: '18:00' },
    work_return: { enabled: true, work_hours_start: '06:00', work_hours_end: '10:00', settle_delay: 300 },
    daily_reset: '00:00',
  },
  scenes: {
    reading: { color_temp: 3000, brightness: 70, transition: 2 },
    focus: { color_temp: 5000, brightness: 100, transition: 2 },
    relax: { color_temp: 2700, rgb: [255, 180, 80], brightness: 40, transition: 3 },
    night: { color_temp: 2200, rgb: [255, 120, 40], brightness: 15, transition: 3 },
    alarm: { color_temp: 6500, brightness: 100, flash: true, flash_interval: 500 },
  },
}

const MODES = [
  { id: 'reading', label: 'Reading', icon: '📖', desc: 'Warm 3000K @ 70%' },
  { id: 'focus', label: 'Focus', icon: '🧠', desc: 'Cool 5000K @ 100%' },
  { id: 'relax', label: 'Relax', icon: '😌', desc: 'Amber 2700K @ 40%' },
  { id: 'sleep', label: 'Sleep', icon: '😴', desc: 'Lights off, darkness' },
  { id: 'alarm', label: 'Alarm', icon: '🚨', desc: 'Flash bright white' },
  { id: 'off', label: 'Off', icon: '⏻', desc: 'Lights off' },
] as const

const AUTOMATIONS = [
  { key: 'adaptive_light', label: 'Adaptive Light (Presence)', desc: 'Turn on/off based on room presence' },
  { key: 'alarm', label: 'Daily Alarm Flash', desc: 'Bright flash alarm at scheduled time' },
  { key: 'evening_sleep', label: 'Evening Sleep', desc: 'Auto sleep mode at 6 PM' },
  { key: 'work_return', label: 'Work Return Sleep', desc: 'Auto sleep when arriving home from work' },
] as const

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function StatusDot({ online }: { online: boolean }) {
  return (
    <span
 className={`inline-block h-2 w-2 rounded-full ${online ? 'bg-emerald-500' : 'bg-red-500/60'}`}
    />
  )
}

function Toggle({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      onClick={() => onChange(!checked)}
      className={`relative h-5 w-9 rounded-full transition-colors ${checked ? 'bg-primary' : 'bg-zinc-700'}`}
    >
      <span
        className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${checked ? 'translate-x-4' : 'translate-x-0.5'}`}
      />
    </button>
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

function TextField({ label, value, onChange, placeholder, type = 'text', hint }: {
  label: string
  value: string | number
  onChange: (v: string) => void
  placeholder?: string
  type?: string
  hint?: string
}) {
  return (
    <div className="mb-3">
      <label className={`mb-1 block ${CONTROL_TEXT} text-zinc-400`}>{label}</label>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full rounded-md border border-zinc-800 bg-zinc-950 px-3 py-1.5 text-sm text-zinc-200 placeholder:text-zinc-600 focus:border-primary focus:outline-none"
      />
      {hint && <p className={`mt-1 ${CONTROL_TEXT} text-zinc-500`}>{hint}</p>}
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
  const [liveState, setLiveState] = useState<any>(null)
  const [loading, setLoading] = useState(true)

  // Load config
  useEffect(() => {
    getHermesConfigRecord()
      .then((cfg: any) => {
        const sr = cfg?.smart_room
        if (sr) {
          setConfig({ ...DEFAULT_CONFIG, ...sr })
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  // Poll live state from runtime (placeholder — runtime not running yet)
  useEffect(() => {
    const poll = async () => {
      try {
        // When the runtime is running, it serves state on localhost:17842
        // For now, this is a placeholder that silently fails
        const resp = await fetch('http://127.0.0.1:17842/state', { signal: AbortSignal.timeout(2000) })
        if (resp.ok) {
          setLiveState(await resp.json())
        }
      } catch {
        // Runtime not running — expected during setup
      }
    }
    poll()
    const interval = setInterval(poll, 10000)
    return () => clearInterval(interval)
  }, [])

  const updateConfig = useCallback(async (newConfig: SmartRoomConfig) => {
    setConfig(newConfig)
    setSaving(true)
    try {
      const cfg = await getHermesConfigRecord()
      cfg.smart_room = newConfig
      await saveHermesConfig(cfg)
    } catch (err) {
      notifyError(err, 'Failed to save smart room config')
    } finally {
      setSaving(false)
    }
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

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-zinc-500">
        Loading smart room settings...
      </div>
    )
  }

  const runtimeUp = !!liveState
  const devices = liveState?.devices || {}
  const light = liveState?.light || config.scenes
  const presence = liveState?.presence || {}
  const activeMode = liveState?.modes
    ? Object.entries(liveState.modes).find(([k, v]) => v && !['manual_override', 'work_return'].includes(k))?.[0]
    : null

  return (
    <OverlayMain className="space-y-4 px-4 pb-8">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Box className="h-5 w-5 text-primary" />
          <h2 className="text-lg font-semibold text-zinc-100">Smart Room</h2>
          <span className={`ml-2 rounded-full px-2 py-0.5 ${CONTROL_TEXT} ${runtimeUp ? 'bg-emerald-500/20 text-emerald-400' : 'bg-zinc-800 text-zinc-500'}`}>
            {runtimeUp ? 'Runtime Online' : 'Runtime Offline'}
          </span>
        </div>
        <div className="flex items-center gap-2">
          {saving && <span className={`${CONTROL_TEXT} text-zinc-500`}>Saving...</span>}
          <button
            onClick={() => { window.location.reload() }}
            className="rounded-md p-1.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
          >
            <RefreshCw className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Enable toggle */}
      <SectionCard title="Plugin" icon={Settings2}>
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm text-zinc-200">Smart Room Engine</p>
            <p className={`${CONTROL_TEXT} text-zinc-500`}>Native presence fusion + Tuya control + automations</p>
          </div>
          <Toggle checked={config.enabled} onChange={(v) => updatePath('enabled', v)} />
        </div>
      </SectionCard>

      {/* Connection Status */}
      <SectionCard title="Connection Status" icon={Cloud}>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="flex items-center gap-2">
            <StatusDot online={runtimeUp} />
            <span className={`${CONTROL_TEXT} text-zinc-400`}>MQTT</span>
          </div>
          <div className="flex items-center gap-2">
            <StatusDot online={!!devices.esp32?.online} />
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
      <SectionCard title="Room State" icon={Brain}>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <div>
            <p className={`${CONTROL_TEXT} text-zinc-500`}>Presence</p>
            <p className="text-sm text-zinc-200">
              {presence.detected ? `Detected (${(presence.confidence * 100).toFixed(0)}%)` : 'Not detected'}
            </p>
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
            <p className="text-sm capitalize text-zinc-200">{liveState?.location?.zone || 'home'}</p>
          </div>
        </div>
      </SectionCard>

      {/* Mode Buttons */}
      <SectionCard title="Modes" icon={Zap}>
        <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
          {MODES.map((mode) => (
            <button
              key={mode.id}
              onClick={() => updatePath('_mode_action', mode.id)}
              className={`flex flex-col items-center rounded-lg border p-3 transition-colors ${
                activeMode === mode.id
                  ? 'border-primary bg-primary/10 text-primary'
                  : 'border-zinc-800 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200'
              }`}
            >
              <span className="text-lg">{mode.icon}</span>
              <span className="mt-1 text-xs font-medium">{mode.label}</span>
              <span className={`mt-0.5 text-[10px] text-zinc-600`}>{mode.desc}</span>
            </button>
          ))}
        </div>
        <p className={`mt-2 ${CONTROL_TEXT} text-zinc-500`}>
          Mode buttons require the runtime to be running. Configure devices below first.
        </p>
      </SectionCard>

      {/* Automations */}
      <SectionCard title="Automations" icon={RefreshCw}>
        <div className="space-y-3">
          {AUTOMATIONS.map((auto) => {
            const enabled = (config.automations as any)?.[auto.key]?.enabled ?? false
            return (
              <div key={auto.key} className="flex items-center justify-between">
                <div>
                  <p className="text-sm text-zinc-200">{auto.label}</p>
                  <p className={`${CONTROL_TEXT} text-zinc-500`}>{auto.desc}</p>
                </div>
                <Toggle
                  checked={enabled}
                  onChange={(v) => updatePath(`automations.${auto.key}.enabled`, v)}
                />
              </div>
            )
          })}
          <div className="flex items-center justify-between border-t border-zinc-800 pt-3">
            <div>
              <p className="text-sm text-zinc-200">Daily Reset</p>
              <p className={`${CONTROL_TEXT} text-zinc-500`}>Reset mode flags at midnight</p>
            </div>
            <input
              type="time"
              value={config.automations.daily_reset}
              onChange={(e) => updatePath('automations.daily_reset', e.target.value)}
              className="rounded-md border border-zinc-800 bg-zinc-950 px-2 py-1 text-sm text-zinc-200"
            />
          </div>
        </div>
      </SectionCard>

      {/* Scene Presets */}
      <SectionCard title="Scene Presets" icon={SlidersHorizontal}>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {Object.entries(config.scenes).map(([name, scene]) => (
            <div key={name} className="rounded-lg border border-zinc-800 bg-zinc-950 p-3">
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
      <SectionCard title="Tuya Devices" icon={Cloud}>
        <div className="mb-3 flex items-start gap-2 rounded-md bg-amber-500/10 p-2 text-amber-400">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <p className={`${CONTROL_TEXT}`}>
            Local keys are needed <strong>once</strong>. Get them from the Tuya IoT Portal (free).
            After that, all control is LAN-only — no cloud. Run <code className="text-amber-300">python scripts/discover_tuya.py</code> to find device IPs.
          </p>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <p className="mb-2 text-sm font-medium text-zinc-300">RGBCW Bulb</p>
            <TextField label="IP Address" value={config.tuya.bulb.ip} onChange={(v) => updatePath('tuya.bulb.ip', v)} placeholder="192.168.1.x" />
            <TextField label="Local Key" value={config.tuya.bulb.local_key} onChange={(v) => updatePath('tuya.bulb.local_key', v)} placeholder="16-char hex key" type="password" />
            <TextField label="Protocol" value={config.tuya.bulb.protocol} onChange={(v) => updatePath('tuya.bulb.protocol', v)} placeholder="3.3" />
          </div>
          <div>
            <p className="mb-2 text-sm font-medium text-zinc-300">HE20 Presence Sensor</p>
            <TextField label="IP Address" value={config.tuya.he20.ip} onChange={(v) => updatePath('tuya.he20.ip', v)} placeholder="192.168.1.x" />
            <TextField label="Local Key" value={config.tuya.he20.local_key} onChange={(v) => updatePath('tuya.he20.local_key', v)} placeholder="16-char hex key" type="password" />
            <TextField label="Protocol" value={config.tuya.he20.protocol} onChange={(v) => updatePath('tuya.he20.protocol', v)} placeholder="3.3" />
          </div>
        </div>
      </SectionCard>

      {/* ESP32 Config */}
      <SectionCard title="ESP32 (ESPresense)" icon={Monitor}>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <TextField label="Room ID" value={config.esp32.room_id} onChange={(v) => updatePath('esp32.room_id', v)} placeholder="smart_room" />
            <TextField label="Presence Topic" value={config.esp32.presence_topic} onChange={(v) => updatePath('esp32.presence_topic', v)} placeholder="espresense/rooms/smart_room/#" />
          </div>
          <div>
            <TextField label="RSSI Enter Threshold" type="number" value={config.esp32.rssi_enter_threshold} onChange={(v) => updatePath('esp32.rssi_enter_threshold', parseInt(v) || -70)} hint="dBm — closer = higher. -70 is typical." />
            <TextField label="RSSI Exit Threshold" type="number" value={config.esp32.rssi_exit_threshold} onChange={(v) => updatePath('esp32.rssi_exit_threshold', parseInt(v) || -85)} hint="dBm — must drop below this to leave." />
            <TextField label="Exit Timeout (seconds)" type="number" value={config.esp32.exit_timeout} onChange={(v) => updatePath('esp32.exit_timeout', parseInt(v) || 60)} />
          </div>
        </div>
      </SectionCard>

      {/* OwnTracks Config */}
      <SectionCard title="OwnTracks (iPhone Location)" icon={Moon}>
        <TextField label="MQTT Topic" value={config.owntracks.topic} onChange={(v) => updatePath('owntracks.topic', v)} placeholder="owntracks/shereef/#" />
        <div>
          <label className={`mb-1 block ${CONTROL_TEXT} text-zinc-400`}>Geofence Zones</label>
          <div className="flex flex-wrap gap-2">
            {config.owntracks.zones.map((zone, i) => (
              <span key={zone} className="rounded-full bg-zinc-800 px-3 py-1 text-xs text-zinc-300">
                {zone}
                <button
                  onClick={() => {
                    const zones = config.owntracks.zones.filter((_, idx) => idx !== i)
                    updatePath('owntracks.zones', zones)
                  }}
                  className="ml-2 text-zinc-500 hover:text-red-400"
                >
                  ×
                </button>
              </span>
            ))}
            <input
              type="text"
              placeholder="add zone..."
              className="w-24 rounded-full border border-zinc-800 bg-zinc-950 px-3 py-1 text-xs text-zinc-200"
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  const val = (e.target as HTMLInputElement).value.trim()
                  if (val) {
                    updatePath('owntracks.zones', [...config.owntracks.zones, val])
                    ;(e.target as HTMLInputElement).value = ''
                  }
                }
              }}
            />
          </div>
        </div>
      </SectionCard>

      {/* MQTT Broker Config */}
      <SectionCard title="MQTT Broker" icon={Cloud}>
        <div className="grid grid-cols-2 gap-4">
          <TextField label="Broker IP" value={config.mqtt.broker} onChange={(v) => updatePath('mqtt.broker', v)} placeholder="127.0.0.1" />
          <TextField label="Port" type="number" value={config.mqtt.port} onChange={(v) => updatePath('mqtt.port', parseInt(v) || 1883)} />
        </div>
      </SectionCard>

      {/* Context & Subconscious */}
      <SectionCard title="Marvi Integration" icon={Brain}>
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-zinc-200">World Context</p>
              <p className={`${CONTROL_TEXT} text-zinc-500`}>Inject room state into session context</p>
            </div>
            <Toggle checked={config.context.enabled} onChange={(v) => updatePath('context.enabled', v)} />
          </div>
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-zinc-200">Subconscious Surface</p>
              <p className={`${CONTROL_TEXT} text-zinc-500`}>Meaningful transitions appear in subconscious</p>
            </div>
            <Toggle checked={config.subconscious.enabled} onChange={(v) => updatePath('subconscious.enabled', v)} />
          </div>
        </div>
      </SectionCard>
    </OverlayMain>
  )
}
