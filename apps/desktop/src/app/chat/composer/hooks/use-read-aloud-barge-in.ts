import { useStore } from '@nanostores/react'
import { useEffect, useRef } from 'react'

import { useI18n } from '@/i18n'
import { BARGE_IN_DEFAULTS, createBargeInGate } from '@/lib/voice-barge-in'
import { stopVoicePlayback } from '@/lib/voice-playback'
import { vpLog } from '@/lib/voice-presence-log'
import { $voicePlayback } from '@/store/voice-playback'

import { useMicRecorder } from './use-mic-recorder'

interface ReadAloudBargeInOptions {
  blocked: boolean
  enabled: boolean
}

export function useReadAloudBargeIn({ blocked, enabled }: ReadAloudBargeInOptions): void {
  const { t } = useI18n()
  const { handle } = useMicRecorder(t.notifications.voice)
  const playback = useStore($voicePlayback)
  const activeRef = useRef(false)

  useEffect(() => {
    const active = enabled && !blocked && playback.source === 'read-aloud' && playback.status === 'speaking'

    if (!active) {
      if (activeRef.current) {
        handle.cancel()
        activeRef.current = false
      }

      return
    }

    if (activeRef.current) {
      return
    }

    activeRef.current = true
    const startedAt = Date.now()
    const gate = createBargeInGate(BARGE_IN_DEFAULTS)
    let interrupted = false
    let peakLevel = 0
    let lastPeakLogAt = 0

    // Guarantee a clean recorder — useMicRecorder.start() silently early-returns
    // if one is already active, which would arm nothing (see use-voice-conversation).
    handle.cancel()
    vpLog('voice', 'read-aloud barge-in armed', { defaults: BARGE_IN_DEFAULTS })
    void handle
      .start({
        onError: err => vpLog('voice', 'read-aloud barge-in mic error', { error: String(err) }),
        onLevel: level => {
          if (interrupted) {
            return
          }

          peakLevel = Math.max(peakLevel, level)
          if (Date.now() - lastPeakLogAt > 1000) {
            vpLog('voice', 'read-aloud barge-in level', { peak: Number(peakLevel.toFixed(3)), threshold: BARGE_IN_DEFAULTS.level })
            lastPeakLogAt = Date.now()
            peakLevel = 0
          }

          if (!gate.update(level, Date.now() - startedAt)) {
            return
          }

          interrupted = true
          vpLog('voice', 'read-aloud barge-in accepted', { elapsedMs: Date.now() - startedAt, level })
          stopVoicePlayback()
          handle.cancel()
          activeRef.current = false
        }
      })
      .catch(err => {
        vpLog('voice', 'read-aloud barge-in mic start failed', { error: String(err) })
        activeRef.current = false
      })

    return () => {
      handle.cancel()
      activeRef.current = false
    }
  }, [blocked, enabled, handle, playback.source, playback.status])
}
