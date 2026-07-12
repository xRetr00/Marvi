import { Leva, useControls } from 'leva'
import { type CSSProperties, useEffect, useState } from 'react'

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

export function Backdrop() {
  const [controlsOpen, setControlsOpen] = useState(false)

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
            loop
            muted
            playsInline
            poster="https://assets.21st.dev/ascii-recipes/thumbnails/user_2nElBLvklOKlAURm6W1PTu6yYFh/ae758991-0c3f-4c6a-9296-33784c65d43b.webp"
            src="https://assets.21st.dev/ascii-recipes/videos/user_2nElBLvklOKlAURm6W1PTu6yYFh/c458eb38-7f4e-4272-8711-59a86e20d624.mp4"
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
