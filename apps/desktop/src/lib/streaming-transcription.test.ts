import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setConnection } from '@/store/session'

import { openStreamingTranscription } from './streaming-transcription'

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  static OPEN = 1
  readyState = FakeWebSocket.OPEN
  binaryType = ''
  sent: unknown[] = []
  private listeners = new Map<string, Array<(event: MessageEvent | Event) => void>>()

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this)
    setTimeout(() => this.emit('open', new Event('open')), 0)
  }

  addEventListener(type: string, listener: (event: MessageEvent | Event) => void) {
    const list = this.listeners.get(type) ?? []
    list.push(listener)
    this.listeners.set(type, list)
  }

  send(value: unknown) {
    this.sent.push(value)
  }

  close() {
    this.readyState = 3
  }

  emit(type: string, event: MessageEvent | Event) {
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event)
    }
  }
}

describe('openStreamingTranscription', () => {
  const originalWebSocket = globalThis.WebSocket

  beforeEach(() => {
    FakeWebSocket.instances = []
    ;(globalThis as { WebSocket: unknown }).WebSocket = FakeWebSocket
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { getGatewayWsUrl: vi.fn(async () => 'ws://127.0.0.1:9119/api/ws?token=t') }
    })
    setConnection({
      authMode: 'token',
      baseUrl: 'http://127.0.0.1:9119',
      isFullscreen: false,
      logs: [],
      mode: 'local',
      nativeOverlayWidth: 0,
      token: 't',
      windowButtonPosition: null,
      wsUrl: 'ws://127.0.0.1:9119/api/ws?token=t'
    })
  })

  afterEach(() => {
    ;(globalThis as { WebSocket: unknown }).WebSocket = originalWebSocket
    setConnection(null)
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('streams float32 frames and resolves the final transcript', async () => {
    const sessionPromise = openStreamingTranscription()
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))

    const ws = FakeWebSocket.instances[0]
    expect(ws.url).toContain('/api/audio/transcribe/stream')
    ws.emit('message', new MessageEvent('message', { data: JSON.stringify({ type: 'ready' }) }))
    const session = await sessionPromise

    session.sendFrame(new Float32Array([0.1, -0.2]))
    expect(ws.sent[0]).toBe(JSON.stringify({ type: 'start', sample_rate: 16000 }))
    expect(ws.sent[1]).toBeInstanceOf(ArrayBuffer)

    const finalPromise = session.finish()
    expect(ws.sent[2]).toBe(JSON.stringify({ type: 'stop' }))
    ws.emit('message', new MessageEvent('message', { data: JSON.stringify({ text: 'hello', type: 'final' }) }))

    await expect(finalPromise).resolves.toBe('hello')
  })

  it('rejects when the backend sends an error after ready', async () => {
    const sessionPromise = openStreamingTranscription()
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))

    const ws = FakeWebSocket.instances[0]
    ws.emit('message', new MessageEvent('message', { data: JSON.stringify({ type: 'ready' }) }))
    const session = await sessionPromise

    const finalPromise = session.finish()
    ws.emit('message', new MessageEvent('message', { data: JSON.stringify({ error: 'torch failed', type: 'error' }) }))

    await expect(finalPromise).rejects.toThrow('torch failed')
  })
})
