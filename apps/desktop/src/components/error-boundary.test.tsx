import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ErrorBoundary } from './error-boundary'

function BrokenView(): never {
  throw new Error('render failed')
}

describe('ErrorBoundary', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    window.hermesDesktop = {
      updates: {
        apply: vi.fn(),
        check: vi.fn().mockResolvedValue({
          behind: 1,
          supported: true,
          targetSha: 'sha'
        }),
        onProgress: vi.fn()
      }
    } as unknown as typeof window.hermesDesktop
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    delete (window as { hermesDesktop?: unknown }).hermesDesktop
  })

  it('shows an update action on the crash screen when an update is available', async () => {
    render(
      <ErrorBoundary label="test">
        <BrokenView />
      </ErrorBoundary>
    )

    await waitFor(() => expect(screen.getByRole('button', { name: 'Update now' })).toBeTruthy())
  })
})
