import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SubconsciousSuggestion } from './activity-service'
import { SuggestionsInbox } from './suggestions-inbox'

afterEach(() => {
  cleanup()
})

function makeSuggestion(overrides: Partial<SubconsciousSuggestion> = {}): SubconsciousSuggestion {
  return {
    id: 's1',
    title: 'Daily digest',
    summary: 'Summarize yesterday every morning.',
    source: 'subconscious',
    category: 'digest',
    tier: 'propose',
    created: new Date().toISOString(),
    ...overrides
  }
}

describe('SuggestionsInbox', () => {
  it('shows a load-failure state when unreachable', () => {
    render(
      <SuggestionsInbox
        busyId={null}
        isAvailable={false}
        isLoading={false}
        onAccept={vi.fn()}
        onDismiss={vi.fn()}
        suggestions={[]}
      />
    )

    expect(screen.getByText(/Couldn't load suggestions/)).toBeTruthy()
  })

  it('shows an informative empty state when there are no pending suggestions', () => {
    render(
      <SuggestionsInbox busyId={null} isAvailable={true} isLoading={false} onAccept={vi.fn()} onDismiss={vi.fn()} suggestions={[]} />
    )

    expect(screen.getByText('No pending suggestions right now.')).toBeTruthy()
  })

  it('renders a card per suggestion with title and summary', () => {
    render(
      <SuggestionsInbox
        busyId={null}
        isAvailable={true}
        isLoading={false}
        onAccept={vi.fn()}
        onDismiss={vi.fn()}
        suggestions={[makeSuggestion()]}
      />
    )

    expect(screen.getByText('Daily digest')).toBeTruthy()
    expect(screen.getByText('Summarize yesterday every morning.')).toBeTruthy()
  })

  it('renders the guarded before/after evidence for config proposals', () => {
    render(
      <SuggestionsInbox
        busyId={null}
        isAvailable={true}
        isLoading={false}
        onAccept={vi.fn()}
        onDismiss={vi.fn()}
        suggestions={[
          makeSuggestion({
            kind: 'config',
            config_spec: {
              path: 'voice.speaker_id.threshold',
              human: 'speaker owner threshold',
              current: 0.45,
              value: 0.4,
              rationale: 'Owner turns clustered below the current threshold.',
              scope: 'user'
            }
          })
        ]}
      />
    )

    expect(screen.getByText(/Change speaker owner threshold/)).toBeTruthy()
  })

  it('calls onAccept / onDismiss with the suggestion id', () => {
    const onAccept = vi.fn()
    const onDismiss = vi.fn()

    render(
      <SuggestionsInbox
        busyId={null}
        isAvailable={true}
        isLoading={false}
        onAccept={onAccept}
        onDismiss={onDismiss}
        suggestions={[makeSuggestion({ id: 'abc123' })]}
      />
    )

    fireEvent.click(screen.getByText('Accept'))
    expect(onAccept).toHaveBeenCalledWith('abc123')

    fireEvent.click(screen.getByText('Dismiss'))
    expect(onDismiss).toHaveBeenCalledWith('abc123')
  })

  it('disables the buttons for the busy suggestion only', () => {
    render(
      <SuggestionsInbox
        busyId="s1"
        isAvailable={true}
        isLoading={false}
        onAccept={vi.fn()}
        onDismiss={vi.fn()}
        suggestions={[makeSuggestion({ id: 's1' }), makeSuggestion({ id: 's2', title: 'Weekly report' })]}
      />
    )

    const acceptButtons = screen.getAllByText('Accept')
    expect(acceptButtons[0].hasAttribute('disabled')).toBe(true)
    expect(acceptButtons[1].hasAttribute('disabled')).toBe(false)
  })
})
