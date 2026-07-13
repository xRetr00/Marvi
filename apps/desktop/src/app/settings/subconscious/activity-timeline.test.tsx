import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { SubconsciousActivityRun } from './activity-service'
import { ActivityTimeline } from './activity-timeline'

afterEach(() => {
  cleanup()
})

function run(overrides: Partial<SubconsciousActivityRun> = {}): SubconsciousActivityRun {
  return { at: new Date().toISOString(), source: 'tick', outcome: 'no_change', summary: null, ...overrides }
}

describe('ActivityTimeline', () => {
  it('shows a load-failure state when the backend is unreachable', () => {
    render(<ActivityTimeline isAvailable={false} isLoading={false} note={null} runs={[]} />)

    expect(screen.getByText(/Couldn't load recent activity/)).toBeTruthy()
  })

  it('shows an informative empty state — never a blank void — when there is no history at all', () => {
    render(<ActivityTimeline isAvailable={true} isLoading={false} note={null} runs={[]} />)

    expect(screen.getByText(/hasn't ticked yet/)).toBeTruthy()
  })

  it('shows the "last checked, nothing new" fallback line when only a bare last-run fact is available', () => {
    const runs = [run({ at: new Date(Date.now() - 4 * 60_000).toISOString(), outcome: null, summary: null })]

    render(<ActivityTimeline isAvailable={true} isLoading={false} note="No per-run history yet" runs={runs} />)

    expect(screen.getByText(/Subconscious is running — last checked/)).toBeTruthy()
    expect(screen.getByText('No per-run history yet')).toBeTruthy()
  })

  it('renders outcome chips for each run, newest first as given', () => {
    const runs = [
      run({ outcome: 'message', summary: 'Told you about the deploy' }),
      run({ outcome: 'no_change', summary: null }),
      run({ outcome: 'error', summary: 'boom' })
    ]

    render(<ActivityTimeline isAvailable={true} isLoading={false} note={null} runs={runs} />)

    expect(screen.getByText('Sent message')).toBeTruthy()
    expect(screen.getByText('Quiet')).toBeTruthy()
    expect(screen.getByText('Error')).toBeTruthy()
  })

  it('renders a source chip per row', () => {
    const runs = [run({ source: 'goblin' }), run({ source: 'distiller' }), run({ source: 'idle_trigger' })]

    render(<ActivityTimeline isAvailable={true} isLoading={false} note={null} runs={runs} />)
    const list = screen.getByRole('list')

    expect(within(list).getByText('Goblin')).toBeTruthy()
    expect(within(list).getByText('Distiller')).toBeTruthy()
    expect(within(list).getByText('Idle')).toBeTruthy()
  })

  it('expands a row with diff/thought to show "What changed" and "What Marvi thought/did"', () => {
    const runs = [
      run({
        outcome: 'message',
        summary: 'Told you about the deploy',
        diff: 'github: PR #42 merged to main',
        thought: 'The deploy pipeline finished — letting you know it shipped.'
      })
    ]

    render(<ActivityTimeline isAvailable={true} isLoading={false} note={null} runs={runs} />)

    expect(screen.queryByText(/PR #42 merged/)).toBeFalsy()
    fireEvent.click(screen.getByText('Sent message'))

    expect(screen.getByText('What changed')).toBeTruthy()
    expect(screen.getByText(/PR #42 merged/)).toBeTruthy()
    expect(screen.getByText('What Marvi thought/did')).toBeTruthy()
    expect(screen.getByText(/letting you know it shipped/)).toBeTruthy()
  })

  it('shows the bare [SILENT] thought honestly when the agent produced nothing more', () => {
    const runs = [run({ outcome: 'diff_silent', diff: 'gmail: 1 new message', thought: '[SILENT]' })]

    render(<ActivityTimeline isAvailable={true} isLoading={false} note={null} runs={runs} />)

    fireEvent.click(screen.getByText('Quiet'))

    expect(screen.getByText('What Marvi thought/did')).toBeTruthy()
    expect(screen.getByText('[SILENT]')).toBeTruthy()
  })

  it('shows the output_path for deep-dive when expanded', () => {
    const runs = [
      run({
        outcome: 'message',
        thought: 'hi',
        output_path: '/home/user/.hermes/cron/output/job1/2026-07-13_10-00-00.md'
      })
    ]

    render(<ActivityTimeline isAvailable={true} isLoading={false} note={null} runs={runs} />)

    fireEvent.click(screen.getByText('Sent message'))
    expect(screen.getByText('/home/user/.hermes/cron/output/job1/2026-07-13_10-00-00.md')).toBeTruthy()
  })

  it('does not toggle rows with nothing to expand', () => {
    const runs = [run({ outcome: 'no_change', summary: null, diff: null, thought: null })]

    render(<ActivityTimeline isAvailable={true} isLoading={false} note={null} runs={runs} />)

    const quietButton = screen.getByText('Quiet').closest('button')
    expect(quietButton?.hasAttribute('disabled')).toBe(true)
  })

  it('filters rows by source via the filter chips', () => {
    const runs = [
      run({ source: 'tick', outcome: 'no_change' }),
      run({ source: 'goblin', outcome: 'message', summary: 'stuck nudge' }),
      run({ source: 'distiller', outcome: 'diff_silent' })
    ]

    render(<ActivityTimeline isAvailable={true} isLoading={false} note={null} runs={runs} />)

    // All three visible by default.
    expect(within(screen.getByRole('list')).getAllByText(/Quiet|Sent message/)).toHaveLength(3)

    fireEvent.click(screen.getByRole('button', { name: 'Goblin' }))
    expect(within(screen.getByRole('list')).getAllByText(/Quiet|Sent message/)).toHaveLength(1)
    expect(within(screen.getByRole('list')).getByText('Sent message')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'All' }))
    expect(within(screen.getByRole('list')).getAllByText(/Quiet|Sent message/)).toHaveLength(3)
  })

  it('shows an informative empty state per-filter when nothing matches', () => {
    const runs = [run({ source: 'tick' })]

    render(<ActivityTimeline isAvailable={true} isLoading={false} note={null} runs={runs} />)

    fireEvent.click(screen.getByRole('button', { name: 'Goblin' }))
    expect(screen.getByText('No activity for this filter yet.')).toBeTruthy()
  })

  it('shows a loading state', () => {
    render(<ActivityTimeline isAvailable={true} isLoading={true} note={null} runs={[]} />)

    expect(screen.getByText('Loading tick history…')).toBeTruthy()
  })
})
