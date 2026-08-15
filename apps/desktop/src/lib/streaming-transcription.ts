import { resolveGatewayWsUrl } from '@hermes/shared'

import { $connection } from '@/store/session'

export interface StreamingTranscriptionSession {
  checkTurn: () => Promise<boolean | null>
  finish: () => Promise<string>
  sendFrame: (samples: Float32Array) => void
}

export interface StreamingTranscriptionOptions {
  /** Called with each partial transcript as the user speaks (trimmed). */
  onPartial?: (text: string) => void
}

function streamingTranscriptionUrl(wsUrl: string): string {
  const url = new URL(wsUrl)
  url.pathname = '/api/audio/transcribe/stream'
  url.searchParams.delete('channel')

  return url.toString()
}

export async function openStreamingTranscription(
  options?: StreamingTranscriptionOptions
): Promise<StreamingTranscriptionSession> {
  const conn = $connection.get()

  if (!conn) {
    throw new Error('Marvi gateway is not connected')
  }

  const baseWsUrl = await resolveGatewayWsUrl(window.hermesDesktop, conn)
  const ws = new WebSocket(streamingTranscriptionUrl(baseWsUrl))
  ws.binaryType = 'arraybuffer'

  // A single persistent message router for the whole session lifetime. Two
  // separate `addEventListener('message', ...)` blocks (one for open/ready,
  // one added later inside `finish`) would race: any `partial` (or even
  // `final`) arriving between them is silently dropped. Routing by
  // `msg.type` through one listener guarantees nothing in-flight is lost.
  let resolveReady: (() => void) | null = null
  let rejectReady: ((error: Error) => void) | null = null
  let resolveFinish: ((text: string) => void) | null = null
  let rejectFinish: ((error: Error) => void) | null = null
  let resolveTurn: ((complete: boolean | null) => void) | null = null
  let rejectTurn: ((error: Error) => void) | null = null
  let settledFinish = false

  const settleFinish = (fn: (() => void) | null) => {
    if (settledFinish) {
      return
    }

    settledFinish = true
    fn?.()
  }

  ws.addEventListener('message', event => {
    let msg: { complete?: boolean; error?: string; text?: string; type?: string }

    try {
      msg = JSON.parse(String(event.data)) as { complete?: boolean; error?: string; text?: string; type?: string }
    } catch {
      return
    }

    if (msg.type === 'ready') {
      resolveReady?.()
      resolveReady = null
      rejectReady = null

      return
    }

    if (msg.type === 'partial') {
      options?.onPartial?.((msg.text || '').trim())

      return
    }

    if (msg.type === 'final') {
      const text = (msg.text || '').trim()
      ws.close()
      settleFinish(() => resolveFinish?.(text))

      return
    }

    if (msg.type === 'turn') {
      resolveTurn?.(typeof msg.complete === 'boolean' ? msg.complete : null)
      resolveTurn = null
      rejectTurn = null

      return
    }

    if (msg.type === 'error') {
      const error = new Error(msg.error || 'Streaming transcription failed')

      if (rejectReady) {
        rejectReady(error)
        resolveReady = null
        rejectReady = null
      }

      ws.close()
      rejectTurn?.(error)
      resolveTurn = null
      rejectTurn = null
      settleFinish(() => rejectFinish?.(error))
    }
  })

  ws.addEventListener('close', () => {
    // If the socket closes before a final arrived, resolve finish with ''
    // rather than leaving the caller hanging.
    settleFinish(() => resolveFinish?.(''))
  })

  await new Promise<void>((resolve, reject) => {
    const timeout = window.setTimeout(() => reject(new Error('Streaming transcription timed out')), 120_000)

    resolveReady = () => {
      window.clearTimeout(timeout)
      resolve()
    }

    rejectReady = error => {
      window.clearTimeout(timeout)
      reject(error)
    }

    ws.addEventListener(
      'open',
      () => ws.send(JSON.stringify({ type: 'start', sample_rate: 16000 })),
      { once: true }
    )
    ws.addEventListener(
      'error',
      () => {
        window.clearTimeout(timeout)
        rejectReady?.(new Error('Streaming transcription connection failed'))
        resolveReady = null
        rejectReady = null
      },
      { once: true }
    )
  })

  return {
    checkTurn: () =>
      new Promise(resolve => {
        if (ws.readyState !== WebSocket.OPEN) {
          resolve(null)

          return
        }

        resolveTurn = resolve
        rejectTurn = () => resolve(null)
        ws.send(JSON.stringify({ type: 'turn' }))
      }),
    sendFrame: samples => {
      if (ws.readyState !== WebSocket.OPEN) {
        return
      }

      const copy = new Float32Array(samples.length)
      copy.set(samples)
      ws.send(copy.buffer)
    },
    finish: () =>
      new Promise((resolve, reject) => {
        resolveFinish = resolve
        rejectFinish = reject

        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'stop' }))
        } else if (ws.readyState === WebSocket.CLOSED) {
          settleFinish(() => resolve(''))
        }
      })
  }
}
