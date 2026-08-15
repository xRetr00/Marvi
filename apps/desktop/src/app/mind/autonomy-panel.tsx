import { useCallback, useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { MessageQuestion, Search, Zap } from '@/lib/icons'
import { relativeTime } from '@/lib/time'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'

import { Pill, SectionHeading } from '../settings/primitives'
import type { SubconsciousActivityRun } from '../settings/subconscious/activity-service'
import { useMarviConfig } from '../settings/subconscious/use-marvi-config'

interface BudgetCategory {
  limit: number
  remaining: number
  used: number
}

interface AutonomyBudget {
  categories: Record<string, BudgetCategory>
  daily_action_budget: number
  date: null | string
  enabled: boolean
  remaining_total: number
  used_total: number
}

interface PendingQuestion {
  answer_text?: null | string
  answered_at?: null | string
  asked_at: null | string
  category: string
  context?: string
  id: string
  question: string
  status: 'answered' | 'cancelled' | 'expired' | 'pending'
}

interface AutonomyStatus {
  budget: AutonomyBudget
  enabled: boolean
  ok: boolean
  pending_questions: PendingQuestion[]
  recent_actions: SubconsciousActivityRun[]
}

const CATEGORY_LABELS: Record<string, string> = {
  research: 'Self-directed research',
  browse: 'Browsing',
  ask_user: 'Asking you something'
}

const QUESTION_STATUS_META: Record<PendingQuestion['status'], { className: string; label: string }> = {
  pending: { label: 'Waiting', className: 'text-amber-500' },
  answered: { label: 'Answered', className: 'text-emerald-500' },
  expired: { label: 'Expired', className: 'text-muted-foreground' },
  cancelled: { label: 'Cancelled', className: 'text-muted-foreground' }
}

/**
 * Mind > Overview's "Autonomy" section (Marvi freedom spec §1.5). Shows
 * today's autonomy budget usage per category, recent autonomous actions
 * (research answered, questions asked), pending questions with their
 * status, and the master + per-category toggles. Backed by
 * `GET /api/autonomy/status`; toggles reuse the shared config channel
 * (`useMarviConfig`, `PUT /api/config`) the rest of Mind/Settings already
 * autosaves through, rather than a bespoke save button per field.
 */
export function AutonomyPanel() {
  const [status, setStatus] = useState<AutonomyStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const marvi = useMarviConfig()

  const load = useCallback(async () => {
    try {
      const response = await window.hermesDesktop.api<AutonomyStatus>({ path: '/api/autonomy/status' })

      setStatus(response)
    } catch (err) {
      notifyError(err, 'Failed to load Autonomy status')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const enabled = marvi.get('autonomy.enabled', status?.budget.enabled ?? true)
  const categories = status?.budget.categories ?? {}
  const pending = status?.pending_questions.filter(q => q.status === 'pending') ?? []
  const resolved = status?.pending_questions.filter(q => q.status !== 'pending').slice(0, 5) ?? []

  const answer = async (id: string) => {
    const value = answers[id]?.trim()

    if (!value) {return}

    try {
      await window.hermesDesktop.api({
        path: `/api/autonomy/questions/${encodeURIComponent(id)}/answer`,
        method: 'POST',
        body: { answer: value }
      })
      setAnswers(current => ({ ...current, [id]: '' }))
      await load()
    } catch (err) {
      notifyError(err, 'Failed to answer Autonomy question')
    }
  }

  return (
    <section>
      <SectionHeading
        icon={Zap}
        meta={status ? `${status.budget.used_total}/${status.budget.daily_action_budget} today` : undefined}
        title="Autonomy"
      />
      <p className="mb-3 text-xs text-muted-foreground">
        Marvi acting on its own between prompts — self-directed research and proactive questions. Bounded by a daily
        budget, logged here, and never silent about what it did.
      </p>

      <div className="mb-4 flex items-center justify-between gap-4 rounded-xl border border-(--ui-stroke-secondary) p-4">
        <div>
          <div className="text-sm font-medium">Enable autonomy</div>
          <div className="text-xs text-muted-foreground">Master switch — off stops all self-directed action.</div>
        </div>
        <Switch
          checked={Boolean(enabled)}
          disabled={marvi.isLoading || marvi.savingPath === 'autonomy.enabled'}
          onCheckedChange={value => void marvi.patch('autonomy.enabled', value).then(() => load())}
        />
      </div>

      {loading ? (
        <div className="rounded-lg border border-dashed p-6 text-center text-xs text-muted-foreground">
          Loading autonomy status…
        </div>
      ) : (
        <>
          <div className="mb-4 divide-y divide-(--ui-stroke-secondary) rounded-lg border border-(--ui-stroke-secondary)">
            {Object.keys(CATEGORY_LABELS).map(category => {
              const cat = categories[category]
              const limit = cat?.limit ?? 0
              const used = cat?.used ?? 0
              const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0

              return (
                <div className="flex items-center justify-between gap-4 p-3" key={category}>
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium">{CATEGORY_LABELS[category]}</div>
                    <div className="mt-1 h-1.5 w-full max-w-48 overflow-hidden rounded-full bg-(--ui-sidebar-surface-background)">
                      <div className={cn('h-full rounded-full bg-primary', pct >= 100 && 'bg-(--ui-red)')} style={{ width: `${pct}%` }} />
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Pill>
                      {used}/{limit} used
                    </Pill>
                    <Input
                      className="h-7 w-16 text-xs"
                      defaultValue={limit}
                      disabled={marvi.isLoading}
                      onBlur={event => {
                        const parsed = Number(event.target.value)

                        if (Number.isFinite(parsed) && parsed >= 0) {
                          void marvi.patch(`autonomy.per_category.${category}`, Math.round(parsed)).then(() => load())
                        }
                      }}
                      type="number"
                    />
                  </div>
                </div>
              )
            })}
          </div>

          <div className="mb-4 flex items-center justify-between gap-4 rounded-xl border border-(--ui-stroke-secondary) p-4">
            <div>
              <div className="text-sm font-medium">Quiet during deep work</div>
              <div className="text-xs text-muted-foreground">
                Hold proactive questions while you're heads-down (they still arrive once you step away).
              </div>
            </div>
            <Switch
              checked={Boolean(marvi.get('autonomy.ask.quiet_in_deep_work', true))}
              disabled={marvi.isLoading}
              onCheckedChange={value => void marvi.patch('autonomy.ask.quiet_in_deep_work', value).then(() => load())}
            />
          </div>

          <div className="mb-5">
            <SectionHeading icon={Search} meta={`${status?.recent_actions.length ?? 0} recent`} title="Recent autonomous actions" />
            {(status?.recent_actions.length ?? 0) === 0 ? (
              <div className="rounded-lg border border-dashed p-4 text-center text-xs text-muted-foreground">
                Nothing yet — Marvi hasn't spent any autonomy budget.
              </div>
            ) : (
              <ul className="divide-y divide-(--ui-stroke-secondary) rounded-lg border border-(--ui-stroke-secondary)">
                {status?.recent_actions.map((run, idx) => (
                  <li className="p-3 text-xs" key={`${run.at ?? 'unknown'}-${idx}`}>
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-muted-foreground">
                        {run.at ? relativeTime(new Date(run.at).getTime()) : 'unknown time'}
                      </span>
                      {run.outcome && <Pill>{run.outcome}</Pill>}
                    </div>
                    {run.summary && <p className="mt-1 text-foreground/80">{run.summary}</p>}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div>
            <SectionHeading icon={MessageQuestion} meta={`${pending.length} pending`} title="Pending questions" />
            {pending.length === 0 && resolved.length === 0 ? (
              <div className="rounded-lg border border-dashed p-4 text-center text-xs text-muted-foreground">
                Marvi hasn't proactively asked you anything yet.
              </div>
            ) : (
              <div className="divide-y divide-(--ui-stroke-secondary) rounded-lg border border-(--ui-stroke-secondary)">
                {[...pending, ...resolved].map(question => {
                  const meta = QUESTION_STATUS_META[question.status]

                  return (
                    <div className="p-3" key={question.id}>
                      <div className="flex items-start justify-between gap-3">
                        <p className="text-sm text-foreground">{question.question}</p>
                        <span className={cn('shrink-0 text-xs font-medium', meta.className)}>{meta.label}</span>
                      </div>
                      {question.answer_text && (
                        <p className="mt-1 text-xs text-muted-foreground">Reply: {question.answer_text}</p>
                      )}
                      {question.status === 'pending' && (
                        <div className="mt-2 flex gap-2">
                          <Input
                            aria-label={`Answer: ${question.question}`}
                            onChange={event => setAnswers(current => ({ ...current, [question.id]: event.target.value }))}
                            onKeyDown={event => {
                              if (event.key === 'Enter') {void answer(question.id)}
                            }}
                            placeholder="Your answer"
                            value={answers[question.id] ?? ''}
                          />
                          <Button disabled={!answers[question.id]?.trim()} onClick={() => void answer(question.id)} size="sm">
                            Answer
                          </Button>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>

          <div className="mt-3 flex justify-end">
            <Button onClick={() => void load()} size="sm" variant="ghost">
              Refresh
            </Button>
          </div>
        </>
      )}
    </section>
  )
}
