import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { KnowledgeViewer } from './knowledge-viewer'

vi.mock('./use-marvi-knowledge', () => ({ useMarviKnowledge: vi.fn() }))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('KnowledgeViewer', () => {
  it('shows a "coming soon" state when no backend surface is wired up yet — never fabricated entries', async () => {
    const { useMarviKnowledge } = await import('./use-marvi-knowledge')
    vi.mocked(useMarviKnowledge).mockReturnValue({ entries: [], isAvailable: false, isLoading: false })

    render(<KnowledgeViewer />)

    expect(screen.getByText(/Coming soon/)).toBeTruthy()
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
