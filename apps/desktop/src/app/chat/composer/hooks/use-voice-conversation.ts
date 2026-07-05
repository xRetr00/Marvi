import { useCallback, useEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { BARGE_IN_DEFAULTS, type BargeInGateState, createBargeInGate } from '@/lib/voice-barge-in'
import { isLikelySelfEchoTranscript } from '@/lib/voice-echo-guard'
import { openStreamingTranscription, type StreamingTranscriptionSession } from '@/lib/streaming-transcription'
import { enqueueSpeech, finishSpeech, startSpeechSession, stopVoicePlayback } from '@/lib/voice-playback'
import { vpLog } from '@/lib/voice-presence-log'
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
  onSubmit: (text: string) => Promise<void> | void
  onTranscribeAudio?: (audio: Blob) => Promise<string>
  streamingSttEnabled?: boolean
  pendingResponse: () => PendingVoiceResponse | null
  semanticTurnEnabled?: boolean
  consumePendingResponse: () => void
}

export function useVoiceConversation({
  bargeInEnabled = true,
  busy,
  enabled,
  onFatalError,
  onInterrupt,
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

          if (isLikelySelfEchoTranscript(transcript)) {
            vpLog('voice', 'self echo rejected', { transcript })

            if (enabledRef.current && !mutedRef.current && !busyRef.current) {
              pendingStartRef.current = true
            }

            setStatus('idle')

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

    const startedAt = Date.now()
    const gate = createBargeInGate(BARGE_IN_DEFAULTS)
    let lastGateState: BargeInGateState = 'idle'
    let peakLevel = 0
    let lastPeakLogAt = 0
    bargeInterruptedRef.current = false

    // useMicRecorder.start() silently early-returns if a recorder is already
    // active, which would arm NOTHING — cancel() first for a guaranteed start.
    handle.cancel()
    vpLog('voice', 'barge-in armed', { defaults: BARGE_IN_DEFAULTS })
    void handle
      .start({
        onError: err => vpLog('voice', 'barge-in mic error', { error: String(err) }),
        onLevel: level => {
          if (bargeInterruptedRef.current) {
            return
          }

          const elapsedMs = Date.now() - startedAt
          // Peak-level heartbeat (~1s): proves the mic is delivering audio during
          // playback and shows how high user speech reaches vs the threshold.
          peakLevel = Math.max(peakLevel, level)
          if (Date.now() - lastPeakLogAt > 1000) {
            vpLog('voice', 'barge-in level', { peak: Number(peakLevel.toFixed(3)), threshold: BARGE_IN_DEFAULTS.level })
            lastPeakLogAt = Date.now()
            peakLevel = 0
          }

          // NOTE(duplex-phase2): to make this echo-robust on speakers, pass a
          // third arg `confirmed=false` here while the audio is believed to be
          // Marvi's own voice (matching isLikelySelfEchoTranscript). Energy-only today.
          const triggered = gate.update(level, elapsedMs)

          if (gate.state !== lastGateState) {
            vpLog('voice', 'barge-in gate', { state: gate.state, level: Number(level.toFixed(3)), elapsedMs })
            lastGateState = gate.state
          }

          if (!triggered) {
            return
          }

          bargeInterruptedRef.current = true
          vpLog('voice', 'barge-in accepted', { elapsedMs, level })
          awaitingSpokenResponseRef.current = false
          consumePendingResponse()
          resetSpeechBuffer()
          pendingStartRef.current = true
          speakingRef.current = false
          stopVoicePlayback()
          handle.cancel()
          setCaption(null)
          setStatus('idle')
          void Promise.resolve(onInterrupt?.()).catch(error =>
            vpLog('voice', 'barge-in interrupt failed', { error: String(error) })
          )
        }
      })
      .catch(err => vpLog('voice', 'barge-in mic start failed', { error: String(err) }))
  }, [bargeInEnabled, consumePendingResponse, handle, onInterrupt])

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
    handle.cancel()
    setCaption(null)
    if (enabledRef.current) {
      pendingStartRef.current = true
    }
    setStatus('idle')
  }, [handle, voiceCopy.playbackFailed])

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
  }, [consumePendingResponse, handle])

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
        void streamingRef.current?.finish().catch(() => '')
        streamingRef.current = null
        handle.cancel()
        setStatus('idle')
      } else if (enabledRef.current && !busyRef.current && statusRef.current === 'idle') {
        pendingStartRef.current = true
      }

      return next
    })
  }, [handle])

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
