import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { GraphTab } from './graph-tab'

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

const api = vi.fn()

class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
  ;(window as unknown as { hermesDesktop: { api: unknown } }).hermesDesktop = { api }
  api.mockResolvedValue({ nodes: [], edges: [] })
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
    window.setTimeout(() => callback(Date.now()), 0)
  )
  vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

const SUBGRAPH = {
  nodes: [
    {
      id: 1,
      type: 'project',
      label: 'NeuDocs',
      summary: 'A docs tool.',
      salience: 0.9,
      source_kind: 'memory',
      source_ref: 'memory:abc'
    },
    { id: 2, type: 'org', label: 'bakery-job', summary: '', salience: 0.5, source_kind: null, source_ref: null }
  ],
  edges: [{ src: 1, dst: 2, relation: 'funds', weight: 1 }]
}

describe('GraphTab', () => {
  it('shows the canonical empty state when there are no nodes and no note', async () => {
    render(<GraphTab />)

    expect(await screen.findByText("Marvi's mind map fills in as it connects what it learns.")).toBeTruthy()
  })

  it('prefers the backend-provided note when present', async () => {
    api.mockResolvedValue({ nodes: [], edges: [], note: 'Custom empty note from backend' })

    render(<GraphTab />)

    expect(await screen.findByText('Custom empty note from backend')).toBeTruthy()
  })

  it('renders the node count and node labels for a canned subgraph', async () => {
    api.mockResolvedValue(SUBGRAPH)

    render(<GraphTab />)

    expect(await screen.findByText('2 nodes')).toBeTruthy()
    expect(await screen.findByText('NeuDocs')).toBeTruthy()
    expect(await screen.findByText('bakery-job')).toBeTruthy()
  })

  it('shows an unavailable/offline message when the fetch fails and no data was ever loaded', async () => {
    api.mockRejectedValue(new Error('network down'))

    render(<GraphTab />)

    expect(await screen.findByText(/Graph is unavailable while the backend is offline/)).toBeTruthy()
  })

  it('clicking a node loads its neighbors and shows the detail panel', async () => {
    api.mockResolvedValue(SUBGRAPH)

    render(<GraphTab />)

    const nodeLabel = await screen.findByText('NeuDocs')
    const nodeGroup = nodeLabel.closest('[role="button"]') as HTMLElement
    expect(nodeGroup).toBeTruthy()

    api.mockResolvedValueOnce(SUBGRAPH)
    fireEvent.click(nodeGroup)

    expect(await screen.findByRole('heading', { name: 'NeuDocs' })).toBeTruthy()
    await waitFor(() => expect(screen.getByText('A docs tool.')).toBeTruthy())
    await waitFor(() => expect(screen.getAllByText(/funds/).length).toBeGreaterThan(0))
  })

  it('debounces a search query into the focus= request param', async () => {
    render(<GraphTab />)
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1))

    fireEvent.change(screen.getByPlaceholderText('Search or focus a node'), { target: { value: 'NeuDocs' } })

    await waitFor(() => expect(api).toHaveBeenCalledTimes(2))
    const lastCall = api.mock.calls[api.mock.calls.length - 1][0] as { path: string }
    expect(lastCall.path).toContain('focus=NeuDocs')
  })

  it('clicking a type chip requests that type filter', async () => {
    render(<GraphTab />)
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByRole('combobox', { name: 'Filter node type' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Project' }))

    await waitFor(() => expect(api).toHaveBeenCalledTimes(2))
    const lastCall = api.mock.calls[api.mock.calls.length - 1][0] as { path: string }
    expect(lastCall.path).toContain('type=project')
  })

  it('edits a selected node from the detail panel', async () => {
    api.mockResolvedValue(SUBGRAPH)
    render(<GraphTab />)

    const nodeGroup = (await screen.findByText('NeuDocs')).closest('[role="button"]') as HTMLElement
    fireEvent.click(nodeGroup)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByLabelText('Label'), { target: { value: 'NeuDocs 2' } })

    api.mockResolvedValueOnce({ ok: true, node: { ...SUBGRAPH.nodes[0], label: 'NeuDocs 2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(api).toHaveBeenCalledWith(
        expect.objectContaining({
          method: 'PUT',
          path: '/api/memory/graph/node',
          body: expect.objectContaining({ id: 1, label: 'NeuDocs 2' })
        })
      )
    )
    expect(await screen.findByRole('heading', { name: 'NeuDocs 2' })).toBeTruthy()
  })

  it('deletes a selected node after confirmation', async () => {
    api.mockResolvedValue(SUBGRAPH)
    render(<GraphTab />)

    const nodeGroup = (await screen.findByText('NeuDocs')).closest('[role="button"]') as HTMLElement
    fireEvent.click(nodeGroup)
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))

    api.mockResolvedValueOnce({ ok: true })
    const deleteButtons = screen.getAllByRole('button', { name: 'Delete' })
    fireEvent.click(deleteButtons[deleteButtons.length - 1]!)

    await waitFor(() =>
      expect(api).toHaveBeenCalledWith({ body: { id: 1 }, method: 'DELETE', path: '/api/memory/graph/node' })
    )
    expect(await screen.findByText('1 node')).toBeTruthy()
  })
})
