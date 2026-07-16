import './styles.css'
// Side-effect: applies the persisted window translucency on load.
import './store/translucency'

import { QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { HashRouter } from 'react-router-dom'

import App from './app'
import { startPerfMonitor } from './app/perf-monitor'
import { ErrorBoundary } from './components/error-boundary'
import { HapticsProvider } from './components/haptics-provider'
import { I18nProvider } from './i18n'
import { installClipboardShim } from './lib/clipboard'
import { queryClient } from './lib/query-client'
import { ThemeProvider } from './themes/context'

installClipboardShim()

if (import.meta.env.MODE !== 'production') {
  import('./app/chat/perf-probe')
}

// The pet overlay rides this same bundle (`?win=overlay`) but mounts a tiny,
// transparent, gateway-less surface instead of the full app. Branch before any
// app-shell work so the overlay window stays cheap.
const win = new URLSearchParams(window.location.search).get('win')

if (win === 'overlay') {
  void import('./app/pet-overlay/overlay-root').then(({ mountPetOverlay }) => mountPetOverlay())
} else if (win === 'island') {
  void import('./app/voice-island/island-root').then(({ mountVoiceIsland }) => mountVoiceIsland())
} else {
  // Primary window only — longtask/freeze instrumentation, see app/perf-monitor.ts.
  startPerfMonitor()

  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <ErrorBoundary label="root">
        <QueryClientProvider client={queryClient}>
          <I18nProvider>
            <ThemeProvider>
              <HapticsProvider>
                <HashRouter>
                  <App />
                </HashRouter>
              </HapticsProvider>
            </ThemeProvider>
          </I18nProvider>
        </QueryClientProvider>
      </ErrorBoundary>
    </StrictMode>
  )
}
