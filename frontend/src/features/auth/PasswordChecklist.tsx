import { Check, Circle } from '@phosphor-icons/react'

import { cn } from '@/utils/cn'

import { PASSWORD_RULES } from './password-policy'

/**
 * The password rules, and which of them the current value satisfies.
 *
 * Stated up front rather than only after a failed submission, because a rule
 * discovered by rejection is a rule the user was asked to guess at. The list is
 * read from `PASSWORD_RULES` rather than written out again here, so a rule
 * cannot be shown as satisfied while the submit check disagrees — the two
 * would then be telling the same person different things about the same string.
 *
 * The state is carried in the icon *and* in text a screen reader can read: a
 * tick that only exists as a shape is decoration, and a satisfied-rule list
 * that is invisible to assistive technology is a list that only works for
 * people who can see the difference between a filled circle and a check.
 *
 * The list is named so it can be referred to as a thing rather than as a run of
 * sentences — "Password requirements" is what tells a listener what the two
 * items that follow are for.
 */
export function PasswordChecklist({
  password,
  className,
}: {
  password: string
  className?: string
}) {
  return (
    <ul aria-label="Password requirements" className={cn('flex flex-col gap-1', className)}>
      {PASSWORD_RULES.map((rule) => {
        const satisfied = rule.satisfied(password)

        return (
          <li
            key={rule.label}
            className={cn(
              'flex items-center gap-1.5 text-[0.8125rem]',
              'transition-colors duration-150 ease-out-expo',
              satisfied ? 'text-success-text' : 'text-ink-subtle',
            )}
          >
            <span aria-hidden="true" className="shrink-0 [&_svg]:size-3.5">
              {satisfied ? <Check weight="bold" /> : <Circle weight="light" />}
            </span>

            {/* The state lives in a nested span so that the rule itself stays
                the element's own text — which keeps it addressable by its
                exact wording rather than by "At least 12 characters, met". */}
            <span>
              {rule.label}
              <span className="sr-only">{satisfied ? ', met' : ', not yet met'}</span>
            </span>
          </li>
        )
      })}
    </ul>
  )
}
