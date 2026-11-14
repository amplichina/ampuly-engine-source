// Bring the selected step into view with a pure viewport calculation.
//
// Opening a step's inspector keeps its card visible. The canvas follows two rules:
//   - it moves only when the card is not already fully in the usable part of the pane, and
//   - it never changes the zoom, and never moves further than it has to.
// The usable part is the pane minus whatever floats over it: the docked inspector on the right, the view switch and
// the panel's reopen button at the top left.

export type Box = { x: number; y: number; width: number; height: number }
export type Viewport = { x: number; y: number; zoom: number }
export type Pane = {
  width: number
  height: number
  /** What covers each edge: the inspector on the right, the overlays at the top left. */
  left?: number
  top?: number
  right?: number
  bottom?: number
}

/** The least shift along one axis that puts [start, start+size] inside [min, max]; 0 when it is already there. */
function shift(start: number, size: number, min: number, max: number): number {
  if (size >= max - min) return min - start // taller/wider than the window: show its beginning
  if (start < min) return min - start
  if (start + size > max) return max - (start + size)
  return 0
}

/** The viewport that brings `card` (in flow coordinates) into view, or null to leave the canvas alone. */
export function bringIntoView(card: Box, pane: Pane, viewport: Viewport, margin = 24): Viewport | null {
  const { zoom } = viewport
  if (!(zoom > 0) || !(pane.width > 0 && pane.height > 0)) return null
  if (!Number.isFinite(card.x) || !Number.isFinite(card.y) || !(card.width > 0) || !(card.height > 0)) return null

  const left = (pane.left ?? 0) + margin
  const top = (pane.top ?? 0) + margin
  const right = pane.width - (pane.right ?? 0) - margin
  const bottom = pane.height - (pane.bottom ?? 0) - margin
  // An inspector wider than the pane, or a pane too small to hold anything: no opinion rather than a wild jump.
  if (!(right > left) || !(bottom > top)) return null

  const dx = shift(card.x * zoom + viewport.x, card.width * zoom, left, right)
  const dy = shift(card.y * zoom + viewport.y, card.height * zoom, top, bottom)
  if (dx === 0 && dy === 0) return null
  return { x: viewport.x + dx, y: viewport.y + dy, zoom }
}
