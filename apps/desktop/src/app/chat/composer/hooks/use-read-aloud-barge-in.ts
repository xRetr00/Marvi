import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import { useI18n } from '@/i18n'
import { startBargeInDetector } from '@/lib/barge-in-detector'
import { stopVoicePlayback } from '@/lib/voice-playback'
import { $voicePlayback } from '@/store/voice-playback'

import { useMicRecorder } from './use-mic-recorder'

interface ReadAloudBargeInOptions {
  blocked: boolean
  enabled: boolean
  streamingSttEnabled?: boolean
}

// Talk-over for the read-aloud / auto-speak path (wake-word replies, message
// read-aloud). Uses the shared two-stage detector (energy pre-gate + Parakeet
// non-echo words) so it works when AEC crushes the mic — same as the hands-free
// conversation path.
export function useReadAloudBargeIn({ blocked, enabled, streamingSttEnabled }: ReadAloudBargeInOptions): void {
  const { t } = useI18n()
  const { handle } = useMicRecorder(t.notifications.voice)
  const playback = useStore($voicePlayback)

  useEffect(() => {
    const active = enabled && !blocked && playback.source === 'read-aloud' && playback.status === 'speaking'

    if (!active) {
      return
    }

    const stop = startBargeInDetector({
      handle,
      streamingSttEnabled: Boolean(streamingSttEnabled),
      onInterrupt: () => stopVoicePlayback()
    })

    return () => stop()
  }, [blocked, enabled, handle, playback.source, playback.status, streamingSttEnabled])
}
