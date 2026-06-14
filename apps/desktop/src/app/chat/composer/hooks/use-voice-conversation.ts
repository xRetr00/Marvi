import { useCallback, useEffect, useRef, useState } from 'react'

import { getActionStatus, runStreamingSttSetup } from '@/hermes'
import { useI18n } from '@/i18n'
import { openStreamingSttSession, type StreamingSttSession } from '@/lib/streaming-stt'
import type { VoiceFillerConfig } from '@/lib/voice-filler'
import { createSpeechPlaybackQueue, stopVoicePlayback, type SpeechPlaybackQueue } from '@/lib/voice-playback'
import { upsertDesktopActionTask } from '@/store/activity'
import { notify, notifyError } from '@/store/notifications'

import { useMicRecorder } from './use-mic-recorder'

export type ConversationStatus = 'idle' | 'listening' | 'transcribing' | 'thinking' | 'speaking'

interface PendingVoiceResponse {
  id: string
  pending: boolean
  text: string
}

interface VoiceConversationOptions {
  busy: boolean
  enabled: boolean
  onFatalError?: () => void
  onSubmit: (text: string) => Promise<void> | void
  sttStreamingEnabled?: boolean
  voiceFillerConfig?: VoiceFillerConfig
  onTranscribeAudio?: (audio: Blob) => Promise<string>
  pendingResponse: () => PendingVoiceResponse | null
  consumePendingResponse: () => void
}

export function useVoiceConversation({
  busy,
  enabled,
  onFatalError,
  onSubmit,
  sttStreamingEnabled = false,
  voiceFillerConfig,
  onTranscribeAudio,
  pendingResponse,
  consumePendingResponse
}: VoiceConversationOptions) {
  const { t } = useI18n()
  const voiceCopy = t.notifications.voice
  const { handle, level } = useMicRecorder(voiceCopy)
  const [status, setStatus] = useState<ConversationStatus>('idle')
  const [muted, setMuted] = useState(false)
  const [transcriptPreview, setTranscriptPreview] = useState('')
  const turnTimeoutRef = useRef<number | null>(null)
  const pendingStartRef = useRef(false)
  const turnClosingRef = useRef(false)
  const awaitingSpokenResponseRef = useRef(false)
  const responseIdRef = useRef<string | null>(null)
  const spokenSourceLengthRef = useRef(0)
  const speechBufferRef = useRef('')
  const speechQueueRef = useRef<SpeechPlaybackQueue | null>(null)
  const enabledRef = useRef(enabled)
  const mutedRef = useRef(muted)
  const busyRef = useRef(busy)
  const statusRef = useRef<ConversationStatus>('idle')
  const wasEnabledRef = useRef(enabled)
  const streamingSessionRef = useRef<StreamingSttSession | null>(null)

  useEffect(() => {
    enabledRef.current = enabled
  }, [enabled])

  useEffect(() => {
    mutedRef.current = muted
  }, [muted])

  useEffect(() => {
    busyRef.current = busy
  }, [busy])

  useEffect(() => {
    statusRef.current = status
  }, [status])

  const clearTurnTimeout = () => {
    if (turnTimeoutRef.current) {
      window.clearTimeout(turnTimeoutRef.current)
      turnTimeoutRef.current = null
    }
  }

  const resetSpeechBuffer = () => {
    responseIdRef.current = null
    spokenSourceLengthRef.current = 0
    speechBufferRef.current = ''
  }

  const stopSpeechQueue = useCallback(() => {
    speechQueueRef.current?.stop()
    speechQueueRef.current = null
  }, [])

  const runStreamingSttInstaller = useCallback(async () => {
    try {
      const started = await runStreamingSttSetup()
      notify({
        kind: 'info',
        title: voiceCopy.streamingUnavailable,
        message: 'Streaming STT setup started.'
      })

      if (!started.ok) {
        notifyError(new Error('spawn failed'), 'Failed to run streaming STT setup')

        return
      }

      for (let attempt = 0; attempt < 150; attempt += 1) {
        await new Promise(resolve => window.setTimeout(resolve, 1200))
        const status = await getActionStatus(started.name, 300)
        upsertDesktopActionTask(status)

        if (!status.running) {
          notify({
            kind: status.exit_code === 0 ? 'success' : 'error',
            title: status.exit_code === 0 ? 'Streaming STT setup complete' : 'Streaming STT setup failed',
            message: status.exit_code === 0 ? 'Restart voice mode and try realtime STT again.' : 'Check the setup log.'
          })

          return
        }
      }
    } catch (error) {
      notifyError(error, 'Failed to run streaming STT setup')
    }
  }, [voiceCopy.streamingUnavailable])

  const appendSpeechText = (text: string) => {
    if (!text) {
      return
    }

    speechBufferRef.current = `${speechBufferRef.current}${text}`
  }

  const takeSpeechChunk = (force = false): string | null => {
    const buffer = speechBufferRef.current.replace(/\s+/g, ' ').trim()

    if (!buffer) {
      speechBufferRef.current = ''

      return null
    }

    const sentence = buffer.match(/^(.+?[.!?。！？])(?:\s+|$)/)

    if (sentence?.[1] && (sentence[1].length >= 8 || force)) {
      const chunk = sentence[1].trim()
      speechBufferRef.current = buffer.slice(sentence[1].length).trim()

      return chunk
    }

    if (!force && buffer.length > 220) {
      const softBoundary = Math.max(
        buffer.lastIndexOf(', ', 180),
        buffer.lastIndexOf('; ', 180),
        buffer.lastIndexOf(': ', 180)
      )

      if (softBoundary > 80) {
        const chunk = buffer.slice(0, softBoundary + 1).trim()
        speechBufferRef.current = buffer.slice(softBoundary + 1).trim()

        return chunk
      }
    }

    if (!force) {
      return null
    }

    speechBufferRef.current = ''

    return buffer
  }

  const handleTurn = useCallback(
    async (forceTranscribe = false) => {
      if (turnClosingRef.current) {
        return
      }

      turnClosingRef.current = true
      clearTurnTimeout()
      setStatus('transcribing')

      try {
        const result = await handle.stop()

        if (!result || (!result.heardSpeech && !forceTranscribe) || !onTranscribeAudio) {
          if (enabledRef.current && !mutedRef.current && !busyRef.current && statusRef.current !== 'speaking') {
            pendingStartRef.current = true
          }

          setStatus('idle')

          return
        }

        try {
          let transcript = ''
          const streamingSession = streamingSessionRef.current
          streamingSessionRef.current = null

          if (streamingSession) {
            try {
              transcript = (await streamingSession.finish()).trim()
            } catch {
              transcript = ''
            }
          }

          if (!transcript) {
            transcript = (await onTranscribeAudio(result.audio)).trim()
          }

          setTranscriptPreview(transcript)

          if (!transcript) {
            if (enabledRef.current) {
              pendingStartRef.current = true
            }

            setStatus('idle')

            return
          }

          awaitingSpokenResponseRef.current = true
          resetSpeechBuffer()
          await onSubmit(transcript)
          setStatus('thinking')
        } catch (error) {
          notifyError(error, voiceCopy.transcriptionFailed)

          if (enabledRef.current && !mutedRef.current && !busyRef.current) {
            pendingStartRef.current = true
          }

          setStatus('idle')
        }
      } finally {
        turnClosingRef.current = false
      }
    },
    [handle, onSubmit, onTranscribeAudio, voiceCopy.transcriptionFailed]
  )

  const startListening = useCallback(async () => {
    pendingStartRef.current = false

    if (!enabledRef.current || mutedRef.current || busyRef.current) {
      return
    }

    if (statusRef.current !== 'idle') {
      return
    }

    try {
      streamingSessionRef.current = null
      setTranscriptPreview('')
      if (sttStreamingEnabled) {
        try {
          streamingSessionRef.current = await openStreamingSttSession({
            onPartial: text => setTranscriptPreview(text)
          })
        } catch (error) {
          console.warn('[voice] Streaming STT unavailable; falling back to standard transcription.', error)
          const message = error instanceof Error ? error.message : voiceCopy.streamingFallback
          const canRunSetup = /sherpa[-_]onnx/i.test(message)
          notify({
            kind: 'warning',
            title: voiceCopy.streamingUnavailable,
            message,
            action: canRunSetup ? { label: 'Run setup', onClick: () => void runStreamingSttInstaller() } : undefined
          })
          streamingSessionRef.current = null
        }
      }

      // VAD tuning mirrors `tools.voice_mode` defaults so the browser loop matches the CLI.
      await handle.start({
        onAudioFrame: samples => streamingSessionRef.current?.sendFrame(samples),
        silenceLevel: 0.075,
        silenceMs: 1_250,
        idleSilenceMs: 12_000,
        onError: error => {
          notifyError(error, voiceCopy.microphoneFailed)
          pendingStartRef.current = false
          onFatalError?.()
        },
        onSilence: () => void handleTurn()
      })
      setStatus('listening')
      turnTimeoutRef.current = window.setTimeout(() => void handleTurn(), 60_000)
    } catch (error) {
      streamingSessionRef.current?.stop()
      streamingSessionRef.current = null
      notifyError(error, voiceCopy.couldNotStartSession)
      pendingStartRef.current = false
      setStatus('idle')
      onFatalError?.()
    }
  }, [
    handle,
    handleTurn,
    onFatalError,
    runStreamingSttInstaller,
    sttStreamingEnabled,
    voiceCopy.couldNotStartSession,
    voiceCopy.microphoneFailed,
    voiceCopy.streamingFallback,
    voiceCopy.streamingUnavailable
  ])

  const ensureSpeechQueue = useCallback(() => {
    if (speechQueueRef.current) {
      return speechQueueRef.current
    }

    const queue = createSpeechPlaybackQueue({ filler: voiceFillerConfig, source: 'voice-conversation' })
    speechQueueRef.current = queue
    setStatus('speaking')

    void queue.done
      .catch(error => {
        notifyError(error, voiceCopy.playbackFailed)
      })
      .finally(() => {
        if (speechQueueRef.current === queue) {
          speechQueueRef.current = null
        }

        if (awaitingSpokenResponseRef.current) {
          setStatus('thinking')
          return
        }

        if (enabledRef.current) {
          pendingStartRef.current = true
        }
        setStatus('idle')
      })

    return queue
  }, [voiceCopy.playbackFailed, voiceFillerConfig])

  const enqueueSpeech = useCallback(
    (text: string) => {
      if (!text) {
        return
      }

      ensureSpeechQueue().enqueue(text)
    },
    [ensureSpeechQueue]
  )

  const closeSpeechQueue = useCallback(() => {
    const queue = speechQueueRef.current

    if (queue) {
      queue.close()
      return
    }

    if (enabledRef.current) {
      pendingStartRef.current = true
    }
    setStatus('idle')
  }, [])

  const start = useCallback(async () => {
    if (!onTranscribeAudio) {
      notify({
        kind: 'warning',
        title: voiceCopy.unavailable,
        message: voiceCopy.configureSpeechToText
      })
      onFatalError?.()

      return
    }

    setMuted(false)
    awaitingSpokenResponseRef.current = false
    resetSpeechBuffer()
    consumePendingResponse()
    pendingStartRef.current = true
    await startListening()
  }, [consumePendingResponse, onFatalError, onTranscribeAudio, startListening, voiceCopy.configureSpeechToText, voiceCopy.unavailable])

  const end = useCallback(async () => {
    pendingStartRef.current = false
    clearTurnTimeout()
    stopVoicePlayback()
    stopSpeechQueue()
    streamingSessionRef.current?.stop()
    streamingSessionRef.current = null
    handle.cancel()
    turnClosingRef.current = false
    awaitingSpokenResponseRef.current = false
    resetSpeechBuffer()
    consumePendingResponse()
    setTranscriptPreview('')
    setMuted(false)
    setStatus('idle')
  }, [consumePendingResponse, handle, stopSpeechQueue])

  const stopTurn = useCallback(() => {
    if (statusRef.current === 'listening') {
      void handleTurn(true)
    }
  }, [handleTurn])

  const toggleMute = useCallback(() => {
    setMuted(value => {
      const next = !value

      if (next) {
        clearTurnTimeout()
        handle.cancel()
        stopSpeechQueue()
        streamingSessionRef.current?.stop()
        streamingSessionRef.current = null
        setTranscriptPreview('')
        setStatus('idle')
      } else if (enabledRef.current && !busyRef.current && statusRef.current === 'idle') {
        pendingStartRef.current = true
      }

      return next
    })
  }, [handle, stopSpeechQueue])

  useEffect(() => {
    if (!enabled) {
      return
    }

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.code !== 'Space' || event.repeat || event.metaKey || event.ctrlKey || event.altKey) {
        return
      }

      if (statusRef.current !== 'listening') {
        return
      }

      event.preventDefault()
      stopTurn()
    }

    window.addEventListener('keydown', onKeyDown, { capture: true })

    return () => window.removeEventListener('keydown', onKeyDown, { capture: true })
  }, [enabled, stopTurn])

  // Drive the loop: after a voice-submitted turn, feed stable chunks into one
  // speech queue as the assistant stream grows. Otherwise start listening when
  // idle between turns.
  useEffect(() => {
    if (!enabled || muted) {
      return
    }

    if (awaitingSpokenResponseRef.current) {
      const response = pendingResponse()

      if (response) {
        if (response.id !== responseIdRef.current) {
          stopSpeechQueue()
          resetSpeechBuffer()
          responseIdRef.current = response.id
        }

        if (response.text.length > spokenSourceLengthRef.current) {
          appendSpeechText(response.text.slice(spokenSourceLengthRef.current))
          spokenSourceLengthRef.current = response.text.length
        }

        let chunk = takeSpeechChunk(!response.pending && !busy)

        while (chunk) {
          enqueueSpeech(chunk)
          chunk = takeSpeechChunk(!response.pending && !busy)
        }

        if (!response.pending && !busy) {
          awaitingSpokenResponseRef.current = false
          consumePendingResponse()
          resetSpeechBuffer()
          closeSpeechQueue()

          return
        }
      }

      if (!busy && status === 'thinking') {
        awaitingSpokenResponseRef.current = false
        resetSpeechBuffer()
        closeSpeechQueue()

        return
      }
    }

    if (busy || status !== 'idle') {
      return
    }

    if (pendingStartRef.current) {
      void startListening()
    }
  }, [
    busy,
    closeSpeechQueue,
    consumePendingResponse,
    enabled,
    enqueueSpeech,
    muted,
    pendingResponse,
    startListening,
    status,
    stopSpeechQueue
  ])

  useEffect(() => {
    if (enabled && !wasEnabledRef.current) {
      void start()
    }

    if (!enabled && wasEnabledRef.current) {
      void end()
    }

    wasEnabledRef.current = enabled
  }, [enabled, end, start])

  return { end, level, muted, start, status, stopTurn, toggleMute, transcriptPreview }
}
