// Goals live in ~/.hermes/goals.json, owned by Workstream A's
// agent/goal_store.py. There is no REST endpoint for goals (Workstream A's
// scope is the CLI/tool surface, not a desktop API), so this reads/writes the
// file directly through the same local-file IPC channel the app already uses
// elsewhere for arbitrary text files (project file preview/edit — see
// `window.hermesDesktop.readFileText` / `writeTextFile` in electron/main.cjs,
// hardened via resolveReadableFileForIpc/resolveRequestedPathForIpc, which
// already expand a leading `~`). This mirrors what the task calls "the same
// mechanism the app uses for other local data".
//
// The goal store's exact on-disk container shape (bare array vs. `{goals:
// [...]}`) isn't nailed down by the spec beyond the field list, so `parseGoals`
// accepts either and `writeGoals` always writes a bare array — the simplest
// shape that round-trips cleanly. If Workstream A's store expects the wrapped
// form, the parser already tolerates it and only the write side would need a
// one-line change.
import type { Goal, GoalHorizon, GoalStatus } from './types'

export const GOALS_PATH = '~/.hermes/goals.json'

function isGoalStatus(value: unknown): value is GoalStatus {
  return value === 'active' || value === 'paused' || value === 'done'
}

function isGoalHorizon(value: unknown): value is GoalHorizon {
  return value === 'short' || value === 'long'
}

function coerceGoal(value: unknown): Goal | null {
  if (!value || typeof value !== 'object') {
    return null
  }

  const raw = value as Record<string, unknown>
  const id = typeof raw.id === 'string' ? raw.id : ''
  const title = typeof raw.title === 'string' ? raw.title : ''

  if (!id || !title) {
    return null
  }

  return {
    id,
    title,
    detail: typeof raw.detail === 'string' ? raw.detail : '',
    status: isGoalStatus(raw.status) ? raw.status : 'active',
    horizon: isGoalHorizon(raw.horizon) ? raw.horizon : 'short',
    created: typeof raw.created === 'string' ? raw.created : new Date(0).toISOString(),
    updated: typeof raw.updated === 'string' ? raw.updated : new Date(0).toISOString()
  }
}

export function parseGoals(text: string): Goal[] {
  const trimmed = text.trim()

  if (!trimmed) {
    return []
  }

  let data: unknown

  try {
    data = JSON.parse(trimmed)
  } catch {
    return []
  }

  const list = Array.isArray(data)
    ? data
    : data && typeof data === 'object' && Array.isArray((data as Record<string, unknown>).goals)
      ? (data as Record<string, unknown>).goals
      : []

  return (list as unknown[]).map(coerceGoal).filter((g): g is Goal => g !== null)
}

export function serializeGoals(goals: Goal[]): string {
  return JSON.stringify(goals, null, 2)
}

/** True when the local file-read/write bridge this service needs exists. */
export function isGoalsBridgeAvailable(): boolean {
  return (
    typeof window !== 'undefined' &&
    Boolean(window.hermesDesktop) &&
    typeof window.hermesDesktop.readFileText === 'function' &&
    typeof window.hermesDesktop.writeTextFile === 'function'
  )
}

export async function readGoals(): Promise<Goal[]> {
  if (!isGoalsBridgeAvailable()) {
    return []
  }

  try {
    const result = await window.hermesDesktop.readFileText(GOALS_PATH)

    return parseGoals(result.text)
  } catch {
    // File doesn't exist yet (no goals created) or isn't readable — either
    // way, an empty goal list is the correct, non-crashing state.
    return []
  }
}

export async function writeGoals(goals: Goal[]): Promise<void> {
  if (!isGoalsBridgeAvailable() || !window.hermesDesktop.writeTextFile) {
    throw new Error('Local file access is unavailable in this environment.')
  }

  await window.hermesDesktop.writeTextFile(GOALS_PATH, serializeGoals(goals))
}

function newGoalId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID()
  }

  return `goal_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`
}

export function createGoal(input: { title: string; detail: string; horizon: GoalHorizon }): Goal {
  const now = new Date().toISOString()

  return {
    id: newGoalId(),
    title: input.title.trim(),
    detail: input.detail.trim(),
    status: 'active',
    horizon: input.horizon,
    created: now,
    updated: now
  }
}
