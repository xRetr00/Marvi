import { useEffect, useState } from 'react'

import type { IslandCard } from '@/lib/island-queue'
import type { VoiceState } from '@/store/voice-presence'

import { DynamicIsland } from './dynamic-island'

type CardAction = { type: 'dismiss'; id?: string } | { type: 'submit'; text: string }

const INITIAL_STATE: VoiceState = { phase: 'off', level: 0, muted: false }

// Apple-style Dynamic Island: a near-black pill anchored top-center in the
// small transparent overlay stage, morphing between a compact idle state and
// an expanded state (waveform or card). Replaces the old fullscreen
// conic-gradient edge glow.
export function GlowOverlayApp() {
  const [state, setState] = useState<VoiceState>(INITIAL_STATE)
  const [card, setCard] = useState<IslandCard | null>(null)

  useEffect(() => {
    const unsub = window.hermesDesktop?.glowOverlay?.onState(payload => setState(payload))
    return () => unsub?.()
  }, [])

  useEffect(() => {
    const unsub = window.hermesDesktop?.glowOverlay?.onCard(next => setCard(next))
    return () => unsub?.()
  }, [])

  useEffect(() => {
    // The stage window is click-through by default; only opt back in when a
    // card with actions is on screen so its buttons are clickable.
    const interactive = Boolean(card?.actions?.length)
    window.hermesDesktop?.glowOverlay?.setIgnoreMouse(!interactive)
    return () => {
      // Never leave the stage window mouse-capturing if this unmounts.
      window.hermesDesktop?.glowOverlay?.setIgnoreMouse(true)
    }
  }, [card])

  const handleCardAction = (payload: CardAction) => {
    window.hermesDesktop?.glowOverlay?.cardAction(payload)
    if (payload.type === 'dismiss') {
      setCard(null)
    }
  }

  const interactive = Boolean(card?.actions?.length)

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
        <DynamicIsland state={state} card={card} onCardAction={handleCardAction} />
      </div>
    </div>
  )
}
