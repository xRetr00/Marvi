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
})
