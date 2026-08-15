import { useEffect, useRef, useState } from 'react'

import type { IslandCard } from '@/lib/island-queue'
import type { IslandWorkState } from '@/lib/island-work'
import type { VoiceState } from '@/store/voice-presence'

import { DynamicIsland } from './dynamic-island'
import { shouldHoldWakeHandoff, WAKE_HANDOFF_MS } from './island-motion'

type CardAction = { type: 'dismiss'; id?: string } | { type: 'submit'; text: string }

const INITIAL_STATE: VoiceState = {
  phase: 'off',
  level: 0,
  muted: false,
  caption: null,
  userCaption: null,
  bargeable: false,
  label: null,
  speakerBadge: null,
  speakerName: null,
  captionIgnored: false,
  deepWorking: false,
  deepMode: null,
  activity: null
}

// Apple-style Dynamic Island: a near-black pill anchored top-center in the
// small transparent overlay stage, morphing between a compact idle state and
// an expanded state (waveform or card). Replaces the old fullscreen edge
// effect with a focused, native-feeling pill.
export function VoiceIslandApp() {
  const [state, setState] = useState<VoiceState>(INITIAL_STATE)
  const stateRef = useRef(state)
  const wakeHandoffRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [card, setCard] = useState<IslandCard | null>(null)
  const [activity, setActivity] = useState<string | null>(null)
  const [work, setWork] = useState<IslandWorkState | null>(null)
  const visibleWork = state.phase === 'off' ? work : null

  useEffect(() => {
    const unsub = window.hermesDesktop?.islandOverlay?.onState(payload => {
      if (shouldHoldWakeHandoff(stateRef.current.phase, payload.phase)) {
        wakeHandoffRef.current ??= setTimeout(() => {
          wakeHandoffRef.current = null
          stateRef.current = payload
          setState(payload)
        }, WAKE_HANDOFF_MS)

        return
      }

      if (wakeHandoffRef.current) {
        clearTimeout(wakeHandoffRef.current)
        wakeHandoffRef.current = null
      }

      stateRef.current = payload
      setState(payload)
    })

    return () => {
      unsub?.()

      if (wakeHandoffRef.current) {
        clearTimeout(wakeHandoffRef.current)
      }
    }
  }, [])

  useEffect(() => {
    const unsub = window.hermesDesktop?.islandOverlay?.onCard(next => setCard(next))

    return () => unsub?.()
  }, [])

  useEffect(() => {
    const unsub = window.hermesDesktop?.islandOverlay?.onActivity(next => setActivity(next))

    return () => unsub?.()
  }, [])

  useEffect(() => {
    const unsub = window.hermesDesktop?.islandOverlay?.onWork(next => setWork(next))

    return () => unsub?.()
  }, [])

  const [summoned, setSummoned] = useState(false)

  useEffect(() => {
    const off = window.hermesDesktop?.islandOverlay?.onSummon(() => setSummoned(true))

    return () => off?.()
  }, [])

  useEffect(() => {
    // The stage window is click-through by default; opt back in only while a
    // collapsible card/work view or the command bar is on screen.
    const interactive = summoned || Boolean(card) || Boolean(visibleWork)
    window.hermesDesktop?.islandOverlay?.setIgnoreMouse(!interactive)

    return () => {
      // Never leave the stage window mouse-capturing if this unmounts.
      window.hermesDesktop?.islandOverlay?.setIgnoreMouse(true)
    }
  }, [card, summoned, visibleWork])

  useEffect(() => {
    // Drop focusability once the command bar closes so the overlay stops
    // stealing focus from whatever app the user summoned it over.
    if (!summoned) {
      window.hermesDesktop?.islandOverlay?.setFocusable(false)
    }
  }, [summoned])

  const handleCardAction = (payload: CardAction) => {
    window.hermesDesktop?.islandOverlay?.cardAction(payload)

    if (payload.type === 'dismiss') {
      setCard(null)
    }
  }

  const closeSummon = () => setSummoned(false)

  const submitSummon = (text: string) => {
    const trimmed = text.trim()

    if (trimmed) {
      window.hermesDesktop?.islandOverlay?.cardAction({ type: 'submit', text: trimmed })
    }

    setSummoned(false)
  }

  const interactive = summoned || Boolean(card) || Boolean(visibleWork)

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'flex-start',
        pointerEvents: 'none'
      }}
    >
      <div style={{ pointerEvents: interactive ? 'auto' : 'none' }}>
        <DynamicIsland
          activity={activity}
          card={card}
          onCardAction={handleCardAction}
          onSummonCancel={closeSummon}
          onSummonSubmit={submitSummon}
          state={state}
          summoned={summoned}
          work={visibleWork}
        />
      </div>
    </div>
  )
}
