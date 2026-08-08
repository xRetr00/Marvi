import { resolveGatewayWsUrl, type ResolveGatewayWsUrlDeps } from '@hermes/shared'

import {
  createDuplexAudioPlayer,
  type DuplexAudioPlayer,
  type DuplexMicCapture,
  startDuplexMicCapture
} from './duplex-audio'
import { type DuplexCard, parseDuplexServerEvent } from './duplex-protocol'
import { type DuplexCommand, DuplexSessionMachine, type DuplexSessionState } from './duplex-session'

const CONNECT_TIMEOUT_MS = 8000
const READY_TIMEOUT_MS = 30_000

export interface DuplexGatewayConnection {
  authMode?: string | null
  profile?: null | string
  wsUrl: string
}

export interface DuplexConnectOptions {
  /** Fires on every state change while the session is live. */
  onState: (state: DuplexSessionState) => void
  /** Fires with the mic's live amplitude (0..1) once capture has started. */
  onLevel?: (level: number) => void
  /**
   * Fires once, whenever the duplex endpoint can't be used — unreachable,
   * auth failed, mic denied, or the socket closes/errors after connecting.
   * Callers should fall back to the legacy voice flow; this client never
   * retries on its own.
   */
  onUnavailable: (reason: string) => void
  /** Fires after Marvi has spoken a model-requested voice-session goodbye. */
  onConversationEnd?: () => void
  /** Surfaces a model-requested card through the shared voice island. */
  onCard?: (card: DuplexCard) => void

  // Test seams — production callers rely on the defaults below.
  getConnection?: () => Promise<DuplexGatewayConnection | null>
  gatewayDeps?: ResolveGatewayWsUrlDeps
  createWebSocket?: (url: string) => WebSocket
  audioPlayerFactory?: () => DuplexAudioPlayer
  micCaptureFactory?: typeof startDuplexMicCapture
  readyTimeoutMs?: number
}

export interface DuplexController {
  /** Stop the session: sends `stop`, tears down mic + audio, closes the socket. */
  stop: () => void
}

function duplexWsUrl(baseWsUrl: string): string {
  const url = new URL(baseWsUrl)
  url.pathname = '/api/voice/duplex'
  url.searchParams.delete('channel')

  return url.toString()
}

function defaultGetConnection(): Promise<DuplexGatewayConnection | null> {
  return Promise.resolve()
    .then(() => window.hermesDesktop?.getConnection?.())
    .then(conn => (conn ? { authMode: conn.authMode, profile: conn.profile, wsUrl: conn.wsUrl } : null))
    .catch(() => null)
}

/**
 * Connect a duplex voice session against `/api/voice/duplex`. Resolves to a
 * controller on success, or `null` if the endpoint is unreachable (the
 * common case until the server workstream ships it) — `onUnavailable` fires
 * exactly once in that case with a human-readable reason, and no mic
 * permission is ever requested.
 *
 * Mic capture only starts after the server's first `ready` event, so a
 * reachable-but-not-yet-ready endpoint (or a socket that opens and then
 * immediately errors) never triggers a spurious microphone prompt.
 */
export async function connectDuplexVoice(options: DuplexConnectOptions): Promise<DuplexController | null> {
  const getConnection = options.getConnection ?? defaultGetConnection
  const gatewayDeps = options.gatewayDeps ?? (window.hermesDesktop as unknown as ResolveGatewayWsUrlDeps) ?? {}
  const createWebSocket = options.createWebSocket ?? (url => new WebSocket(url))
  const createPlayer = options.audioPlayerFactory ?? createDuplexAudioPlayer
  const startMic = options.micCaptureFactory ?? startDuplexMicCapture

  const conn = await getConnection().catch(() => null)

  if (!conn) {
    options.onUnavailable('no gateway connection available')

    return null
  }

  let baseWsUrl: string

  try {
    baseWsUrl = await resolveGatewayWsUrl(gatewayDeps, conn)
  } catch (error) {
    options.onUnavailable(error instanceof Error ? error.message : 'failed to resolve gateway websocket url')

    return null
  }

  let ws: WebSocket

  try {
    ws = createWebSocket(duplexWsUrl(baseWsUrl))
  } catch (error) {
    options.onUnavailable(error instanceof Error ? error.message : 'failed to open duplex websocket')

    return null
  }

  const machine = new DuplexSessionMachine()
  const player = createPlayer()
  let mic: DuplexMicCapture | null = null
  let stopped = false
  let readyTimer: number | null = null

  const clearReadyTimer = () => {
    if (readyTimer !== null) {
      window.clearTimeout(readyTimer)
      readyTimer = null
    }
  }

  const sendJson = (payload: unknown) => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload))
    }
  }

  const runCommands = (commands: DuplexCommand[]) => {
    for (const command of commands) {
      switch (command.type) {
        case 'reset_playback':
          player.reset(command.sampleRate)

          break

        case 'enqueue_audio':
          player.enqueueChunk(command.data, command.seq)

          break

        case 'expect_playback_end':
          player.expectEnd()

          break

        case 'send_playback_done':
          sendJson({ type: 'playback_done' })

          break

        case 'send_stop':
          sendJson({ type: 'stop' })

          break
      }
    }
  }

  const emitState = () => options.onState(machine.state)

  const teardown = (reason: string) => {
    if (stopped) {
      return
    }

    stopped = true
    clearReadyTimer()
    mic?.stop()
    mic = null
    player.destroy()

    try {
      ws.close()
    } catch {
      // already closing/closed
    }

    options.onUnavailable(reason)
  }

  player.onDrained(() => {
    if (stopped) {
      return
    }

    runCommands(machine.notifyPlaybackFinished())
    emitState()
  })

  const connected = await new Promise<boolean>(resolve => {
    const timeout = window.setTimeout(() => resolve(false), CONNECT_TIMEOUT_MS)
    ws.addEventListener(
      'open',
      () => {
        window.clearTimeout(timeout)
        resolve(true)
      },
      { once: true }
    )
    ws.addEventListener(
      'error',
      () => {
        window.clearTimeout(timeout)
        resolve(false)
      },
      { once: true }
    )
  })

  if (!connected || stopped) {
    player.destroy()

    try {
      ws.close()
    } catch {
      // already closing/closed
    }

    options.onUnavailable('duplex websocket failed to connect')

    return null
  }

  readyTimer = window.setTimeout(
    () => teardown('duplex speech recognition did not become ready in time'),
    options.readyTimeoutMs ?? READY_TIMEOUT_MS
  )

  ws.addEventListener('message', event => {
    if (stopped) {
      return
    }

    let payload: unknown

    try {
      payload = JSON.parse(String(event.data))
    } catch {
      return
    }

    const parsedEvent = parseDuplexServerEvent(payload)
    const wasReady = machine.state.phase !== 'connecting'
    runCommands(machine.applyRawEvent(payload))
    emitState()

    if (parsedEvent?.type === 'card_show') {
      options.onCard?.(parsedEvent.card)
    }

    if (parsedEvent?.type === 'conversation_end') {
      stopped = true
      clearReadyTimer()
      mic?.stop()
      mic = null
      player.destroy()
      options.onConversationEnd?.()

      try {
        ws.close()
      } catch {
        // already closing/closed
      }

      return
    }

    // Start capturing the mic on the transition INTO 'listening' from
    // 'connecting' (i.e. the first `ready`), not on every subsequent
    // `ready`-shaped message — mic capture should only ever start once.
    if (!wasReady && machine.state.phase === 'listening' && !mic) {
      clearReadyTimer()
      void startMic({
        onError: error => teardown(error.message),
        onFrame: base64 => sendJson({ type: 'audio', data: base64 }),
        onLevel: options.onLevel
      })
        .then(capture => {
          if (stopped) {
            capture.stop()
          } else {
            mic = capture
          }
        })
        .catch(error => teardown(error instanceof Error ? error.message : 'microphone unavailable'))
    }
  })

  ws.addEventListener('close', () => teardown('duplex websocket closed'))
  ws.addEventListener('error', () => teardown('duplex websocket error'))

  emitState()

  return {
    stop: () => {
      if (stopped) {
        return
      }

      stopped = true
      clearReadyTimer()
      runCommands(machine.close())
      mic?.stop()
      mic = null
      player.destroy()

      try {
        ws.close()
      } catch {
        // already closing/closed
      }
    }
  }
}
