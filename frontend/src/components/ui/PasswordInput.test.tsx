import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Field } from './Field'
import { PasswordInput } from './PasswordInput'

/**
 * The two behaviours a masked field owes its user.
 *
 * Neither of these can be checked from the page that renders this, because both
 * are about what the control does between keystrokes rather than about what a
 * submit sends. The Caps Lock case in particular is the one every hand-written
 * password field gets wrong: it is invisible in the DOM, so a test at the form
 * level would pass while a real user typed a correct password into a keyboard
 * that had been capitalising everything since the last time they touched it.
 */

function renderPasswordField(props: { onKeyDown?: () => void } = {}) {
  render(
    <Field label="Password" required>
      <PasswordInput {...props} />
    </Field>,
  )
}

describe('PasswordInput', () => {
  it('masks the value by default', () => {
    renderPasswordField()

    expect(screen.getByLabelText(/^Password\*?$/)).toHaveAttribute('type', 'password')
  })

  it('reveals on demand, and the label says which way the next press goes', async () => {
    const user = userEvent.setup()
    renderPasswordField()

    const field = screen.getByLabelText(/^Password\*?$/)

    await user.click(screen.getByRole('button', { name: 'Show password' }))
    expect(field).toHaveAttribute('type', 'text')

    // The name describes the next press rather than the current state. A fixed
    // label plus `aria-pressed` would announce "Show password, pressed" and
    // leave the listener to work out what "pressed" means here.
    await user.click(screen.getByRole('button', { name: 'Hide password' }))
    expect(field).toHaveAttribute('type', 'password')
  })

  it('toggles without submitting the form it sits in', () => {
    renderPasswordField()

    // The default for a `<button>` inside a `<form>` is `submit`, which would
    // make the reveal affordance send the form the first time anyone used it.
    expect(screen.getByRole('button', { name: 'Show password' })).toHaveAttribute(
      'type',
      'button',
    )
  })

  it('warns while Caps Lock is on, and stops when the field loses focus', async () => {
    const user = userEvent.setup()
    renderPasswordField()

    await user.click(screen.getByLabelText(/^Password\*?$/))

    expect(screen.queryByText('Caps Lock is on.')).not.toBeInTheDocument()

    await user.keyboard('{CapsLock}')
    await user.keyboard('a')

    expect(await screen.findByText('Caps Lock is on.')).toBeInTheDocument()

    // Cleared on blur rather than left standing: the warning is a claim about
    // the keyboard *right now*, and one that outlives the keystroke that proved
    // it is a false alarm printed next to a form.
    await user.tab()
    expect(screen.queryByText('Caps Lock is on.')).not.toBeInTheDocument()
  })

  it('passes the caller its own keyboard handlers', async () => {
    const onKeyDown = vi.fn()
    const user = userEvent.setup()
    renderPasswordField({ onKeyDown })

    await user.type(screen.getByLabelText(/^Password\*?$/), 'a')

    // The Caps Lock listener has to run on every keystroke without consuming
    // the event — a field that swallowed `onKeyDown` would break any caller
    // that had its own reason to watch the keyboard.
    expect(onKeyDown).toHaveBeenCalled()
  })
})
