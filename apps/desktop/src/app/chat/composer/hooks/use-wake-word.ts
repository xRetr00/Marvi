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
  const { handle } = useMicRecorder(voiceCopy)
  const [status, setStatus] = useState<WakeWordStatus>('idle')
  const wakeSessionRef = useRef<WakeWordSession | null>(null)
  const streamingSessionRef = useRef<StreamingSttSession | null>(null)
  const detectedRef = useRef(false)
  const stoppingRef = useRef(false)
  const restartTimerRef = useRef<number | null>(null)
  const commandTimerRef = useRef<number | null>(null)
  const enabledRef = useRef(enabled)
  const busyRef = useRef(busy)
  const statusRef = useRef<WakeWordStatus>('idle')

  useEffect(() => {
    enabledRef.current = enabled
  }, [enabled])

  useEffect(() => {
    busyRef.current = busy
  }, [busy])

  useEffect(() => {
    statusRef.current = status
  }, [status])

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
    handle.cancel()
    detectedRef.current = false
    stoppingRef.current = false
    setStatus('idle')
  }, [handle])

  const scheduleRestart = useCallback(() => {
    if (!enabledRef.current || busyRef.current) {
      setStatus('idle')
      return
    }

    restartTimerRef.current = window.setTimeout(() => {
      restartTimerRef.current = null
      setStatus('idle')
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
      const recording = await handle.stop()
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

        if (!transcript && recording?.audio && onTranscribeAudio) {
          transcript = (await onTranscribeAudio(recording.audio)).trim()
        }

        const command = stripWakePhrase(transcript, wakeConfig.phrases)

        if (command) {
          await onSubmit(command)
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
    handle,
    onSubmit,
    onTranscribeAudio,
    scheduleRestart,
    voiceCopy.noSpeechDetected,
    voiceCopy.transcriptionFailed,
    voiceCopy.tryRecordingAgain,
    wakeConfig.phrases
  ])

  useEffect(() => {
    if (!wakeConfig.enabled || !enabled || busy || !onTranscribeAudio) {
      stop()
      return
    }

    if (status !== 'idle' || stoppingRef.current || restartTimerRef.current) {
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
            commandTimerRef.current = window.setTimeout(() => void finishCapture(), wakeConfig.commandTimeoutMs)
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

        await handle.start({
          idleSilenceMs: 12_000,
          onAudioFrame: samples => {
            wakeSessionRef.current?.sendFrame(samples)
            streamingSessionRef.current?.sendFrame(samples)
            if (detectedRef.current && statusRef.current === 'woken') {
              setStatus('listening')
            }
          },
          onError: error => notifyError(error, voiceCopy.microphoneFailed),
          onSilence: () => void finishCapture(),
          silenceLevel: 0.075,
          silenceMs: 1_250
        })
        setStatus('armed')
      } catch (error) {
        if (!cancelled) {
          notifyError(error, voiceCopy.streamingUnavailable)
          stop()
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
    finishCapture,
    handle,
    onTranscribeAudio,
    stop,
    status,
    sttStreamingEnabled,
    voiceCopy.microphoneFailed,
    voiceCopy.streamingUnavailable,
    wakeConfig.commandTimeoutMs,
    wakeConfig.enabled
  ])

  useEffect(() => () => stop(), [stop])

  return { armed: status !== 'idle', status, stop }
}
