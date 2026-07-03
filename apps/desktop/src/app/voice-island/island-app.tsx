import { useEffect, useState } from 'react'

import type { IslandCard } from '@/lib/island-queue'
import type { VoiceState } from '@/store/voice-presence'

import { DynamicIsland } from './dynamic-island'

type CardAction = { type: 'dismiss'; id?: string } | { type: 'submit'; text: string }

const INITIAL_STATE: VoiceState = { phase: 'off', level: 0, muted: false, caption: null, userCaption: null }

// Apple-style Dynamic Island: a near-black pill anchored top-center in the
// small transparent overlay stage, morphing between a compact idle state and
// an expanded state (waveform or card). Replaces the old fullscreen edge
// effect with a focused, native-feeling pill.
export function VoiceIslandApp() {
  const [state, setState] = useState<VoiceState>(INITIAL_STATE)
  const [card, setCard] = useState<IslandCard | null>(null)
  const [activity, setActivity] = useState<string | null>(null)

  useEffect(() => {
    const unsub = window.hermesDesktop?.islandOverlay?.onState(payload => setState(payload))
    return () => unsub?.()
  }, [])

  useEffect(() => {
    const unsub = window.hermesDesktop?.islandOverlay?.onCard(next => setCard(next))
    return () => unsub?.()
  }, [])

  useEffect(() => {
    const unsub = window.hermesDesktop?.islandOverlay?.onActivity(next => setActivity(next))
    return () => unsub?.()
  }, [])

  const [summoned, setSummoned] = useState(false)

  useEffect(() => {
    const off = window.hermesDesktop?.islandOverlay?.onSummon(() => setSummoned(true))
    return () => off?.()
  }, [])

  useEffect(() => {
    // The stage window is click-through by default; only opt back in when a
    // card with actions is on screen, or the command bar is open, so those
    // controls are clickable/typeable.
    const interactive = summoned || Boolean(card?.actions?.length)
    window.hermesDesktop?.islandOverlay?.setIgnoreMouse(!interactive)
    return () => {
      // Never leave the stage window mouse-capturing if this unmounts.
      window.hermesDesktop?.islandOverlay?.setIgnoreMouse(true)
    }
  }, [card, summoned])

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

  const interactive = summoned || Boolean(card?.actions?.length)

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
          state={state}
          card={card}
          activity={activity}
          onCardAction={handleCardAction}
          summoned={summoned}
          onSummonSubmit={submitSummon}
          onSummonCancel={closeSummon}
        />
      </div>
    </div>
  )
}
