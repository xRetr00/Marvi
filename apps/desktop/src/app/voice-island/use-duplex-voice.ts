import { useEffect, useRef, useState } from 'react'

import { showIslandCard } from '@/store/island-cards'

import { connectDuplexVoice, type DuplexController } from './duplex-client'
import { type DuplexSessionState, INITIAL_DUPLEX_STATE } from './duplex-session'

const RECOVERY_DELAYS_MS = [400, 800, 1600] as const

export interface UseDuplexVoiceResult {
  /** True once we've confirmed the duplex endpoint is reachable and live. */
  available: boolean
  status: 'active' | 'connecting' | 'idle' | 'unavailable'
  state: DuplexSessionState
  level: number
}

/**
 * Connects to the duplex voice endpoint while `enabled`. If it connects, this
 * hook owns the mic + `/api/voice/duplex` socket for as long as the island
 * overlay stays mounted and `state`/`level` become the authoritative voice
 * presentation. If it cannot connect, `available` stays false and the caller
 * keeps rendering off the legacy `$voiceState` IPC push; this hook never reads
 * or writes that store. A session that was already live gets a short bounded
 * reconnect ladder so an OS/background mic or socket interruption does not
 * silently end hands-free mode; initial connection failures still fall back.
 */
export function useDuplexVoice(enabled: boolean, onConversationEnd?: () => void): UseDuplexVoiceResult {
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

    let cancelled = false
    let hadLiveSession = false
    let recoveryAttempt = 0
    let retryTimer: number | null = null

    const connect = () => {
      setStatus('connecting')

      void connectDuplexVoice({
        onCard: card => showIslandCard(card, { allowWhenFocused: true }),
        onConversationEnd,
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
          if (active) {
            hadLiveSession = true
            recoveryAttempt = 0
          }
          setAvailable(active)
          setStatus(active ? 'active' : 'connecting')
          setState(next)
        },
        onUnavailable: reason => {
          if (cancelled) {
            return
          }

          setAvailable(false)
          setState(INITIAL_DUPLEX_STATE)
          setLevel(0)

          const delay = hadLiveSession ? RECOVERY_DELAYS_MS[recoveryAttempt] : undefined
          if (typeof delay === 'number') {
            recoveryAttempt += 1
            console.debug(`[voice-island] duplex interrupted; reconnecting in ${delay}ms:`, reason)
            setStatus('connecting')
            retryTimer = window.setTimeout(connect, delay)
          } else {
            console.debug('[voice-island] duplex voice unavailable, using legacy voice flow:', reason)
            setStatus('unavailable')
          }
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
    }

    connect()

    return () => {
      cancelled = true
      if (retryTimer !== null) {
        window.clearTimeout(retryTimer)
      }
      controllerRef.current?.stop()
      controllerRef.current = null
    }
  }, [enabled, onConversationEnd])

  return { available, level, state, status }
}
