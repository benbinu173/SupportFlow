import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Button } from './Button'

describe('Button', () => {
  it('defaults to type="button"', () => {
    render(<Button>Save</Button>)

    // Not the HTML default of `submit`. A stray button silently submitting the
    // form around it is a bug that only appears in the one form that forgot to
    // say otherwise.
    expect(screen.getByRole('button', { name: 'Save' })).toHaveAttribute('type', 'button')
  })

  it('keeps the label mounted while loading', () => {
    render(<Button loading>Saving</Button>)

    // The label is hidden rather than unmounted, so the button does not collapse
    // to the spinner's width and move the thing the user was aiming at.
    expect(screen.getByText('Saving')).toHaveClass('invisible')
  })

  it('reports and enforces the loading state', () => {
    render(<Button loading>Saving</Button>)

    const button = screen.getByRole('button')

    expect(button).toHaveAttribute('aria-busy', 'true')
    expect(button).toBeDisabled()
  })

  it('leaves a caller-disabled button without aria-busy', () => {
    // Disabled and busy are different claims; only one of them is worth
    // announcing as a wait.
    render(<Button disabled>Save</Button>)

    const button = screen.getByRole('button')

    expect(button).toBeDisabled()
    expect(button).not.toHaveAttribute('aria-busy')
  })

  it('names itself from its text content', () => {
    render(<Button leadingIcon={<span aria-hidden="true">+</span>}>New ticket</Button>)

    expect(screen.getByRole('button', { name: 'New ticket' })).toBeInTheDocument()
  })
})
