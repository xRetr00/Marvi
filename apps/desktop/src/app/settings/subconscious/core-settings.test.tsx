import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PresenceSettings, SubconsciousCoreSettings } from './core-settings'
import type { useMarviConfig } from './use-marvi-config'

vi.mock('./use-activitywatch-status', () => ({
  useActivityWatchStatus: vi.fn(() => ({ checked: true, checking: false, reachable: true }))
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function fakeMarvi(values: Record<string, unknown> = {}): ReturnType<typeof useMarviConfig> {
  const patch = vi.fn(async () => undefined)

  return {
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
  it('toggles subconscious.enabled via the switch', () => {
    const marvi = fakeMarvi({ 'subconscious.enabled': false })
    render(<SubconsciousCoreSettings marvi={marvi} />)

    fireEvent.click(screen.getByLabelText('Enable subconscious'))

    expect(marvi.patch).toHaveBeenCalledWith('subconscious.enabled', true)
  })

  it('commits the tick interval on blur', () => {
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

  it('toggles flow gating', () => {
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
