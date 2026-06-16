import { resolveGatewayWsUrl } from '@/lib/gateway-ws-url'
import { $connection } from '@/store/session'

export interface WakeWordConfig {
  boost: number
  commandTimeoutMs: number
  cooldownMs: number
  debug: boolean
  enabled: boolean
  phrases: string[]
  provider: string
  sampleRate: number
  threshold: number
}

export interface WakeWordSession {
  sendFrame: (samples: Float32Array) => void
  stop: () => void
}

export interface WakeWordOptions {
  debug?: boolean
  onDetected: (phrase: string) => void
}

const DEFAULT_WAKE_PHRASES = [
  'hey marvi',
  'hi marvi',
  'okay marvi',
  'ok marvi',
  'yo marvi',
  'marvi',
  'hey marve',
  'hey marvy',
  'hey marvie',
  'hey marfi',
  'hey marfe',
  'hey marvey',
  'marve',
  'marvy',
  'marvie',
  'marfi',
  'marfe',
  'marvey'
]

function clampNumber(value: unknown, fallback: number, min: number, max: number): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.max(min, Math.min(max, Math.round(parsed))) : fallback
}

function normalizePhrase(value: unknown): string {
  return String(value ?? '').trim().toLowerCase().replace(/\s+/g, ' ')
}

export function normalizeWakeWordConfig(value: unknown): WakeWordConfig {
  const record = value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
  const rawPhrases = Array.isArray(record.phrases) ? record.phrases : DEFAULT_WAKE_PHRASES
  const phrases = [...new Set(rawPhrases.map(normalizePhrase).filter(Boolean))]

  return {
    boost: Number.isFinite(Number(record.boost)) ? Number(record.boost) : 2,
    commandTimeoutMs: clampNumber(record.command_timeout_ms, 8000, 1000, 30000),
    cooldownMs: clampNumber(record.cooldown_ms, 1200, 0, 10000),
    debug: record.debug === true,
    enabled: record.enabled === true,
    phrases: phrases.length ? phrases : DEFAULT_WAKE_PHRASES,
    provider: normalizePhrase(record.provider) || 'sherpa_onnx',
    sampleRate: clampNumber(record.sample_rate, 16000, 8000, 48000),
    threshold: Number.isFinite(Number(record.threshold)) ? Number(record.threshold) : 0.35
  }
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

export function stripWakePhrase(text: string, phrases: readonly string[]): string {
  let result = text.trim()
  const sorted = [...phrases].map(normalizePhrase).filter(Boolean).sort((a, b) => b.length - a.length)

  for (const phrase of sorted) {
    const pattern = new RegExp(`^${escapeRegExp(phrase)}(?:[\\s,.:;!?-]+|$)`, 'i')
    if (pattern.test(result)) {
      result = result.replace(pattern, '').trim()
      break
    }
  }

  return result
}

function wakeWordUrl(wsUrl: string): string {
  const url = new URL(wsUrl)
  url.pathname = '/api/audio/wake-word/stream'
  url.searchParams.delete('channel')

  return url.toString()
}

export async function openWakeWordSession(options: WakeWordOptions): Promise<WakeWordSession> {
  const conn = $connection.get()

  if (!conn) {
    throw new Error('Marvi gateway is not connected')
  }

  const baseWsUrl = await resolveGatewayWsUrl(window.hermesDesktop, conn)
  const ws = new WebSocket(wakeWordUrl(baseWsUrl))
  ws.binaryType = 'arraybuffer'

  await new Promise<void>((resolve, reject) => {
    const timeout = window.setTimeout(() => reject(new Error('Wake-word detection timed out')), 120_000)

    ws.addEventListener(
      'open',
      () => {
        ws.send(JSON.stringify({ type: 'start', debug: options.debug === true, sample_rate: 16000 }))
      },
      { once: true }
    )

    ws.addEventListener(
      'error',
      () => {
        window.clearTimeout(timeout)
        reject(new Error('Wake-word detection connection failed'))
      },
      { once: true }
    )

    ws.addEventListener('message', event => {
      let payload: unknown

      try {
        payload = JSON.parse(String(event.data))
      } catch {
        return
      }

      const message = payload as { error?: string; phrase?: string; type?: string }
      if (message.type === 'ready') {
        window.clearTimeout(timeout)
        resolve()
      } else if (message.type === 'detected') {
        options.onDetected(message.phrase || '')
      } else if (message.type === 'error') {
        window.clearTimeout(timeout)
        reject(new Error(message.error || 'Wake-word detection failed'))
      }
    })
  })

  return {
    sendFrame: samples => {
      if (ws.readyState !== WebSocket.OPEN) {
        return
      }

      const copy = new Float32Array(samples.length)
      copy.set(samples)
      ws.send(copy.buffer)
    },
    stop: () => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'stop' }))
      }
      ws.close()
    }
  }
}
