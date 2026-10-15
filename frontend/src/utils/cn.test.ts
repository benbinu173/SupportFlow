import { describe, expect, it } from 'vitest'

import { cn } from './cn'

describe('cn', () => {
  it('drops falsy values', () => {
    expect(cn('a', false, undefined, null, 'b')).toBe('a b')
  })

  it('resolves conflicting Tailwind utilities to the last one', () => {
    // The whole reason this wrapper exists rather than a template literal: a
    // component's own padding must be overridable by the caller's className
    // without either of them needing to know the other's class order.
    expect(cn('px-2 py-1', 'px-4')).toBe('py-1 px-4')
  })

  it('keeps utilities that only look like conflicts', () => {
    expect(cn('p-4', 'px-8')).toBe('p-4 px-8')
  })

  it('returns an empty string when everything is falsy', () => {
    expect(cn(false, undefined)).toBe('')
  })
})
