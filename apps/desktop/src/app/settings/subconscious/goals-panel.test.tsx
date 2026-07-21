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

  describe('inferred goals', () => {
    it('shows an "Inferred" badge on origin=inferred goals but not on user goals', async () => {
      readGoals.mockResolvedValue([
        goal({ id: 'g1', title: 'User goal', origin: 'user' }),
        goal({ id: 'g2', title: 'Auto goal', origin: 'inferred' })
      ])

      await renderPanel()
      await screen.findByText('User goal')

      expect(screen.getByText('Auto goal').closest('div')?.parentElement?.textContent).toContain('Inferred')
      expect(screen.getByText('User goal').closest('div')?.parentElement?.textContent).not.toContain('Inferred')
    })

    it('does not show a badge for a goal with no origin field (pre-existing goal)', async () => {
      const legacyGoal = goal({ id: 'g1', title: 'Legacy goal' })
      delete (legacyGoal as Partial<Goal>).origin
      readGoals.mockResolvedValue([legacyGoal])

      await renderPanel()
      await screen.findByText('Legacy goal')

      expect(screen.queryByText('Inferred')).toBeNull()
    })

    it('"Keep" flips origin to "user" and persists via writeGoals', async () => {
      readGoals.mockResolvedValue([goal({ id: 'g1', title: 'Auto goal', origin: 'inferred' })])

      await renderPanel()
      await screen.findByText('Auto goal')

      fireEvent.click(screen.getByRole('button', { name: 'Keep' }))

      await waitFor(() =>
        expect(writeGoals).toHaveBeenCalledWith([expect.objectContaining({ id: 'g1', origin: 'user' })])
      )
    })

    it('does not show "Keep" for a user-origin goal', async () => {
      readGoals.mockResolvedValue([goal({ id: 'g1', title: 'User goal', origin: 'user' })])

      await renderPanel()
      await screen.findByText('User goal')

      expect(screen.queryByRole('button', { name: 'Keep' })).toBeNull()
    })

    it('delete works the same for an inferred goal as any other', async () => {
      window.confirm = vi.fn(() => true)
      readGoals.mockResolvedValue([goal({ id: 'g1', title: 'Auto goal', origin: 'inferred' })])

      await renderPanel()
      await screen.findByText('Auto goal')

      fireEvent.click(screen.getByTitle('Delete'))
      fireEvent.click(screen.getByRole('button', { name: 'Delete' }))

      await waitFor(() => expect(writeGoals).toHaveBeenCalledWith([]))
    })
  })
})
