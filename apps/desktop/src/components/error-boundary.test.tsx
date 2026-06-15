import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ErrorBoundary } from './error-boundary'

function BrokenView(): never {
  throw new Error('render failed')
}

let transientRenderCount = 0

function TransientAssistantUiRace() {
  transientRenderCount += 1

  if (transientRenderCount === 1) {
    throw new Error('tapClientLookup: Index 0 out of bounds (length: 0)')
  }

  return <div>recovered</div>
}

describe('ErrorBoundary', () => {
  beforeEach(() => {
    transientRenderCount = 0
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

  it('recovers from transient assistant-ui lookup races without showing the crash screen', async () => {
    render(
      <ErrorBoundary label="test">
        <TransientAssistantUiRace />
      </ErrorBoundary>,
      { onRecoverableError: () => undefined }
    )

    await waitFor(() => expect(screen.getByText('recovered')).toBeTruthy())
    expect(screen.queryByRole('button', { name: 'Update now' })).toBeNull()
  })
})
