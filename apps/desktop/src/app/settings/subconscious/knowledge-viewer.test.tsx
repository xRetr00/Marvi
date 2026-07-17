import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { KnowledgeViewer } from './knowledge-viewer'

vi.mock('./use-marvi-knowledge', () => ({ useMarviKnowledge: vi.fn(), MARVI_KNOWLEDGE_KEY: ['marvi-knowledge'] }))
vi.mock('./use-memory-archive', () => ({ useMemoryArchive: vi.fn() }))

const EMPTY_ARCHIVE = { entries: [], isAvailable: true, isLoading: false, restoringId: null, restore: vi.fn() }

beforeEach(async () => {
  const { useMemoryArchive } = await import('./use-memory-archive')
  vi.mocked(useMemoryArchive).mockReturnValue(EMPTY_ARCHIVE)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('KnowledgeViewer', () => {
  it('shows a load-failure state when the backend surface is unreachable — never fabricated entries', async () => {
    const { useMarviKnowledge } = await import('./use-marvi-knowledge')
    vi.mocked(useMarviKnowledge).mockReturnValue({ entries: [], isAvailable: false, isLoading: false })

    render(<KnowledgeViewer />)

    expect(screen.getByText(/Couldn't load what Marvi knows/)).toBeTruthy()
  })

  it('shows a distilled-nothing-yet state once available but empty', async () => {
    const { useMarviKnowledge } = await import('./use-marvi-knowledge')
    vi.mocked(useMarviKnowledge).mockReturnValue({ entries: [], isAvailable: true, isLoading: false })

    render(<KnowledgeViewer />)

    expect(screen.getByText('Nothing distilled yet.')).toBeTruthy()
  })

  it('renders distilled entries when present', async () => {
    const { useMarviKnowledge } = await import('./use-marvi-knowledge')
    vi.mocked(useMarviKnowledge).mockReturnValue({
      entries: [{ id: '1', summary: 'You debugged the auth flow for 2 hours.', source: 'presence', createdAt: '2026-01-01T00:00:00.000Z' }],
      isAvailable: true,
      isLoading: false
    })

    render(<KnowledgeViewer />)

    expect(screen.getByText('You debugged the auth flow for 2 hours.')).toBeTruthy()
    expect(screen.getByText('Presence')).toBeTruthy()
  })
})

describe('KnowledgeViewer — Archived section (Loop 3, memory decay)', () => {
  beforeEach(async () => {
    const { useMarviKnowledge } = await import('./use-marvi-knowledge')
    vi.mocked(useMarviKnowledge).mockReturnValue({ entries: [], isAvailable: true, isLoading: false })
  })

  it('is collapsed by default and shows no archived entries until opened', async () => {
    const { useMemoryArchive } = await import('./use-memory-archive')
    vi.mocked(useMemoryArchive).mockReturnValue({
      ...EMPTY_ARCHIVE,
      entries: [{ id: 'memory:abc123', target: 'memory', text: 'A stale fact', archived_at: '2026-01-01T00:00:00.000Z' }]
    })

    render(<KnowledgeViewer />)

    expect(screen.getByText('Archived (1)')).toBeTruthy()
    expect(screen.queryByText('A stale fact')).toBeNull()
  })

  it('reveals archived entries and a Restore button when opened', async () => {
    const { useMemoryArchive } = await import('./use-memory-archive')
    vi.mocked(useMemoryArchive).mockReturnValue({
      ...EMPTY_ARCHIVE,
      entries: [{ id: 'memory:abc123', target: 'memory', text: 'A stale fact', archived_at: '2026-01-01T00:00:00.000Z' }]
    })

    render(<KnowledgeViewer />)
    fireEvent.click(screen.getByText('Archived (1)'))

    expect(screen.getByText('A stale fact')).toBeTruthy()
    expect(screen.getByText('Restore')).toBeTruthy()
  })

  it('calls restore with the entry id when Restore is clicked', async () => {
    const { useMemoryArchive } = await import('./use-memory-archive')
    const restore = vi.fn().mockResolvedValue(undefined)
    vi.mocked(useMemoryArchive).mockReturnValue({
      ...EMPTY_ARCHIVE,
      entries: [{ id: 'memory:abc123', target: 'memory', text: 'A stale fact', archived_at: '2026-01-01T00:00:00.000Z' }],
      restore
    })

    render(<KnowledgeViewer />)
    fireEvent.click(screen.getByText('Archived (1)'))
    fireEvent.click(screen.getByText('Restore'))

    expect(restore).toHaveBeenCalledWith('memory:abc123')
  })

  it('shows an empty state once opened with nothing archived', async () => {
    const { useMemoryArchive } = await import('./use-memory-archive')
    vi.mocked(useMemoryArchive).mockReturnValue(EMPTY_ARCHIVE)

    render(<KnowledgeViewer />)
    fireEvent.click(screen.getByText('Archived'))

    expect(screen.getByText(/Nothing archived yet/)).toBeTruthy()
  })
})
