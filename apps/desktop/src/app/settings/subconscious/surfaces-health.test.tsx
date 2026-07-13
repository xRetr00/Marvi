import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { SubconsciousSurfaceStatus } from './activity-service'
import { SurfacesHealth } from './surfaces-health'

afterEach(() => {
  cleanup()
})

function makeSurface(overrides: Partial<SubconsciousSurfaceStatus> = {}): SubconsciousSurfaceStatus {
  return {
    surface: 'gmail',
    status: 'ok',
    cursor_age_seconds: 120,
    quiet_streak: 2,
    effective_interval_seconds: 180,
    consecutive_failures: 0,
    last_error: null,
    last_success_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    next_retry_at: null,
    ...overrides
  }
}

describe('SurfacesHealth', () => {
  it('shows a load-failure state when unreachable', () => {
    render(<SurfacesHealth isAvailable={false} isLoading={false} surfaces={[]} />)

    expect(screen.getByText(/Couldn't load surface health/)).toBeTruthy()
  })

  it('shows an informative empty state when no surfaces are connected', () => {
    render(<SurfacesHealth isAvailable={true} isLoading={false} surfaces={[]} />)

    expect(screen.getByText(/No connected accounts yet/)).toBeTruthy()
  })

  it('renders a row per surface with its status label', () => {
    render(
      <SurfacesHealth
        isAvailable={true}
        isLoading={false}
        surfaces={[makeSurface({ surface: 'gmail', status: 'ok' }), makeSurface({ surface: 'github', status: 'error' })]}
      />
    )

    expect(screen.getByText('gmail')).toBeTruthy()
    expect(screen.getByText('github')).toBeTruthy()
    expect(screen.getByText('Synced')).toBeTruthy()
    expect(screen.getByText('Error')).toBeTruthy()
  })

  it('shows "never synced" when the surface has no last_success_at', () => {
    render(<SurfacesHealth isAvailable={true} isLoading={false} surfaces={[makeSurface({ last_success_at: null })]} />)

    expect(screen.getByText(/never synced/)).toBeTruthy()
  })

  it('shows a loading state', () => {
    render(<SurfacesHealth isAvailable={true} isLoading={true} surfaces={[]} />)

    expect(screen.getByText('Loading surfaces…')).toBeTruthy()
  })
})
