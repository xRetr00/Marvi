import { useStore } from '@nanostores/react'
import { useEffect, useRef } from 'react'

import { useI18n } from '@/i18n'
import { createBargeInGate } from '@/lib/voice-barge-in'
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
    const gate = createBargeInGate({ graceMs: 700, level: 0.32, sustainedMs: 350 })
    let interrupted = false

    void handle.start({
      onError: () => undefined,
      onLevel: level => {
        if (interrupted || !gate.update(level, Date.now() - startedAt)) {
          return
        }

        interrupted = true
        vpLog('voice', 'read-aloud barge-in accepted', { elapsedMs: Date.now() - startedAt, level })
        stopVoicePlayback()
        handle.cancel()
        activeRef.current = false
      }
    }).catch(() => {
      activeRef.current = false
    })

    return () => {
      handle.cancel()
      activeRef.current = false
    }
  }, [blocked, enabled, handle, playback.source, playback.status])
}
