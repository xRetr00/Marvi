import { useEffect, useRef } from 'react'

const SOURCE = 'https://21st.dev/ascii-editor/demos/generated/ref-098.webp'
const CELL_SIZE = 10
const FRAME_MS = 120

function drawCover(context: CanvasRenderingContext2D, image: HTMLImageElement, width: number, height: number) {
  const scale = Math.max(width / image.naturalWidth, height / image.naturalHeight)
  const drawWidth = image.naturalWidth * scale
  const drawHeight = image.naturalHeight * scale

  context.drawImage(image, (width - drawWidth) / 2, (height - drawHeight) / 2, drawWidth, drawHeight)
}

export function AsciiFlowerBackdrop() {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current

    if (!canvas) {
      return
    }

    const image = new Image()
    let frame = 0
    let lastDraw = 0
    let disposed = false

    image.crossOrigin = 'anonymous'

    const draw = (time: number) => {
      if (disposed) {
        return
      }

      frame = requestAnimationFrame(draw)

      if (!image.complete || time - lastDraw < FRAME_MS) {
        return
      }

      lastDraw = time
      const bounds = canvas.getBoundingClientRect()
      const scale = Math.min(window.devicePixelRatio, 1.25, 1280 / bounds.width)
      const width = Math.max(1, Math.round(bounds.width * scale))
      const height = Math.max(1, Math.round(bounds.height * scale))

      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width
        canvas.height = height
      }

      const context = canvas.getContext('2d', { alpha: false, willReadFrequently: true })

      if (!context) {
        return
      }

      context.fillStyle = '#001b0b'
      context.fillRect(0, 0, width, height)
      context.globalAlpha = 0.28
      drawCover(context, image, width, height)
      context.globalAlpha = 1

      let pixels: ImageData

      try {
        pixels = context.getImageData(0, 0, width, height)
      } catch {
        return
      }

      const flicker = 0.8 + Math.sin(time / 130) * 0.2

      for (let y = 0; y < height; y += CELL_SIZE) {
        for (let x = 0; x < width; x += CELL_SIZE) {
          const offset = (Math.min(y + CELL_SIZE / 2, height - 1) * width + Math.min(x + CELL_SIZE / 2, width - 1)) * 4
          const luminance = (pixels.data[offset] * 0.2126 + pixels.data[offset + 1] * 0.7152 + pixels.data[offset + 2] * 0.0722) / 255
          const coverage = ((x * 17 + y * 31) % 100) / 100

          if (coverage > 0.36 || luminance < 0.07) {
            continue
          }

          const size = Math.max(1, Math.round(luminance * CELL_SIZE))
          context.fillStyle = `rgba(0, 255, 102, ${luminance * 0.65 * flicker})`
          context.fillRect(x + (CELL_SIZE - size) / 2, y + (CELL_SIZE - size) / 2, size, size)
        }
      }

      for (let y = 0; y < height; y += 4) {
        context.fillStyle = 'rgba(0, 0, 0, 0.15)'
        context.fillRect(0, y, width, 1)
      }

      const vignette = context.createRadialGradient(width / 2, height / 2, height * 0.1, width / 2, height / 2, Math.max(width, height) * 0.7)
      vignette.addColorStop(0, 'rgba(0, 0, 0, 0)')
      vignette.addColorStop(1, 'rgba(0, 0, 0, 0.55)')
      context.fillStyle = vignette
      context.fillRect(0, 0, width, height)
    }

    image.onload = () => {
      frame = requestAnimationFrame(draw)
    }

    image.src = SOURCE

    return () => {
      disposed = true
      cancelAnimationFrame(frame)
    }
  }, [])

  return <canvas aria-hidden className="block size-full" ref={canvasRef} />
}
