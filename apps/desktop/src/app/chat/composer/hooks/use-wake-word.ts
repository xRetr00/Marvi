import { useCallback, useEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { isLikelyHallucination, isLikelySelfEchoTranscript } from '@/lib/voice-echo-guard'
import { openStreamingTranscription, type StreamingTranscriptionSession } from '@/lib/streaming-transcription'
import { vpLog } from '@/lib/voice-presence-log'
import {
  normalizeWakeWordConfig,
  openWakeWordSession,
  stripWakePhrase,
  type WakeWordConfig,
  type WakeWordSession
} from '@/lib/wake-word'
import { notify, notifyError } from '@/store/notifications'
import { setUserCaption } from '@/store/voice-presence'

import { useMicRecorder } from './use-mic-recorder'

export type WakeWordStatus = 'idle' | 'arming' | 'armed' | 'woken' | 'listening' | 'transcribing'

// Ignore wake detections in the first moment after the mic opens — the mic
// warmup / first buffered frames (e.g. right when presence mode is switched on)
// can produce a spurious hotword hit. See issue: false positive on presence on.
const WAKE_STARTUP_GRACE_MS = 900

// Conversation continuity: after a turn, keep capturing follow-ups WITHOUT the
// wake phrase. The conversation ends (and the wake word re-arms) on either
// signal (option C): the user goes quiet for CONVERSATION_IDLE_MS, or says an
// end phrase like "goodbye" / "that's all".
const CONVERSATION_IDLE_MS = 9_000

const END_PHRASE_RE =
  /^(?:ok(?:ay)?\s+)?(?:thanks?\s+)?(?:that'?s?\s+(?:all|it)|that\s+is\s+all|good\s?bye|bye(?:\s+now)?|stop\s+listening|we'?re\s+done|were\s+done|end\s+conversation|nothing\s+else)[.!\s]*$/i

function isEndPhrase(command: string): boolean {
  return END_PHRASE_RE.test(command.trim())
}

interface WakeWordOptions {
  busy: boolean
  config?: WakeWordConfig
  enabled: boolean
  /** Hand wake activation to the shared duplex conversation instead of using the legacy command recorder. */
  onWakeDetected?: () => Promise<void> | void
  onSubmit: (text: string) => Promise<void> | void
  onTranscribeAudio?: (audio: Blob) => Promise<string>
  semanticTurnEnabled?: boolean
  streamingSttEnabled?: boolean
}

function encodePcmFramesAsWav(frames: Float32Array[], sampleRate = 16000): Blob | null {
  const sampleCount = frames.reduce((total, frame) => total + frame.length, 0)

  if (!sampleCount) {
    return null
  }

  const bytesPerSample = 2
  const dataBytes = sampleCount * bytesPerSample
  const buffer = new ArrayBuffer(44 + dataBytes)
  const view = new DataView(buffer)

  const writeAscii = (offset: number, value: string) => {
    for (let i = 0; i < value.length; i += 1) {
      view.setUint8(offset + i, value.charCodeAt(i))
    }
  }

  writeAscii(0, 'RIFF')
  view.setUint32(4, 36 + dataBytes, true)
  writeAscii(8, 'WAVE')
  writeAscii(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * bytesPerSample, true)
  view.setUint16(32, bytesPerSample, true)
  view.setUint16(34, 8 * bytesPerSample, true)
  writeAscii(36, 'data')
  view.setUint32(40, dataBytes, true)

  let offset = 44

  for (const frame of frames) {
    for (const sample of frame) {
      const clamped = Math.max(-1, Math.min(1, sample))
      view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true)
      offset += bytesPerSample
    }
  }

  return new Blob([buffer], { type: 'audio/wav' })
}

export function useWakeWord({
  busy,
  config,
  enabled,
  onWakeDetected,
  onSubmit,
  onTranscribeAudio,
  semanticTurnEnabled = true,
  streamingSttEnabled
}: WakeWordOptions) {
  const { t } = useI18n()
  const voiceCopy = t.notifications.voice
  const wakeConfig = config ?? normalizeWakeWordConfig(undefined)

  const wakeConfigKey = [
    enabled,
    wakeConfig.enabled,
    wakeConfig.provider,
    wakeConfig.sampleRate,
    wakeConfig.phrases.join('\u0000'),
    wakeConfig.threshold,
    wakeConfig.boost,
    wakeConfig.debug,
    wakeConfig.commandTimeoutMs,
    wakeConfig.cooldownMs
  ].join('|')

  const { handle } = useMicRecorder(voiceCopy)
  const [status, setStatus] = useState<WakeWordStatus>('idle')
  const [startTick, setStartTick] = useState(0)
  const transcribeAvailable = Boolean(onTranscribeAudio)
  const wakeSessionRef = useRef<WakeWordSession | null>(null)
  const streamingSessionRef = useRef<StreamingTranscriptionSession | null>(null)
  const streamingOpenRef = useRef<Promise<StreamingTranscriptionSession | null> | null>(null)
  const streamingErrorRef = useRef<unknown>(null)
  const streamedCommandFramesRef = useRef(0)
  const detectedRef = useRef(false)
  const resumeCaptureRef = useRef(false)
  const conversationEndTimerRef = useRef<number | null>(null)
  const stoppingRef = useRef(false)
  const startupFailedRef = useRef(false)
  const pendingRestartAfterSubmitRef = useRef(false)
  const commandFramesRef = useRef<Float32Array[]>([])
  const micOpenedAtRef = useRef(0)
  const restartTimerRef = useRef<number | null>(null)
  const commandTimerRef = useRef<number | null>(null)
  const enabledRef = useRef(enabled)
  const busyRef = useRef(busy)
  const statusRef = useRef<WakeWordStatus>('idle')
  const handleRef = useRef(handle)
  const onSubmitRef = useRef(onSubmit)
  const onWakeDetectedRef = useRef(onWakeDetected)
  const onTranscribeAudioRef = useRef(onTranscribeAudio)
  const finishCaptureRef = useRef<(() => Promise<void>) | null>(null)
  const finishIfTurnCompleteRef = useRef<(() => Promise<boolean | void>) | null>(null)

  const debugLog = useCallback(
    (message: string, detail?: Record<string, unknown>) => {
      if (!wakeConfig.debug) {
        return
      }

      console.info(`[wake-word] ${message}`, detail ?? {})
      vpLog('wake', message, detail)
    },
    [wakeConfig.debug]
  )

  useEffect(() => {
    enabledRef.current = enabled
  }, [enabled])

  useEffect(() => {
    busyRef.current = busy
  }, [busy])

  useEffect(() => {
    statusRef.current = status
  }, [status])

  useEffect(() => {
    handleRef.current = handle
  }, [handle])

  useEffect(() => {
    onSubmitRef.current = onSubmit
    onWakeDetectedRef.current = onWakeDetected
    onTranscribeAudioRef.current = onTranscribeAudio
  }, [onSubmit, onTranscribeAudio, onWakeDetected])

  useEffect(() => {
    startupFailedRef.current = false
  }, [wakeConfigKey])

  const clearTimers = () => {
    if (restartTimerRef.current) {
      window.clearTimeout(restartTimerRef.current)
      restartTimerRef.current = null
    }

    if (commandTimerRef.current) {
      window.clearTimeout(commandTimerRef.current)
      commandTimerRef.current = null
    }

    if (conversationEndTimerRef.current) {
      window.clearTimeout(conversationEndTimerRef.current)
      conversationEndTimerRef.current = null
    }
  }

  const stopWakeSession = () => {
    wakeSessionRef.current?.stop()
    wakeSessionRef.current = null
  }

  const stopStreamingSession = () => {
    void Promise.resolve(streamingSessionRef.current?.finish()).catch(() => '')
    streamingSessionRef.current = null
    streamingOpenRef.current = null
    streamedCommandFramesRef.current = 0
  }

  const stop = useCallback(() => {
    clearTimers()
    stopWakeSession()
    stopStreamingSession()
    if (statusRef.current !== 'idle') {
      handleRef.current.cancel()
    }
    detectedRef.current = false
    resumeCaptureRef.current = false
    pendingRestartAfterSubmitRef.current = false
    stoppingRef.current = false
    commandFramesRef.current = []
    setStatus('idle')
    setUserCaption(null)
  }, [])

  const scheduleRestart = useCallback(() => {
    // Plain restart = the conversation is over → re-arm the wake word.
    resumeCaptureRef.current = false

    if (!enabledRef.current || busyRef.current) {
      debugLog('restart skipped (disabled or busy)')
      setStatus('idle')
      setUserCaption(null)

      return
    }

    debugLog('restart scheduled', { cooldownMs: wakeConfig.cooldownMs })
    restartTimerRef.current = window.setTimeout(() => {
      restartTimerRef.current = null
      setStatus('idle')
      setUserCaption(null)
      setStartTick(tick => tick + 1)
    }, wakeConfig.cooldownMs)
  }, [debugLog, wakeConfig.cooldownMs])

  /** Take the microphone back after the duplex conversation closes. `stop`
   * clears the handed-off `woken` presentation synchronously; the normal
   * cooldown then opens a fresh wake session without overlapping duplex. */
  const rearm = useCallback(() => {
    stop()
    scheduleRestart()
  }, [scheduleRestart, stop])

  const scheduleRestartAfterSubmittedTurn = useCallback(() => {
    // Stay in the conversation: once the agent + TTS finish (!busy), resume
    // capturing the next turn directly (no wake phrase). The resume runs in the
    // effect below and via this fallback timer, whichever sees !busy first.
    resumeCaptureRef.current = true
    pendingRestartAfterSubmitRef.current = true
    setStatus('idle')
    setUserCaption(null)

    restartTimerRef.current = window.setTimeout(() => {
      restartTimerRef.current = null

      if (!pendingRestartAfterSubmitRef.current || busyRef.current) {
        return
      }

      pendingRestartAfterSubmitRef.current = false
      setStartTick(tick => tick + 1)
    }, Math.max(wakeConfig.cooldownMs, 900))
  }, [wakeConfig.cooldownMs])

  const finishCapture = useCallback(async () => {
    if (stoppingRef.current) {
      return
    }

    stoppingRef.current = true
    clearTimers()
    stopWakeSession()

    let submittedCommand = false

    try {
      const recording = await handleRef.current.stop()
      const heardSpeech = recording?.heardSpeech ?? false
      const detected = detectedRef.current
      const commandFrames = commandFramesRef.current
      const commandAudio = encodePcmFramesAsWav(commandFrames, wakeConfig.sampleRate)
      debugLog('finish capture', {
        commandAudioBytes: commandAudio?.size ?? 0,
        commandFrames: commandFrames.length,
        detected
      })
      detectedRef.current = false
      commandFramesRef.current = []

      if (detected) {
        setStatus('transcribing')
        let transcript = ''
        const streamingSession = streamingSessionRef.current ?? (await streamingOpenRef.current)

        if (streamingSession) {
          for (const frame of commandFrames.slice(streamedCommandFramesRef.current)) {
            streamingSession.sendFrame(frame)
          }

          transcript = (await streamingSession.finish()).trim()
        }

        if (!transcript) {
          const transcribeAudio = onTranscribeAudioRef.current

          if (transcribeAudio && commandAudio) {
            transcript = (await transcribeAudio(commandAudio)).trim()
          } else if (streamingSttEnabled) {
            const error = streamingErrorRef.current
            throw error instanceof Error ? error : new Error(voiceCopy.streamingUnavailable)
          }
        }

        streamingSessionRef.current = null
        streamingOpenRef.current = null
        streamedCommandFramesRef.current = 0

        if (transcript) {
          setUserCaption(transcript)
        }

        if (isLikelySelfEchoTranscript(transcript)) {
          vpLog('wake', 'self echo rejected', { transcript })

          return
        }

        const command = stripWakePhrase(transcript, wakeConfig.phrases)
        debugLog('transcribed command', {
          command,
          transcript,
          wakePhraseStripped: command !== transcript
        })

        if (command && isLikelyHallucination(command)) {
          // Drop known STT hallucinations on silence/noise ("you", "okay",
          // "thank you", ...). submittedCommand stays false -> finally
          // re-arms the wake word (does not continue the conversation).
          vpLog('wake', 'rejected (no speech / hallucination)', { command, heardSpeech })
          debugLog('rejected (no speech / hallucination)', { command, heardSpeech })
        } else if (command && isEndPhrase(command)) {
          // Option C end trigger: an end phrase closes the conversation (don't
          // submit it). submittedCommand stays false -> finally re-arms the wake
          // word.
          debugLog('end phrase -> ending conversation', { command })
        } else if (command) {
          await onSubmitRef.current(command)
          submittedCommand = true
        } else if (transcript) {
          notify({ kind: 'warning', title: voiceCopy.noSpeechDetected, message: voiceCopy.tryRecordingAgain })
        } else {
          debugLog('no command transcript after wake')
        }
      }
    } catch (error) {
      notifyError(error, voiceCopy.transcriptionFailed)
    } finally {
      streamingSessionRef.current = null
      streamingOpenRef.current = null
      streamingErrorRef.current = null
      streamedCommandFramesRef.current = 0
      stoppingRef.current = false
      if (submittedCommand) {
        scheduleRestartAfterSubmittedTurn()
      } else {
        scheduleRestart()
      }
    }
  }, [
    scheduleRestart,
    scheduleRestartAfterSubmittedTurn,
    voiceCopy.noSpeechDetected,
    voiceCopy.streamingUnavailable,
    voiceCopy.transcriptionFailed,
    voiceCopy.tryRecordingAgain,
    debugLog,
    streamingSttEnabled,
    wakeConfig.phrases,
    wakeConfig.sampleRate
  ])

  useEffect(() => {
    finishCaptureRef.current = finishCapture
  }, [finishCapture])

  const finishIfTurnComplete = useCallback(async () => {
    if (!detectedRef.current) {
      return
    }

    const streamingSession = semanticTurnEnabled ? streamingSessionRef.current ?? (await streamingOpenRef.current) : null
    const complete = await streamingSession?.checkTurn().catch(() => null)

    if (complete === false) {
      vpLog('wake', 'turn incomplete')
      debugLog('turn incomplete')
      return false
    }

    if (complete === true) {
      vpLog('wake', 'turn complete')
    }
    await finishCaptureRef.current?.()
  }, [debugLog, semanticTurnEnabled])

  useEffect(() => {
    finishIfTurnCompleteRef.current = finishIfTurnComplete
  }, [finishIfTurnComplete])

  useEffect(() => {
    if (!pendingRestartAfterSubmitRef.current || busy || statusRef.current !== 'idle' || restartTimerRef.current) {
      return
    }

    pendingRestartAfterSubmitRef.current = false
    if (resumeCaptureRef.current) {
      setStartTick(tick => tick + 1) // resume the conversation (capture without wake)
    } else {
      scheduleRestart()
    }
  }, [busy, scheduleRestart])

  useEffect(() => {
    if (!wakeConfig.enabled || !enabled || busy || (!transcribeAvailable && !onWakeDetectedRef.current)) {
      stop()

      return
    }

    if (startupFailedRef.current || statusRef.current !== 'idle' || stoppingRef.current || restartTimerRef.current) {
      return
    }

    let cancelled = false

    const start = async () => {
      try {
        // Conversation continuity: resume capturing the next turn directly, with
        // no wake session and no phrase. FREEZE-PROOF: cancel the mic first (a
        // still-active recorder makes useMicRecorder.start() silently no-op ->
        // no audio callbacks -> stuck 'listening'), and arm a hard idle timer
        // that always ends the conversation. Ends via silence / end phrase too.
        if (resumeCaptureRef.current) {
          debugLog('conversation continue (no wake phrase)')
          handleRef.current.cancel()
          detectedRef.current = true
          commandFramesRef.current = []
          streamingErrorRef.current = null
          streamedCommandFramesRef.current = 0

          if (streamingSttEnabled) {
            streamingOpenRef.current = openStreamingTranscription({ onPartial: text => setUserCaption(text) })
              .then(session => {
                streamingSessionRef.current = session
                return session
              })
              .catch(error => {
                streamingErrorRef.current = error
                return null
              })
          }

          setStatus('listening')
          micOpenedAtRef.current = Date.now()

          // Hard safety: if nothing resolves the turn, end the conversation.
          conversationEndTimerRef.current = window.setTimeout(() => {
            conversationEndTimerRef.current = null
            void finishCaptureRef.current?.()
          }, CONVERSATION_IDLE_MS + 2_000)

          try {
            await handleRef.current.start({
              idleSilenceMs: CONVERSATION_IDLE_MS,
              onAudioFrame: samples => {
                commandFramesRef.current.push(new Float32Array(samples))
                if (streamingSessionRef.current) {
                  streamingSessionRef.current.sendFrame(samples)
                  streamedCommandFramesRef.current = commandFramesRef.current.length
                }
              },
              onError: error => notifyError(error, voiceCopy.microphoneFailed),
              onSilence: () => finishIfTurnCompleteRef.current?.(),
              silenceLevel: 0.075,
              silenceMs: 1_250
            })
          } catch (error) {
            debugLog('resume capture failed', { error: String(error) })
            scheduleRestart()
            return
          }

          commandTimerRef.current = window.setTimeout(() => void finishCaptureRef.current?.(), wakeConfig.commandTimeoutMs)

          return
        }

        setStatus('arming')
        debugLog('arming')

        const session = await openWakeWordSession({
          debug: wakeConfig.debug,
          onDetected: phrase => {
            if (detectedRef.current) {
              return
            }

            const sinceOpen = Date.now() - micOpenedAtRef.current
            if (micOpenedAtRef.current && sinceOpen < WAKE_STARTUP_GRACE_MS) {
              debugLog('detection ignored (startup grace)', { phrase, sinceOpen })
              return
            }

            debugLog('detected', { phrase })
            detectedRef.current = true
            stopWakeSession()
            stopStreamingSession()
            commandFramesRef.current = []
            streamingErrorRef.current = null

            // Presence is only the activation gate. The explicit voice-mode
            // duplex client owns mic, STT, speaker ID, Island state, barge-in,
            // TTS, and follow-up turns after the wake phrase.
            if (onWakeDetectedRef.current) {
              setStatus('woken')
              handleRef.current.cancel()
              void onWakeDetectedRef.current()
              return
            }

            if (streamingSttEnabled) {
              streamingOpenRef.current = openStreamingTranscription({ onPartial: text => setUserCaption(text) })
                .then(session => {
                  streamingSessionRef.current = session

                  for (const frame of commandFramesRef.current) {
                    session.sendFrame(frame)
                  }

                  streamedCommandFramesRef.current = commandFramesRef.current.length

                  return session
                })
                .catch(error => {
                  streamingErrorRef.current = error

                  return null
                })
            }

            setStatus('woken')
            commandTimerRef.current = window.setTimeout(() => void finishCaptureRef.current?.(), wakeConfig.commandTimeoutMs)
          }
        })

        if (cancelled) {
          session.stop()
          setStatus('idle')

          return
        }

        wakeSessionRef.current = session
        micOpenedAtRef.current = Date.now()

        await handleRef.current.start({
          idleSilenceMs: 12_000,
          onAudioFrame: samples => {
            if (!detectedRef.current) {
              wakeSessionRef.current?.sendFrame(samples)

              return
            }

            commandFramesRef.current.push(new Float32Array(samples))

            if (streamingSessionRef.current) {
              streamingSessionRef.current.sendFrame(samples)
              streamedCommandFramesRef.current = commandFramesRef.current.length
            }

            if (wakeConfig.debug && commandFramesRef.current.length % 20 === 1) {
              debugLog('capturing command frames', {
                frames: commandFramesRef.current.length,
                samples: commandFramesRef.current.reduce((total, frame) => total + frame.length, 0)
              })
            }

            if (statusRef.current === 'woken') {
              setStatus('listening')
            }
          },
          onError: error => notifyError(error, voiceCopy.microphoneFailed),
          onSilence: () => {
            if (detectedRef.current) {
              return finishIfTurnCompleteRef.current?.()
            }

            return undefined
          },
          silenceLevel: 0.075,
          silenceMs: 1_250
        })
        setStatus('armed')
        debugLog('armed')
      } catch (error) {
        if (!cancelled) {
          startupFailedRef.current = true
          debugLog('startup failed', { error: String(error) })
          notifyError(error, voiceCopy.streamingUnavailable)
          clearTimers()
          stopWakeSession()
          stopStreamingSession()
          handleRef.current.cancel()
          detectedRef.current = false
          stoppingRef.current = false
          commandFramesRef.current = []
          pendingRestartAfterSubmitRef.current = false
          setStatus('idle')
        }
      }
    }

    void start()

    return () => {
      cancelled = true
    }
  }, [
    busy,
    debugLog,
    enabled,
    scheduleRestart,
    startTick,
    stop,
    transcribeAvailable,
    voiceCopy.microphoneFailed,
    voiceCopy.streamingUnavailable,
    wakeConfig.commandTimeoutMs,
    wakeConfig.debug,
    wakeConfig.enabled,
    streamingSttEnabled
  ])

  useEffect(() => () => stop(), [stop])

  return { armed: status !== 'idle', rearm, status, stop }
}
