import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { Input } from '@/components/ui/input'
import { Network } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'

import { SectionHeading } from '../settings/primitives'

interface GraphNode {
  id: number
  label: string
  salience: number
  source_kind?: null | string
  source_ref?: null | string
  summary: string
  type: string
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

const NODE_TYPE_LABELS: Record<string, string> = {
  person: 'Person',
  project: 'Project',
  fact: 'Fact',
  event: 'Event',
  preference: 'Preference',
  place: 'Place',
  topic: 'Topic',
  goal: 'Goal',
  device: 'Device',
  org: 'Org'
}

const NODE_TYPES = Object.keys(NODE_TYPE_LABELS)

const NODE_TYPE_COLORS: Record<string, string> = {
  person: '#f97316',
  project: '#6366f1',
  fact: '#64748b',
  event: '#ec4899',
  preference: '#14b8a6',
  place: '#22c55e',
  topic: '#38bdf8',
  goal: '#eab308',
  device: '#a855f7',
  org: '#ef4444'
}

const DEFAULT_NOTE = "Marvi's mind map fills in as it connects what it learns."

function colorForType(type: string): string {
  return NODE_TYPE_COLORS[type] ?? '#94a3b8'
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

/** The Mind page's "Graph" tab -- a read-only, interactive force-directed view over Marvi's knowledge graph. */
export function GraphTab() {
  const [data, setData] = useState<GraphResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)
  const [query, setQuery] = useState('')
  const [typeFilter, setTypeFilter] = useState<null | string>(null)
  const [selectedId, setSelectedId] = useState<null | number>(null)
  const [neighborData, setNeighborData] = useState<GraphResponse | null>(null)

  const load = useCallback(async (focus: string, type: null | string) => {
    setLoading(true)

    try {
      const params = new URLSearchParams()

      if (focus.trim()) {params.set('focus', focus.trim())}

      if (type) {params.set('type', type)}
      params.set('depth', '2')

      const response = await window.hermesDesktop.api<GraphResponse>({
        path: `/api/memory/graph?${params.toString()}`
      })

      setData(response)
      setError(false)
    } catch (err) {
      setError(true)
      notifyError(err, 'Failed to load Graph')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const handle = window.setTimeout(() => void load(query, typeFilter), 250)

    return () => window.clearTimeout(handle)
  }, [load, query, typeFilter])

  const nodes = useMemo(() => data?.nodes ?? [], [data])
  const edges = useMemo(() => data?.edges ?? [], [data])
  const nodesById = useMemo(() => new Map(nodes.map(n => [n.id, n])), [nodes])
  const selectedNode = selectedId !== null ? (nodesById.get(selectedId) ?? null) : null

  const nodeIds = useMemo(() => nodes.map(n => n.id), [nodes])

  const edgePairs = useMemo<[number, number][]>(
    () => edges.filter(e => nodesById.has(e.src) && nodesById.has(e.dst)).map(e => [e.src, e.dst]),
    [edges, nodesById]
  )

  const [containerRef, { width, height }] = useMeasuredSize<HTMLDivElement>()
  const positions = useForceLayout(nodeIds, edgePairs, width, height)

  async function selectNode(node: GraphNode) {
    setSelectedId(node.id)
    setNeighborData(null)

    try {
      const params = new URLSearchParams({ depth: '1', focus: node.label, type: node.type })

      const response = await window.hermesDesktop.api<GraphResponse>({
        path: `/api/memory/graph?${params.toString()}`
      })

      setNeighborData(response)
    } catch (err) {
      notifyError(err, 'Failed to load node connections')
    }
  }

  const neighborLines = useMemo(() => {
    if (!selectedNode || !neighborData) {
      return []
    }

    const byId = new Map(neighborData.nodes.map(n => [n.id, n]))
    byId.set(selectedNode.id, selectedNode)

    return neighborData.edges.map(edge => {
      const src = byId.get(edge.src)
      const dst = byId.get(edge.dst)

      return { key: `${edge.src}-${edge.relation}-${edge.dst}`, src: src?.label ?? '?', relation: edge.relation, dst: dst?.label ?? '?' }
    })
  }, [selectedNode, neighborData])

  return (
    <div className="grid gap-5">
      <section>
        <SectionHeading icon={Network} meta={`${nodes.length} node${nodes.length === 1 ? '' : 's'}`} title="Graph" />
        <p className="mb-3 text-xs text-muted-foreground">
          Marvi's knowledge graph -- people, projects, facts, events, and how they connect. Click a node for its
          summary, source, and neighbors.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <Input
            className="max-w-xs"
            onChange={event => setQuery(event.target.value)}
            placeholder="Search or focus a node"
            value={query}
          />
          <div className="flex flex-wrap gap-1.5">
            <button className={typeChipClass(typeFilter === null)} onClick={() => setTypeFilter(null)} type="button">
              All
            </button>
            {NODE_TYPES.map(type => (
              <button className={typeChipClass(typeFilter === type)} key={type} onClick={() => setTypeFilter(type)} type="button">
                {NODE_TYPE_LABELS[type]}
              </button>
            ))}
          </div>
        </div>
      </section>

      {error && nodes.length === 0 ? (
        <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
          Graph is unavailable while the backend is offline.{' '}
          <button className="underline" onClick={() => void load(query, typeFilter)} type="button">
            Retry
          </button>
        </div>
      ) : loading && nodes.length === 0 ? (
        <div className="px-3 py-6 text-center text-xs text-muted-foreground">Loading…</div>
      ) : nodes.length === 0 ? (
        <div className="rounded-md border border-dashed border-(--ui-stroke-secondary) px-3 py-10 text-center text-xs text-muted-foreground">
          {data?.note || DEFAULT_NOTE}
        </div>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[1fr_18rem]">
          <div
            className="relative h-[26rem] w-full overflow-hidden rounded-xl border border-(--ui-stroke-secondary) bg-(--ui-sidebar-surface-background)"
            ref={containerRef}
          >
            <svg className="size-full" height={height} width={width}>
              <g>
                {edges.map(edge => {
                  const a = positions.get(edge.src)
                  const b = positions.get(edge.dst)

                  if (!a || !b) {
                    return null
                  }

                  const mx = (a.x + b.x) / 2
                  const my = (a.y + b.y) / 2

                  return (
                    <g key={`${edge.src}-${edge.relation}-${edge.dst}`}>
                      <line
                        className="text-muted-foreground/25"
                        stroke="currentColor"
                        strokeWidth={Math.min(3, 0.6 + edge.weight * 0.3)}
                        x1={a.x}
                        x2={b.x}
                        y1={a.y}
                        y2={b.y}
                      />
                      <text className="fill-muted-foreground/60" fontSize={9} textAnchor="middle" x={mx} y={my}>
                        {edge.relation}
                      </text>
                    </g>
                  )
                })}
              </g>
              <g>
                {nodes.map(node => {
                  const p = positions.get(node.id)

                  if (!p) {
                    return null
                  }

                  const isSelected = node.id === selectedId
                  const r = nodeRadius(node.salience)

                  return (
                    <g
                      className="cursor-pointer"
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
                        cx={p.x}
                        cy={p.y}
                        fill={colorForType(node.type)}
                        fillOpacity={isSelected ? 0.95 : 0.75}
                        r={r}
                        stroke={isSelected ? 'white' : 'transparent'}
                        strokeWidth={isSelected ? 2 : 0}
                      />
                      <text
                        className="fill-foreground/90"
                        fontSize={10}
                        fontWeight={isSelected ? 600 : 400}
                        textAnchor="middle"
                        x={p.x}
                        y={p.y + r + 11}
                      >
                        {node.label.length > 22 ? `${node.label.slice(0, 21)}…` : node.label}
                      </text>
                    </g>
                  )
                })}
              </g>
            </svg>
          </div>

          <div className="rounded-xl border border-(--ui-stroke-secondary) p-4">
            {selectedNode ? (
              <div className="grid gap-3">
                <div>
                  <span
                    className="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[0.65rem] font-medium text-white"
                    style={{ backgroundColor: colorForType(selectedNode.type) }}
                  >
                    {NODE_TYPE_LABELS[selectedNode.type] ?? selectedNode.type}
                  </span>
                  <h3 className="mt-1.5 text-sm font-semibold text-foreground">{selectedNode.label}</h3>
                </div>
                {selectedNode.summary && <p className="text-xs text-muted-foreground">{selectedNode.summary}</p>}
                <div className="text-[0.65rem] text-muted-foreground">
                  Salience {Math.round(selectedNode.salience * 100)}%
                  {selectedNode.source_kind && ` · via ${selectedNode.source_kind}`}
                </div>
                <div>
                  <h4 className="mb-1.5 text-[0.68rem] font-medium tracking-wide text-muted-foreground uppercase">
                    Connections
                  </h4>
                  {neighborLines.length === 0 ? (
                    <p className="text-xs text-muted-foreground">No recorded connections yet.</p>
                  ) : (
                    <ul className="grid gap-1.5">
                      {neighborLines.map(line => (
                        <li className="text-xs text-foreground/85" key={line.key}>
                          {line.src} <span className="text-muted-foreground">—{line.relation}→</span> {line.dst}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">Click a node to see its summary, source, and neighbors.</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function typeChipClass(active: boolean) {
  return cn(
    'rounded-full border px-2.5 py-1 text-[0.65rem] font-medium transition',
    active
      ? 'border-primary/40 bg-primary/10 text-primary'
      : 'border-(--ui-stroke-secondary) text-muted-foreground hover:text-foreground'
  )
}
