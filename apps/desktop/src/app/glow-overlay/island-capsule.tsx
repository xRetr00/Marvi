import { useEffect, useState } from 'react'

import type { IslandCard } from '@/lib/island-queue'

export function IslandCapsule() {
  const [card, setCard] = useState<IslandCard | null>(null)

  useEffect(() => {
    const off = window.hermesDesktop?.glowOverlay?.onCard(next => setCard(next))
    return () => off?.()
  }, [])

  useEffect(() => {
    // The capsule is the only clickable part of the otherwise click-through
    // glow window. Capture clicks while a card with actions is shown.
    const interactive = Boolean(card?.actions?.length)
    window.hermesDesktop?.glowOverlay?.setIgnoreMouse(!interactive)
    return () => {
      // Never leave the fullscreen window mouse-capturing if this unmounts.
      window.hermesDesktop?.glowOverlay?.setIgnoreMouse(true)
    }
  }, [card])

  if (!card) {
    return null
  }

  const dismiss = () => {
    window.hermesDesktop?.glowOverlay?.cardAction({ type: 'dismiss', id: card.id })
    setCard(null)
  }

  return (
    <div
      style={{
        position: 'absolute',
        bottom: 48,
        left: '50%',
        transform: 'translateX(-50%)',
        // The glow root is click-through; the capsule opts back in so its
        // buttons are clickable when the window is made interactive.
        pointerEvents: 'auto',
        width: 320,
        background: 'rgba(18,18,22,0.72)',
        backdropFilter: 'blur(16px)',
        WebkitBackdropFilter: 'blur(16px)',
        border: '0.5px solid rgba(255,255,255,0.14)',
        borderRadius: 18,
        padding: '16px 18px',
        color: '#f2f2f7',
        fontFamily: 'system-ui, sans-serif'
      }}
    >
      {card.title && (
        <div style={{ fontSize: 11, letterSpacing: '0.12em', textTransform: 'uppercase', color: '#aab', marginBottom: 8 }}>
          {card.title}
        </div>
      )}
      {card.body && <div style={{ fontSize: 14, lineHeight: 1.5, marginBottom: card.actions?.length ? 14 : 0 }}>{card.body}</div>}
      {card.actions?.length ? (
        <div style={{ display: 'flex', gap: 8 }}>
          {card.actions.map(a => (
            <button
              key={a.id}
              onClick={() => {
                if (a.value) {
                  window.hermesDesktop?.glowOverlay?.cardAction({ type: 'submit', text: a.value })
                }
                dismiss()
              }}
              style={{
                flex: 1,
                background: a.id === 'primary' ? '#2b5bd0' : 'transparent',
                border: '0.5px solid rgba(255,255,255,0.2)',
                color: '#fff',
                borderRadius: 10,
                padding: '8px 0',
                fontSize: 13,
                cursor: 'pointer'
              }}
            >
              {a.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  )
}
