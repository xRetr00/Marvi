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

export interface SpeechPlaybackQueue {
  close: () => void
  done: Promise<boolean>
  enqueue: (text: string) => void
  stop: () => void
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
  const queue = createSpeechPlaybackQueue(options)
  queue.enqueue(text)
  queue.close()

  return queue.done
}

export function createSpeechPlaybackQueue(options: VoicePlaybackOptions): SpeechPlaybackQueue {
  stopVoicePlayback()

  const chunks: string[] = []
  const responsePromises = new Map<number, ReturnType<typeof speakText>>()
  const waiters: Array<() => void> = []
  let closed = false
  let playIndex = 0
  let active = true

  const notify = () => {
    for (const waiter of waiters.splice(0)) {
      waiter()
    }
  }

  const ownSequence = sequence
  const isCurrent = () => ownSequence === sequence

  const waitForMore = () => new Promise<void>(resolve => waiters.push(resolve))

  const prefetch = (index: number) => {
    if (index >= chunks.length || responsePromises.has(index) || !isCurrent()) {
      return
    }

    responsePromises.set(index, speakText(chunks[index]))
  }

  const prefetchLookahead = () => {
    prefetch(playIndex)
    prefetch(playIndex + 1)
    prefetch(playIndex + 2)
  }

  const run = async (): Promise<boolean> => {
    setVoicePlaybackState(currentState('preparing', options))

    try {
      while (isCurrent() && active) {
        prefetchLookahead()

        if (playIndex >= chunks.length) {
          if (closed) {
            setVoicePlaybackState(currentState('idle'))

            return chunks.length > 0
          }

          setVoicePlaybackState(currentState('preparing', options))
          await waitForMore()
          continue
        }

        const responsePromise = responsePromises.get(playIndex)

        if (!responsePromise) {
          return false
        }

        const response = await responsePromise
        prefetchLookahead()

        if (!(await playAudioDataUrl(response.data_url, options, isCurrent))) {
          return false
        }

        currentAudio = null
        playIndex += 1
      }

      return false
    } catch (error) {
      if (isCurrent()) {
        currentStop = null
        currentAudio = null
        setVoicePlaybackState(currentState('idle'))
      }

      throw error
    }
  }

  const done = run()

  return {
    close: () => {
      closed = true
      notify()
    },
    done,
    enqueue: text => {
      if (closed || !active || !isCurrent()) {
        return
      }

      const nextChunks = splitSpeechQueueText(text)
      if (nextChunks.length === 0) {
        return
      }

      for (const chunk of nextChunks) {
        chunks.push(chunk)
      }
      prefetchLookahead()
      notify()
    },
    stop: () => {
      active = false
      closed = true
      notify()
      if (isCurrent()) {
        stopVoicePlayback()
      }
    }
  }
}

export function isVoicePlaybackActive() {
  return $voicePlayback.get().status !== 'idle'
}
