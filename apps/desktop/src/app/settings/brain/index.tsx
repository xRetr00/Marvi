import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Brain, Search } from '@/lib/icons'
import { relativeTime } from '@/lib/time'
import { notify, notifyError } from '@/store/notifications'

import { Caption, ListRow, LoadingState, Pill, SectionHeading, SettingsContent, ToggleRow } from '../primitives'
import { StringListEditor } from '../subconscious/string-list-editor'

import { indexBrainNow, searchBrain, updateBrainConfig } from './brain-service'
import type { BrainSearchResult } from './brain-service'
import { useBrainStatus } from './use-brain-status'

const DEFAULT_EXCLUDES_HINT = 'Default excludes always apply: .git, node_modules, venv, dist, build, __pycache__.'

// Marvi's local document-recall surface ("Brain tab" of Settings → Presence,
// see docs/superpowers/specs/2026-07-14-marvi-deep-subconscious-brain-design.md
// §7.3): watched-folder + exclude-pattern editors, index stats, a manual
// reindex trigger, and a search box hitting the same FTS5 index the
// recall_files tool queries. Mirrors ../subconscious/'s hook/service split
// (use-brain-status.ts + brain-service.ts) and reuses its primitives/list
// editor rather than inventing new ones.
export function BrainSettings() {
  const brain = useBrainStatus()

  if (brain.isLoading && !brain.status) {
    return <LoadingState label="Loading Brain settings" />
  }

  if (!brain.isAvailable && !brain.status) {
    return (
      <SettingsContent>
        <div className="grid min-h-48 place-items-center text-center text-sm text-muted-foreground">
          Couldn't load Brain settings.{' '}
          <button className="underline" onClick={() => void brain.refetch()} type="button">
            Retry
          </button>
        </div>
      </SettingsContent>
    )
  }

  return (
    <SettingsContent>
      <BrainCoreSettings brain={brain} />
    </SettingsContent>
  )
}

function renderSnippet(snippet: string) {
  // BrainStore.search wraps matched terms in literal '[' ']' (FTS5's
  // snippet() with those markers, see tools/brain/store.py) — render them as
  // highlighted spans instead of showing the raw brackets.
  return snippet.split(/(\[[^\]]*\])/g).map((part, index) =>
    part.startsWith('[') && part.endsWith(']') ? (
      <mark className="rounded-[2px] bg-primary/20 px-0.5 text-foreground" key={index}>
        {part.slice(1, -1)}
      </mark>
    ) : (
      <span key={index}>{part}</span>
    )
  )
}

function BrainCoreSettings({ brain }: { brain: ReturnType<typeof useBrainStatus> }) {
  const status = brain.status
  const [folders, setFolders] = useState<string[]>(status?.folders ?? [])
  const [exclude, setExclude] = useState<string[]>(status?.exclude ?? [])
  const [savingField, setSavingField] = useState<null | 'enabled' | 'exclude' | 'folders'>(null)
  const [reindexing, setReindexing] = useState(false)
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<BrainSearchResult[]>([])
  const [searching, setSearching] = useState(false)
  const [searched, setSearched] = useState(false)

  useEffect(() => {
    if (status) {
      setFolders(status.folders)
      setExclude(status.exclude)
    }
  }, [status])

  const enabled = status?.enabled ?? false
  const hasFolders = folders.length > 0

  async function persist(
    field: 'enabled' | 'exclude' | 'folders',
    patch: { enabled?: boolean; exclude?: string[]; folders?: string[] },
    rollback: () => void,
    errorLabel: string
  ) {
    setSavingField(field)

    try {
      await updateBrainConfig(patch)
      await brain.refetch()
    } catch (err) {
      rollback()
      notifyError(err, errorLabel)
    } finally {
      setSavingField(null)
    }
  }

  function handleFoldersChange(next: string[]) {
    const previous = folders

    setFolders(next)
    void persist('folders', { folders: next }, () => setFolders(previous), 'Failed to update watched folders')
  }

  function handleExcludeChange(next: string[]) {
    const previous = exclude

    setExclude(next)
    void persist('exclude', { exclude: next }, () => setExclude(previous), 'Failed to update exclude patterns')
  }

  function handleToggle(next: boolean) {
    void persist('enabled', { enabled: next }, () => {}, next ? 'Failed to enable Brain' : 'Failed to disable Brain')
  }

  async function reindexNow() {
    setReindexing(true)

    try {
      const result = await indexBrainNow()
      const errorSuffix = result.errors ? `, ${result.errors} error${result.errors === 1 ? '' : 's'}` : ''

      notify({ kind: 'success', message: `Indexed ${result.indexed} changed file${result.indexed === 1 ? '' : 's'}${errorSuffix}` })
      await brain.refetch()
    } catch (err) {
      notifyError(err, 'Brain reindex failed')
    } finally {
      setReindexing(false)
    }
  }

  async function runSearch() {
    const trimmed = query.trim()

    if (!trimmed) {
      return
    }

    setSearching(true)

    try {
      const response = await searchBrain(trimmed)
      setResults(response.results)
      setSearched(true)
    } catch (err) {
      notifyError(err, 'Brain search failed')
    } finally {
      setSearching(false)
    }
  }

  const lastRun = status?.last_run

  const lastRunLabel = lastRun?.at
    ? `${relativeTime(new Date(lastRun.at).getTime())}${lastRun.errors ? ` — ${lastRun.errors} error${lastRun.errors === 1 ? '' : 's'}` : ''}`
    : 'Never run yet'

  return (
    <>
      <SectionHeading icon={Brain} meta={`${status?.files ?? 0} files`} title="Brain" />
      <Caption>
        A private, local full-text index of the folders you list below — plain SQLite search, no vectors, nothing
        uploaded anywhere. Once indexed, Marvi can recall matching passages from chat, voice, and background
        thinking.
      </Caption>

      <ToggleRow
        checked={enabled}
        description={
          hasFolders
            ? 'Keep the index refreshed on a background schedule.'
            : 'Add a watched folder below before enabling — there is nothing to index yet.'
        }
        disabled={(!enabled && !hasFolders) || savingField === 'enabled'}
        label="Enable Brain"
        onChange={handleToggle}
      />

      {!enabled && !hasFolders && (
        <div className="mt-3 rounded-md border border-dashed border-(--ui-stroke-secondary) px-3 py-6 text-center text-xs text-muted-foreground">
          Brain is off — add a folder to give Marvi memory of your files.
        </div>
      )}

      <ListRow
        below={
          <div className="mt-2">
            <StringListEditor
              disabled={savingField === 'folders'}
              emptyLabel="No folders yet — add an absolute path to start indexing."
              onChange={handleFoldersChange}
              placeholder="D:\Projects\my-notes"
              values={folders}
            />
          </div>
        }
        description="Absolute paths only. Each addition is checked against disk on save — a path that doesn't exist is rejected."
        title="Watched folders"
      />

      <ListRow
        below={
          <div className="mt-2">
            <StringListEditor
              disabled={savingField === 'exclude'}
              emptyLabel={DEFAULT_EXCLUDES_HINT}
              onChange={handleExcludeChange}
              placeholder="*.min.js"
              values={exclude}
            />
          </div>
        }
        description="Glob substrings to skip while indexing, on top of the built-in defaults."
        title="Exclude patterns"
      />

      <ListRow
        action={
          <div className="flex items-center gap-2">
            <Pill>{status?.chunks ?? 0} passages</Pill>
            <Button disabled={reindexing || !hasFolders} onClick={() => void reindexNow()} size="sm" variant="outline">
              {reindexing ? 'Indexing…' : 'Reindex now'}
            </Button>
          </div>
        }
        description={`Last run: ${lastRunLabel}`}
        title="Index stats"
      />

      <div className="my-4 h-px bg-border/30" />

      <SectionHeading icon={Search} title="Search the index" />
      {!hasFolders ? (
        <div className="rounded-md border border-dashed border-(--ui-stroke-secondary) px-3 py-6 text-center text-xs text-muted-foreground">
          Nothing to search yet — add a watched folder first.
        </div>
      ) : (
        <>
          <div className="flex gap-2">
            <Input
              onChange={event => setQuery(event.target.value)}
              onKeyDown={event => {
                if (event.key === 'Enter') {
                  void runSearch()
                }
              }}
              placeholder="Search indexed files"
              value={query}
            />
            <Button disabled={searching || !query.trim()} onClick={() => void runSearch()} variant="outline">
              {searching ? 'Searching…' : 'Search'}
            </Button>
          </div>

          {searched && results.length === 0 ? (
            <p className="mt-3 text-xs text-muted-foreground">No matches for "{query.trim()}".</p>
          ) : null}

          {results.length > 0 ? (
            <div className="mt-3 divide-y divide-(--ui-stroke-secondary) rounded-md border border-(--ui-stroke-secondary)">
              {results.map(result => (
                <div className="p-3" key={`${result.path}:${result.chunk_index}`}>
                  <div className="truncate text-xs font-medium text-foreground">{result.path}</div>
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{renderSnippet(result.snippet)}</p>
                </div>
              ))}
            </div>
          ) : null}
        </>
      )}
    </>
  )
}
