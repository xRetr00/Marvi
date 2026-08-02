import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AutonomyPanel } from './autonomy-panel'

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('../settings/subconscious/use-marvi-config', () => ({
  useMarviConfig: () => ({
    activate: vi.fn(),
    config: {},
    isError: false,
    isLoading: false,
    savingPath: null,
    refetch: vi.fn(),
    patch: vi.fn(async () => undefined),
    get: (_path: string, fallback: unknown) => fallback
  })
}))

const api = vi.fn()

const CANNED_STATUS = {
  ok: true,
  enabled: true,
  budget: {
    date: '2026-07-24',
    enabled: true,
    daily_action_budget: 8,
    used_total: 3,
    remaining_total: 5,
    categories: {
      research: { limit: 4, used: 2, remaining: 2 },
      browse: { limit: 2, used: 0, remaining: 2 },
      ask_user: { limit: 3, used: 1, remaining: 2 }
    }
  },
  recent_actions: [
    { at: '2026-07-24T10:00:00.000Z', source: 'autonomy', outcome: 'message', summary: 'Researched the Ziraat pattern' }
  ],
  pending_questions: [
    { id: 'q1', question: 'Shift your morning brief later?', category: 'reflection', status: 'pending', asked_at: '2026-07-24T09:00:00.000Z' }
  ]
}

beforeEach(() => {
  ;(window as unknown as { hermesDesktop: { api: unknown } }).hermesDesktop = { api }
  api.mockResolvedValue(CANNED_STATUS)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

describe('AutonomyPanel', () => {
  it('shows the loading state before the status response resolves', () => {
    render(<AutonomyPanel />)
    expect(screen.getByText('Loading autonomy status…')).toBeTruthy()
  })

  it('renders today\'s budget usage total in the section meta', async () => {
    render(<AutonomyPanel />)
    expect(await screen.findByText('3/8 today')).toBeTruthy()
  })

  it('renders per-category used/limit counts', async () => {
    render(<AutonomyPanel />)
    expect(await screen.findByText('2/4 used')).toBeTruthy()
    expect(await screen.findByText('1/3 used')).toBeTruthy()
  })

  it('renders a recent autonomous action summary', async () => {
    render(<AutonomyPanel />)
    expect(await screen.findByText('Researched the Ziraat pattern')).toBeTruthy()
  })

  it('renders a pending question with its status', async () => {
    render(<AutonomyPanel />)
    expect(await screen.findByText('Shift your morning brief later?')).toBeTruthy()
    expect(await screen.findByText('Waiting')).toBeTruthy()
  })

  it('shows an answered question\'s reply text', async () => {
    api.mockResolvedValue({
      ...CANNED_STATUS,
      pending_questions: [
        {
          id: 'q2',
          question: 'Want the report earlier?',
          category: 'reflection',
          status: 'answered',
          asked_at: '2026-07-23T09:00:00.000Z',
          answer_text: 'Yes please'
        }
      ]
    })
    render(<AutonomyPanel />)
    expect(await screen.findByText('Answered')).toBeTruthy()
    expect(await screen.findByText('Reply: Yes please')).toBeTruthy()
  })

  it('shows empty states when nothing has happened yet', async () => {
    api.mockResolvedValue({
      ok: true,
      enabled: true,
      budget: {
        date: '2026-07-24',
        enabled: true,
        daily_action_budget: 8,
        used_total: 0,
        remaining_total: 8,
        categories: { research: { limit: 4, used: 0, remaining: 4 }, browse: { limit: 2, used: 0, remaining: 2 }, ask_user: { limit: 3, used: 0, remaining: 3 } }
      },
      recent_actions: [],
      pending_questions: []
    })
    render(<AutonomyPanel />)
    expect(await screen.findByText("Nothing yet — Marvi hasn't spent any autonomy budget.")).toBeTruthy()
    expect(await screen.findByText("Marvi hasn't proactively asked you anything yet.")).toBeTruthy()
  })

  it('requests /api/autonomy/status on mount', async () => {
    render(<AutonomyPanel />)
    await waitFor(() => expect(api).toHaveBeenCalledWith({ path: '/api/autonomy/status' }))
  })

  it('answers the exact pending question', async () => {
    render(<AutonomyPanel />)
    const input = await screen.findByLabelText('Answer: Shift your morning brief later?')
    fireEvent.change(input, { target: { value: 'Yes, move it to 9am' } })
    fireEvent.click(screen.getByRole('button', { name: 'Answer' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith({
      path: '/api/autonomy/questions/q1/answer',
      method: 'POST',
      body: { answer: 'Yes, move it to 9am' }
    }))
  })
})
