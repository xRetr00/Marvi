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

import { useMicRecorder } from './use-mic-recorder'

interface WakeWordOptions {
  busy: boolean
  config?: WakeWordConfig
  enabled: boolean
  onSubmit: (text: string) => Promise<void> | void
  onTranscribeAudio?: (audio: Blob) => Promise<string>
}

export function useWakeWord({ busy, config, enabled, onSubmit, onTranscribeAudio }: WakeWordOptions) {
  const { t } = useI18n()
  const voiceCopy = t.notifications.voice
  const wakeConfig = config ?? normalizeWakeWordConfig(undefined)
  const { handle } = useMicRecorder(voiceCopy)
  const [armed, setArmed] = useState(false)
  const wakeSessionRef = useRef<WakeWordSession | null>(null)
  const detectedRef = useRef(false)
  const stoppingRef = useRef(false)
  const restartTimerRef = useRef<number | null>(null)
  const commandTimerRef = useRef<number | null>(null)
  const enabledRef = useRef(enabled)
  const busyRef = useRef(busy)

  useEffect(() => {
    enabledRef.current = enabled
  }, [enabled])

  useEffect(() => {
    busyRef.current = busy
  }, [busy])

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

  const stop = useCallback(() => {
    clearTimers()
    stopWakeSession()
    handle.cancel()
    detectedRef.current = false
    stoppingRef.current = false
    setArmed(false)
  }, [handle])

  const scheduleRestart = useCallback(() => {
    if (!enabledRef.current || busyRef.current) {
      setArmed(false)
      return
    }

    restartTimerRef.current = window.setTimeout(() => {
      restartTimerRef.current = null
      setArmed(false)
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

      if (detected && recording?.audio && onTranscribeAudio) {
        const transcript = (await onTranscribeAudio(recording.audio)).trim()
        const command = stripWakePhrase(transcript, wakeConfig.phrases)

        if (command) {
          await onSubmit(command)
        } else if (transcript) {
          notify({ kind: 'warning', title: voiceCopy.noSpeechDetected, message: voiceCopy.tryRecordingAgain })
        }
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

    if (armed || stoppingRef.current || restartTimerRef.current) {
      return
    }

    let cancelled = false

    const start = async () => {
      try {
        const session = await openWakeWordSession({
          onDetected: () => {
            if (detectedRef.current) {
              return
            }

            detectedRef.current = true
            commandTimerRef.current = window.setTimeout(() => void finishCapture(), wakeConfig.commandTimeoutMs)
          }
        })

        if (cancelled) {
          session.stop()
          return
        }

        wakeSessionRef.current = session
        await handle.start({
          idleSilenceMs: 12_000,
          onAudioFrame: samples => wakeSessionRef.current?.sendFrame(samples),
          onError: error => notifyError(error, voiceCopy.microphoneFailed),
          onSilence: () => void finishCapture(),
          silenceLevel: 0.075,
          silenceMs: 1_250
        })
        setArmed(true)
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
    armed,
    busy,
    enabled,
    finishCapture,
    handle,
    onTranscribeAudio,
    stop,
    voiceCopy.microphoneFailed,
    voiceCopy.streamingUnavailable,
    wakeConfig.commandTimeoutMs,
    wakeConfig.enabled
  ])

  useEffect(() => () => stop(), [stop])

  return { armed, stop }
}
