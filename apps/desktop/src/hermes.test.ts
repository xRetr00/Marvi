import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  getSessionMessages,
  listAllProfileSessions,
  listSessions,
  runStreamingSttSetup,
  speakText,
  warmTextToSpeech
} from './hermes'

const emptySessionsResponse = {
  limit: 0,
  offset: 0,
  sessions: [],
  total: 0
}

describe('Hermes REST session helpers', () => {
  let api: ReturnType<typeof vi.fn>

  beforeEach(() => {
    api = vi.fn().mockResolvedValue(emptySessionsResponse)
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { api }
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('uses a longer timeout for the single-profile session list', async () => {
    await listSessions(50, 1)

    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({
        path: '/api/sessions?limit=50&offset=0&min_messages=1&archived=exclude&order=recent',
        timeoutMs: 60_000
      })
    )
  })

  it('uses a longer timeout for the all-profile session list', async () => {
    await listAllProfileSessions(50, 1)

    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({
        path: '/api/profiles/sessions?limit=50&offset=0&min_messages=1&archived=exclude&order=recent&profile=all',
        timeoutMs: 60_000
      })
    )
  })

  it('tags cross-profile message reads for Electron routing and backend lookup', async () => {
    api.mockResolvedValue({ messages: [], session_id: 'session-1' })

    await getSessionMessages('session-1', 'xiaoxuxu')

    expect(api).toHaveBeenCalledWith({
      path: '/api/sessions/session-1/messages?profile=xiaoxuxu',
      profile: 'xiaoxuxu'
    })
  })

  it('uses a provider-sized timeout for desktop speech synthesis', async () => {
    api.mockResolvedValue({
      data_url: 'data:audio/mpeg;base64,AA==',
      mime_type: 'audio/mpeg',
      ok: true,
      provider: 'piper'
    })

    await speakText('Hello')

    expect(api).toHaveBeenCalledWith({
      body: { text: 'Hello' },
      method: 'POST',
      path: '/api/audio/speak',
      timeoutMs: 360_000
    })
  })

  it('fires a desktop TTS warm request', async () => {
    api.mockResolvedValue({ ok: true, warmed: true })

    await warmTextToSpeech()

    expect(api).toHaveBeenCalledWith({
      method: 'POST',
      path: '/api/audio/tts/warm',
      timeoutMs: 360_000
    })
  })

  it('fires the streaming STT setup action', async () => {
    api.mockResolvedValue({ key: 'sherpa_onnx', name: 'tools-post-setup', ok: true, pid: 1234 })

    await runStreamingSttSetup()

    expect(api).toHaveBeenCalledWith({
      method: 'POST',
      path: '/api/audio/streaming-stt/setup',
      timeoutMs: 30_000
    })
  })
})
