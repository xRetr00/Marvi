import { useEffect, useRef } from 'react'

import { glowSpeedMs, targetAmplitude } from './glow-model'
import { IslandCapsule } from './island-capsule'
import type { VoicePhase, VoiceState } from '@/store/voice-presence'

const BLOBS = [
  { hue: '#ff4f9d', ox: 0.18, oy: 1.02, rx: 0.42, ry: 0.5, sx: 1, sy: 0.6, sp: 0.7 },
  { hue: '#7a4bff', ox: 0.82, oy: 1.0, rx: 0.4, ry: 0.5, sx: -0.9, sy: 0.7, sp: 0.55 },
  { hue: '#3f8cff', ox: 0.5, oy: 1.05, rx: 0.5, ry: 0.55, sx: 0.6, sy: 0.5, sp: 0.9 },
  { hue: '#9a5bff', ox: -0.02, oy: 0.2, rx: 0.5, ry: 0.6, sx: 0.5, sy: 0.6, sp: 0.45 },
  { hue: '#34d6d6', ox: 1.02, oy: 0.2, rx: 0.5, ry: 0.6, sx: -0.5, sy: 0.6, sp: 0.5 },
  { hue: '#ff8a4f', ox: 0.8, oy: -0.04, rx: 0.45, ry: 0.5, sx: -0.6, sy: 0.5, sp: 0.6 }
]

export function GlowOverlayApp() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const stateRef = useRef<VoiceState>({ phase: 'off', level: 0, muted: false })

  useEffect(() => {
    const unsub = window.hermesDesktop?.glowOverlay?.onState(payload => {
      stateRef.current = payload
    })
    return unsub
  }, [])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) {
      return
    }

    const ctx = canvas.getContext('2d')
    if (!ctx) {
      return
    }

    let raf = 0
    let amp = 0
    let t = 0
    let prev = 0

    const resize = () => {
      // Clamp DPR: a fullscreen blur(80px) glow gains nothing from >2x backing
      // store, and unclamped 4K@200% would be wasteful. Matches the repo's other canvases.
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      canvas.width = Math.round(window.innerWidth * dpr)
      canvas.height = Math.round(window.innerHeight * dpr)
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    resize()
    window.addEventListener('resize', resize)

    const draw = (ts: number) => {
      const dt = prev === 0 ? 0.016 : Math.min(0.05, (ts - prev) / 1000)
      prev = ts
      const { phase, level } = stateRef.current
      // Flow faster while listening/speaking, slower while idle/thinking.
      const speedFactor = 3000 / glowSpeedMs(phase as VoicePhase)
      t += dt * speedFactor
      const target = targetAmplitude(phase as VoicePhase, level)
      amp += (target - amp) * 0.12

      const W = window.innerWidth
      const H = window.innerHeight
      ctx.clearRect(0, 0, W, H)

      if (amp > 0.01) {
        ctx.globalCompositeOperation = 'lighter'
        ctx.filter = 'blur(80px)'
        for (const b of BLOBS) {
          const dx = Math.sin(t * b.sp) * 60 * b.sx
          const dy = Math.cos(t * b.sp * 0.8) * 50 * b.sy
          const cx = b.ox * W + dx
          const cy = b.oy * H + dy
          const r = Math.max(W, H) * 0.32 * (0.7 + amp * 0.6)
          const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r)
          g.addColorStop(0, hexAlpha(b.hue, 0.55 * amp))
          g.addColorStop(1, hexAlpha(b.hue, 0))
          ctx.fillStyle = g
          ctx.beginPath()
          ctx.ellipse(cx, cy, r * b.rx, r * b.ry, 0, 0, Math.PI * 2)
          ctx.fill()
        }
        ctx.filter = 'none'
        ctx.globalCompositeOperation = 'source-over'
      }

      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)

    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', resize)
    }
  }, [])

  return (
    <>
      <canvas ref={canvasRef} style={{ display: 'block', width: '100vw', height: '100vh' }} />
      <IslandCapsule />
    </>
  )
}

function hexAlpha(hex: string, a: number): string {
  const n = parseInt(hex.slice(1), 16)
  const r = (n >> 16) & 255
  const g = (n >> 8) & 255
  const b = n & 255
  return `rgba(${r},${g},${b},${a})`
}
