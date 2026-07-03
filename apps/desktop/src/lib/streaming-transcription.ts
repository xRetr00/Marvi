import { resolveGatewayWsUrl } from '@hermes/shared'

import { $connection } from '@/store/session'

export interface StreamingTranscriptionSession {
  finish: () => Promise<string>
  sendFrame: (samples: Float32Array) => void
}

function streamingTranscriptionUrl(wsUrl: string): string {
  const url = new URL(wsUrl)
  url.pathname = '/api/audio/transcribe/stream'
  url.searchParams.delete('channel')

  return url.toString()
}

export async function openStreamingTranscription(): Promise<StreamingTranscriptionSession> {
  const conn = $connection.get()

  if (!conn) {
    throw new Error('Marvi gateway is not connected')
  }

  const baseWsUrl = await resolveGatewayWsUrl(window.hermesDesktop, conn)
  const ws = new WebSocket(streamingTranscriptionUrl(baseWsUrl))
  ws.binaryType = 'arraybuffer'

  await new Promise<void>((resolve, reject) => {
    const timeout = window.setTimeout(() => reject(new Error('Streaming transcription timed out')), 120_000)

    ws.addEventListener(
      'open',
      () => ws.send(JSON.stringify({ type: 'start', sample_rate: 16000 })),
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
      const msg = JSON.parse(String(event.data)) as { error?: string; type?: string }

      if (msg.type === 'ready') {
        window.clearTimeout(timeout)
        resolve()
      } else if (msg.type === 'error') {
        window.clearTimeout(timeout)
        reject(new Error(msg.error || 'Streaming transcription failed'))
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
    finish: () =>
      new Promise((resolve, reject) => {
        ws.addEventListener('message', event => {
          const msg = JSON.parse(String(event.data)) as { error?: string; text?: string; type?: string }

          if (msg.type === 'final') {
            ws.close()
            resolve((msg.text || '').trim())
          } else if (msg.type === 'error') {
            ws.close()
            reject(new Error(msg.error || 'Streaming transcription failed'))
          }
        })
        ws.addEventListener('error', () => reject(new Error('Streaming transcription connection failed')), { once: true })
        ws.addEventListener('close', () => resolve(''), { once: true })

        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'stop' }))
        }
      })
  }
}
