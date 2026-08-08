// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DuplexMicCaptureOptions } from './duplex-audio'
import { connectDuplexVoice } from './duplex-client'
import type { DuplexSessionState } from './duplex-session'

type Listener = (event?: unknown) => void

/** Minimal hand-rolled WebSocket double: just enough of the API surface
 *  duplex-client.ts touches (addEventListener/once, send, close, readyState). */
class FakeWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3

  readyState = FakeWebSocket.CONNECTING
  sent: unknown[] = []
  private listeners = new Map<string, Array<{ fn: Listener; once?: boolean }>>()

  addEventListener(type: string, fn: Listener, options?: { once?: boolean }) {
    const list = this.listeners.get(type) ?? []
    list.push({ fn, once: options?.once })
    this.listeners.set(type, list)
  }

  send(data: string) {
    this.sent.push(JSON.parse(data))
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED
    this.emit('close')
  }

  emit(type: string, event?: unknown) {
    const list = this.listeners.get(type) ?? []

    // Snapshot before invoking — a handler may add/remove listeners.
    for (const entry of [...list]) {
      entry.fn(event)
    }

    if (list.some(entry => entry.once)) {
      this.listeners.set(
        type,
        list.filter(entry => !entry.once)
      )
    }
  }

  open() {
    this.readyState = FakeWebSocket.OPEN
    this.emit('open')
  }

  message(payload: unknown) {
    this.emit('message', { data: JSON.stringify(payload) })
  }
}

/**
 * `connectDuplexVoice` is async and only calls `createWebSocket` after
 * awaiting `getConnection()` + `resolveGatewayWsUrl()` — i.e. a couple of
 * microtask ticks after being invoked, and BEFORE it resolves (it blocks
 * awaiting the socket's open/error event). Tests need to drive the fake
 * socket (open/message/close) concurrently with that in-flight call, not
 * after awaiting it — this gate hands back the socket the moment
 * `createWebSocket` is actually called, without guessing microtask counts.
 */
function socketGate() {
  let resolve!: (socket: FakeWebSocket) => void

  const ready = new Promise<FakeWebSocket>(r => {
    resolve = r
  })

  const createWebSocket = () => {
    const socket = new FakeWebSocket()
    resolve(socket)

    return socket as unknown as WebSocket
  }

  return { createWebSocket, ready }
}

const DEFAULT_CONNECTION = { authMode: 'token', profile: null, wsUrl: 'ws://gateway.local/api/ws?token=abc' }

// Deliberately NOT annotated with `: DuplexAudioPlayer` — that would widen
// each method back down to the interface's plain function type and lose the
// vi.Mock methods (.mockClear() etc.) tests below rely on. Structural typing
// still lets this satisfy `DuplexAudioPlayer` wherever it's passed in.
function fakeAudioPlayer() {
  const player = {
    drainedCallback: null as (() => void) | null,
    destroy: vi.fn(),
    enqueueChunk: vi.fn(),
    expectEnd: vi.fn(),
    onDrained: vi.fn((cb: (() => void) | null) => {
      player.drainedCallback = cb
    }),
    reset: vi.fn()
  }

  return player
}

function fakeMicCapture() {
  const stop = vi.fn()
  let lastOptions: DuplexMicCaptureOptions | undefined

  const factory = vi.fn(async (options: DuplexMicCaptureOptions) => {
    lastOptions = options

    return { stop }
  })

  return { factory, getOptions: () => lastOptions, stop }
}

beforeEach(() => {
  vi.stubGlobal('WebSocket', FakeWebSocket)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('connectDuplexVoice', () => {
  it('reports unavailable and never requests the mic when there is no gateway connection', async () => {
    const onUnavailable = vi.fn()
    const mic = fakeMicCapture()

    const controller = await connectDuplexVoice({
      audioPlayerFactory: fakeAudioPlayer,
      getConnection: async () => null,
      micCaptureFactory: mic.factory,
      onState: vi.fn(),
      onUnavailable
    })

    expect(controller).toBeNull()
    expect(onUnavailable).toHaveBeenCalledTimes(1)
    expect(mic.factory).not.toHaveBeenCalled()
  })

  it('reports unavailable when the socket never opens (times out / errors)', async () => {
    const onUnavailable = vi.fn()
    const gate = socketGate()

    const promise = connectDuplexVoice({
      audioPlayerFactory: fakeAudioPlayer,
      createWebSocket: gate.createWebSocket,
      getConnection: async () => DEFAULT_CONNECTION,
      onState: vi.fn(),
      onUnavailable
    })

    const socket = await gate.ready
    // Never call socket.open() — simulate a connection that just errors out.
    socket.emit('error')

    const controller = await promise
    expect(controller).toBeNull()
    expect(onUnavailable).toHaveBeenCalledWith('duplex websocket failed to connect')
  })

  it('connects, waits for ready before touching the mic, and streams frames as base64 audio messages', async () => {
    const onState = vi.fn<(state: DuplexSessionState) => void>()
    const player = fakeAudioPlayer()
    const mic = fakeMicCapture()
    const gate = socketGate()

    const controllerPromise = connectDuplexVoice({
      audioPlayerFactory: () => player,
      createWebSocket: gate.createWebSocket,
      getConnection: async () => DEFAULT_CONNECTION,
      micCaptureFactory: mic.factory,
      onState,
      onUnavailable: vi.fn()
    })

    const socket = await gate.ready
    socket.open()

    const controller = await controllerPromise
    expect(controller).not.toBeNull()

    // Mic must not be requested before `ready`.
    expect(mic.factory).not.toHaveBeenCalled()

    socket.message({ type: 'ready' })
    // Let the mic factory's microtask resolve.
    await Promise.resolve()
    await Promise.resolve()

    expect(mic.factory).toHaveBeenCalledTimes(1)
    expect(onState).toHaveBeenCalledWith(expect.objectContaining({ phase: 'listening' }))

    mic.getOptions()?.onFrame('BASE64FRAME')
    expect(socket.sent).toContainEqual({ type: 'audio', data: 'BASE64FRAME' })
  })

  it('abandons an opened socket that never reaches speech-recognition ready', async () => {
    const onUnavailable = vi.fn()
    const mic = fakeMicCapture()
    const gate = socketGate()

    const controllerPromise = connectDuplexVoice({
      audioPlayerFactory: fakeAudioPlayer,
      createWebSocket: gate.createWebSocket,
      getConnection: async () => DEFAULT_CONNECTION,
      micCaptureFactory: mic.factory,
      onState: vi.fn(),
      onUnavailable,
      readyTimeoutMs: 5
    })

    const socket = await gate.ready
    socket.open()
    await controllerPromise
    await new Promise(resolve => window.setTimeout(resolve, 10))

    expect(onUnavailable).toHaveBeenCalledWith('duplex speech recognition did not become ready in time')
    expect(mic.factory).not.toHaveBeenCalled()
    expect(socket.readyState).toBe(FakeWebSocket.CLOSED)
  })

  it('wires tts events into the audio player and sends playback_done once it drains', async () => {
    const player = fakeAudioPlayer()
    const gate = socketGate()

    const connectPromise = connectDuplexVoice({
      audioPlayerFactory: () => player,
      createWebSocket: gate.createWebSocket,
      getConnection: async () => DEFAULT_CONNECTION,
      micCaptureFactory: fakeMicCapture().factory,
      onState: vi.fn(),
      onUnavailable: vi.fn()
    })

    const socket = await gate.ready
    socket.open()
    await connectPromise

    socket.message({ type: 'tts_start' })
    expect(player.reset).toHaveBeenCalled()

    socket.message({ type: 'tts_chunk', data: 'AAAA', seq: 0 })
    expect(player.enqueueChunk).toHaveBeenCalledWith('AAAA', 0)

    socket.message({ type: 'tts_end' })
    expect(player.expectEnd).toHaveBeenCalled()

    // Simulate the audio transport finishing playback.
    player.drainedCallback?.()

    expect(socket.sent).toContainEqual({ type: 'playback_done' })
  })

  it('barge_in resets the player immediately', async () => {
    const player = fakeAudioPlayer()
    const gate = socketGate()

    const connectPromise = connectDuplexVoice({
      audioPlayerFactory: () => player,
      createWebSocket: gate.createWebSocket,
      getConnection: async () => DEFAULT_CONNECTION,
      micCaptureFactory: fakeMicCapture().factory,
      onState: vi.fn(),
      onUnavailable: vi.fn()
    })

    const socket = await gate.ready
    socket.open()
    await connectPromise

    socket.message({ type: 'tts_start' })
    player.reset.mockClear()

    socket.message({ type: 'barge_in' })

    expect(player.reset).toHaveBeenCalledTimes(1)
  })

  it('forwards voice cards from the shared duplex socket', async () => {
    const gate = socketGate()
    const onCard = vi.fn()

    const connectPromise = connectDuplexVoice({
      audioPlayerFactory: fakeAudioPlayer,
      createWebSocket: gate.createWebSocket,
      getConnection: async () => DEFAULT_CONNECTION,
      micCaptureFactory: fakeMicCapture().factory,
      onCard,
      onState: vi.fn(),
      onUnavailable: vi.fn()
    })

    const socket = await gate.ready
    socket.open()
    await connectPromise

    socket.message({ type: 'card_show', card: { id: 'c1', kind: 'result', title: 'Weather', body: 'Sunny' } })

    expect(onCard).toHaveBeenCalledWith({ id: 'c1', kind: 'result', title: 'Weather', body: 'Sunny' })
  })

  it('ends cleanly when the voice model requests conversation_end', async () => {
    const player = fakeAudioPlayer()
    const gate = socketGate()
    const onConversationEnd = vi.fn()
    const onUnavailable = vi.fn()

    const connectPromise = connectDuplexVoice({
      audioPlayerFactory: () => player,
      createWebSocket: gate.createWebSocket,
      getConnection: async () => DEFAULT_CONNECTION,
      micCaptureFactory: fakeMicCapture().factory,
      onConversationEnd,
      onState: vi.fn(),
      onUnavailable
    })

    const socket = await gate.ready
    socket.open()
    await connectPromise

    socket.message({ type: 'conversation_end' })

    expect(socket.sent).toContainEqual({ type: 'stop' })
    expect(player.destroy).toHaveBeenCalled()
    expect(onConversationEnd).toHaveBeenCalledTimes(1)
    expect(onUnavailable).not.toHaveBeenCalled()
  })

  it('a malformed server message is ignored without throwing or changing state', async () => {
    const onState = vi.fn()
    const gate = socketGate()

    const connectPromise = connectDuplexVoice({
      audioPlayerFactory: fakeAudioPlayer,
      createWebSocket: gate.createWebSocket,
      getConnection: async () => DEFAULT_CONNECTION,
      micCaptureFactory: fakeMicCapture().factory,
      onState,
      onUnavailable: vi.fn()
    })

    const socket = await gate.ready
    socket.open()
    await connectPromise
    onState.mockClear()

    // Syntactically invalid JSON is dropped before ever reaching the state
    // machine (no onState call at all for this one).
    expect(() => socket.emit('message', { data: 'not json{{{' })).not.toThrow()
    // Valid JSON but an unrecognized `type` parses fine and still triggers a
    // state emit (with state unchanged), matching applyRawEvent's contract.
    expect(() => socket.message({ type: 'unknown_event_type' })).not.toThrow()

    expect(onState).toHaveBeenCalledTimes(1)
    expect(onState).toHaveBeenCalledWith(expect.objectContaining({ phase: 'connecting' }))
  })

  it('stop() sends stop, tears down mic + player, and closes the socket', async () => {
    const player = fakeAudioPlayer()
    const mic = fakeMicCapture()
    const gate = socketGate()

    const connectPromise = connectDuplexVoice({
      audioPlayerFactory: () => player,
      createWebSocket: gate.createWebSocket,
      getConnection: async () => DEFAULT_CONNECTION,
      micCaptureFactory: mic.factory,
      onState: vi.fn(),
      onUnavailable: vi.fn()
    })

    const socket = await gate.ready
    socket.open()
    const controller = await connectPromise
    socket.message({ type: 'ready' })
    await Promise.resolve()
    await Promise.resolve()

    controller!.stop()

    expect(socket.sent).toContainEqual({ type: 'stop' })
    expect(mic.stop).toHaveBeenCalled()
    expect(player.destroy).toHaveBeenCalled()
    expect(socket.readyState).toBe(FakeWebSocket.CLOSED)
  })

  it('tears everything down and reports unavailable when the socket closes unexpectedly after connecting', async () => {
    const player = fakeAudioPlayer()
    const onUnavailable = vi.fn()
    const gate = socketGate()

    const connectPromise = connectDuplexVoice({
      audioPlayerFactory: () => player,
      createWebSocket: gate.createWebSocket,
      getConnection: async () => DEFAULT_CONNECTION,
      micCaptureFactory: fakeMicCapture().factory,
      onState: vi.fn(),
      onUnavailable
    })

    const socket = await gate.ready
    socket.open()
    await connectPromise
    onUnavailable.mockClear()

    socket.emit('close')

    expect(onUnavailable).toHaveBeenCalledWith('duplex websocket closed')
    expect(player.destroy).toHaveBeenCalled()
  })
})
