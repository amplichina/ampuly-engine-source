// Undo and redo over whole-draft snapshots. The draft table is immutable (every edit makes a new array), so a
// snapshot is a reference, equality is identity, and restoring one can never half-apply.

export interface DraftHistory<T> {
  past: T[]
  future: T[]
}

export const HISTORY_LIMIT = 50

export function emptyHistory<T>(): DraftHistory<T> {
  return { past: [], future: [] }
}

/** Take a snapshot before a change. A new change makes whatever was undone unreachable: redo is for going back to
 *  where you just were, not for merging two lines of edits. */
export function recorded<T>(history: DraftHistory<T>, current: T): DraftHistory<T> {
  const past = history.past[history.past.length - 1] === current ? history.past : [...history.past, current]
  return { past: past.length > HISTORY_LIMIT ? past.slice(past.length - HISTORY_LIMIT) : past, future: [] }
}

/** A snapshot taken because an edit *may* follow (the step editor got the focus). Nothing has changed yet, so
 *  what was undone stays redoable; `changed` below is what closes that line once an edit really happens. */
export function snapshotted<T>(history: DraftHistory<T>, current: T): DraftHistory<T> {
  const past = history.past[history.past.length - 1] === current ? history.past : [...history.past, current]
  return { past: past.length > HISTORY_LIMIT ? past.slice(past.length - HISTORY_LIMIT) : past, future: history.future }
}

/** The draft became a value that neither undo nor redo put there: a real edit. The undone line is closed. */
export function changed<T>(history: DraftHistory<T>): DraftHistory<T> {
  return history.future.length ? { past: history.past, future: [] } : history
}

/** Which canvas shortcut a key press is, if any. Undo and redo leave text fields to the browser's own undo; copying
 *  a step works from inside the step editor too -- that is where the keyboard is when a step is open -- and is
 *  claimed there so the browser's bookmark dialog does not open instead. */
export function canvasShortcut(press: {
  key: string; ctrl: boolean; meta: boolean; shift: boolean; alt: boolean
  inField: boolean; insideCanvas: boolean; expanded: boolean
}): 'undo' | 'redo' | 'duplicate' | null {
  if (!(press.ctrl || press.meta) || press.alt) return null
  if (!press.expanded && !press.insideCanvas) return null
  const key = press.key.toLowerCase()
  if (key === 'd') return press.shift ? null : 'duplicate'
  if (press.inField) return null
  if (key === 'y') return press.shift ? null : 'redo'
  if (key === 'z') return press.shift ? 'redo' : 'undo'
  return null
}

/** Step back. Snapshots identical to the present are skipped, so an edit session that changed nothing costs no
 *  extra press. `value` is undefined when there is nothing to go back to. */
export function undone<T>(history: DraftHistory<T>, current: T): { history: DraftHistory<T>; value: T | undefined } {
  const past = [...history.past]
  let previous = past.pop()
  while (previous === current && past.length) previous = past.pop()
  if (previous === undefined || previous === current) return { history: { past, future: history.future }, value: undefined }
  return { history: { past, future: [...history.future, current] }, value: previous }
}

/** Step forward again, after an undo and before any new change. */
export function redone<T>(history: DraftHistory<T>, current: T): { history: DraftHistory<T>; value: T | undefined } {
  const future = [...history.future]
  let next = future.pop()
  while (next === current && future.length) next = future.pop()
  if (next === undefined || next === current) return { history: { past: history.past, future }, value: undefined }
  return { history: { past: [...history.past, current], future }, value: next }
}
