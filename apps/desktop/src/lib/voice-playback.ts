import { speakText } from '@/hermes'
import {
  $voicePlayback,
  setVoicePlaybackState,
  type VoicePlaybackSource,
  type VoicePlaybackState
} from '@/store/voice-playback'

import { sanitizeTextForSpeech } from './speech-text'
import { rememberSpokenText } from './voice-echo-guard'

// Free Edge TTS occasionally hands back audio that never fires `playing`/`ended`
// nor `error` — leaving voice mode stuck "speaking" forever. Reject if playback
// fails to start or stalls mid-stream for this long.
const PLAYBACK_STALL_MS = 15_000

// NOTE(duplex): gapless streaming player buffers. The whole utterance now plays
// through ONE AudioContext with a single contiguous timeline, so these apply
// once at the start of an utterance — not per sentence like the old code. Small
// = low time-to-first-audio; if PocketTTS chunks arrive unevenly and you hear
// underruns, nudge STREAM_START_BUFFER_SECONDS up.
// See docs/design/2026-07-05-voice-duplex-design.md.
const STREAM_START_BUFFER_SECONDS = 0.3
const STREAM_UNDERRUN_BUFFER_SECONDS = 0.1
const OUTPUT_PRIME_SECONDS = 0.12

// Absolute cap so a session can never hang the voice loop even if the audio
// clock or a fetch misbehaves — finishSpeech() always resolves within this.
const SESSION_SAFETY_MS = 180_000

export interface VoicePlaybackOptions {
  messageId?: string | null
  source: VoicePlaybackSource
}

interface GaplessPlayerDeps {
  createAudioContext?: () => AudioContext
  fetchImpl?: typeof fetch
  getConnection?: () => Promise<{ authMode?: string; baseUrl: string; token?: string | null } | null>
}

function pcm16Base64ToFloat32(encoded: string): Float32Array {
  const raw = atob(encoded)
  const bytes = new Uint8Array(raw.length)

  for (let i = 0; i < raw.length; i += 1) {
    bytes[i] = raw.charCodeAt(i)
  }

  const pcm = new Int16Array(bytes.buffer)
  const samples = new Float32Array(pcm.length)

  for (let i = 0; i < pcm.length; i += 1) {
    samples[i] = Math.max(-1, pcm[i] / 32768)
  }

  return samples
}

export function voicePlaybackLevel(samples: Float32Array): number {
  let energy = 0

  for (let i = 0; i < samples.length; i += 1) {
    energy += samples[i] * samples[i]
  }

  return samples.length ? Math.min(1, Math.sqrt(energy / samples.length) * 4) : 0
}

function idlePlaybackState(sequence: number): VoicePlaybackState {
  return { audioElement: null, caption: null, level: 0, messageId: null, sequence, source: null, status: 'idle' }
}

/**
 * Gapless streaming TTS player. Text segments are enqueued as the LLM streams;
 * the player synthesizes the next segment while the current one plays and
 * schedules every PCM chunk on a single AudioContext timeline, so speech is
 * continuous instead of restarting per sentence.
 */
class GaplessPlayer {
  private readonly createAudioContext: () => AudioContext
  private readonly fetchImpl: typeof fetch
  private readonly getConnection: () => Promise<{ authMode?: string; baseUrl: string; token?: string | null } | null>

  private ctx: AudioContext | null = null
  private nextTime = 0
  private sampleRate = 24000
  private started = false
  private primed = false
  private queue: string[] = []
  private pumping = false
  private moreComing = false
  private closed = true
  private stopped = false
  private playedChunks = 0
  private caption: string | null = null
  private sequence = 0
  private abort: AbortController | null = null
  private readonly sources = new Set<AudioBufferSourceNode>()
  private drainResolvers: Array<(playedAudio: boolean) => void> = []
  private finalizeTimer: number | null = null
  private safetyTimer: number | null = null
  private options: VoicePlaybackOptions = { source: 'voice-conversation' }

  constructor(deps: GaplessPlayerDeps = {}) {
    this.createAudioContext = deps.createAudioContext ?? (() => new AudioContext())
    this.fetchImpl = deps.fetchImpl ?? ((...args) => fetch(...args))
    this.getConnection =
      deps.getConnection ??
      (async () => {
        try {
          return (await window.hermesDesktop?.getConnection?.()) ?? null
        } catch {
          return null
        }
      })
  }

  get sequenceId(): number {
    return this.sequence
  }

  isActive(): boolean {
    return !this.closed
  }

  /** True when the last session ended via stop() (user/interrupt) rather than
   *  finishing naturally — callers use this to skip a fallback after a stop. */
  wasStopped(): boolean {
    return this.stopped
  }

  /** Begin a new utterance session, replacing any previous one. */
  start(options: VoicePlaybackOptions): void {
    this.hardStop(false)
    this.closed = false
    this.stopped = false
    this.moreComing = true
    this.started = false
    this.primed = false
    this.playedChunks = 0
    this.caption = null
    this.queue = []
    this.nextTime = 0
    // Abandon any stale pump from a previous session (e.g. a fetch that never
    // resolved). The new sequence makes the old pump exit on its next tick.
    this.pumping = false
    this.options = options
    this.sequence += 1
    this.abort = new AbortController()

    try {
      this.ctx = this.createAudioContext()
      void this.ctx.resume?.()?.catch?.(() => undefined)
    } catch {
      this.ctx = null
    }

    setVoicePlaybackState({
      audioElement: null,
      caption: null,
      level: 0,
      messageId: options.messageId ?? null,
      sequence: this.sequence,
      source: options.source,
      status: 'speaking'
    })

    this.safetyTimer = window.setTimeout(() => this.finalize(), SESSION_SAFETY_MS)
  }

  enqueue(text: string): void {
    if (this.closed || !this.moreComing) {
      return
    }

    const clean = sanitizeTextForSpeech(text)

    if (!clean) {
      return
    }

    rememberSpokenText(clean)
    this.caption = clean
    this.publishOutput(0)
    this.queue.push(clean)
    void this.pump()
  }

  /** No more text is coming. Resolves once queued audio has finished playing
   *  (or the session is stopped). Returns whether any audio actually played. */
  finish(): Promise<boolean> {
    this.moreComing = false

    return new Promise<boolean>(resolve => {
      if (this.closed) {
        resolve(this.playedChunks > 0)

        return
      }

      this.drainResolvers.push(resolve)
      void this.pump()
      this.scheduleFinalizeIfDrained()
    })
  }

  /** Hard stop: abort synthesis, silence scheduled audio, mark idle. */
  stop(): void {
    this.stopped = true
    this.hardStop(true)
  }

  private hardStop(publishIdle: boolean): void {
    const wasActive = !this.closed
    this.closed = true
    this.moreComing = false
    this.queue = []

    if (this.finalizeTimer !== null) {
      window.clearTimeout(this.finalizeTimer)
      this.finalizeTimer = null
    }

    if (this.safetyTimer !== null) {
      window.clearTimeout(this.safetyTimer)
      this.safetyTimer = null
    }

    this.abort?.abort()
    this.abort = null

    for (const source of this.sources) {
      try {
        source.stop()
      } catch {
        // already stopped
      }
    }

    this.sources.clear()

    void this.ctx?.close?.()
    this.ctx = null

    if (publishIdle && wasActive) {
      setVoicePlaybackState(idlePlaybackState(this.sequence))
    }

    this.resolveDrain()
  }

  private resolveDrain(): void {
    const resolvers = this.drainResolvers
    this.drainResolvers = []

    for (const resolve of resolvers) {
      resolve(this.playedChunks > 0)
    }
  }

  private async pump(): Promise<void> {
    if (this.pumping || this.closed) {
      return
    }

    const seq = this.sequence
    this.pumping = true

    try {
      while (this.queue.length && !this.closed && this.sequence === seq) {
        const text = this.queue.shift() as string
        await this.streamSegment(text, seq)
      }
    } finally {
      // Only the current session's pump owns the pumping flag / finalize.
      if (this.sequence === seq) {
        this.pumping = false
        this.scheduleFinalizeIfDrained()
      }
    }
  }

  private scheduleFinalizeIfDrained(): void {
    if (this.closed || this.moreComing || this.queue.length > 0 || this.pumping) {
      return
    }

    if (this.finalizeTimer !== null) {
      return
    }

    // All text synthesized and scheduled; finalize once the last chunk finishes.
    const remainingMs = this.ctx ? Math.max(0, (this.nextTime - this.ctx.currentTime) * 1000) : 0
    this.finalizeTimer = window.setTimeout(() => this.finalize(), remainingMs + 60)
  }

  private finalize(): void {
    if (this.closed) {
      return
    }

    this.closed = true

    if (this.finalizeTimer !== null) {
      window.clearTimeout(this.finalizeTimer)
      this.finalizeTimer = null
    }

    if (this.safetyTimer !== null) {
      window.clearTimeout(this.safetyTimer)
      this.safetyTimer = null
    }

    this.abort?.abort()
    this.abort = null
    this.sources.clear()
    void this.ctx?.close?.()
    this.ctx = null

    // Only clear the shared state if it still belongs to this session (a newer
    // session may already have taken over).
    if ($voicePlayback.get().sequence === this.sequence) {
      setVoicePlaybackState(idlePlaybackState(this.sequence))
    }

    this.resolveDrain()
  }

  private async streamSegment(text: string, seq: number): Promise<void> {
    const conn = await this.getConnection()

    if (!conn || conn.authMode === 'oauth' || !conn.token || !this.ctx || this.closed || this.sequence !== seq) {
      return
    }

    let response: Response

    try {
      response = await this.fetchImpl(`${conn.baseUrl.replace(/\/+$/, '')}/api/audio/speak/stream`, {
        body: JSON.stringify({ text }),
        headers: { 'Content-Type': 'application/json', 'X-Hermes-Session-Token': conn.token },
        method: 'POST',
        signal: this.abort?.signal
      })
    } catch {
      return // aborted or network error; drain logic still finalizes
    }

    if (!response.ok || !response.body) {
      return
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    try {
      while (!this.closed) {
        const { done, value } = await reader.read()

        if (done) {
          break
        }

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''

        for (const line of lines) {
          if (!line.trim()) {
            continue
          }

          const event = JSON.parse(line) as { audio?: string; error?: string; sample_rate?: number; type?: string }

          if ((event.type === 'start' || event.type === 'sample_rate') && event.sample_rate) {
            this.sampleRate = event.sample_rate
          } else if (event.type === 'chunk' && event.audio) {
            this.scheduleChunk(event.audio)
          }
        }
      }
    } catch {
      // aborted mid-read; finalize handles cleanup
    }
  }

  private primeOnce(): void {
    if (this.primed || !this.ctx) {
      return
    }

    this.primed = true
    const frames = Math.max(1, Math.floor((this.ctx.sampleRate || this.sampleRate) * OUTPUT_PRIME_SECONDS))
    const silent = this.ctx.createBuffer(1, frames, this.ctx.sampleRate || this.sampleRate)
    const source = this.ctx.createBufferSource()
    source.buffer = silent
    source.connect(this.ctx.destination)
    source.start(this.ctx.currentTime)
    this.nextTime = this.ctx.currentTime + OUTPUT_PRIME_SECONDS
  }

  private scheduleChunk(encoded: string): void {
    if (!this.ctx || this.closed) {
      return
    }

    this.primeOnce()

    const samples = pcm16Base64ToFloat32(encoded)
    const level = voicePlaybackLevel(samples)
    const audioBuffer = this.ctx.createBuffer(1, samples.length, this.sampleRate)
    audioBuffer.getChannelData(0).set(samples)
    const source = this.ctx.createBufferSource()
    source.buffer = audioBuffer
    source.connect(this.ctx.destination)

    const buffer = this.started ? STREAM_UNDERRUN_BUFFER_SECONDS : STREAM_START_BUFFER_SECONDS
    this.nextTime = Math.max(this.nextTime, this.ctx.currentTime + buffer)
    source.start(this.nextTime)
    this.nextTime += samples.length / this.sampleRate
    this.started = true
    this.playedChunks += 1
    this.publishOutput(level)

    this.sources.add(source)

    source.onended = () => {
      this.sources.delete(source)

      if (this.sources.size === 0) {
        this.publishOutput(0)
      }
    }
  }

  private publishOutput(level: number): void {
    const current = $voicePlayback.get()

    if (current.sequence !== this.sequence || this.closed) {
      return
    }

    setVoicePlaybackState({
      ...current,
      caption: this.caption,
      level
    })
  }
}

export const gaplessPlayer = new GaplessPlayer()

/** Test seam: build a player with injected AudioContext/fetch/connection. */
export function createGaplessPlayerForTest(deps: GaplessPlayerDeps): GaplessPlayer {
  return new GaplessPlayer(deps)
}

// --- Streaming session API (used by the voice conversation loop) -----------

/** Begin a gapless speaking session for an utterance. */
export function startSpeechSession(options: VoicePlaybackOptions): void {
  gaplessPlayer.start(options)
}

/** Feed one sentence/clause into the current session (non-blocking). */
export function enqueueSpeech(text: string): void {
  gaplessPlayer.enqueue(text)
}

/** Mark the utterance complete; resolves once audio finishes (or is stopped). */
export function finishSpeech(): Promise<boolean> {
  return gaplessPlayer.finish()
}

// --- Legacy one-shot API ----------------------------------------------------

let currentAudio: HTMLAudioElement | null = null
let legacySequence = 0

function stopLegacyAudio(): void {
  if (currentAudio) {
    currentAudio.pause()
    currentAudio.src = ''
    currentAudio.load()
    currentAudio = null
  }
}

export function stopVoicePlayback(): void {
  legacySequence += 1
  gaplessPlayer.stop()
  stopLegacyAudio()
  setVoicePlaybackState(idlePlaybackState(gaplessPlayer.sequenceId))
}

/**
 * One-shot speak of a complete piece of text (read-aloud / auto-speak). Streams
 * gaplessly via the player; falls back to Edge `speakText` when streaming
 * produced no audio (e.g. PocketTTS unavailable).
 */
export async function playSpeechText(text: string, options: VoicePlaybackOptions): Promise<boolean> {
  stopVoicePlayback()

  const speakableText = sanitizeTextForSpeech(text)

  if (!speakableText) {
    return false
  }

  startSpeechSession(options)
  enqueueSpeech(speakableText)
  const playedAudio = await finishSpeech()

  if (playedAudio) {
    return true
  }

  // Stopped by the user/interrupt — don't resurrect audio via the fallback.
  if (gaplessPlayer.wasStopped()) {
    return false
  }

  // Streaming produced nothing — fall back to the non-streaming Edge clip.
  const ownSequence = ++legacySequence
  const isCurrent = () => ownSequence === legacySequence

  setVoicePlaybackState({
    audioElement: null,
    caption: speakableText,
    level: 0,
    messageId: options.messageId ?? null,
    sequence: ownSequence,
    source: options.source,
    status: 'preparing'
  })

  let response: { data_url: string }

  try {
    response = await speakText(speakableText)
  } catch {
    if (isCurrent()) {
      setVoicePlaybackState(idlePlaybackState(ownSequence))
    }

    return false
  }

  if (!isCurrent()) {
    return false
  }

  const audio = new Audio(response.data_url)
  currentAudio = audio
  // The legacy clip has no PCM stream to meter; a steady baseline still makes
  // its island waveform visibly speak instead of looking frozen.
  setVoicePlaybackState({
    audioElement: audio,
    caption: speakableText,
    level: 0.35,
    messageId: options.messageId ?? null,
    sequence: ownSequence,
    source: options.source,
    status: 'speaking'
  })

  try {
    await new Promise<void>((resolve, reject) => {
      let stall: number | null = null

      const cleanup = () => {
        if (stall !== null) {
          window.clearTimeout(stall)
          stall = null
        }

        audio.removeEventListener('ended', onEnded)
        audio.removeEventListener('error', onError)
        audio.removeEventListener('timeupdate', armStall)
      }

      const armStall = () => {
        if (stall !== null) {
          window.clearTimeout(stall)
        }

        stall = window.setTimeout(() => {
          cleanup()
          reject(new Error('Playback stalled'))
        }, PLAYBACK_STALL_MS)
      }

      const onEnded = () => {
        cleanup()
        resolve()
      }

      const onError = () => {
        cleanup()
        reject(new Error('Playback failed'))
      }

      audio.addEventListener('ended', onEnded, { once: true })
      audio.addEventListener('error', onError, { once: true })
      audio.addEventListener('timeupdate', armStall)
      armStall()
      void audio.play().catch(onError)
    })
  } finally {
    if (isCurrent()) {
      currentAudio = null
      setVoicePlaybackState(idlePlaybackState(ownSequence))
    }
  }

  return isCurrent()
}

export function isVoicePlaybackActive(): boolean {
  return $voicePlayback.get().status !== 'idle'
}

const INTERRUPT_TTL_MS = 120_000
let interruptedAt: null | number = null

export function markVoicePlaybackInterrupted() {
  interruptedAt = Date.now()
}

export function takeVoicePlaybackInterrupted(): boolean {
  const at = interruptedAt
  interruptedAt = null

  return at !== null && Date.now() - at < INTERRUPT_TTL_MS
}
