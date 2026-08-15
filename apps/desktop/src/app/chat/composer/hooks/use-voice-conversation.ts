import { useCallback, useEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { startBargeInDetector } from '@/lib/barge-in-detector'
import { openStreamingTranscription, type StreamingTranscriptionSession } from '@/lib/streaming-transcription'
import { isLikelyHallucination, isLikelySelfEchoTranscript } from '@/lib/voice-echo-guard'
import { enqueueSpeech, finishSpeech, startSpeechSession, stopVoicePlayback } from '@/lib/voice-playback'
import { vpLog } from '@/lib/voice-presence-log'
import { isVoiceStopCommand } from '@/lib/voice-stop-word'
import { notify, notifyError } from '@/store/notifications'
import { setUserCaption } from '@/store/voice-presence'

import { useMicRecorder } from './use-mic-recorder'

export type ConversationStatus = 'idle' | 'listening' | 'transcribing' | 'thinking' | 'speaking'

interface PendingVoiceResponse {
  id: string
  pending: boolean
  text: string
}

interface VoiceConversationOptions {
  bargeInEnabled?: boolean
  busy: boolean
  enabled: boolean
  onFatalError?: () => void
  onInterrupt?: () => Promise<void> | void
  onStopWord?: () => void
  onSubmit: (text: string) => Promise<void> | void
  onTranscribeAudio?: (audio: Blob) => Promise<string>
  streamingSttEnabled?: boolean
  pendingResponse: () => PendingVoiceResponse | null
  semanticTurnEnabled?: boolean
  consumePendingResponse: () => void
}

/** How long a barge-triggered interrupt may take to settle before we submit
 *  the captured utterance anyway. */
const INTERRUPT_SETTLE_TIMEOUT_MS = 5_000

export function useVoiceConversation({
  bargeInEnabled = true,
  busy,
  enabled,
  onFatalError,
  onInterrupt,
  onStopWord,
  onSubmit,
  onTranscribeAudio,
  streamingSttEnabled,
  pendingResponse,
  semanticTurnEnabled = true,
  consumePendingResponse
}: VoiceConversationOptions) {
  const { t } = useI18n()
  const voiceCopy = t.notifications.voice
  const { handle, level } = useMicRecorder(voiceCopy)
  const [status, setStatus] = useState<ConversationStatus>('idle')
  const [muted, setMuted] = useState(false)
  const [caption, setCaption] = useState<string | null>(null)
  const turnTimeoutRef = useRef<number | null>(null)
  const pendingStartRef = useRef(false)
  const turnClosingRef = useRef(false)
  const awaitingSpokenResponseRef = useRef(false)
  const responseIdRef = useRef<string | null>(null)
  const spokenSourceLengthRef = useRef(0)
  const speechBufferRef = useRef('')
  const enabledRef = useRef(enabled)
  const mutedRef = useRef(muted)
  const busyRef = useRef(busy)
  const statusRef = useRef<ConversationStatus>('idle')
  const wasEnabledRef = useRef(enabled)
  const streamingRef = useRef<StreamingTranscriptionSession | null>(null)
  // duplex: one gapless speaking session per utterance (fed sentence-by-sentence
  // as the LLM streams) instead of a separate play call per sentence.
  const speakingRef = useRef(false)
  const bargeInterruptedRef = useRef(false)
  const bargeInStopRef = useRef<(() => void) | null>(null)
  const onStopWordRef = useRef(onStopWord)
  const onInterruptRef = useRef(onInterrupt)

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    onInterruptRef.current = onInterrupt
  }, [onInterrupt])

  useEffect(() => {
    onStopWordRef.current = onStopWord
  }, [onStopWord])

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
        const streaming = streamingRef.current
        streamingRef.current = null

        if (!result || (!result.heardSpeech && !forceTranscribe) || !onTranscribeAudio) {
          void streaming?.finish().catch(() => '')

          if (enabledRef.current && !mutedRef.current && !busyRef.current && statusRef.current !== 'speaking') {
            pendingStartRef.current = true
          }

          setStatus('idle')

          return
        }

        try {
          const transcript = (await (streaming ? streaming.finish() : onTranscribeAudio(result.audio))).trim()

          if (!transcript) {
            if (enabledRef.current) {
              pendingStartRef.current = true
            }

            setStatus('idle')

            return
          }

          if (isLikelySelfEchoTranscript(transcript) || isLikelyHallucination(transcript)) {
            vpLog('voice', 'transcript rejected (echo / hallucination)', { transcript })

            if (enabledRef.current && !mutedRef.current && !busyRef.current) {
              pendingStartRef.current = true
            }

            setStatus('idle')

            return
          }

          if (isVoiceStopCommand(transcript)) {
            setUserCaption(null)
            setStatus('idle')
            onStopWordRef.current?.()

            return
          }

          setUserCaption(transcript)
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

  const handleSilence = useCallback(async () => {
    const complete = semanticTurnEnabled ? await streamingRef.current?.checkTurn().catch(() => null) : null

    if (complete === false) {
      vpLog('voice', 'turn incomplete')

      return false
    }

    if (complete === true) {
      vpLog('voice', 'turn complete')
    }

    await handleTurn()
  }, [handleTurn, semanticTurnEnabled])

  const startListening = useCallback(async () => {
    pendingStartRef.current = false

    if (!enabledRef.current || mutedRef.current || busyRef.current) {
      return
    }

    if (statusRef.current !== 'idle') {
      return
    }

    setUserCaption(null)

    let streaming: StreamingTranscriptionSession | null = null

    try {
      vpLog('stt', 'listen start', { streamingSttEnabled })
      streaming = streamingSttEnabled
        ? await openStreamingTranscription({
            onPartial: text => {
              vpLog('stt', 'partial', { len: text.length })
              setUserCaption(text)
            }
          })
        : null
      vpLog('stt', streaming ? 'streaming open' : 'streaming disabled (no provider configured)')
      streamingRef.current = streaming
      const activeStreaming = streaming
      // VAD tuning mirrors `tools.voice_mode` defaults so the browser loop matches the CLI.
      await handle.start({
        silenceLevel: 0.075,
        silenceMs: 1_250,
        idleSilenceMs: 12_000,
        onAudioFrame: activeStreaming ? samples => activeStreaming.sendFrame(samples) : undefined,
        onError: error => {
          notifyError(error, voiceCopy.microphoneFailed)
          pendingStartRef.current = false
          onFatalError?.()
        },
        onSilence: () => handleSilence()
      })
      setStatus('listening')
      // Clear any prior turn-timeout before arming a fresh one. Each listen
      // cycle reassigns turnTimeoutRef; without clearing first, a stale 60s
      // timer from an earlier cycle survives and later fires handleTurn() in
      // the middle of a new listen, cutting it short (or, after enough idle
      // re-listens, wedging the loop into a state it doesn't re-arm from).
      clearTurnTimeout()
      turnTimeoutRef.current = window.setTimeout(() => void handleTurn(), 60_000)
    } catch (error) {
      vpLog('stt', 'listen start failed', { error: String(error) })
      void streaming?.finish().catch(() => '')
      streamingRef.current = null
      notifyError(error, voiceCopy.couldNotStartSession)
      pendingStartRef.current = false
      setStatus('idle')
      onFatalError?.()
    }
  }, [handle, handleSilence, onFatalError, streamingSttEnabled, voiceCopy.couldNotStartSession, voiceCopy.microphoneFailed])

  // Arm barge-in for the whole speaking session (mic stays open through
  // playback; talking over Marvi stops her). Armed once per utterance.
  const armBargeIn = useCallback(() => {
    if (!bargeInEnabled) {
      return
    }

    bargeInterruptedRef.current = false
    bargeInStopRef.current?.()
    // Two-stage barge-in: energy pre-gate feeds the STT, a non-echo transcript
    // confirms it's really the user (robust when AEC crushes the mic to ~0.02).
    bargeInStopRef.current = startBargeInDetector({
      handle,
      streamingSttEnabled: Boolean(streamingSttEnabled),
      onInterrupt: () => {
        bargeInterruptedRef.current = true
        awaitingSpokenResponseRef.current = false
        consumePendingResponse()
        resetSpeechBuffer()
        pendingStartRef.current = true
        speakingRef.current = false
        stopVoicePlayback()
        setCaption(null)
        setStatus('idle')
        void Promise.resolve(onInterrupt?.()).catch(error =>
          vpLog('voice', 'barge-in interrupt failed', { error: String(error) })
        )
      }
    })
  }, [bargeInEnabled, consumePendingResponse, handle, onInterrupt, streamingSttEnabled])

  const disarmBargeIn = useCallback(() => {
    bargeInStopRef.current?.()
    bargeInStopRef.current = null
  }, [])

  // Feed one sentence/clause into the gapless player, starting the session on
  // the first chunk. Non-blocking — the player pipelines synthesis + playback.
  const feedSpeaking = useCallback(
    (chunk: string) => {
      if (!speakingRef.current) {
        speakingRef.current = true
        setStatus('speaking')
        startSpeechSession({ source: 'voice-conversation' })
        armBargeIn()
      }

      setCaption(chunk)
      enqueueSpeech(chunk)
    },
    [armBargeIn]
  )

  // Utterance complete: wait for the queued audio to finish, then disarm and
  // hand back to listening (unless a barge-in already took over).
  const endSpeaking = useCallback(async () => {
    if (!speakingRef.current) {
      return
    }

    try {
      await finishSpeech()
    } catch (error) {
      notifyError(error, voiceCopy.playbackFailed)
    }

    if (bargeInterruptedRef.current) {
      return
    }

    speakingRef.current = false
    disarmBargeIn()
    setCaption(null)

    if (enabledRef.current) {
      pendingStartRef.current = true
    }

    setStatus('idle')
  }, [disarmBargeIn, voiceCopy.playbackFailed])

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
  }, [
    consumePendingResponse,
    onFatalError,
    onTranscribeAudio,
    startListening,
    voiceCopy.configureSpeechToText,
    voiceCopy.unavailable
  ])

  const end = useCallback(async () => {
    pendingStartRef.current = false
    clearTurnTimeout()
    stopVoicePlayback()
    speakingRef.current = false
    disarmBargeIn()
    void streamingRef.current?.finish().catch(() => '')
    streamingRef.current = null
    handle.cancel()
    turnClosingRef.current = false
    awaitingSpokenResponseRef.current = false
    resetSpeechBuffer()
    consumePendingResponse()
    setMuted(false)
    setStatus('idle')
    setCaption(null)
    setUserCaption(null)
  }, [consumePendingResponse, disarmBargeIn, handle])

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
        stopVoicePlayback()
        speakingRef.current = false
        disarmBargeIn()
        void streamingRef.current?.finish().catch(() => '')
        streamingRef.current = null
        handle.cancel()
        setStatus('idle')
      } else if (enabledRef.current && !busyRef.current && statusRef.current === 'idle') {
        pendingStartRef.current = true
      }

      return next
    })
  }, [disarmBargeIn, handle])

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

  // Drive the loop: after a voice-submitted turn, speak stable chunks as the
  // assistant stream grows. Otherwise start listening when idle between turns.
  useEffect(() => {
    if (!enabled || muted) {
      return
    }

    // Feed the gapless speaking session as the assistant stream grows. Runs
    // even while status is 'speaking' so new sentences keep the audio flowing
    // (the player schedules them contiguously — no per-sentence restart).
    if (awaitingSpokenResponseRef.current) {
      const response = pendingResponse()

      if (response) {
        if (response.id !== responseIdRef.current) {
          resetSpeechBuffer()
          responseIdRef.current = response.id
        }

        if (response.text.length > spokenSourceLengthRef.current) {
          appendSpeechText(response.text.slice(spokenSourceLengthRef.current))
          spokenSourceLengthRef.current = response.text.length
        }

        const final = !response.pending && !busy
        let chunk: string | null

        while ((chunk = takeSpeechChunk(final))) {
          feedSpeaking(chunk)
        }

        if (final) {
          awaitingSpokenResponseRef.current = false
          consumePendingResponse()
          resetSpeechBuffer()

          if (speakingRef.current) {
            void endSpeaking()
          } else {
            // Nothing spoken (empty reply) — go straight back to listening.
            pendingStartRef.current = true
            setStatus('idle')
          }

          return
        }

        return
      }

      if (!busy && status === 'thinking') {
        awaitingSpokenResponseRef.current = false
        resetSpeechBuffer()
        pendingStartRef.current = true
        setStatus('idle')

        return
      }
    }

    if (busy || status !== 'idle') {
      return
    }

    if (pendingStartRef.current) {
      void startListening()
    }
  }, [busy, consumePendingResponse, enabled, endSpeaking, feedSpeaking, muted, pendingResponse, startListening, status])

  useEffect(() => {
    if (enabled && !wasEnabledRef.current) {
      void start()
    }

    if (!enabled && wasEnabledRef.current) {
      void end()
    }

    wasEnabledRef.current = enabled
  }, [enabled, end, start])

  return { caption, end, level, muted, start, status, stopTurn, toggleMute }
}
