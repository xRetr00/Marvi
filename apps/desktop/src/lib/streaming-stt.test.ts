import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'

import { openStreamingSttSession } from './streaming-stt'

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  static OPEN = 1

  binaryType = ''
  readyState = FakeWebSocket.OPEN
  sent: unknown[] = []
  private listeners = new Map<string, Array<(event: { data?: unknown }) => void>>()

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this)
    setTimeout(() => this.emit('open', {}), 0)
  }

  addEventListener(type: string, listener: (event: { data?: unknown }) => void): void {
    const next = this.listeners.get(type) ?? []
    next.push(listener)
    this.listeners.set(type, next)
  }

  close(): void {
    this.readyState = 3
  }

  emit(type: string, event: { data?: unknown }): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event)
    }
  }

  receive(payload: unknown): void {
    this.emit('message', { data: JSON.stringify(payload) })
  }

  send(payload: unknown): void {
    this.sent.push(payload)
  }
}

describe('openStreamingSttSession', () => {
  const originalWebSocket = globalThis.WebSocket

  beforeEach(() => {
    FakeWebSocket.instances = []
    ;(globalThis as { WebSocket: unknown }).WebSocket = FakeWebSocket
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = {
      getGatewayWsUrl: vi.fn(async () => 'ws://127.0.0.1:9119/api/ws?token=test')
    }
    $connection.set({
      baseUrl: 'http://127.0.0.1:9119',
      isFullscreen: false,
      nativeOverlayWidth: 0,
      token: 'test',
      wsUrl: 'ws://127.0.0.1:9119/api/ws?token=test',
      logs: [],
      windowButtonPosition: null
    })
  })

  afterEach(() => {
    ;(globalThis as { WebSocket: unknown }).WebSocket = originalWebSocket
    Reflect.deleteProperty(window, 'hermesDesktop')
    $connection.set(null)
  })

  it('forwards partial transcript messages to the caller', async () => {
    const partials: string[] = []
    const promise = openStreamingSttSession({ onPartial: text => partials.push(text) })

    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    const ws = FakeWebSocket.instances[0]
    ws.receive({ type: 'ready' })
    const session = await promise

    ws.receive({ type: 'partial', text: 'hello' })
    ws.receive({ type: 'partial', text: 'hello world' })
    ws.receive({ type: 'final', text: 'hello world' })

    await expect(session.finish()).resolves.toBe('hello world')
    expect(partials).toEqual(['hello', 'hello world'])
  })
})
