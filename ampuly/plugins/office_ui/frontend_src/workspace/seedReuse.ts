// The builder re-seeds React Flow from the node table on every edit. Handing it a fresh object for every card makes
// it re-render every card and wire: at 300 steps one keystroke in a name cost ~190 ms (4.7 plan §353 wants ≤100 ms).
// A card whose seed did not change keeps the object React Flow already holds -- which also carries what React Flow
// added itself (its measured size, whether it is selected) -- so only the card that changed is drawn again.

/** Equal as data, with one rule for functions: any function equals any other.
 *
 *  Seeds are rebuilt on every render, so each card's callbacks are new closures each time. They are only ever
 *  wrappers around the canvas's stable callbacks, bound to the card's own id (`commit: (f, v) => cardEditCommit(id,
 *  f, v)`); two seeds for one id that agree on everything else do the same thing. A callback that closed over
 *  anything else would have to show that difference in the data too. */
export function sameData(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (typeof a === 'function' && typeof b === 'function') return true
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false
    for (let index = 0; index < a.length; index++) if (!sameData(a[index], b[index])) return false
    return true
  }
  const left = a as Record<string, unknown>
  const right = b as Record<string, unknown>
  const keys = Object.keys(left)
  if (keys.length !== Object.keys(right).length) return false
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(right, key) || !sameData(left[key], right[key])) return false
  }
  return true
}

/** The seed last handed over, for one workflow: another workflow's cards are never taken for this one's. */
export interface SeedMemory<T> {
  scope: string
  byId: ReadonlyMap<string, T>
}

export function seedMemory<T extends { id: string }>(scope: string, seed: readonly T[]): SeedMemory<T> {
  return { scope, byId: new Map(seed.map(item => [item.id, item])) }
}

/** What React Flow should hold after a re-seed: for each item of the new seed, the object it already holds when that
 *  item's seed is unchanged since last time, the new seed otherwise -- passed through `carry` with the held object,
 *  when there is one, so what React Flow added to it is not lost. In the new seed's order (a lane before its cards).
 *  Returns `current` itself when nothing changed, so React does not render at all. */
export function keepUnchanged<T extends { id: string }>(
  previous: SeedMemory<T> | null, scope: string, seed: readonly T[], current: readonly T[], carry?: (fresh: T, held: T) => T,
): T[] {
  const live = previous && previous.scope === scope ? new Map(current.map(item => [item.id, item])) : null
  const next = seed.map(item => {
    const before = previous?.byId.get(item.id)
    const held = live?.get(item.id)
    if (!held) return item
    return before && sameData(before, item) ? held : carry ? carry(item, held) : item
  })
  return next.length === current.length && next.every((item, index) => item === current[index]) ? current as T[] : next
}

/** A changed card keeps the size React Flow measured and whether it is selected. Without a size React Flow hides a
 *  card until it has measured it again (a blink on every keystroke); a size that did change is measured again anyway. */
export function carryMeasuredAndSelected<T extends { measured?: unknown; selected?: boolean }>(fresh: T, held: T): T {
  if (held.measured === undefined && !held.selected) return fresh
  return { ...fresh, ...(held.measured !== undefined ? { measured: held.measured } : {}), ...(held.selected ? { selected: true } : {}) }
}

/** A changed wire keeps whether it is selected (its ✕ stays where the person left it). */
export function carrySelected<T extends { selected?: boolean }>(fresh: T, held: T): T {
  return held.selected ? { ...fresh, selected: true } : fresh
}
