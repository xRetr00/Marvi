import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Goal } from './types'

const isGoalsBridgeAvailable = vi.fn(() => true)
const readGoals = vi.fn()
const writeGoals = vi.fn()
const createGoal = vi.fn()

vi.mock('./goals-service', () => ({
  isGoalsBridgeAvailable: () => isGoalsBridgeAvailable(),
  readGoals: () => readGoals(),
  writeGoals: (goals: Goal[]) => writeGoals(goals),
  createGoal: (input: unknown) => createGoal(input)
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

function goal(overrides: Partial<Goal> = {}): Goal {
  return {
    id: 'g1',
    title: 'Ship the release',
    detail: 'Finish workstream D',
    status: 'active',
    horizon: 'short',
    created: '2026-01-01T00:00:00.000Z',
    updated: '2026-01-01T00:00:00.000Z',
    ...overrides
  }
}

beforeEach(() => {
  isGoalsBridgeAvailable.mockReturnValue(true)
  readGoals.mockResolvedValue([])
  writeGoals.mockResolvedValue(undefined)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

async function renderPanel() {
  const { GoalsPanel } = await import('./goals-panel')

  return render(<GoalsPanel />)
}

describe('GoalsPanel', () => {
  it('shows an empty state when there are no goals', async () => {
    await renderPanel()

    expect(await screen.findByText(/No goals yet/)).toBeTruthy()
  })

  it('renders goals grouped by status', async () => {
    readGoals.mockResolvedValue([goal(), goal({ id: 'g2', title: 'Paused one', status: 'paused' })])

    await renderPanel()

    expect(await screen.findByText('Ship the release')).toBeTruthy()
    expect(screen.getByText('Paused one')).toBeTruthy()
    expect(screen.getByText('Active')).toBeTruthy()
    expect(screen.getByText('Paused')).toBeTruthy()
  })

  it('marks a goal done and persists via writeGoals', async () => {
    readGoals.mockResolvedValue([goal()])

    await renderPanel()
    await screen.findByText('Ship the release')

    fireEvent.click(screen.getByTitle('Mark done'))

    await waitFor(() =>
      expect(writeGoals).toHaveBeenCalledWith([expect.objectContaining({ id: 'g1', status: 'done' })])
    )
  })

  it('opens the add-goal dialog and creates a goal', async () => {
    createGoal.mockReturnValue(goal({ id: 'new', title: 'New goal' }))

    await renderPanel()
    await screen.findByText(/No goals yet/)

    fireEvent.click(screen.getByRole('button', { name: /Add goal/ }))
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'New goal' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add goal' }))

    await waitFor(() => expect(writeGoals).toHaveBeenCalledWith([goal({ id: 'new', title: 'New goal' })]))
  })

  it('shows an unavailable state when the local file bridge is missing', async () => {
    isGoalsBridgeAvailable.mockReturnValue(false)

    await renderPanel()

    expect(await screen.findByText('Goals unavailable')).toBeTruthy()
    expect(readGoals).not.toHaveBeenCalled()
  })
})
