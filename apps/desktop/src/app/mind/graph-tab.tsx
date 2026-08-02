import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Loader } from '@/components/ui/loader'
import { SearchField } from '@/components/ui/search-field'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { useI18n } from '@/i18n'
import { Network, Pencil, Trash2 } from '@/lib/icons'
import { notifyError } from '@/store/notifications'

import { SectionHeading } from '../settings/primitives'

const NODE_TYPES = [
  'person',
  'project',
  'fact',
  'event',
  'preference',
  'place',
  'topic',
  'goal',
  'device',
  'org'
] as const

type GraphNodeType = (typeof NODE_TYPES)[number]

interface GraphNode {
  id: number
  label: string
  salience: number
  source_kind?: null | string
  source_ref?: null | string
  summary: string
  type: GraphNodeType
}

interface GraphEdge {
  dst: number
  relation: string
  src: number
  weight: number
}

interface GraphResponse {
  edges: GraphEdge[]
  nodes: GraphNode[]
  note?: string
}

const NODE_TYPE_COLORS: Record<GraphNodeType, string> = {
  person: 'var(--ui-orange)',
  project: 'var(--ui-blue)',
  fact: 'var(--ui-text-secondary)',
  event: 'var(--ui-red)',
  preference: 'var(--ui-cyan)',
  place: 'var(--ui-green)',
  topic: 'var(--ui-cyan)',
  goal: 'var(--ui-yellow)',
  device: 'var(--ui-purple)',
  org: 'var(--ui-red)'
}

function colorForType(type: GraphNodeType): string {
  return NODE_TYPE_COLORS[type] ?? 'var(--ui-text-secondary)'
}

function nodeRadius(salience: number): number {
  return 8 + Math.max(0, Math.min(1, salience)) * 10
}

// ---------------------------------------------------------------------------
// Dependency-free force-directed layout: a tiny velocity-based spring
// simulation (repulsion + edge springs + centering, alpha-decayed like
// d3-force's convention, but with zero external deps). Positions persist
// across re-layouts by node id so a re-fetch that keeps most of the same
// nodes doesn't reshuffle the whole picture.
// ---------------------------------------------------------------------------

interface SimPoint {
  id: number
  vx: number
  vy: number
  x: number
  y: number
}

function useForceLayout(nodeIds: number[], edgePairs: [number, number][], width: number, height: number) {
  const [positions, setPositions] = useState<Map<number, { x: number; y: number }>>(new Map())
  const simRef = useRef<Map<number, SimPoint>>(new Map())
  const nodeKey = nodeIds.join(',')
  const edgeKey = edgePairs.map(pair => pair.join('-')).join(',')

  // eslint-disable-next-line no-restricted-syntax -- force simulation positions are an imperative animation cache.
  useEffect(() => {
    if (width <= 0 || height <= 0) {
      return
    }

    const cx = width / 2
    const cy = height / 2
    const previous = simRef.current
    const next = new Map<number, SimPoint>()

    nodeIds.forEach((id, index) => {
      const existing = previous.get(id)

      if (existing) {
        next.set(id, existing)

        return
      }

      const angle = (index / Math.max(1, nodeIds.length)) * Math.PI * 2
      const radius = Math.min(width, height) * 0.28
      next.set(id, { id, vx: 0, vy: 0, x: cx + Math.cos(angle) * radius, y: cy + Math.sin(angle) * radius })
    })
    simRef.current = next

    let alpha = 1
    let raf = 0

    const tick = () => {
      const pts = Array.from(simRef.current.values())
      const n = pts.length

      for (let i = 0; i < n; i += 1) {
        for (let j = i + 1; j < n; j += 1) {
          const a = pts[i]!
          const b = pts[j]!
          let dx = a.x - b.x
          let dy = a.y - b.y
          let distSq = dx * dx + dy * dy

          if (distSq < 1) {
            dx = Math.random() - 0.5
            dy = Math.random() - 0.5
            distSq = 1
          }

          const dist = Math.sqrt(distSq)
          const force = (2200 * alpha) / distSq
          const fx = (dx / dist) * force
          const fy = (dy / dist) * force
          a.vx += fx
          a.vy += fy
          b.vx -= fx
          b.vy -= fy
        }
      }

      for (const [src, dst] of edgePairs) {
        const a = simRef.current.get(src)
        const b = simRef.current.get(dst)

        if (!a || !b) {
          continue
        }

        const dx = b.x - a.x
        const dy = b.y - a.y
        const dist = Math.max(1, Math.sqrt(dx * dx + dy * dy))
        const force = (dist - 130) * 0.02 * alpha
        const fx = (dx / dist) * force
        const fy = (dy / dist) * force
        a.vx += fx
        a.vy += fy
        b.vx -= fx
        b.vy -= fy
      }

      for (const p of pts) {
        p.vx += (cx - p.x) * 0.008 * alpha
        p.vy += (cy - p.y) * 0.008 * alpha
        p.vx *= 0.86
        p.vy *= 0.86
        p.x = Math.min(width - 24, Math.max(24, p.x + p.vx))
        p.y = Math.min(height - 24, Math.max(24, p.y + p.vy))
      }

      alpha *= 0.985
      setPositions(new Map(pts.map(p => [p.id, { x: p.x, y: p.y }])))

      if (alpha > 0.02) {
        raf = requestAnimationFrame(tick)
      }
    }

    raf = requestAnimationFrame(tick)

    return () => cancelAnimationFrame(raf)
    // Re-run only when the actual node/edge SET changes, or the viewport
    // resizes -- not on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeKey, edgeKey, width, height])

  return positions
}

function useMeasuredSize<T extends HTMLElement>() {
  const ref = useRef<null | T>(null)
  const [size, setSize] = useState({ height: 420, width: 760 })

  useEffect(() => {
    const el = ref.current

    if (!el) {
      return
    }

    const observer = new ResizeObserver(entries => {
      const entry = entries[0]

      if (!entry) {
        return
      }

      const { width, height } = entry.contentRect

      if (width > 0 && height > 0) {
        setSize({ width, height })
      }
    })

    observer.observe(el)

    return () => observer.disconnect()
  }, [])

  return [ref, size] as const
}

interface GraphNodeDraft {
  id: number
  label: string
  salience: number
  summary: string
  type: GraphNodeType
}

/** Interactive force-directed view over Marvi's editable knowledge graph. */
export function GraphTab() {
  const { t } = useI18n()
  const copy = t.mind.graph
  const [data, setData] = useState<GraphResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)
  const [query, setQuery] = useState('')
  const [typeFilter, setTypeFilter] = useState<null | GraphNodeType>(null)
  const [selectedId, setSelectedId] = useState<null | number>(null)
  const [neighborData, setNeighborData] = useState<GraphResponse | null>(null)
  const [editing, setEditing] = useState<GraphNodeDraft | null>(null)
  const [deleting, setDeleting] = useState<GraphNode | null>(null)
  const [saving, setSaving] = useState(false)
  const [editError, setEditError] = useState<null | string>(null)

  const load = useCallback(
    async (focus: string, type: null | GraphNodeType) => {
      setLoading(true)

      try {
        const params = new URLSearchParams({ depth: '2' })

        if (focus.trim()) {
          params.set('focus', focus.trim())
        }

        if (type) {
          params.set('type', type)
        }

        setData(await window.hermesDesktop.api<GraphResponse>({ path: `/api/memory/graph?${params.toString()}` }))
        setError(false)
      } catch (err) {
        setError(true)
        notifyError(err, copy.loadFailed)
      } finally {
        setLoading(false)
      }
    },
    [copy.loadFailed]
  )

  useEffect(() => {
    const handle = window.setTimeout(() => void load(query, typeFilter), 250)

    return () => window.clearTimeout(handle)
  }, [load, query, typeFilter])

  const nodes = useMemo(() => data?.nodes ?? [], [data])
  const edges = useMemo(() => data?.edges ?? [], [data])
  const nodesById = useMemo(() => new Map(nodes.map(node => [node.id, node])), [nodes])
  const selectedNode = selectedId !== null ? (nodesById.get(selectedId) ?? null) : null
  const nodeIds = useMemo(() => nodes.map(node => node.id), [nodes])

  const edgePairs = useMemo<[number, number][]>(
    () => edges.filter(edge => nodesById.has(edge.src) && nodesById.has(edge.dst)).map(edge => [edge.src, edge.dst]),
    [edges, nodesById]
  )

  const [containerRef, { width, height }] = useMeasuredSize<HTMLDivElement>()
  const positions = useForceLayout(nodeIds, edgePairs, width, height)

  async function selectNode(node: GraphNode) {
    setSelectedId(node.id)
    setNeighborData(null)

    try {
      const params = new URLSearchParams({ depth: '1', focus: node.label, type: node.type })

      setNeighborData(await window.hermesDesktop.api<GraphResponse>({ path: `/api/memory/graph?${params.toString()}` }))
    } catch (err) {
      notifyError(err, copy.connectionsFailed)
    }
  }

  const neighborLines = useMemo(() => {
    if (!selectedNode || !neighborData) {
      return []
    }

    const byId = new Map(neighborData.nodes.map(node => [node.id, node]))
    byId.set(selectedNode.id, selectedNode)

    return neighborData.edges.map(edge => ({
      dst: byId.get(edge.dst)?.label ?? '?',
      key: `${edge.src}-${edge.relation}-${edge.dst}`,
      relation: edge.relation,
      src: byId.get(edge.src)?.label ?? '?'
    }))
  }, [selectedNode, neighborData])

  async function saveEdit() {
    if (!editing || !editing.label.trim()) {
      return
    }

    setSaving(true)
    setEditError(null)

    try {
      const { node } = await window.hermesDesktop.api<{ node: GraphNode; ok: true }>({
        body: { ...editing, label: editing.label.trim(), summary: editing.summary.trim() },
        method: 'PUT',
        path: '/api/memory/graph/node'
      })

      setData(previous =>
        previous ? { ...previous, nodes: previous.nodes.map(item => (item.id === node.id ? node : item)) } : previous
      )
      setNeighborData(previous =>
        previous ? { ...previous, nodes: previous.nodes.map(item => (item.id === node.id ? node : item)) } : previous
      )
      setEditing(null)
    } catch (err) {
      setEditError(err instanceof Error ? err.message : copy.saveFailed)
    } finally {
      setSaving(false)
    }
  }

  async function deleteSelected() {
    if (!deleting) {
      return
    }

    const previous = data
    const id = deleting.id
    setData(current =>
      current
        ? {
            ...current,
            edges: current.edges.filter(edge => edge.src !== id && edge.dst !== id),
            nodes: current.nodes.filter(node => node.id !== id)
          }
        : current
    )
    setSelectedId(null)
    setNeighborData(null)

    try {
      await window.hermesDesktop.api({ body: { id }, method: 'DELETE', path: '/api/memory/graph/node' })
    } catch (err) {
      setData(previous)
      setSelectedId(id)
      throw err
    }
  }

  return (
    <div className="grid gap-5">
      <section>
        <SectionHeading icon={Network} meta={copy.nodeCount(nodes.length)} title={copy.title} />
        <p className="mb-3 text-xs text-muted-foreground">{copy.description}</p>
        <div className="flex items-center gap-3">
          <SearchField
            containerClassName="max-w-xs flex-1"
            loading={loading}
            onChange={setQuery}
            placeholder={copy.searchPlaceholder}
            value={query}
          />
          <Select
            onValueChange={value => setTypeFilter(value === 'all' ? null : (value as GraphNodeType))}
            value={typeFilter ?? 'all'}
          >
            <SelectTrigger aria-label={copy.filterLabel} className="w-36" size="sm">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{copy.allTypes}</SelectItem>
              {NODE_TYPES.map(type => (
                <SelectItem key={type} value={type}>
                  {copy.types[type]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </section>

      {error && nodes.length === 0 ? (
        <div className="py-8 text-center text-sm text-muted-foreground">
          {copy.unavailable}{' '}
          <Button onClick={() => void load(query, typeFilter)} size="inline" variant="textStrong">
            {t.common.retry}
          </Button>
        </div>
      ) : loading && nodes.length === 0 ? (
        <div className="grid place-items-center py-6">
          <Loader label={t.common.loading} type="lemniscate-bloom" />
        </div>
      ) : nodes.length === 0 ? (
        <div className="py-10 text-center text-xs text-muted-foreground">{data?.note || copy.empty}</div>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_19rem]">
          <div
            className="relative h-[30rem] w-full overflow-hidden rounded-xl border border-(--ui-stroke-tertiary) bg-(--ui-sidebar-surface-background)"
            ref={containerRef}
          >
            <svg aria-label={copy.canvasLabel} className="size-full" height={height} width={width}>
              <g>
                {edges.map(edge => {
                  const a = positions.get(edge.src)
                  const b = positions.get(edge.dst)

                  if (!a || !b) {
                    return null
                  }

                  return (
                    <g key={`${edge.src}-${edge.relation}-${edge.dst}`}>
                      <line
                        className="text-muted-foreground/20"
                        stroke="currentColor"
                        strokeWidth={Math.min(3, 0.6 + edge.weight * 0.3)}
                        x1={a.x}
                        x2={b.x}
                        y1={a.y}
                        y2={b.y}
                      />
                      <text
                        className="fill-muted-foreground/55"
                        fontSize={9}
                        textAnchor="middle"
                        x={(a.x + b.x) / 2}
                        y={(a.y + b.y) / 2}
                      >
                        {edge.relation}
                      </text>
                    </g>
                  )
                })}
              </g>
              <g>
                {nodes.map(node => {
                  const point = positions.get(node.id)

                  if (!point) {
                    return null
                  }

                  const selected = node.id === selectedId
                  const radius = nodeRadius(node.salience)

                  return (
                    <g
                      aria-label={`${node.label}, ${copy.types[node.type]}`}
                      className="cursor-pointer outline-none"
                      key={node.id}
                      onClick={() => void selectNode(node)}
                      onKeyDown={event => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault()
                          void selectNode(node)
                        }
                      }}
                      role="button"
                      tabIndex={0}
                    >
                      <circle
                        cx={point.x}
                        cy={point.y}
                        fill={colorForType(node.type)}
                        fillOpacity={selected ? 1 : 0.72}
                        r={radius}
                        stroke={selected ? 'var(--ui-text-primary)' : 'transparent'}
                        strokeWidth={selected ? 2 : 0}
                      />
                      <text
                        className="fill-foreground/90"
                        fontSize={10}
                        fontWeight={selected ? 600 : 400}
                        textAnchor="middle"
                        x={point.x}
                        y={point.y + radius + 11}
                      >
                        {node.label.length > 22 ? `${node.label.slice(0, 21)}…` : node.label}
                      </text>
                    </g>
                  )
                })}
              </g>
            </svg>
          </div>

          <aside className="min-h-52 border-l border-(--ui-stroke-tertiary) pl-4">
            {selectedNode ? (
              <div className="grid gap-4">
                <div>
                  <div className="flex items-center gap-1.5 text-[0.68rem] font-medium text-muted-foreground">
                    <span
                      aria-hidden
                      className="size-2 rounded-full"
                      style={{ backgroundColor: colorForType(selectedNode.type) }}
                    />
                    {copy.types[selectedNode.type]}
                  </div>
                  <h3 className="mt-1 text-sm font-semibold text-foreground">{selectedNode.label}</h3>
                </div>

                <div className="flex gap-1.5">
                  <Button
                    onClick={() => {
                      setEditError(null)
                      setEditing({
                        id: selectedNode.id,
                        label: selectedNode.label,
                        salience: selectedNode.salience,
                        summary: selectedNode.summary,
                        type: selectedNode.type
                      })
                    }}
                    size="xs"
                    variant="secondary"
                  >
                    <Pencil aria-hidden />
                    {copy.edit}
                  </Button>
                  <Button onClick={() => setDeleting(selectedNode)} size="xs" variant="ghost">
                    <Trash2 aria-hidden />
                    {copy.delete}
                  </Button>
                </div>

                {selectedNode.summary ? (
                  <p className="text-xs leading-5 text-muted-foreground">{selectedNode.summary}</p>
                ) : null}
                <div className="text-[0.68rem] text-muted-foreground">
                  {copy.salience(Math.round(selectedNode.salience * 100))}
                  {selectedNode.source_kind ? ` · ${copy.source(selectedNode.source_kind)}` : ''}
                </div>
                <div>
                  <h4 className="mb-2 text-[0.68rem] font-medium tracking-wide text-muted-foreground uppercase">
                    {copy.connections}
                  </h4>
                  {neighborLines.length === 0 ? (
                    <p className="text-xs text-muted-foreground">{copy.noConnections}</p>
                  ) : (
                    <ul className="grid gap-2">
                      {neighborLines.map(line => (
                        <li className="text-xs leading-4 text-foreground/85" key={line.key}>
                          {line.src} <span className="text-muted-foreground">—{line.relation}→</span> {line.dst}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            ) : (
              <div className="grid gap-4">
                <p className="text-xs leading-5 text-muted-foreground">{copy.selectHint}</p>
                <div className="grid grid-cols-2 gap-x-3 gap-y-2">
                  {NODE_TYPES.map(type => (
                    <div className="flex items-center gap-1.5 text-[0.68rem] text-muted-foreground" key={type}>
                      <span
                        aria-hidden
                        className="size-2 rounded-full"
                        style={{ backgroundColor: colorForType(type) }}
                      />
                      {copy.types[type]}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </aside>
        </div>
      )}

      <Dialog onOpenChange={open => !open && !saving && setEditing(null)} open={Boolean(editing)}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{copy.editTitle(editing?.label ?? '')}</DialogTitle>
          </DialogHeader>
          {editing ? (
            <div className="grid gap-4">
              <label className="grid gap-1.5 text-xs text-muted-foreground">
                {copy.label}
                <Input
                  autoFocus
                  maxLength={200}
                  onChange={event =>
                    setEditing(current => (current ? { ...current, label: event.target.value } : current))
                  }
                  value={editing.label}
                />
              </label>
              <label className="grid gap-1.5 text-xs text-muted-foreground">
                {copy.summary}
                <Textarea
                  className="min-h-28 resize-y"
                  maxLength={2000}
                  onChange={event =>
                    setEditing(current => (current ? { ...current, summary: event.target.value } : current))
                  }
                  value={editing.summary}
                />
              </label>
              <div className="grid grid-cols-2 gap-4">
                <label className="grid gap-1.5 text-xs text-muted-foreground">
                  {copy.type}
                  <Select
                    onValueChange={type =>
                      setEditing(current => (current ? { ...current, type: type as GraphNodeType } : current))
                    }
                    value={editing.type}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {NODE_TYPES.map(type => (
                        <SelectItem key={type} value={type}>
                          {copy.types[type]}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </label>
                <label className="grid gap-1.5 text-xs text-muted-foreground">
                  {copy.salience(Math.round(editing.salience * 100))}
                  <input
                    className="h-8 accent-primary"
                    max="1"
                    min="0"
                    onChange={event =>
                      setEditing(current => (current ? { ...current, salience: Number(event.target.value) } : current))
                    }
                    step="0.05"
                    type="range"
                    value={editing.salience}
                  />
                </label>
              </div>
              {editError ? <p className="text-xs text-destructive">{editError}</p> : null}
            </div>
          ) : null}
          <DialogFooter>
            <Button disabled={saving} onClick={() => setEditing(null)} variant="ghost">
              {t.common.cancel}
            </Button>
            <Button disabled={saving || !editing?.label.trim()} onClick={() => void saveEdit()}>
              {saving ? t.common.saving : t.common.save}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        confirmLabel={t.common.delete}
        description={copy.deleteDescription}
        destructive
        onClose={() => setDeleting(null)}
        onConfirm={deleteSelected}
        open={Boolean(deleting)}
        title={copy.deleteTitle(deleting?.label ?? '')}
      />
    </div>
  )
}
