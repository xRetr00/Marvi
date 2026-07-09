import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createGoal, isGoalsBridgeAvailable, parseGoals, readGoals, serializeGoals, writeGoals } from './goals-service'
import type { Goal } from './types'

const readFileText = vi.fn()
const writeTextFile = vi.fn()

function installBridge() {
  ;(window as unknown as { hermesDesktop: { readFileText: typeof readFileText; writeTextFile: typeof writeTextFile } }).hermesDesktop = {
    readFileText,
    writeTextFile
  }
}

describe('goals-service', () => {
  beforeEach(() => {
    readFileText.mockReset()
    writeTextFile.mockReset()
  })

  afterEach(() => {
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  })

  describe('parseGoals', () => {
    it('parses a bare JSON array', () => {
      const goals = parseGoals(
        JSON.stringify([
          { id: '1', title: 'Ship the release', detail: '', status: 'active', horizon: 'short', created: 'a', updated: 'a' }
        ])
      )

      expect(goals).toHaveLength(1)
      expect(goals[0].title).toBe('Ship the release')
    })

    it('parses a {goals: [...]} wrapper', () => {
      const goals = parseGoals(
        JSON.stringify({
          goals: [{ id: '1', title: 'Wrapped', detail: '', status: 'paused', horizon: 'long', created: 'a', updated: 'a' }]
        })
      )

      expect(goals).toHaveLength(1)
      expect(goals[0].status).toBe('paused')
    })

    it('drops malformed entries instead of throwing', () => {
      expect(parseGoals('not json')).toEqual([])
      expect(parseGoals('')).toEqual([])
      expect(parseGoals(JSON.stringify([{ title: 'no id' }, { id: 'ok', title: 'valid' }]))).toHaveLength(1)
    })

    it('defaults unknown status/horizon values instead of rejecting the goal', () => {
      const [goal] = parseGoals(JSON.stringify([{ id: '1', title: 'x', status: 'bogus', horizon: 'bogus' }]))

      expect(goal.status).toBe('active')
      expect(goal.horizon).toBe('short')
    })
  })

  it('round-trips through serializeGoals + parseGoals', () => {
    const goal = createGoal({ title: 'Learn presence', detail: 'Ship workstream D', horizon: 'short' })
    const roundTripped = parseGoals(serializeGoals([goal]))

    expect(roundTripped).toEqual([goal])
  })

  describe('bridge availability', () => {
    it('is false with no bridge installed', () => {
      expect(isGoalsBridgeAvailable()).toBe(false)
    })

    it('is true once readFileText + writeTextFile are installed', () => {
      installBridge()
      expect(isGoalsBridgeAvailable()).toBe(true)
    })
  })

  describe('readGoals', () => {
    it('returns an empty list when the bridge is unavailable', async () => {
      expect(await readGoals()).toEqual([])
    })

    it('returns an empty list (not a throw) when the file read fails, e.g. ENOENT', async () => {
      installBridge()
      readFileText.mockRejectedValue(new Error('ENOENT'))

      expect(await readGoals()).toEqual([])
    })

    it('parses goals from a successful read', async () => {
      installBridge()
      const goal: Goal = createGoal({ title: 'Test', detail: '', horizon: 'long' })
      readFileText.mockResolvedValue({ text: serializeGoals([goal]) })

      expect(await readGoals()).toEqual([goal])
    })
  })

  describe('writeGoals', () => {
    it('throws when the bridge is unavailable, instead of silently no-oping', async () => {
      await expect(writeGoals([])).rejects.toThrow()
    })

    it('writes serialized goals to ~/.hermes/goals.json', async () => {
      installBridge()
      const goal = createGoal({ title: 'Persisted', detail: '', horizon: 'short' })

      await writeGoals([goal])

      expect(writeTextFile).toHaveBeenCalledWith('~/.hermes/goals.json', serializeGoals([goal]))
    })
  })
})
