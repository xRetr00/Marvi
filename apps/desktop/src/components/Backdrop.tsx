import { useStore } from '@nanostores/react'
import { Leva, useControls } from 'leva'
import { useEffect, useState } from 'react'

import { $backgroundMode, $backgroundOpacity, $backgroundQuality, backgroundFor } from '@/store/background'

import { AsciiCanvasBackdrop } from './ascii-flower-backdrop'

const SMART_SWITCH_INTERVAL = 5 * 60 * 1000

export function Backdrop() {
  const [controlsOpen, setControlsOpen] = useState(false)
  const backgroundMode = useStore($backgroundMode)
  const backgroundOpacity = useStore($backgroundOpacity)
  const backgroundQuality = useStore($backgroundQuality)
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

  const background = backgroundFor(backgroundMode, backgroundIndex)

  return (
    <>
      <Leva collapsed hidden={!import.meta.env.DEV || !controlsOpen} titleBar={{ title: 'backdrop', drag: true }} />

      <div aria-hidden className="pointer-events-none absolute inset-0 -z-1" style={{ opacity: backgroundOpacity / 100 }}>
        {background.kind === 'canvas' ? (
          <AsciiCanvasBackdrop {...background} quality={backgroundQuality} />
        ) : (
          <video
            autoPlay
            className="block size-full object-cover"
            key={background.src}
            loop
            muted
            playsInline
            poster={background.poster}
            src={background.src}
          />
        )}
      </div>
    </>
  )
}
