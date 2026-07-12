import { useEffect, useRef, useState } from 'react'

import { connectDuplexVoice, type DuplexController } from './duplex-client'
import { type DuplexSessionState, INITIAL_DUPLEX_STATE } from './duplex-session'

export interface UseDuplexVoiceResult {
  /** True once we've confirmed the duplex endpoint is reachable and live. */
  available: boolean
  status: 'active' | 'connecting' | 'idle' | 'unavailable'
  state: DuplexSessionState
  level: number
}

/**
 * Tries the duplex voice endpoint once while `enabled`. If it connects, this
 * hook owns the mic + `/api/voice/duplex` socket for as long as the island
 * overlay stays mounted and `state`/`level` become the authoritative voice
 * presentation. If it cannot connect, `available` stays false and the caller
 * keeps rendering off the legacy `$voiceState` IPC push; this hook never reads
 * or writes that store.
 */
export function useDuplexVoice(enabled: boolean): UseDuplexVoiceResult {
  const [state, setState] = useState<DuplexSessionState>(INITIAL_DUPLEX_STATE)
  const [available, setAvailable] = useState(false)
  const [status, setStatus] = useState<UseDuplexVoiceResult['status']>('idle')
  const [level, setLevel] = useState(0)
  const controllerRef = useRef<DuplexController | null>(null)

  useEffect(() => {
    if (!enabled) {
      setAvailable(false)
      setStatus('idle')
      setState(INITIAL_DUPLEX_STATE)
      setLevel(0)

      return
    }

    setStatus('connecting')

    let cancelled = false

    void connectDuplexVoice({
      onLevel: next => {
        if (!cancelled) {
          setLevel(next)
        }
      },
      onState: next => {
        if (cancelled) {
          return
        }

        const active = next.phase !== 'connecting' && next.phase !== 'closed'
        setAvailable(active)
        setStatus(active ? 'active' : 'connecting')
        setState(next)
      },
      onUnavailable: reason => {
        if (cancelled) {
          return
        }

         
        console.debug('[voice-island] duplex voice unavailable, using legacy voice flow:', reason)
        setAvailable(false)
        setStatus('unavailable')
        setState(INITIAL_DUPLEX_STATE)
        setLevel(0)
      }
    })
      .then(controller => {
        if (cancelled) {
          controller?.stop()
        } else {
          controllerRef.current = controller
        }
      })
      .catch(() => undefined)

    return () => {
      cancelled = true
      controllerRef.current?.stop()
      controllerRef.current = null
    }
  }, [enabled])

  return { available, level, state, status }
}
