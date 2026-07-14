import type { FormEvent, ReactNode } from 'react'
import { useEffect, useState } from 'react'

import { PageLoader } from '@/components/page-loader'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { triggerHaptic } from '@/lib/haptics'
import { Check, Pause, Pencil, Play, Plus, Trash2 } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notify, notifyError } from '@/store/notifications'

import { CONTROL_TEXT } from '../constants'
import { EmptyState, Pill } from '../primitives'

import { createGoal, isGoalsBridgeAvailable, readGoals, writeGoals } from './goals-service'
import type { Goal, GoalHorizon, GoalStatus } from './types'

const STATUS_ORDER: GoalStatus[] = ['active', 'paused', 'done']
const STATUS_LABEL: Record<GoalStatus, string> = { active: 'Active', paused: 'Paused', done: 'Done' }
const HORIZON_LABEL: Record<GoalHorizon, string> = { short: 'Short-term', long: 'Long-term' }

export interface GoalTemplate {
  id: string
  title: string
  detail: string
  horizon: GoalHorizon
}

type EditorState = { mode: 'closed' } | { mode: 'create'; template?: GoalTemplate } | { goal: Goal; mode: 'edit' }

/** Goals panel: list/add/edit/complete goals backed by ~/.hermes/goals.json. */
export function GoalsPanel({ templates = [] }: { templates?: GoalTemplate[] }) {
  const [goals, setGoals] = useState<Goal[] | null>(null)
  const [loadError, setLoadError] = useState(false)
  const [editor, setEditor] = useState<EditorState>({ mode: 'closed' })
  const [pendingDelete, setPendingDelete] = useState<Goal | null>(null)
  const [busyId, setBusyId] = useState<null | string>(null)
  const bridgeAvailable = isGoalsBridgeAvailable()

  const load = async () => {
    try {
      setGoals(await readGoals())
      setLoadError(false)
    } catch (err) {
      setLoadError(true)
      notifyError(err, 'Failed to load goals')
    }
  }

  useEffect(() => {
    if (bridgeAvailable) {
      void load()
    } else {
      setGoals([])
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- load once on mount
  }, [])

  async function persist(next: Goal[], successMessage?: string) {
    const previous = goals
    setGoals(next)

    try {
      await writeGoals(next)

      if (successMessage) {
        notify({ kind: 'success', message: successMessage })
      }
    } catch (err) {
      setGoals(previous)
      notifyError(err, 'Failed to save goals')
    }
  }

  async function handleSetStatus(goal: Goal, status: GoalStatus) {
    if (!goals) {
      return
    }

    setBusyId(goal.id)
    triggerHaptic('selection')

    await persist(
      goals.map(g => (g.id === goal.id ? { ...g, status, updated: new Date().toISOString() } : g)),
      status === 'done' ? `Marked "${goal.title}" done` : undefined
    )
    setBusyId(null)
  }

  async function handleDelete() {
    if (!goals || !pendingDelete) {
      return
    }

    const target = pendingDelete
    setPendingDelete(null)
    await persist(
      goals.filter(g => g.id !== target.id),
      `Deleted "${target.title}"`
    )
  }

  async function handleEditorSave(values: { title: string; detail: string; horizon: GoalHorizon }) {
    if (!goals) {
      return
    }

    if (editor.mode === 'create') {
      const created = createGoal(values)
      await persist([...goals, created], `Added "${created.title}"`)
    } else if (editor.mode === 'edit') {
      await persist(
        goals.map(g =>
          g.id === editor.goal.id
            ? { ...g, title: values.title.trim(), detail: values.detail.trim(), horizon: values.horizon, updated: new Date().toISOString() }
            : g
        ),
        `Saved "${values.title.trim()}"`
      )
    }

    setEditor({ mode: 'closed' })
  }

  if (!bridgeAvailable) {
    return (
      <EmptyState
        description="Goals need local file access, which isn't available in this environment."
        title="Goals unavailable"
      />
    )
  }

  if (goals === null) {
    return <PageLoader className="min-h-32" label="Loading goals" />
  }

  if (loadError && goals.length === 0) {
    return (
      <div className="grid min-h-32 place-items-center text-center">
        <div>
          <div className="text-sm font-medium">Couldn't load goals</div>
          <Button className="mt-2" onClick={() => void load()} size="sm" variant="outline">
            Retry
          </Button>
        </div>
      </div>
    )
  }

  const grouped = STATUS_ORDER.map(status => ({ status, goals: goals.filter(g => g.status === status) })).filter(
    group => group.goals.length > 0
  )

  return (
    <div className="grid gap-3">
      {goals.length === 0 ? (
        <div className="rounded-md border border-dashed border-(--ui-stroke-secondary) px-3 py-6 text-center text-xs text-muted-foreground">
          No goals yet. Goals steer Marvi's subconscious tick — add one to give it direction.
        </div>
      ) : (
        <div className="grid gap-4">
          {grouped.map(group => (
            <div className="grid gap-1" key={group.status}>
              <div className="flex items-center gap-2 px-0.5 text-[0.68rem] font-medium tracking-wide text-muted-foreground uppercase">
                {STATUS_LABEL[group.status]}
                <span className="text-muted-foreground/50">{group.goals.length}</span>
              </div>
              <div className="divide-y divide-(--ui-stroke-secondary) rounded-md border border-(--ui-stroke-secondary)">
                {group.goals.map(goal => (
                  <GoalRow
                    busy={busyId === goal.id}
                    goal={goal}
                    key={goal.id}
                    onComplete={() => void handleSetStatus(goal, 'done')}
                    onDelete={() => setPendingDelete(goal)}
                    onEdit={() => setEditor({ goal, mode: 'edit' })}
                    onTogglePause={() => void handleSetStatus(goal, goal.status === 'paused' ? 'active' : 'paused')}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      <Button className="w-fit gap-1.5" onClick={() => setEditor({ mode: 'create' })} size="sm" variant="outline">
        <Plus className="size-3.5" />
        Add goal
      </Button>

      {templates.length > 0 && (
        <div className="grid gap-2 sm:grid-cols-2">
          {templates.map(template => (
            <button
              className="rounded-lg border border-(--ui-stroke-secondary) p-3 text-left transition-colors hover:bg-(--ui-control-hover-background)"
              key={template.id}
              onClick={() => setEditor({ mode: 'create', template })}
              type="button"
            >
              <div className="text-xs font-medium text-foreground">{template.title}</div>
              <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{template.detail}</p>
            </button>
          ))}
        </div>
      )}

      <GoalEditorDialog editor={editor} onClose={() => setEditor({ mode: 'closed' })} onSave={handleEditorSave} />

      <Dialog onOpenChange={open => !open && setPendingDelete(null)} open={pendingDelete !== null}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Delete goal</DialogTitle>
            <DialogDescription>
              {pendingDelete ? (
                <>
                  Delete <span className="font-medium text-foreground">{pendingDelete.title}</span>? This can't be undone.
                </>
              ) : null}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button onClick={() => setPendingDelete(null)} variant="outline">
              Cancel
            </Button>
            <Button onClick={() => void handleDelete()} variant="destructive">
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

function GoalRow({
  goal,
  busy,
  onComplete,
  onTogglePause,
  onEdit,
  onDelete
}: {
  goal: Goal
  busy: boolean
  onComplete: () => void
  onTogglePause: () => void
  onEdit: () => void
  onDelete: () => void
}) {
  const isPaused = goal.status === 'paused'
  const isDone = goal.status === 'done'

  return (
    <div className="flex items-start justify-between gap-3 px-3 py-2.5">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className={cn('truncate text-sm font-medium text-foreground', isDone && 'line-through opacity-60')}>
            {goal.title}
          </span>
          <Pill>{HORIZON_LABEL[goal.horizon]}</Pill>
        </div>
        {goal.detail && <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">{goal.detail}</p>}
      </div>

      <div className="flex shrink-0 items-center gap-0.5">
        {!isDone && (
          <>
            <Button
              aria-label={isPaused ? 'Resume goal' : 'Pause goal'}
              disabled={busy}
              onClick={onTogglePause}
              size="icon-xs"
              title={isPaused ? 'Resume' : 'Pause'}
              type="button"
              variant="ghost"
            >
              {isPaused ? <Play /> : <Pause />}
            </Button>
            <Button
              aria-label="Mark done"
              disabled={busy}
              onClick={onComplete}
              size="icon-xs"
              title="Mark done"
              type="button"
              variant="ghost"
            >
              <Check />
            </Button>
            <Button aria-label="Edit goal" onClick={onEdit} size="icon-xs" title="Edit" type="button" variant="ghost">
              <Pencil />
            </Button>
          </>
        )}
        <Button
          aria-label="Delete goal"
          className="hover:text-destructive"
          disabled={busy}
          onClick={onDelete}
          size="icon-xs"
          title="Delete"
          type="button"
          variant="ghost"
        >
          <Trash2 />
        </Button>
      </div>
    </div>
  )
}

function GoalEditorDialog({
  editor,
  onClose,
  onSave
}: {
  editor: EditorState
  onClose: () => void
  onSave: (values: { title: string; detail: string; horizon: GoalHorizon }) => Promise<void>
}) {
  const open = editor.mode !== 'closed'
  const isEdit = editor.mode === 'edit'
  const initial = isEdit ? editor.goal : editor.mode === 'create' ? editor.template : null

  const [title, setTitle] = useState('')
  const [detail, setDetail] = useState('')
  const [horizon, setHorizon] = useState<GoalHorizon>('short')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<null | string>(null)

  useEffect(() => {
    if (!open) {
      return
    }

    setTitle(initial?.title ?? '')
    setDetail(initial?.detail ?? '')
    setHorizon(initial?.horizon ?? 'short')
    setError(null)
    setSaving(false)
  }, [initial, open])

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    const trimmedTitle = title.trim()

    if (!trimmedTitle) {
      setError('Title is required.')

      return
    }

    setSaving(true)
    setError(null)

    try {
      await onSave({ title: trimmedTitle, detail, horizon })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save goal.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog onOpenChange={value => !value && !saving && onClose()} open={open}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit goal' : 'New goal'}</DialogTitle>
          <DialogDescription>Goals steer Marvi's subconscious tick toward what you actually want.</DialogDescription>
        </DialogHeader>

        <form className="grid gap-4" onSubmit={handleSubmit}>
          <GoalField htmlFor="goal-title" label="Title">
            <Input autoFocus id="goal-title" onChange={e => setTitle(e.target.value)} value={title} />
          </GoalField>

          <GoalField htmlFor="goal-detail" label="Detail" optional>
            <Textarea
              className="min-h-20"
              id="goal-detail"
              onChange={e => setDetail(e.target.value)}
              placeholder="What does progress look like?"
              value={detail}
            />
          </GoalField>

          <GoalField htmlFor="goal-horizon" label="Horizon">
            <Select onValueChange={value => setHorizon(value as GoalHorizon)} value={horizon}>
              <SelectTrigger className={cn('h-9', CONTROL_TEXT)} id="goal-horizon">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="short">{HORIZON_LABEL.short}</SelectItem>
                <SelectItem value="long">{HORIZON_LABEL.long}</SelectItem>
              </SelectContent>
            </Select>
          </GoalField>

          {error && <p className="text-xs text-destructive">{error}</p>}

          <DialogFooter>
            <Button disabled={saving} onClick={onClose} type="button" variant="outline">
              Cancel
            </Button>
            <Button disabled={saving} type="submit">
              {saving ? 'Saving…' : isEdit ? 'Save changes' : 'Add goal'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function GoalField({
  children,
  htmlFor,
  label,
  optional
}: {
  children: ReactNode
  htmlFor: string
  label: string
  optional?: boolean
}) {
  return (
    <div className="grid gap-1.5">
      <label className="flex items-baseline gap-2 text-xs font-medium text-foreground" htmlFor={htmlFor}>
        {label}
        {optional && <span className="text-[0.65rem] font-normal text-muted-foreground">optional</span>}
      </label>
      {children}
    </div>
  )
}
