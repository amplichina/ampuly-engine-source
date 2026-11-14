// The two marks the builder writes straight onto card elements (not through React, which would rewrite them on the
// next patch): the open step's (data-inspected) and, while a wire is being dragged, whether it may land on a card
// (data-wire). On a big flow React Flow renders only the cards in view, so a card that comes back into view is a new
// element without them; this puts them back the moment it is mounted.

type Marked = Pick<Element, 'getAttribute' | 'setAttribute' | 'removeAttribute'>

export interface CardMarks {
  /** The step whose editor is open ('' when none). */
  inspected: string
  /** While a wire is dragged: each step's verdict ('ok', or why it is refused); null otherwise. */
  wire: ReadonlyMap<string, string> | null
}

export function applyCardMarks(element: Marked, marks: CardMarks): void {
  const id = element.getAttribute('data-id') ?? ''
  if (!id) return
  if (id === marks.inspected) element.setAttribute('data-inspected', '')
  else element.removeAttribute('data-inspected')
  const verdict = marks.wire?.get(id)
  if (verdict) element.setAttribute('data-wire', verdict === 'ok' ? 'ok' : 'refused')
  else element.removeAttribute('data-wire')
}
