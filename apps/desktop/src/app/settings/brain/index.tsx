import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Brain, Search, X, Zap } from '@/lib/icons'
import { relativeTime } from '@/lib/time'
import { notify, notifyError } from '@/store/notifications'

import { Caption, ListRow, LoadingState, Pill, SectionHeading, SettingsContent, ToggleRow } from '../primitives'
import { StringListEditor } from '../subconscious/string-list-editor'

import { indexBrainNow, searchBrain, updateBrainConfig } from './brain-service'
import type { BrainConfigPatch, BrainSearchResult } from './brain-service'
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
  const [savingField, setSavingField] = useState<null | 'autoBuild' | 'enabled' | 'exclude' | 'folders'>(null)
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
    field: 'autoBuild' | 'enabled' | 'exclude' | 'folders',
    patch: BrainConfigPatch,
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

  // "Auto-build" is one master switch over three independent config flags
  // (brain.auto_discover + brain.collect.email/github) -- flipping it off
  // stops PC folder discovery and both document collectors in one action;
  // flipping it back on re-enables all three. No rollback state needed
  // beyond the shared `persist` optimistic-update/rollback plumbing.
  const autoBuildEnabled = status?.auto_discover ?? true

  function handleAutoBuildToggle(next: boolean) {
    void persist(
      'autoBuild',
      { auto_discover: next, collect: { email: next, github: next } },
      () => {},
      next ? 'Failed to enable auto-build' : 'Failed to disable auto-build'
    )
  }

  // Discovered folders the last "Brain indexer" pass found (see
  // tools/brain/discovery.py) -- hide any already excluded so "Remove"
  // reads as immediate even though the discovered list itself only
  // reconciles on the next (throttled, once/24h) discovery pass.
  const discoveredFolders = (status?.discovered_folders ?? []).filter(path => !exclude.includes(path))

  function handleRemoveDiscoveredFolder(path: string) {
    const previous = exclude
    const next = [...exclude, path]

    setExclude(next)
    void persist('exclude', { exclude: next }, () => setExclude(previous), 'Failed to exclude folder')
  }

  // Per-source collected-document counts (tools/brain/collected.py's
  // collected_counts()) rolled up into the three buckets the Brain tab
  // shows: email, github, and "agent" (everything brain_store_document
  // wrote, regardless of which caller's source string it used -- chat,
  // subconscious, reflection, dreaming, ...).
  const collected = status?.collected ?? {}
  const collectedEmail = collected.email ?? 0
  const collectedGithub = collected.github ?? 0

  const collectedAgent = Object.entries(collected).reduce(
    (sum, [source, count]) => (source === 'email' || source === 'github' ? sum : sum + count),
    0
  )

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

      <SectionHeading icon={Zap} title="Auto-build" />
      <Caption>
        Let Marvi grow this index on its own: scan your Documents/Desktop/Downloads for likely note folders, and pull
        README/docs from your GitHub repos and document-shaped email into the index — on top of whatever you list
        above.
      </Caption>
      <ToggleRow
        checked={autoBuildEnabled}
        description="Discovers folders, and collects from GitHub and email, on the same background schedule as the index."
        disabled={savingField === 'autoBuild'}
        label="Auto-build"
        onChange={handleAutoBuildToggle}
      />

      {discoveredFolders.length > 0 && (
        <ListRow
          below={
            <ul className="mt-2 flex flex-wrap gap-1.5">
              {discoveredFolders.map(path => (
                <li
                  className="flex items-center gap-1 rounded-[3px] bg-muted px-1.5 py-0.5 text-[0.7rem] text-foreground"
                  key={path}
                >
                  <span className="max-w-56 truncate">{path}</span>
                  <button
                    aria-label={`Remove ${path}`}
                    className="text-muted-foreground hover:text-destructive"
                    disabled={savingField === 'exclude'}
                    onClick={() => handleRemoveDiscoveredFolder(path)}
                    type="button"
                  >
                    <X className="size-3" />
                  </button>
                </li>
              ))}
            </ul>
          }
          description="Found automatically, indexed alongside your watched folders. Removing one excludes it from future discovery."
          title="Discovered folders"
        />
      )}

      <ListRow
        action={
          <div className="flex items-center gap-2">
            <Pill>Email {collectedEmail}</Pill>
            <Pill>GitHub {collectedGithub}</Pill>
            <Pill>Agent {collectedAgent}</Pill>
          </div>
        }
        description="Documents Marvi has pulled in from email, GitHub, and its own background thinking."
        title="Collected"
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
