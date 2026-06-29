import { useEffect, useRef } from 'react'

import { glowSpeedMs, targetAmplitude } from './glow-model'
import { IslandCapsule } from './island-capsule'
import type { VoicePhase, VoiceState } from '@/store/voice-presence'

// Apple-Intelligence-style edge glow: a rotating multi-colour conic gradient,
// masked to a soft border ring and heavily blurred, so the screen's edge — not a
// rectangular frame or discrete blobs — comes alive. Two counter-rotating rings
// (a tight inner one + a wider, softer outer bloom) give it depth. Amplitude and
// rotation speed are driven live by the voice state; opacity eases to 0 when idle.
const GLOW_CSS = `
@property --ai-angle { syntax: '<angle>'; initial-value: 0deg; inherits: false; }
.ai-glow-root { position: fixed; inset: 0; overflow: hidden; pointer-events: none; --ai-amp: 0; --ai-spin: 8s; }
.ai-glow-ring {
  position: absolute; inset: 0;
  border-radius: 28px;
  padding: clamp(44px, 6vw, 96px);
  background: conic-gradient(from var(--ai-angle) at 50% 50%,
    #ff6ec4, #ff9d6e, #b06bff, #4f9cff, #36d6c3, #8a7bff, #ff6ec4);
  filter: blur(42px) saturate(1.35);
  opacity: var(--ai-amp);
  -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
  -webkit-mask-composite: xor;
  mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
  mask-composite: exclude;
  animation: ai-spin var(--ai-spin) linear infinite;
  will-change: opacity;
}
.ai-glow-ring.ai-glow-outer {
  padding: clamp(72px, 9vw, 150px);
  filter: blur(78px) saturate(1.15);
  opacity: calc(var(--ai-amp) * 0.55);
  animation-direction: reverse;
  animation-duration: calc(var(--ai-spin) * 1.7);
}
@keyframes ai-spin { to { --ai-angle: 360deg; } }
@media (prefers-reduced-motion: reduce) { .ai-glow-ring { animation: none; } }
`

export function GlowOverlayApp() {
  const rootRef = useRef<HTMLDivElement | null>(null)
  const stateRef = useRef<VoiceState>({ phase: 'off', level: 0, muted: false })

  useEffect(() => {
    const unsub = window.hermesDesktop?.glowOverlay?.onState(payload => {
      stateRef.current = payload
    })
    return () => unsub?.()
  }, [])

  // Ease the ring's intensity toward the target amplitude and feed the rotation
  // speed per phase. Pure CSS-variable updates — the conic rotation and blur are
  // GPU-composited, so this stays cheap.
  useEffect(() => {
    let raf = 0
    let amp = 0

    const tick = () => {
      const { phase, level } = stateRef.current
      const target = targetAmplitude(phase as VoicePhase, level)
      amp += (target - amp) * 0.1

      const el = rootRef.current
      if (el) {
        el.style.setProperty('--ai-amp', amp.toFixed(3))
        el.style.setProperty('--ai-spin', `${(glowSpeedMs(phase as VoicePhase) / 1000).toFixed(2)}s`)
      }

      raf = requestAnimationFrame(tick)
    }

    raf = requestAnimationFrame(tick)

    return () => cancelAnimationFrame(raf)
  }, [])

  return (
    <div className="ai-glow-root" ref={rootRef}>
      <style>{GLOW_CSS}</style>
      <div className="ai-glow-ring ai-glow-outer" />
      <div className="ai-glow-ring" />
      <IslandCapsule />
    </div>
  )
}
