import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { TimelineTab } from './timeline-tab'

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

const api = vi.fn()

beforeEach(() => {
  ;(window as unknown as { hermesDesktop: { api: unknown } }).hermesDesktop = { api }
  api.mockResolvedValue({ episodes: [] })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

const EPISODE = {
  id: 1,
  ts: '2026-07-17T10:00:00.000Z',
  kind: 'task',
  actor: 'marvi',
  title: 'Fixed the build',
  summary: 'CI was red, now green.',
  source: 'activity:tick'
}

describe('TimelineTab', () => {
  it('shows the canonical empty state when there are no episodes and no note', async () => {
    render(<TimelineTab />)

    expect(await screen.findByText("Marvi's episodic memory starts filling as it observes your days.")).toBeTruthy()
  })

  it('prefers the backend-provided note when present', async () => {
    api.mockResolvedValue({ episodes: [], note: 'Custom empty note from backend' })

    render(<TimelineTab />)

    expect(await screen.findByText('Custom empty note from backend')).toBeTruthy()
  })

  it('renders episodes grouped under a day heading with kind pill and title', async () => {
    api.mockResolvedValue({ episodes: [EPISODE] })

    render(<TimelineTab />)

    const item = (await screen.findByText('Fixed the build')).closest('li') as HTMLElement
    expect(within(item).getByText('CI was red, now green.')).toBeTruthy()
    expect(within(item).getByText('Task')).toBeTruthy()
  })

  it('shows an unavailable/offline message when the fetch fails and no data was ever loaded', async () => {
    api.mockRejectedValue(new Error('network down'))

    render(<TimelineTab />)

    expect(await screen.findByText(/Timeline is unavailable while the backend is offline/)).toBeTruthy()
  })

  it('debounces a search query into the q= request param', async () => {
    render(<TimelineTab />)
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1))

    fireEvent.change(screen.getByPlaceholderText('Search episodes'), { target: { value: 'frontend' } })

    await waitFor(() => expect(api).toHaveBeenCalledTimes(2))
    const lastCall = api.mock.calls[api.mock.calls.length - 1][0] as { path: string }
    expect(lastCall.path).toContain('q=frontend')
  })

  it('clicking a kind chip requests that kind filter', async () => {
    render(<TimelineTab />)
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByText('Room'))

    await waitFor(() => expect(api).toHaveBeenCalledTimes(2))
    const lastCall = api.mock.calls[api.mock.calls.length - 1][0] as { path: string }
    expect(lastCall.path).toContain('kind=room')
  })
})
