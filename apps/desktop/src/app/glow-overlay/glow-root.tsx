import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { ErrorBoundary } from '@/components/error-boundary'

import { GlowOverlayApp } from './glow-overlay-app'

export function mountGlowOverlay(): void {
  const style = document.createElement('style')
  style.textContent = 'html,body,#root{background:transparent !important;overflow:hidden;}'
  document.head.appendChild(style)

  const root = document.getElementById('root')

  if (!root) {
    return
  }

  createRoot(root).render(
    <StrictMode>
      <ErrorBoundary label="glow-overlay">
        <GlowOverlayApp />
      </ErrorBoundary>
    </StrictMode>
  )
}
