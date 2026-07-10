import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PresenceSettings, SubconsciousCoreSettings } from './core-settings'
import type { useMarviConfig } from './use-marvi-config'

vi.mock('./use-activitywatch-status', () => ({
  useActivityWatchStatus: vi.fn(() => ({ checked: true, checking: false, reachable: true }))
}))

vi.mock('./activation-service', () => ({
  enableSubconscious: vi.fn(async () => ({ ok: true, enabled: true })),
  disableSubconscious: vi.fn(async () => ({ ok: true, enabled: false })),
  setupPresence: vi.fn(async () => ({ ok: true, enabled: true, job_ok: true })),
  pausePresence: vi.fn(async () => ({ ok: true, enabled: false }))
}))

vi.mock('@/store/notifications', () => ({
  notifyError: vi.fn()
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function fakeMarvi(values: Record<string, unknown> = {}): ReturnType<typeof useMarviConfig> {
  const patch = vi.fn(async () => undefined)

  // Mirrors the real hook: run the activation action, swallowing errors the
  // way the optimistic-update wrapper does.
  const activate = vi.fn(async (_path: string, _value: unknown, action: () => Promise<unknown>) => {
    try {
      await action()
    } catch {
      /* the real hook rolls back + toasts */
    }
  })

  return {
    activate,
    config: {},
    isError: false,
    isLoading: false,
    savingPath: null,
    refetch: vi.fn(),
    patch,
    get: <T,>(path: string, fallback: T): T => (path in values ? (values[path] as T) : fallback)
  } as unknown as ReturnType<typeof useMarviConfig>
}

describe('SubconsciousCoreSettings', () => {
  it('enables the subconscious via the activation endpoint, not a raw config patch', async () => {
    const { enableSubconscious } = await import('./activation-service')
    const marvi = fakeMarvi({ 'subconscious.enabled': false, 'subconscious.interval': '30m' })
    render(<SubconsciousCoreSettings marvi={marvi} />)

    fireEvent.click(screen.getByLabelText('Enable subconscious'))

    expect(marvi.activate).toHaveBeenCalledWith(
      'subconscious.enabled',
      true,
      expect.any(Function),
      'Failed to enable the subconscious tick'
    )
    await waitFor(() => expect(enableSubconscious).toHaveBeenCalledWith('30m'))
    expect(marvi.patch).not.toHaveBeenCalledWith('subconscious.enabled', expect.anything())
  })

  it('disables the subconscious via the activation endpoint', async () => {
    const { disableSubconscious } = await import('./activation-service')
    const marvi = fakeMarvi({ 'subconscious.enabled': true })
    render(<SubconsciousCoreSettings marvi={marvi} />)

    fireEvent.click(screen.getByLabelText('Enable subconscious'))

    expect(marvi.activate).toHaveBeenCalledWith(
      'subconscious.enabled',
      false,
      expect.any(Function),
      'Failed to disable the subconscious tick'
    )
    await waitFor(() => expect(disableSubconscious).toHaveBeenCalled())
    expect(marvi.patch).not.toHaveBeenCalledWith('subconscious.enabled', expect.anything())
  })

  it('commits the tick interval on blur as a plain config patch', () => {
    const marvi = fakeMarvi({ 'subconscious.enabled': true, 'subconscious.interval': '20m' })
    render(<SubconsciousCoreSettings marvi={marvi} />)

    const field = screen.getByDisplayValue('20m')
    fireEvent.change(field, { target: { value: '1h' } })
    fireEvent.blur(field)

    expect(marvi.patch).toHaveBeenCalledWith('subconscious.interval', '1h')
  })
})

describe('PresenceSettings', () => {
  it('shows the ActivityWatch reachability indicator', () => {
    render(<PresenceSettings marvi={fakeMarvi()} />)

    expect(screen.getByText('ActivityWatch reachable')).toBeTruthy()
  })

  it('enables presence via the setup endpoint, not a raw config patch', async () => {
    const { setupPresence } = await import('./activation-service')
    const marvi = fakeMarvi({ 'presence.enabled': false })
    render(<PresenceSettings marvi={marvi} />)

    fireEvent.click(screen.getByLabelText('Enable presence'))

    expect(marvi.activate).toHaveBeenCalledWith(
      'presence.enabled',
      true,
      expect.any(Function),
      'Failed to set up presence'
    )
    await waitFor(() => expect(setupPresence).toHaveBeenCalled())
    expect(marvi.patch).not.toHaveBeenCalledWith('presence.enabled', expect.anything())
  })

  it('disables presence via the pause endpoint', async () => {
    const { pausePresence } = await import('./activation-service')
    const marvi = fakeMarvi({ 'presence.enabled': true })
    render(<PresenceSettings marvi={marvi} />)

    fireEvent.click(screen.getByLabelText('Enable presence'))

    expect(marvi.activate).toHaveBeenCalledWith(
      'presence.enabled',
      false,
      expect.any(Function),
      'Failed to pause presence'
    )
    await waitFor(() => expect(pausePresence).toHaveBeenCalled())
    expect(marvi.patch).not.toHaveBeenCalledWith('presence.enabled', expect.anything())
  })

  it('surfaces a degraded setup result (ok: false) as an error toast', async () => {
    const { setupPresence } = await import('./activation-service')
    const { notifyError } = await import('@/store/notifications')

    vi.mocked(setupPresence).mockResolvedValueOnce({
      ok: false,
      enabled: true,
      activitywatch_available: false,
      watcher_ok: false,
      watcher_message: 'skipped',
      job_ok: false,
      job_message: 'failed to create presence distiller job: boom'
    })

    const marvi = fakeMarvi({ 'presence.enabled': false })
    render(<PresenceSettings marvi={marvi} />)

    fireEvent.click(screen.getByLabelText('Enable presence'))

    await waitFor(() =>
      expect(notifyError).toHaveBeenCalledWith(expect.any(Error), 'Presence setup reported a problem')
    )
  })

  it('toggles flow gating as a plain config patch', () => {
    const marvi = fakeMarvi({ 'presence.enabled': true, 'presence.flow_gating': true })
    render(<PresenceSettings marvi={marvi} />)

    fireEvent.click(screen.getByLabelText('Flow-aware delivery'))

    expect(marvi.patch).toHaveBeenCalledWith('presence.flow_gating', false)
  })

  it('adds a denylist entry', () => {
    const marvi = fakeMarvi({ 'presence.enabled': true, 'presence.denylist': [] })
    render(<PresenceSettings marvi={marvi} />)

    fireEvent.change(screen.getByPlaceholderText('Title substring to strip'), { target: { value: 'Private Tab' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    expect(marvi.patch).toHaveBeenCalledWith('presence.denylist', ['Private Tab'])
  })
})
