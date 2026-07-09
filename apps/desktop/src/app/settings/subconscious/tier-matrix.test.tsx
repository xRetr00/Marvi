import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TierMatrix } from './tier-matrix'

afterEach(() => cleanup())

describe('TierMatrix', () => {
  it('shows an empty hint and suggested categories when no tiers are configured', () => {
    render(<TierMatrix onChange={vi.fn()} tiers={{}} />)

    expect(screen.getByText(/No categories configured yet/)).toBeTruthy()
    expect(screen.getByText('goals')).toBeTruthy()
  })

  it('adds a suggested category at the default "propose" tier on click', () => {
    const onChange = vi.fn()
    render(<TierMatrix onChange={onChange} tiers={{}} />)

    fireEvent.click(screen.getByText('goals'))

    expect(onChange).toHaveBeenCalledWith({ goals: 'propose' })
  })

  it('adds a custom category via the text input', () => {
    const onChange = vi.fn()
    render(<TierMatrix onChange={onChange} tiers={{}} />)

    fireEvent.change(screen.getByPlaceholderText('Category name'), { target: { value: 'Mail Sync' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    expect(onChange).toHaveBeenCalledWith({ mail_sync: 'propose' })
  })

  it('lists existing categories and removes one on trash click', () => {
    const onChange = vi.fn()
    render(<TierMatrix onChange={onChange} tiers={{ goals: 'auto', overnight_diff: 'notify' }} />)

    expect(screen.getByText('goals')).toBeTruthy()
    expect(screen.getByText('overnight_diff')).toBeTruthy()

    fireEvent.click(screen.getByLabelText('Remove goals'))

    expect(onChange).toHaveBeenCalledWith({ overnight_diff: 'notify' })
  })
})
