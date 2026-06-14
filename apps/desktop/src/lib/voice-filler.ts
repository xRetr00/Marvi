export type VoiceFillerType = 'chime' | 'typing'

export interface VoiceFillerConfig {
  enabled: boolean
  minimumPlayDurationMs: number
  responseDeliveryDelayMs: number
  startDelayMs: number
  type: VoiceFillerType
}

interface FillerSound {
  start: () => void
  stop: () => void
}

interface VoiceFillerDependencies {
  createSound?: (type: VoiceFillerType) => FillerSound | null
}

export interface VoiceFillerController {
  beforeResponse: () => Promise<void>
  stopNow: () => void
  waiting: () => void
}

type BrowserAudioContext = typeof AudioContext

function clampMs(value: number, min: number, max: number, fallback: number): number {
  return Number.isFinite(value) ? Math.max(min, Math.min(max, Math.round(value))) : fallback
}

export function normalizeVoiceFillerConfig(value: unknown): VoiceFillerConfig {
  const record = value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
  const type = record.type === 'chime' ? 'chime' : 'typing'

  return {
    enabled: record.enabled === true,
    minimumPlayDurationMs: clampMs(Number(record.minimum_play_duration_ms), 1000, 5000, 1800),
    responseDeliveryDelayMs: clampMs(Number(record.response_delivery_delay_ms), 0, 1000, 150),
    startDelayMs: clampMs(Number(record.start_delay_ms), 500, 5000, 1400),
    type
  }
}

function createWebAudioFillerSound(type: VoiceFillerType): FillerSound | null {
  if (typeof window === 'undefined') {
    return null
  }

  const audioWindow = window as Window & { webkitAudioContext?: BrowserAudioContext }
  const AudioContextCtor = window.AudioContext || audioWindow.webkitAudioContext

  if (!AudioContextCtor) {
    return null
  }

  let context: AudioContext | null = null
  let interval = 0
  let stopped = true

  const playTone = (frequency: number, durationMs: number, gainValue: number) => {
    if (!context || stopped) {
      return
    }

    const oscillator = context.createOscillator()
    const gain = context.createGain()
    const now = context.currentTime

    oscillator.frequency.value = frequency
    oscillator.type = type === 'chime' ? 'sine' : 'triangle'
    gain.gain.setValueAtTime(0.0001, now)
    gain.gain.exponentialRampToValueAtTime(gainValue, now + 0.015)
    gain.gain.exponentialRampToValueAtTime(0.0001, now + durationMs / 1000)
    oscillator.connect(gain)
    gain.connect(context.destination)
    oscillator.start(now)
    oscillator.stop(now + durationMs / 1000 + 0.03)
  }

  const tick = () => {
    if (type === 'chime') {
      playTone(660, 120, 0.025)
      window.setTimeout(() => playTone(880, 90, 0.018), 130)
      return
    }

    playTone(420 + Math.random() * 180, 35, 0.012)
  }

  return {
    start: () => {
      if (!context || context.state === 'closed') {
        context = new AudioContextCtor()
      }

      stopped = false
      void context.resume()
      tick()
      interval = window.setInterval(tick, type === 'chime' ? 1500 : 260)
    },
    stop: () => {
      stopped = true
      if (interval) {
        window.clearInterval(interval)
        interval = 0
      }
    }
  }
}

function delay(ms: number): Promise<void> {
  if (ms <= 0) {
    return Promise.resolve()
  }

  return new Promise(resolve => window.setTimeout(resolve, ms))
}

export function createVoiceFillerController(
  config: VoiceFillerConfig,
  dependencies: VoiceFillerDependencies = {}
): VoiceFillerController {
  let startTimer = 0
  let startedAt = 0
  let sound: FillerSound | null = null
  const createSound = dependencies.createSound ?? createWebAudioFillerSound

  const clearStartTimer = () => {
    if (startTimer) {
      window.clearTimeout(startTimer)
      startTimer = 0
    }
  }

  const stopSound = () => {
    if (sound) {
      sound.stop()
      sound = null
      startedAt = 0
    }
  }

  return {
    beforeResponse: async () => {
      if (!config.enabled) {
        return
      }

      clearStartTimer()

      if (!sound || startedAt === 0) {
        return
      }

      stopSound()
      await delay(config.responseDeliveryDelayMs)
    },
    stopNow: () => {
      clearStartTimer()
      stopSound()
    },
    waiting: () => {
      if (!config.enabled || startTimer || sound) {
        return
      }

      startTimer = window.setTimeout(() => {
        startTimer = 0
        sound = createSound(config.type)
        if (!sound) {
          return
        }

        startedAt = Date.now()
        sound.start()
      }, config.startDelayMs)
    }
  }
}
