import { useEffect, useRef } from 'react'

const BAR_COUNT = 28
const MAX_DPR = 2
const SMOOTHING = 0.18
const IDLE_EASE = 0.08

interface IslandWaveformProps {
  level: number
  active: boolean
  width: number
  height: number
}

/**
 * Canvas-drawn mirrored-bar waveform. Amplitude eases toward `level` each
 * frame so bar heights don't snap; each bar additionally gets a touch of
 * per-bar jitter (deterministic, seeded by index) so the whole thing reads as
 * a live waveform rather than a single pulsing block. When `active` is false
 * the rAF loop stops and a flat idle line is drawn once — no heavy loop while
 * idle.
 */
export function IslandWaveform({ level, active, width, height }: IslandWaveformProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const levelRef = useRef(level)
  const ampRef = useRef(0)
  const rafRef = useRef<number | null>(null)
  const phaseRef = useRef(0)

  useEffect(() => {
    levelRef.current = level
  }, [level])

  useEffect(() => {
    const canvas = canvasRef.current

    if (!canvas) {
      return
    }

    const ctx = canvas.getContext('2d')

    if (!ctx) {
      return
    }

    const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR)
    canvas.width = Math.max(1, Math.round(width * dpr))
    canvas.height = Math.max(1, Math.round(height * dpr))
    canvas.style.width = `${width}px`
    canvas.style.height = `${height}px`

    const drawFlat = () => {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, width, height)
      ctx.fillStyle = 'rgba(255,255,255,0.28)'
      const midY = height / 2
      const barWidth = Math.max(1.5, width / (BAR_COUNT * 2))
      const gap = (width - barWidth * BAR_COUNT) / (BAR_COUNT - 1)

      for (let i = 0; i < BAR_COUNT; i++) {
        const x = i * (barWidth + gap)
        ctx.fillRect(x, midY - 1, barWidth, 2)
      }
    }

    const drawFrame = () => {
      const target = Math.max(0, Math.min(1, levelRef.current))
      ampRef.current += (target - ampRef.current) * SMOOTHING
      phaseRef.current += 0.12

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, width, height)

      const midY = height / 2
      const barWidth = Math.max(1.5, width / (BAR_COUNT * 2))
      const gap = (width - barWidth * BAR_COUNT) / (BAR_COUNT - 1)
      const amp = ampRef.current

      for (let i = 0; i < BAR_COUNT; i++) {
        const x = i * (barWidth + gap)
        // Deterministic per-bar jitter + a slow travelling wave so bars don't
        // move in lockstep — reads as a natural waveform, not a single blob.
        const seed = Math.sin(i * 12.9898) * 43758.5453
        const jitter = seed - Math.floor(seed)
        const wave = Math.sin(phaseRef.current + i * 0.55) * 0.5 + 0.5
        const bar = amp * (0.35 + 0.65 * wave) * (0.6 + 0.4 * jitter)
        const barHeight = Math.max(2, Math.min(height - 2, bar * height))

        const alpha = 0.55 + amp * 0.45
        ctx.fillStyle = `rgba(255,255,255,${alpha.toFixed(3)})`
        ctx.fillRect(x, midY - barHeight / 2, barWidth, barHeight)
      }

      rafRef.current = requestAnimationFrame(drawFrame)
    }

    if (!active) {
      // Ease amplitude down to 0 once, then draw the flat idle line — no
      // ongoing rAF loop while idle.
      ampRef.current *= 1 - IDLE_EASE
      drawFlat()

      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current)
        rafRef.current = null
      }

      return
    }

    rafRef.current = requestAnimationFrame(drawFrame)

    return () => {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current)
        rafRef.current = null
      }
    }
  }, [active, width, height])

  return <canvas ref={canvasRef} />
}
