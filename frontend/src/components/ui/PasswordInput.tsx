import { Eye, EyeSlash } from '@phosphor-icons/react'
import { useState, type KeyboardEvent } from 'react'

import { Input, type InputProps } from './Input'

export type PasswordInputProps = Omit<InputProps, 'type' | 'trailingAffordance'>

/**
 * A password field, with the two things every password field should have and
 * most do not.
 *
 * **A reveal toggle.** Typing a long passphrase into a masked field, on a
 * keyboard you are not used to, is the single most common reason people
 * mistype a password they know perfectly well. The risk of showing it is
 * shoulder-surfing on a screen the user is already looking at; the risk of not
 * showing it is a failed sign-in and a reset. It is off by default so the
 * choice stays theirs.
 *
 * **A Caps Lock warning.** A masked field gives no feedback, so Caps Lock
 * turns a correct password into a wrong one with nothing on screen to explain
 * it — and users read that as "the site is broken" rather than "my keyboard is
 * capitalised". It cannot be detected while idle, only from a keystroke, so the
 * warning appears when one arrives and clears when the field loses focus:
 * leaving it up after the key is off would be a false alarm printed next to a
 * form.
 *
 * `getModifierState` is read through an optional call because it is a real
 * method on a real `KeyboardEvent` and not every environment implements it. A
 * missing one should cost the warning, not the ability to type.
 */
export function PasswordInput({ id, onKeyDown, onKeyUp, onBlur, ...rest }: PasswordInputProps) {
  const [revealed, setRevealed] = useState(false)
  const [capsLockOn, setCapsLockOn] = useState(false)

  function readCapsLock(event: KeyboardEvent<HTMLInputElement>) {
    setCapsLockOn(event.getModifierState?.('CapsLock') ?? false)
  }

  return (
    <>
      <Input
        {...rest}
        id={id}
        type={revealed ? 'text' : 'password'}
        onKeyDown={(event) => {
          readCapsLock(event)
          onKeyDown?.(event)
        }}
        onKeyUp={(event) => {
          readCapsLock(event)
          onKeyUp?.(event)
        }}
        onBlur={(event) => {
          setCapsLockOn(false)
          onBlur?.(event)
        }}
        trailingAffordance={
          <button
            type="button"
            // The label changes with the state rather than staying fixed
            // alongside an `aria-pressed`, so the control is described by what
            // pressing it will do. Announcing "Show password, pressed" makes the
            // listener work out what "pressed" means here.
            aria-label={revealed ? 'Hide password' : 'Show password'}
            onClick={() => {
              setRevealed((wasRevealed) => !wasRevealed)
            }}
            className="grid size-7 place-items-center rounded-md text-ink-subtle transition-colors duration-150 ease-out-expo hover:text-ink focus-visible:text-ink"
          >
            {revealed ? (
              <EyeSlash weight="light" className="size-4" />
            ) : (
              <Eye weight="light" className="size-4" />
            )}
          </button>
        }
      />

      {capsLockOn && (
        <p aria-live="polite" className="text-[0.8125rem] text-warning-text">
          Caps Lock is on.
        </p>
      )}
    </>
  )
}
