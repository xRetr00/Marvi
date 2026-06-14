import { resolveGatewayWsUrl } from '@/lib/gateway-ws-url'
import { $connection } from '@/store/session'

export interface StreamingSttSession {
  finish: () => Promise<string>
  sendFrame: (samples: Float32Array) => void
  stop: () => void
}

export interface StreamingSttOptions {
  onPartial?: (text: string) => void
}

function streamingSttUrl(wsUrl: string): string {
  const url = new URL(wsUrl)
  url.pathname = '/api/audio/transcribe/stream'
  url.searchParams.delete('channel')

  return url.toString()
}

export async function openStreamingSttSession(options: StreamingSttOptions = {}): Promise<StreamingSttSession> {
  const conn = $connection.get()

  if (!conn) {
    throw new Error('Marvi gateway is not connected')
  }

  const baseWsUrl = await resolveGatewayWsUrl(window.hermesDesktop, conn)
  const ws = new WebSocket(streamingSttUrl(baseWsUrl))
  ws.binaryType = 'arraybuffer'

  let finalText = ''
  let finalResolve: ((text: string) => void) | null = null
  let finalReject: ((error: Error) => void) | null = null
  let ready = false

  const finalPromise = new Promise<string>((resolve, reject) => {
    finalResolve = resolve
    finalReject = reject
  })

  await new Promise<void>((resolve, reject) => {
    const timeout = window.setTimeout(() => reject(new Error('Streaming transcription timed out')), 10_000)

    ws.addEventListener(
      'open',
      () => {
        ws.send(JSON.stringify({ type: 'start', sample_rate: 16000 }))
      },
      { once: true }
    )

    ws.addEventListener(
      'error',
      () => {
        window.clearTimeout(timeout)
        reject(new Error('Streaming transcription connection failed'))
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

      const message = payload as { error?: string; text?: string; type?: string }
      if (message.type === 'ready') {
        ready = true
        window.clearTimeout(timeout)
        resolve()
      } else if (message.type === 'partial') {
        const text = message.text || ''
        if (text) {
          finalText = text
          options.onPartial?.(text)
        }
      } else if (message.type === 'final') {
        finalText = message.text || ''
        finalResolve?.(finalText)
        ws.close()
      } else if (message.type === 'error') {
        const error = new Error(message.error || 'Streaming transcription failed')
        window.clearTimeout(timeout)
        if (ready) {
          finalReject?.(error)
        } else {
          reject(error)
        }
      }
    })
  })

  return {
    finish: () => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'end' }))
      }

      return finalPromise
    },
    sendFrame: samples => {
      if (ws.readyState !== WebSocket.OPEN) {
        return
      }

      const copy = new Float32Array(samples.length)
      copy.set(samples)
      ws.send(copy.buffer)
    },
    stop: () => {
      finalResolve?.(finalText)
      ws.close()
    }
  }
}
