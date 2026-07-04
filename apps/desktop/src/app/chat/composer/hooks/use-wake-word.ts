import { useCallback, useEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
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

interface WakeWordOptions {
  busy: boolean
  config?: WakeWordConfig
  enabled: boolean
  onSubmit: (text: string) => Promise<void> | void
  onTranscribeAudio?: (audio: Blob) => Promise<string>
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
  onSubmit,
  onTranscribeAudio,
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
  const stoppingRef = useRef(false)
  const startupFailedRef = useRef(false)
  const pendingRestartAfterSubmitRef = useRef(false)
  const commandFramesRef = useRef<Float32Array[]>([])
  const restartTimerRef = useRef<number | null>(null)
  const commandTimerRef = useRef<number | null>(null)
  const enabledRef = useRef(enabled)
  const busyRef = useRef(busy)
  const statusRef = useRef<WakeWordStatus>('idle')
  const handleRef = useRef(handle)
  const onSubmitRef = useRef(onSubmit)
  const onTranscribeAudioRef = useRef(onTranscribeAudio)
  const finishCaptureRef = useRef<(() => Promise<void>) | null>(null)

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
    onTranscribeAudioRef.current = onTranscribeAudio
  }, [onSubmit, onTranscribeAudio])

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
  }

  const stopWakeSession = () => {
    wakeSessionRef.current?.stop()
    wakeSessionRef.current = null
  }

  const stopStreamingSession = () => {
    void streamingSessionRef.current?.finish().catch(() => '')
    streamingSessionRef.current = null
    streamingOpenRef.current = null
    streamedCommandFramesRef.current = 0
  }

  const stop = useCallback(() => {
    clearTimers()
    stopWakeSession()
    stopStreamingSession()
    handleRef.current.cancel()
    detectedRef.current = false
    pendingRestartAfterSubmitRef.current = false
    stoppingRef.current = false
    commandFramesRef.current = []
    setStatus('idle')
    setUserCaption(null)
  }, [])

  const scheduleRestart = useCallback(() => {
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

  const scheduleRestartAfterSubmittedTurn = useCallback(() => {
    pendingRestartAfterSubmitRef.current = true
    setStatus('idle')
    setUserCaption(null)

    restartTimerRef.current = window.setTimeout(() => {
      restartTimerRef.current = null

      if (!pendingRestartAfterSubmitRef.current || busyRef.current) {
        return
      }

      pendingRestartAfterSubmitRef.current = false
      scheduleRestart()
    }, Math.max(wakeConfig.cooldownMs, 1500))
  }, [scheduleRestart, wakeConfig.cooldownMs])

  const finishCapture = useCallback(async () => {
    if (stoppingRef.current) {
      return
    }

    stoppingRef.current = true
    clearTimers()
    stopWakeSession()

    let submittedCommand = false

    try {
      await handleRef.current.stop()
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

        const command = stripWakePhrase(transcript, wakeConfig.phrases)
        debugLog('transcribed command', {
          command,
          transcript,
          wakePhraseStripped: command !== transcript
        })

        if (command) {
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

  useEffect(() => {
    if (!pendingRestartAfterSubmitRef.current || busy || statusRef.current !== 'idle' || restartTimerRef.current) {
      return
    }

    pendingRestartAfterSubmitRef.current = false
    scheduleRestart()
  }, [busy, scheduleRestart])

  useEffect(() => {
    if (!wakeConfig.enabled || !enabled || busy || !transcribeAvailable) {
      stop()

      return
    }

    if (startupFailedRef.current || statusRef.current !== 'idle' || stoppingRef.current || restartTimerRef.current) {
      return
    }

    let cancelled = false

    const start = async () => {
      try {
        setStatus('arming')
        debugLog('arming')

        const session = await openWakeWordSession({
          debug: wakeConfig.debug,
          onDetected: phrase => {
            if (detectedRef.current) {
              return
            }

            debugLog('detected', { phrase })
            detectedRef.current = true
            stopWakeSession()
            stopStreamingSession()
            commandFramesRef.current = []
            streamingErrorRef.current = null

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
              void finishCaptureRef.current?.()
            }
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

  return { armed: status !== 'idle', status, stop }
}
