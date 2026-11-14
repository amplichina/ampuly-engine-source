// Where the builder's canvas starts (frontend refactor U2-8). Pure.
//
// Fitting a whole flow into the pane is right for a small flow and useless for a real one: fifteen steps fitted into
// a laptop window are cards nobody can read, and the first thing the person does is zoom in. So: fit when fitting
// keeps the cards readable; otherwise start at a readable zoom on the beginning of the flow -- its top-left, where the
// first steps are -- and leave the rest to panning and the map. "Fit" is still one click away in the controls.

export const READABLE_ZOOM = 0.7

export type Box = { x: number; y: number; width: number; height: number }
export type Viewport = { x: number; y: number; zoom: number }

/** The viewport to start with, or null to keep the fitted one. */
export function readableStart(bounds: Box, pane: { width: number; height: number }, options: { leftInset?: number; topInset?: number } = {}): Viewport | null {
  if (!(bounds.width > 0 && bounds.height > 0 && pane.width > 0 && pane.height > 0)) return null
  const fitted = Math.min(pane.width / bounds.width, pane.height / bounds.height)
  if (fitted >= READABLE_ZOOM) return null
  // Room for what floats over the canvas's top-left (the view switch, the panel's reopen button).
  const left = options.leftInset ?? 24
  const top = options.topInset ?? 56
  return { x: left - bounds.x * READABLE_ZOOM, y: top - bounds.y * READABLE_ZOOM, zoom: READABLE_ZOOM }
}
