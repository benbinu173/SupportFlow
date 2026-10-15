import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { Checkbox } from './Checkbox'

describe('Checkbox', () => {
  it('takes its accessible name from the visible label', () => {
    render(<Checkbox label="Notify the customer" />)

    // The visible text *is* the accessible name, rather than a bare box with an
    // `aria-label` that then drifts from what is on screen.
    expect(screen.getByRole('checkbox', { name: 'Notify the customer' })).toBeInTheDocument()
  })

  it('toggles from the label text', async () => {
    const user = userEvent.setup()
    render(<Checkbox label="Notify the customer" />)

    const box = screen.getByRole('checkbox')

    await user.click(screen.getByText('Notify the customer'))

    expect(box).toBeChecked()
  })

  it('keeps the check glyph a direct sibling of the input', () => {
    // `peer-checked:` compiles to `:where(.peer):checked ~ *`, which only
    // reaches siblings. Nesting the glyph any deeper silently stops the tick
    // from appearing, with no type error and no failing assertion anywhere else
    // — so the structure itself is the thing worth pinning.
    const { container } = render(<Checkbox label="Notify the customer" />)
    const input = container.querySelector('input')

    expect(input?.nextElementSibling?.tagName.toLowerCase()).toBe('svg')
  })

  it('keeps the description out of the accessible name', () => {
    render(<Checkbox label="Auto-assign" description="Round-robin across the team." />)

    const box = screen.getByRole('checkbox')

    // The hint is *described*, not named. Wrapped inside the label it would
    // become part of the name — unskippable for a screen reader user, and
    // unmatchable for voice control saying "click Auto-assign". Both halves are
    // asserted so a regression that re-wraps the hint fails loudly.
    expect(box).toHaveAccessibleName('Auto-assign')
    expect(box).toHaveAccessibleDescription('Round-robin across the team.')
  })
})
