import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ConnectedAccounts } from './connected-accounts'
import type { useMarviConfig } from './use-marvi-config'

vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: vi.fn() }))

afterEach(() => {
  cleanup()
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

function fakeMarvi(overrides: Partial<Record<string, unknown>> = {}): ReturnType<typeof useMarviConfig> {
  const patch = vi.fn(async () => undefined)
  const values: Record<string, unknown> = { 'composio.surfaces': [], 'composio.api_key': '', ...overrides }

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

describe('ConnectedAccounts', () => {
  it('shows an empty state and popular surface suggestions when none are configured', () => {
    render(<ConnectedAccounts marvi={fakeMarvi()} />)

    expect(screen.getByText('No surfaces configured yet.')).toBeTruthy()
    expect(screen.getByText('gmail')).toBeTruthy()
  })

  it('adds a suggested surface via patch', () => {
    const marvi = fakeMarvi()
    render(<ConnectedAccounts marvi={marvi} />)

    fireEvent.click(screen.getByText('gmail'))

    expect(marvi.patch).toHaveBeenCalledWith('composio.surfaces', ['gmail'])
  })

  it('renders the connect command for each configured surface', () => {
    render(<ConnectedAccounts marvi={fakeMarvi({ 'composio.surfaces': ['github'] })} />)

    expect(screen.getByText('hermes composio connect github')).toBeTruthy()
  })

  it('copies the connect command to the clipboard', async () => {
    const writeClipboard = vi.fn().mockResolvedValue(true)

    ;(window as unknown as { hermesDesktop: { writeClipboard: typeof writeClipboard } }).hermesDesktop = {
      writeClipboard
    }

    render(<ConnectedAccounts marvi={fakeMarvi({ 'composio.surfaces': ['github'] })} />)

    fireEvent.click(screen.getByTitle('Copy command'))

    await Promise.resolve()
    expect(writeClipboard).toHaveBeenCalledWith('hermes composio connect github')
  })
})
