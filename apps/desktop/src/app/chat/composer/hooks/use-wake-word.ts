import { useCallback, useEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { notify, notifyError } from '@/store/notifications'
import {
  normalizeWakeWordConfig,
  openWakeWordSession,
  stripWakePhrase,
  type WakeWordConfig,
  type WakeWordSession
} from '@/lib/wake-word'
import { openStreamingSttSession, type StreamingSttSession } from '@/lib/streaming-stt'

import { useMicRecorder } from './use-mic-recorder'

export type WakeWordStatus = 'idle' | 'arming' | 'armed' | 'woken' | 'listening' | 'transcribing'

interface WakeWordOptions {
  busy: boolean
  config?: WakeWordConfig
  enabled: boolean
  onSubmit: (text: string) => Promise<void> | void
  onTranscribeAudio?: (audio: Blob) => Promise<string>
  sttStreamingEnabled?: boolean
}

export function useWakeWord({
  busy,
  config,
  enabled,
  onSubmit,
  onTranscribeAudio,
  sttStreamingEnabled = false
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
    wakeConfig.commandTimeoutMs,
    wakeConfig.cooldownMs
  ].join('|')
  const { handle } = useMicRecorder(voiceCopy)
  const [status, setStatus] = useState<WakeWordStatus>('idle')
  const [startTick, setStartTick] = useState(0)
  const transcribeAvailable = Boolean(onTranscribeAudio)
  const wakeSessionRef = useRef<WakeWordSession | null>(null)
  const streamingSessionRef = useRef<StreamingSttSession | null>(null)
  const detectedRef = useRef(false)
  const stoppingRef = useRef(false)
  const startupFailedRef = useRef(false)
  const restartTimerRef = useRef<number | null>(null)
  const commandTimerRef = useRef<number | null>(null)
  const enabledRef = useRef(enabled)
  const busyRef = useRef(busy)
  const statusRef = useRef<WakeWordStatus>('idle')
  const handleRef = useRef(handle)
  const onSubmitRef = useRef(onSubmit)
  const onTranscribeAudioRef = useRef(onTranscribeAudio)
  const finishCaptureRef = useRef<(() => Promise<void>) | null>(null)

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
    streamingSessionRef.current?.stop()
    streamingSessionRef.current = null
  }

  const stop = useCallback(() => {
    clearTimers()
    stopWakeSession()
    stopStreamingSession()
    handleRef.current.cancel()
    detectedRef.current = false
    stoppingRef.current = false
    setStatus('idle')
  }, [])

  const scheduleRestart = useCallback(() => {
    if (!enabledRef.current || busyRef.current) {
      setStatus('idle')
      return
    }

    restartTimerRef.current = window.setTimeout(() => {
      restartTimerRef.current = null
      setStatus('idle')
      setStartTick(tick => tick + 1)
    }, wakeConfig.cooldownMs)
  }, [wakeConfig.cooldownMs])

  const finishCapture = useCallback(async () => {
    if (stoppingRef.current) {
      return
    }

    stoppingRef.current = true
    clearTimers()
    stopWakeSession()

    try {
      const recording = await handleRef.current.stop()
      const detected = detectedRef.current
      detectedRef.current = false

      if (detected) {
        setStatus('transcribing')
        let transcript = ''

        try {
          transcript = (await streamingSessionRef.current?.finish())?.trim() ?? ''
        } catch (error) {
          console.warn('[wake-word] Streaming STT failed; falling back to standard transcription.', error)
        } finally {
          streamingSessionRef.current = null
        }

        const transcribeAudio = onTranscribeAudioRef.current
        if (!transcript && recording?.audio && transcribeAudio) {
          transcript = (await transcribeAudio(recording.audio)).trim()
        }

        const command = stripWakePhrase(transcript, wakeConfig.phrases)

        if (command) {
          await onSubmitRef.current(command)
        } else if (transcript) {
          notify({ kind: 'warning', title: voiceCopy.noSpeechDetected, message: voiceCopy.tryRecordingAgain })
        }
      } else {
        stopStreamingSession()
      }
    } catch (error) {
      notifyError(error, voiceCopy.transcriptionFailed)
    } finally {
      stoppingRef.current = false
      scheduleRestart()
    }
  }, [
    scheduleRestart,
    voiceCopy.noSpeechDetected,
    voiceCopy.transcriptionFailed,
    voiceCopy.tryRecordingAgain,
    wakeConfig.phrases
  ])

  useEffect(() => {
    finishCaptureRef.current = finishCapture
  }, [finishCapture])

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
        const session = await openWakeWordSession({
          onDetected: () => {
            if (detectedRef.current) {
              return
            }

            detectedRef.current = true
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
        if (sttStreamingEnabled) {
          try {
            streamingSessionRef.current = await openStreamingSttSession()
          } catch (error) {
            console.warn('[wake-word] Streaming STT unavailable; falling back to standard transcription.', error)
            streamingSessionRef.current = null
          }
        }

        await handleRef.current.start({
          idleSilenceMs: 12_000,
          onAudioFrame: samples => {
            wakeSessionRef.current?.sendFrame(samples)
            streamingSessionRef.current?.sendFrame(samples)
            if (detectedRef.current && statusRef.current === 'woken') {
              setStatus('listening')
            }
          },
          onError: error => notifyError(error, voiceCopy.microphoneFailed),
          onSilence: () => void finishCaptureRef.current?.(),
          silenceLevel: 0.075,
          silenceMs: 1_250
        })
        setStatus('armed')
      } catch (error) {
        if (!cancelled) {
          startupFailedRef.current = true
          notifyError(error, voiceCopy.streamingUnavailable)
          clearTimers()
          stopWakeSession()
          stopStreamingSession()
          handleRef.current.cancel()
          detectedRef.current = false
          stoppingRef.current = false
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
    enabled,
    startTick,
    stop,
    sttStreamingEnabled,
    transcribeAvailable,
    voiceCopy.microphoneFailed,
    voiceCopy.streamingUnavailable,
    wakeConfig.commandTimeoutMs,
    wakeConfig.enabled
  ])

  useEffect(() => () => stop(), [stop])

  return { armed: status !== 'idle', status, stop }
}
