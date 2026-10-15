import { IconContext } from '@phosphor-icons/react'
import type { ReactNode } from 'react'

/**
 * Sets the default icon weight and size for the whole tree.
 *
 * ADR-010 bans Lucide's and Material's thick strokes and names Phosphor Light
 * as the alternative, so the weight is set once here rather than repeated at
 * several hundred call sites — where, inevitably, a few would be forgotten and
 * the result would be a toolbar with two different stroke weights in it.
 *
 * Any icon can still override locally by passing `weight` directly, which is
 * what `Checkbox` does: a check mark inside a 16px box needs more weight to
 * survive than a 16px icon in open space does.
 */
export function IconProvider({ children }: { children: ReactNode }) {
  return (
    <IconContext.Provider value={{ weight: 'light', size: 16, mirrored: false }}>
      {children}
    </IconContext.Provider>
  )
}
