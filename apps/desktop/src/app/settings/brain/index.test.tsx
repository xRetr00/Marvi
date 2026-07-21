import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { BrainStatus } from './brain-service'
import type { BrainStatusState } from './use-brain-status'

import { BrainSettings } from './index'

vi.mock('./use-brain-status', () => ({ useBrainStatus: vi.fn() }))

vi.mock('./brain-service', () => ({
  updateBrainConfig: vi.fn(async () => ({ ok: true, brain: { enabled: true, folders: [], exclude: [], schedule: 'every 6h' } })),
  indexBrainNow: vi.fn(async () => ({ ok: true, indexed: 3, skipped: 1, removed: 0, errors: 0, files: 3, chunks: 9, indexed_at: '2026-07-14T00:00:00+00:00' })),
  searchBrain: vi.fn(async () => ({ ok: true, results: [] }))
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function fakeStatus(overrides: Partial<BrainStatus> = {}): BrainStatus {
  return {
    ok: true,
    enabled: false,
    folders: [],
    exclude: [],
    schedule: 'every 6h',
    files: 0,
    chunks: 0,
    indexed_at: null,
    last_run: { at: null, indexed: 0, skipped: 0, removed: 0, errors: 0 },
    auto_discover: true,
    max_auto_folders: 5,
    auto_folders: [],
    collect_email: true,
    collect_github: true,
    github_max_repos: 10,
    discovered_folders: [],
    last_discovery: { at: null, folders: [] },
    collected: {},
    last_collect: { at: null, email: null, github: null },
    ...overrides
  }
}

function fakeBrainState(overrides: Partial<BrainStatusState> = {}): BrainStatusState {
  return {
    status: fakeStatus(),
    isAvailable: true,
    isLoading: false,
    refetch: vi.fn(async () => undefined),
    ...overrides
  }
}

async function mockUseBrainStatus(state: BrainStatusState) {
  const { useBrainStatus } = await import('./use-brain-status')
  vi.mocked(useBrainStatus).mockReturnValue(state)
}

describe('BrainSettings', () => {
  it('shows a loading state while the initial status fetch is in flight', async () => {
    await mockUseBrainStatus(fakeBrainState({ status: null, isLoading: true }))

    render(<BrainSettings />)

    expect(screen.getByRole('status', { name: 'Loading Brain settings' })).toBeTruthy()
  })

  it('shows a load-failure state when the backend surface is unreachable', async () => {
    await mockUseBrainStatus(fakeBrainState({ status: null, isAvailable: false, isLoading: false }))

    render(<BrainSettings />)

    expect(screen.getByText(/Couldn't load Brain settings/)).toBeTruthy()
  })

  it('shows the informative empty state when Brain is off with no folders', async () => {
    await mockUseBrainStatus(fakeBrainState({ status: fakeStatus({ enabled: false, folders: [] }) }))

    render(<BrainSettings />)

    expect(screen.getByText('Brain is off — add a folder to give Marvi memory of your files.')).toBeTruthy()
  })

  it('disables the enable toggle until a folder is added', async () => {
    await mockUseBrainStatus(fakeBrainState({ status: fakeStatus({ enabled: false, folders: [] }) }))

    render(<BrainSettings />)

    expect(screen.getByLabelText('Enable Brain')).toHaveProperty('disabled', true)
  })

  it('renders index stats and last-run info once folders are configured', async () => {
    await mockUseBrainStatus(
      fakeBrainState({
        status: fakeStatus({
          enabled: true,
          folders: ['D:\\Projects\\notes'],
          files: 12,
          chunks: 40,
          last_run: { at: '2026-07-14T00:00:00+00:00', indexed: 2, skipped: 10, removed: 0, errors: 0 }
        })
      })
    )

    render(<BrainSettings />)

    expect(screen.getByText('12 files')).toBeTruthy()
    expect(screen.getByText('40 passages')).toBeTruthy()
    expect(screen.queryByText('Brain is off — add a folder to give Marvi memory of your files.')).toBeNull()
  })

  it('surfaces a run with errors in the last-run label', async () => {
    await mockUseBrainStatus(
      fakeBrainState({
        status: fakeStatus({
          enabled: true,
          folders: ['D:\\Projects\\notes'],
          last_run: { at: '2026-07-14T00:00:00+00:00', indexed: 2, skipped: 0, removed: 0, errors: 3 }
        })
      })
    )

    render(<BrainSettings />)

    expect(screen.getByText(/3 errors/)).toBeTruthy()
  })

  describe('folders editor', () => {
    it('adds a folder via updateBrainConfig and refetches on success', async () => {
      const { updateBrainConfig } = await import('./brain-service')
      const refetch = vi.fn(async () => undefined)
      await mockUseBrainStatus(fakeBrainState({ status: fakeStatus({ folders: [] }), refetch }))

      render(<BrainSettings />)

      fireEvent.change(screen.getByPlaceholderText('D:\\Projects\\my-notes'), { target: { value: 'D:\\Docs' } })
      fireEvent.click(screen.getAllByRole('button', { name: 'Add' })[0])

      await waitFor(() => expect(updateBrainConfig).toHaveBeenCalledWith({ folders: ['D:\\Docs'] }))
      await waitFor(() => expect(refetch).toHaveBeenCalled())
    })

    it('rolls back the optimistic add and shows an error toast when the folder does not exist', async () => {
      const { updateBrainConfig } = await import('./brain-service')
      const { notifyError } = await import('@/store/notifications')
      vi.mocked(updateBrainConfig).mockRejectedValueOnce(new Error('400: {"detail":"Folder(s) not found on disk: D:\\\\Nope"}'))
      await mockUseBrainStatus(fakeBrainState({ status: fakeStatus({ folders: [] }) }))

      render(<BrainSettings />)

      fireEvent.change(screen.getByPlaceholderText('D:\\Projects\\my-notes'), { target: { value: 'D:\\Nope' } })
      fireEvent.click(screen.getAllByRole('button', { name: 'Add' })[0])

      await waitFor(() => expect(notifyError).toHaveBeenCalledWith(expect.any(Error), 'Failed to update watched folders'))
      // Rolled back: the folder chip should not remain in the list.
      await waitFor(() => expect(screen.queryByText('D:\\Nope')).toBeNull())
    })

    it('removes a folder via updateBrainConfig', async () => {
      const { updateBrainConfig } = await import('./brain-service')
      await mockUseBrainStatus(fakeBrainState({ status: fakeStatus({ enabled: true, folders: ['D:\\Docs'] }) }))

      render(<BrainSettings />)

      fireEvent.click(screen.getByLabelText('Remove D:\\Docs'))

      await waitFor(() => expect(updateBrainConfig).toHaveBeenCalledWith({ folders: [] }))
    })
  })

  describe('enable toggle', () => {
    it('enables Brain via updateBrainConfig once a folder exists', async () => {
      const { updateBrainConfig } = await import('./brain-service')
      await mockUseBrainStatus(fakeBrainState({ status: fakeStatus({ enabled: false, folders: ['D:\\Docs'] }) }))

      render(<BrainSettings />)

      fireEvent.click(screen.getByLabelText('Enable Brain'))

      await waitFor(() => expect(updateBrainConfig).toHaveBeenCalledWith({ enabled: true }))
    })
  })

  describe('reindex', () => {
    it('reindexes now and reports the result', async () => {
      const { indexBrainNow } = await import('./brain-service')
      const { notify } = await import('@/store/notifications')
      await mockUseBrainStatus(fakeBrainState({ status: fakeStatus({ enabled: true, folders: ['D:\\Docs'] }) }))

      render(<BrainSettings />)

      fireEvent.click(screen.getByRole('button', { name: 'Reindex now' }))

      await waitFor(() => expect(indexBrainNow).toHaveBeenCalled())
      await waitFor(() => expect(notify).toHaveBeenCalledWith({ kind: 'success', message: 'Indexed 3 changed files' }))
    })

    it('disables the reindex button when there are no folders', async () => {
      await mockUseBrainStatus(fakeBrainState({ status: fakeStatus({ folders: [] }) }))

      render(<BrainSettings />)

      expect(screen.getByRole('button', { name: 'Reindex now' })).toHaveProperty('disabled', true)
    })
  })

  describe('search', () => {
    it('shows a prompt instead of a search box when there are no folders', async () => {
      await mockUseBrainStatus(fakeBrainState({ status: fakeStatus({ folders: [] }) }))

      render(<BrainSettings />)

      expect(screen.getByText('Nothing to search yet — add a watched folder first.')).toBeTruthy()
      expect(screen.queryByPlaceholderText('Search indexed files')).toBeNull()
    })

    it('runs a search and renders result snippets', async () => {
      const { searchBrain } = await import('./brain-service')
      vi.mocked(searchBrain).mockResolvedValueOnce({
        ok: true,
        results: [{ path: 'D:\\Docs\\contract.md', chunk_index: 0, snippet: 'the [contract] renews annually', score: -1.2 }]
      })
      await mockUseBrainStatus(fakeBrainState({ status: fakeStatus({ enabled: true, folders: ['D:\\Docs'] }) }))

      render(<BrainSettings />)

      fireEvent.change(screen.getByPlaceholderText('Search indexed files'), { target: { value: 'contract' } })
      fireEvent.click(screen.getByRole('button', { name: 'Search' }))

      await waitFor(() => expect(searchBrain).toHaveBeenCalledWith('contract'))
      expect(await screen.findByText('D:\\Docs\\contract.md')).toBeTruthy()
      expect(screen.getByText('contract')).toBeTruthy()
    })

    it('shows a no-matches message after an empty search', async () => {
      await mockUseBrainStatus(fakeBrainState({ status: fakeStatus({ enabled: true, folders: ['D:\\Docs'] }) }))

      render(<BrainSettings />)

      fireEvent.change(screen.getByPlaceholderText('Search indexed files'), { target: { value: 'nothing' } })
      fireEvent.click(screen.getByRole('button', { name: 'Search' }))

      expect(await screen.findByText('No matches for "nothing".')).toBeTruthy()
    })

    it('does not search on an empty/whitespace query', async () => {
      const { searchBrain } = await import('./brain-service')
      await mockUseBrainStatus(fakeBrainState({ status: fakeStatus({ enabled: true, folders: ['D:\\Docs'] }) }))

      render(<BrainSettings />)

      expect(screen.getByRole('button', { name: 'Search' })).toHaveProperty('disabled', true)
      expect(searchBrain).not.toHaveBeenCalled()
    })
  })

  describe('auto-build', () => {
    it('reflects auto_discover in the toggle state', async () => {
      await mockUseBrainStatus(fakeBrainState({ status: fakeStatus({ auto_discover: false }) }))

      render(<BrainSettings />)

      expect(screen.getByLabelText('Auto-build').getAttribute('aria-checked')).toBe('false')
    })

    it('disabling auto-build patches auto_discover and both collect flags off', async () => {
      const { updateBrainConfig } = await import('./brain-service')
      await mockUseBrainStatus(fakeBrainState({ status: fakeStatus({ auto_discover: true }) }))

      render(<BrainSettings />)

      fireEvent.click(screen.getByLabelText('Auto-build'))

      await waitFor(() =>
        expect(updateBrainConfig).toHaveBeenCalledWith({
          auto_discover: false,
          collect: { email: false, github: false }
        })
      )
    })
  })

  describe('discovered folders', () => {
    it('lists discovered folders not already excluded', async () => {
      await mockUseBrainStatus(
        fakeBrainState({ status: fakeStatus({ discovered_folders: ['D:\\Users\\me\\Documents\\Notes'] }) })
      )

      render(<BrainSettings />)

      expect(screen.getByText('D:\\Users\\me\\Documents\\Notes')).toBeTruthy()
    })

    it('hides the Discovered folders section once every candidate is already excluded', async () => {
      await mockUseBrainStatus(
        fakeBrainState({
          status: fakeStatus({ discovered_folders: ['D:\\Notes'], exclude: ['D:\\Notes'] })
        })
      )

      render(<BrainSettings />)

      // 'D:\Notes' still legitimately appears once, as a chip in the
      // Exclude patterns editor -- what this asserts is that the Discovered
      // folders section itself doesn't render a second, redundant listing.
      expect(screen.queryByText('Discovered folders')).toBeNull()
    })

    it('removing a discovered folder adds it to the exclude list', async () => {
      const { updateBrainConfig } = await import('./brain-service')
      await mockUseBrainStatus(
        fakeBrainState({ status: fakeStatus({ discovered_folders: ['D:\\Notes'], exclude: ['*.min.js'] }) })
      )

      render(<BrainSettings />)

      fireEvent.click(screen.getByLabelText('Remove D:\\Notes'))

      await waitFor(() => expect(updateBrainConfig).toHaveBeenCalledWith({ exclude: ['*.min.js', 'D:\\Notes'] }))
    })
  })

  describe('collected counters', () => {
    it('shows email/github counts and rolls up every other source into "Agent"', async () => {
      await mockUseBrainStatus(
        fakeBrainState({
          status: fakeStatus({ collected: { email: 3, github: 2, chat: 1, subconscious: 4 } })
        })
      )

      render(<BrainSettings />)

      expect(screen.getByText('Email 3')).toBeTruthy()
      expect(screen.getByText('GitHub 2')).toBeTruthy()
      expect(screen.getByText('Agent 5')).toBeTruthy()
    })
  })
})
