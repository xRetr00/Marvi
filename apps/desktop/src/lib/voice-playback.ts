import { speakText } from '@/hermes'
import {
  $voicePlayback,
  setVoicePlaybackState,
  type VoicePlaybackSource,
  type VoicePlaybackState
} from '@/store/voice-playback'

import { sanitizeTextForSpeech } from './speech-text'

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

function splitSpeechQueueText(text: string): string[] {
  const speakableText = sanitizeTextForSpeech(text)

  if (!speakableText) {
    return []
  }

  const sentencePattern = /[^.!?。！？]+[.!?。！？]+(?:["')\]]+)?|[^.!?。！？]+$/g
  return speakableText.match(sentencePattern)?.map(part => part.trim()).filter(Boolean) ?? [speakableText]
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

async function playAudioDataUrl(dataUrl: string, options: VoicePlaybackOptions, isCurrent: () => boolean): Promise<boolean> {
  if (!isCurrent()) {
    return false
  }

  const audio = new Audio(dataUrl)
  currentAudio = audio
  setVoicePlaybackState(currentState('speaking', options, audio))

  await new Promise<void>((resolve, reject) => {
    const cleanup = () => {
      audio.removeEventListener('ended', onEnded)
      audio.removeEventListener('error', onError)
      currentStop = null
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
    void audio.play().catch(reject)
  })

  return isCurrent()
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
    const response = await speakText(speakableText)

    if (!(await playAudioDataUrl(response.data_url, options, isCurrent))) {
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

export async function playSpeechTextQueue(text: string, options: VoicePlaybackOptions): Promise<boolean> {
  stopVoicePlayback()

  const chunks = splitSpeechQueueText(text)

  if (chunks.length === 0) {
    return false
  }

  const ownSequence = sequence
  const isCurrent = () => ownSequence === sequence

  setVoicePlaybackState(currentState('preparing', options))

  try {
    for (const chunk of chunks) {
      const response = await speakText(chunk)

      if (!(await playAudioDataUrl(response.data_url, options, isCurrent))) {
        return false
      }

      currentAudio = null
      if (isCurrent()) {
        setVoicePlaybackState(currentState('preparing', options))
      }
    }

    if (!isCurrent()) {
      return false
    }

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
