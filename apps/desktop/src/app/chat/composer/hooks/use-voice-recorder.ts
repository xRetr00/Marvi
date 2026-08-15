import { useEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { openStreamingTranscription, type StreamingTranscriptionSession } from '@/lib/streaming-transcription'
import { isLikelySelfEchoTranscript } from '@/lib/voice-echo-guard'
import { vpLog } from '@/lib/voice-presence-log'
import { notify, notifyError } from '@/store/notifications'

import type { VoiceActivityState, VoiceStatus } from '../types'

import { useMicRecorder } from './use-mic-recorder'

interface VoiceRecorderOptions {
  maxRecordingSeconds: number
  onTranscribeAudio?: (audio: Blob) => Promise<string>
  streamingSttEnabled?: boolean
  focusInput: () => void
  onTranscript: (text: string) => void
}

export function useVoiceRecorder({
  maxRecordingSeconds,
  onTranscribeAudio,
  streamingSttEnabled,
  focusInput,
  onTranscript
}: VoiceRecorderOptions) {
  const { t } = useI18n()
  const voiceCopy = t.notifications.voice
  const { handle, level, recording } = useMicRecorder(voiceCopy)
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus>('idle')
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const startedAtRef = useRef(0)
  const streamingRef = useRef<StreamingTranscriptionSession | null>(null)
  const intervalRef = useRef<number | null>(null)
  const timeoutRef = useRef<number | null>(null)

  const clearTimers = () => {
    if (intervalRef.current) {
      window.clearInterval(intervalRef.current)
      intervalRef.current = null
    }

    if (timeoutRef.current) {
      window.clearTimeout(timeoutRef.current)
      timeoutRef.current = null
    }
  }

  useEffect(() => () => clearTimers(), [])

  const stop = async () => {
    clearTimers()
    const result = await handle.stop()
    const streaming = streamingRef.current
    streamingRef.current = null

    if (!result) {
      void streaming?.finish().catch(() => '')
      setVoiceStatus('idle')

      return
    }

    if (!onTranscribeAudio) {
      setVoiceStatus('idle')

      return
    }

    setVoiceStatus('transcribing')

    try {
      const transcript = (await (streaming ? streaming.finish() : onTranscribeAudio(result.audio))).trim()

      if (!transcript) {
        notify({ kind: 'warning', title: voiceCopy.noSpeechDetected, message: voiceCopy.tryRecordingAgain })
      } else if (isLikelySelfEchoTranscript(transcript)) {
        vpLog('voice', 'self echo rejected', { transcript })
      } else {
        onTranscript(transcript)
      }
    } catch (error) {
      notifyError(error, voiceCopy.transcriptionFailed)
    } finally {
      setVoiceStatus('idle')
      focusInput()
    }
  }

  const start = async () => {
    if (!onTranscribeAudio) {
      notify({ kind: 'warning', title: voiceCopy.unavailable, message: voiceCopy.transcriptionUnavailable })

      return
    }

    let streaming: StreamingTranscriptionSession | null = null

    try {
      streaming = streamingSttEnabled ? await openStreamingTranscription() : null
      streamingRef.current = streaming
      const activeStreaming = streaming
      await handle.start({
        onAudioFrame: activeStreaming ? samples => activeStreaming.sendFrame(samples) : undefined,
        onError: error => notifyError(error, voiceCopy.recordingFailed)
      })
      startedAtRef.current = Date.now()
      setElapsedSeconds(0)
      setVoiceStatus('recording')
      intervalRef.current = window.setInterval(() => setElapsedSeconds((Date.now() - startedAtRef.current) / 1000), 250)
      const cap = Math.max(1, Math.min(Math.trunc(maxRecordingSeconds), 600))
      timeoutRef.current = window.setTimeout(() => void stop(), cap * 1000)
    } catch (error) {
      void streaming?.finish().catch(() => '')
      streamingRef.current = null
      setVoiceStatus('idle')
      notifyError(error, voiceCopy.recordingFailed)
    }
  }

  const dictate = () => {
    if (recording) {
      void stop()
    } else if (voiceStatus === 'idle') {
      void start()
    }
  }

  const voiceActivityState: VoiceActivityState = {
    elapsedSeconds,
    level,
    status: voiceStatus
  }

  return { dictate, voiceActivityState, voiceStatus }
}
