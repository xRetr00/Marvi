import { speakText } from '@/hermes'
import {
  $voicePlayback,
  setVoicePlaybackState,
  type VoicePlaybackSource,
  type VoicePlaybackState
} from '@/store/voice-playback'

import { sanitizeTextForSpeech } from './speech-text'

// Free Edge TTS occasionally hands back audio that never fires `playing`/`ended`
// nor `error` — leaving voice mode stuck "speaking" forever. Reject if playback
// fails to start or stalls mid-stream for this long (rearmed on each progress
// tick, so legitimately long speech is never cut off).
const PLAYBACK_STALL_MS = 15_000
const STREAM_START_BUFFER_SECONDS = 0.12
const STREAM_UNDERRUN_BUFFER_SECONDS = 0.08

let currentAudio: HTMLAudioElement | null = null
let currentStop: (() => void) | null = null
let sequence = 0

function currentState(
  status: VoicePlaybackState['status'],
  options?: VoicePlaybackOptions,
  audioElement: HTMLAudioElement | null = null
): VoicePlaybackState {
  return {
    audioElement,
    messageId: options?.messageId ?? null,
    sequence,
    source: options?.source ?? null,
    status
  }
}

export interface VoicePlaybackOptions {
  messageId?: string | null
  source: VoicePlaybackSource
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

async function playStreamingSpeechText(text: string, options: VoicePlaybackOptions): Promise<boolean> {
  const conn = await window.hermesDesktop.getConnection().catch(() => null)

  if (!conn || conn.authMode === 'oauth' || !conn.token) {
    return false
  }

  const response = await fetch(`${conn.baseUrl.replace(/\/+$/, '')}/api/audio/speak/stream`, {
    body: JSON.stringify({ text }),
    headers: {
      'Content-Type': 'application/json',
      'X-Hermes-Session-Token': conn.token
    },
    method: 'POST'
  })

  if (!response.ok || !response.body) {
    return false
  }

  const audioContext = new AudioContext()
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let sampleRate = 24000
  let nextTime = audioContext.currentTime
  let playedChunks = 0
  let failed = false
  let stopped = false
  let startedStream = false

  currentStop = () => {
    stopped = true
    void audioContext.close?.()
  }
  setVoicePlaybackState(currentState('speaking', options))

  const playChunk = (encoded: string) => {
    const samples = pcm16Base64ToFloat32(encoded)
    const audioBuffer = audioContext.createBuffer(1, samples.length, sampleRate)
    audioBuffer.getChannelData(0).set(samples)
    const source = audioContext.createBufferSource()
    source.buffer = audioBuffer
    source.connect(audioContext.destination)
    nextTime = Math.max(
      nextTime,
      audioContext.currentTime + (startedStream ? STREAM_UNDERRUN_BUFFER_SECONDS : STREAM_START_BUFFER_SECONDS)
    )
    source.start(nextTime)
    nextTime += samples.length / sampleRate
    startedStream = true
  }

  while (!stopped) {
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
      if (event.type === 'start' && event.sample_rate) {
        sampleRate = event.sample_rate
      } else if (event.type === 'chunk' && event.audio) {
        playChunk(event.audio)
        playedChunks += 1
      } else if (event.type === 'error') {
        failed = true
        stopped = true
        break
      }
    }
  }

  if (!stopped) {
    await new Promise(resolve => window.setTimeout(resolve, Math.max(0, (nextTime - audioContext.currentTime) * 1000)))
  }

  currentStop = null
  await audioContext.close?.()
  return !stopped && !failed && playedChunks > 0
}

export function stopVoicePlayback() {
  sequence += 1
  currentStop?.()
  currentStop = null

  if (currentAudio) {
    currentAudio.pause()
    currentAudio.src = ''
    currentAudio.load()
    currentAudio = null
  }

  setVoicePlaybackState({
    audioElement: null,
    messageId: null,
    sequence,
    source: null,
    status: 'idle'
  })
}

export async function playSpeechText(text: string, options: VoicePlaybackOptions): Promise<boolean> {
  stopVoicePlayback()

  const speakableText = sanitizeTextForSpeech(text)

  if (!speakableText) {
    return false
  }

  const ownSequence = sequence
  const isCurrent = () => ownSequence === sequence

  setVoicePlaybackState(currentState('preparing', options))

  try {
    if (await playStreamingSpeechText(speakableText, options).catch(() => false)) {
      if (isCurrent()) {
        setVoicePlaybackState(currentState('idle'))
      }
      return true
    }

    if (!isCurrent()) {
      return false
    }

    const response = await speakText(speakableText)

    if (!isCurrent()) {
      return false
    }

    const audio = new Audio(response.data_url)
    currentAudio = audio
    setVoicePlaybackState(currentState('speaking', options, audio))

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
        currentStop = null
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

      currentStop = () => {
        cleanup()
        resolve()
      }

      audio.addEventListener('ended', onEnded, { once: true })
      audio.addEventListener('error', onError, { once: true })
      audio.addEventListener('timeupdate', armStall)
      armStall()
      void audio.play().catch(onError)
    })

    if (!isCurrent()) {
      return false
    }

    currentAudio = null
    setVoicePlaybackState(currentState('idle'))

    return true
  } catch (error) {
    if (isCurrent()) {
      currentStop = null
      currentAudio = null
      setVoicePlaybackState(currentState('idle'))
    }

    throw error
  }
}

export function isVoicePlaybackActive() {
  return $voicePlayback.get().status !== 'idle'
}
