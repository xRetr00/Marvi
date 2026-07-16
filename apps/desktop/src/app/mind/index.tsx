import { useCallback, useEffect, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import { Activity, Brain, Clock, FolderOpen, Link as LinkIcon, Search } from '@/lib/icons'
import { notify, notifyError } from '@/store/notifications'

import { Pill, SectionHeading } from '../settings/primitives'
import { ActivitySection } from '../settings/subconscious/activity-section'
import { fetchLearningSummary, type LearningSummaryResponse } from '../settings/subconscious/activity-service'
import { GoalsPanel, type GoalTemplate } from '../settings/subconscious/goals-panel'
import { KnowledgeViewer } from '../settings/subconscious/knowledge-viewer'
import { useMarviConfig } from '../settings/subconscious/use-marvi-config'

import { ComposioTab } from './composio-tab'
import { TimelineTab } from './timeline-tab'

interface Initiative {
  id: string
  detail: string
  trigger: string
  trigger_value?: string
  status: string
}

interface BrainState {
  enabled: boolean
  folders: string[]
  exclude: string[]
  schedule: string
  files: number
  chunks: number
  indexed_at: null | string
}

interface MindState {
  ok: boolean
  narrative: string
  initiatives: Initiative[]
  goal_templates: GoalTemplate[]
  brain: BrainState
}

interface BrainResult {
  path: string
  chunk_index: number
  snippet: string
  score: number
}

type MindTab = 'overview' | 'noticed' | 'goals' | 'brain' | 'knowledge' | 'composio' | 'timeline'

export function MindView() {
  const [tab, setTab] = useState<MindTab>('overview')
  const [state, setState] = useState<MindState | null>(null)
  const [error, setError] = useState(false)
  const [learning, setLearning] = useState<LearningSummaryResponse | null>(null)

  const load = useCallback(async () => {
    try {
      setState(await window.hermesDesktop.api<MindState>({ path: '/api/mind' }))

      try {
        setLearning(await fetchLearningSummary())
      } catch {
        setLearning(null)
      }

      setError(false)
    } catch (err) {
      setError(true)
      notifyError(err, 'Failed to load Mind')
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <section className="flex h-full min-h-0 flex-col bg-background">
      <header className="shrink-0 border-b border-border/50 px-6 pb-4 pt-[calc(var(--titlebar-height)+1rem)]">
        <div className="mx-auto max-w-5xl">
          <div className="flex items-center gap-3">
            <div className="grid size-9 place-items-center rounded-xl border border-primary/20 bg-primary/8 text-primary">
              <Brain className="size-5" />
            </div>
            <div>
              <h1 className="text-lg font-semibold tracking-tight">Mind</h1>
              <p className="text-xs text-muted-foreground">
                Marvi's goals, working model, local recall, and durable knowledge.
              </p>
            </div>
          </div>
          <Tabs className="mt-4" onValueChange={value => setTab(value as MindTab)} value={tab}>
            <TabsList>
              <TabsTrigger value="overview">Overview</TabsTrigger>
              <TabsTrigger value="noticed">
                <Activity className="mr-1.5 size-3.5" />
                Noticed
              </TabsTrigger>
              <TabsTrigger value="goals">Goals</TabsTrigger>
              <TabsTrigger value="brain">Brain</TabsTrigger>
              <TabsTrigger value="knowledge">What Marvi knows</TabsTrigger>
              <TabsTrigger value="timeline">
                <Clock className="mr-1.5 size-3.5" />
                Timeline
              </TabsTrigger>
              <TabsTrigger value="composio">
                <LinkIcon className="mr-1.5 size-3.5" />
                Composio
              </TabsTrigger>
            </TabsList>
          </Tabs>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-20 pt-5">
        <div className="mx-auto max-w-5xl">
          {error && !state ? (
            <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
              Mind is unavailable while the backend is offline.{' '}
              <button className="underline" onClick={() => void load()}>
                Retry
              </button>
            </div>
          ) : tab === 'overview' ? (
            <Overview learning={learning} onRefresh={load} state={state} />
          ) : tab === 'goals' ? (
            <GoalsPanel templates={state?.goal_templates ?? []} />
          ) : tab === 'noticed' ? (
            <div className="grid gap-3">
              <p className="text-xs text-muted-foreground">
                A durable inbox for proactive notices, Goblin shoulder taps, background thoughts, and suggestions you
                may have missed.
              </p>
              <ActivitySection />
            </div>
          ) : tab === 'brain' ? (
            <BrainPanel brain={state?.brain ?? null} onRefresh={load} />
          ) : tab === 'timeline' ? (
            <TimelineTab />
          ) : tab === 'composio' ? (
            <ComposioTab />
          ) : (
            <KnowledgeViewer />
          )}
        </div>
      </div>
    </section>
  )
}

function Overview({
  state,
  learning,
  onRefresh
}: {
  state: MindState | null
  learning: LearningSummaryResponse | null
  onRefresh: () => Promise<void>
}) {
  const pending = state?.initiatives.filter(item => item.status === 'pending') ?? []

  async function cancel(id: string) {
    try {
      await window.hermesDesktop.api({ path: `/api/mind/initiatives/${id}/cancel`, method: 'POST', body: {} })
      await onRefresh()
    } catch (err) {
      notifyError(err, 'Failed to cancel initiative')
    }
  }

  return (
    <div className="grid gap-7">
      <section>
        <SectionHeading icon={Brain} meta={state?.narrative ? 'Updated' : 'Learning'} title="Working model" />
        <p className="mb-3 text-xs text-muted-foreground">
          A private, bounded narrative Marvi refreshes after meaningful background thinking.
        </p>
        <div className="min-h-28 whitespace-pre-wrap rounded-xl border border-(--ui-stroke-secondary) bg-(--ui-sidebar-surface-background) p-4 text-sm leading-6 text-foreground/90">
          {state?.narrative ||
            'Marvi is still learning the threads that matter. The nightly reflection will build this without exposing private markers in chat.'}
        </div>
      </section>

      <section>
        <SectionHeading icon={Search} meta={`${pending.length} pending`} title="Initiatives" />
        <p className="mb-3 text-xs text-muted-foreground">
          Small follow-ups Marvi chose to revisit at the right time. At most three execute per day.
        </p>
        {pending.length === 0 ? (
          <div className="rounded-lg border border-dashed p-6 text-center text-xs text-muted-foreground">
            No pending initiatives.
          </div>
        ) : (
          <div className="divide-y divide-(--ui-stroke-secondary) rounded-lg border border-(--ui-stroke-secondary)">
            {pending.map(item => (
              <div className="flex items-start justify-between gap-4 p-3" key={item.id}>
                <div>
                  <div className="text-sm text-foreground">{item.detail}</div>
                  <div className="mt-1 flex gap-2">
                    <Pill>{item.trigger.replaceAll('_', ' ')}</Pill>
                    {item.trigger_value && <Pill>{item.trigger_value}</Pill>}
                  </div>
                </div>
                <Button onClick={() => void cancel(item.id)} size="sm" variant="ghost">
                  Cancel
                </Button>
              </div>
            ))}
          </div>
        )}
      </section>

      <LearningPanel learning={learning} onRefresh={onRefresh} />
    </div>
  )
}

const LEARNING_DESCRIPTIONS: Record<string, string> = {
  trust: 'Learns which suggestion categories have earned more or less autonomy.',
  room_habit: 'Notices repeated manual room modes, lighting changes, and cancellations.',
  voice_threshold: 'Tunes speaker recognition only after enough labelled voice evidence.',
  focus_apps: 'Finds applications where you repeatedly spend uninterrupted focus time.',
  escalation: 'Learns which voice requests should go straight to the deeper reasoning lane.',
  timing: 'Experiments with quieter proactive delivery windows; off until you enable it.'
}

function LearningPanel({
  learning,
  onRefresh
}: {
  learning: LearningSummaryResponse | null
  onRefresh: () => Promise<void>
}) {
  const marvi = useMarviConfig()
  const rows = learning?.loops ?? []

  return (
    <section>
      <SectionHeading
        icon={Brain}
        meta={`${rows.reduce((sum, row) => sum + row.pending, 0)} pending`}
        title="What Marvi is learning"
      />
      <p className="mb-3 text-xs text-muted-foreground">
        Local, consent-first learning loops. Every config or automation change waits in Suggestions for your approval.
      </p>
      {rows.length === 0 ? (
        <div className="rounded-lg border border-dashed p-6 text-center text-xs text-muted-foreground">
          Collecting data — first tuning proposal after a week of use.
        </div>
      ) : (
        <div className="divide-y divide-(--ui-stroke-secondary) rounded-lg border border-(--ui-stroke-secondary)">
          {rows.map(row => {
            const checked = marvi.get(row.config_path, row.enabled)

            return (
              <div className="flex items-center justify-between gap-4 p-3" key={row.loop}>
                <div className="min-w-0">
                  <div className="flex items-center gap-2 text-sm font-medium capitalize">
                    {row.loop.replaceAll('_', ' ')}
                    <Pill>{row.samples} samples</Pill>
                    {row.pending > 0 && <Pill>{row.pending} pending</Pill>}
                  </div>
                  <p className="mt-0.5 text-xs text-muted-foreground">{LEARNING_DESCRIPTIONS[row.loop]}</p>
                  {row.last_proposal && (
                    <p className="mt-1 truncate text-xs text-foreground/70">Last: {row.last_proposal}</p>
                  )}
                </div>
                <Switch
                  checked={Boolean(checked)}
                  disabled={marvi.isLoading || marvi.savingPath === row.config_path}
                  onCheckedChange={value => void marvi.patch(row.config_path, value).then(() => onRefresh())}
                />
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

function BrainPanel({ brain, onRefresh }: { brain: BrainState | null; onRefresh: () => Promise<void> }) {
  const [enabled, setEnabled] = useState(false)
  const [folders, setFolders] = useState('')
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<BrainResult[]>([])
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (brain) {
      setEnabled(brain.enabled)
      setFolders(brain.folders.join('\n'))
    }
  }, [brain])

  const parsedFolders = useMemo(
    () =>
      folders
        .split(/\r?\n/)
        .map(value => value.trim())
        .filter(Boolean),
    [folders]
  )

  async function save() {
    setBusy(true)

    try {
      await window.hermesDesktop.api({
        path: '/api/brain/config',
        method: 'PUT',
        body: { enabled, folders: parsedFolders, schedule: brain?.schedule || 'every 6h' }
      })
      notify({ kind: 'success', message: 'Brain settings saved' })
      await onRefresh()
    } catch (err) {
      notifyError(err, 'Failed to save Brain settings')
    } finally {
      setBusy(false)
    }
  }

  async function indexNow() {
    setBusy(true)

    try {
      const result = await window.hermesDesktop.api<{ indexed: number }>({
        path: '/api/brain/index',
        method: 'POST',
        body: {}
      })

      notify({ kind: 'success', message: `Indexed ${result.indexed} changed files` })
      await onRefresh()
    } catch (err) {
      notifyError(err, 'Brain indexing failed')
    } finally {
      setBusy(false)
    }
  }

  async function search() {
    if (!query.trim()) {
      return
    }

    try {
      const response = await window.hermesDesktop.api<{ results: BrainResult[] }>({
        path: `/api/brain/search?q=${encodeURIComponent(query)}`
      })

      setResults(response.results)
    } catch (err) {
      notifyError(err, 'Brain search failed')
    }
  }

  return (
    <div className="grid gap-6">
      <section>
        <SectionHeading icon={FolderOpen} meta={`${brain?.files ?? 0} files`} title="Local folders" />
        <p className="mb-3 text-xs text-muted-foreground">
          Private SQLite full-text recall. Only folders you list are indexed; no vectors or uploads.
        </p>
        <div className="rounded-xl border border-(--ui-stroke-secondary) p-4">
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-sm font-medium">Enable Brain recall</div>
              <div className="text-xs text-muted-foreground">Refresh changed files every 30 minutes.</div>
            </div>
            <Switch checked={enabled} onCheckedChange={setEnabled} />
          </div>
          <label className="mt-4 grid gap-1.5 text-xs font-medium">
            Folders, one per line
            <Textarea
              className="min-h-28 font-mono text-xs"
              onChange={event => setFolders(event.target.value)}
              placeholder="D:\\Projects\nC:\\Users\\me\\Documents"
              value={folders}
            />
          </label>
          <div className="mt-3 flex gap-2">
            <Button disabled={busy} onClick={() => void save()} size="sm">
              Save
            </Button>
            <Button disabled={busy || !enabled} onClick={() => void indexNow()} size="sm" variant="outline">
              Index now
            </Button>
          </div>
        </div>
      </section>

      <section>
        <SectionHeading icon={Search} meta={`${brain?.chunks ?? 0} passages`} title="Test recall" />
        <div className="flex gap-2">
          <Input
            onChange={event => setQuery(event.target.value)}
            onKeyDown={event => event.key === 'Enter' && void search()}
            placeholder="Search indexed files"
            value={query}
          />
          <Button onClick={() => void search()} variant="outline">
            Search
          </Button>
        </div>
        {results.length > 0 && (
          <div className="mt-3 divide-y divide-(--ui-stroke-secondary) rounded-lg border border-(--ui-stroke-secondary)">
            {results.map(result => (
              <div className="p-3" key={`${result.path}:${result.chunk_index}`}>
                <div className="truncate text-xs font-medium">{result.path}</div>
                <p className="mt-1 text-xs text-muted-foreground">{result.snippet}</p>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
