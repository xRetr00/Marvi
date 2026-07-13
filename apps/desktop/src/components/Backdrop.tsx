import { Leva, useControls } from 'leva'
import { useStore } from '@nanostores/react'
import { type CSSProperties, useEffect, useState } from 'react'

import { $backgroundMode, backgroundFor } from '@/store/background'

const BLEND_MODES = [
  'normal',
  'multiply',
  'screen',
  'overlay',
  'darken',
  'lighten',
  'color-dodge',
  'color-burn',
  'hard-light',
  'soft-light',
  'difference',
  'exclusion',
  'hue',
  'saturation',
  'color',
  'luminosity'
] as const

type BlendMode = (typeof BLEND_MODES)[number]
const SMART_SWITCH_INTERVAL = 5 * 60 * 1000

export function Backdrop() {
  const [controlsOpen, setControlsOpen] = useState(false)
  const backgroundMode = useStore($backgroundMode)
  const [backgroundIndex, setBackgroundIndex] = useState(0)

  useEffect(() => {
    if (backgroundMode !== 'auto') {
      return
    }

    const timer = window.setInterval(() => setBackgroundIndex(index => index + 1), SMART_SWITCH_INTERVAL)

    return () => window.clearInterval(timer)
  }, [backgroundMode])

  useEffect(() => {
    if (!import.meta.env.DEV) {
      return
    }

    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null

      const editing =
        target?.isContentEditable ||
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target instanceof HTMLSelectElement

      if (editing || event.repeat || event.altKey || event.ctrlKey || event.metaKey) {
        return
      }

      if (event.shiftKey && event.code === 'KeyY') {
        setControlsOpen(open => !open)
      }
    }

    window.addEventListener('keydown', onKeyDown)

    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  const shape = useControls(
    'UI / Shape',
    { radiusScalar: { value: 0.2, min: 0, max: 2, step: 0.1, label: 'radius scalar' } },
    { collapsed: true }
  )

  useEffect(() => {
    document.documentElement.style.setProperty('--radius-scalar', String(shape.radiusScalar))
  }, [shape.radiusScalar])

  const artwork = useControls(
    'Backdrop / Electric Gaze',
    {
      enabled: { value: true, label: 'on' },
      opacity: { value: 0.15, min: 0, max: 1, step: 0.005 },
      blendMode: { value: 'difference' as BlendMode, options: BLEND_MODES, label: 'blend' },
      objectPosition: {
        value: 'center',
        options: ['top left', 'top right', 'bottom left', 'bottom right', 'center', 'top', 'bottom', 'left', 'right'],
        label: 'position'
      },
      scale: { value: 100, min: 100, max: 300, step: 5, label: 'height (dvh)' }
    },
    { collapsed: true }
  )
  const background = backgroundFor(backgroundMode, backgroundIndex)

  return (
    <>
      <Leva collapsed hidden={!import.meta.env.DEV || !controlsOpen} titleBar={{ title: 'backdrop', drag: true }} />

      {artwork.enabled && (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 z-2"
          style={{
            mixBlendMode: artwork.blendMode as CSSProperties['mixBlendMode'],
            opacity: artwork.opacity
          }}
        >
          <video
            autoPlay
            className="w-auto min-w-dvw object-cover"
            key={background.src}
            loop
            muted
            playsInline
            poster={background.poster}
            src={background.src}
            style={{
              height: `${artwork.scale}dvh`,
              objectPosition: artwork.objectPosition
            }}
          />
        </div>
      )}
    </>
  )
}
