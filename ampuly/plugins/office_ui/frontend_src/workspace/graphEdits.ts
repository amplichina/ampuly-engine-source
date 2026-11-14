// The node quick bar's graph edits (4.7 Q02): 前加 / 后加 put a step *between* two steps instead of beside them,
// 删除 says what it will cost before it happens and joins the gap it leaves, and 一键对齐 lays every step out by
// dependency layer (4.7 A02). Each returns a whole new node table and changes nothing else, so a caller records one undo snapshot
// per edit and the save path (workflow_update_nodes, which re-validates the dependencies and refuses a cycle) is the
// only way any of it reaches the package.
import type { WorkflowNode } from '../components/WorkflowsPage'
import { undeliveredInputs } from './neededDeliverables'
import { NODE_WIDTH, type RunGraphLayout } from './runGraphLayout'
import { placedPosition } from './workflowGraph'

function unique(ids: readonly string[]): string[] {
  return [...new Set(ids)]
}

/** A step that is not in the table yet, placed after `anchorId`: it waits for the anchor, and every step that
 *  waited for the anchor now waits for it instead. Returns null when the anchor is not in the table. The step keeps
 *  the position it was given (placeNewStep, on a flow laid out by hand); without one the layout places it. */
export function insertAfter(nodes: readonly WorkflowNode[], anchorId: string, step: WorkflowNode): WorkflowNode[] | null {
  const at = nodes.findIndex(node => node.id === anchorId)
  if (at < 0 || nodes.some(node => node.id === step.id)) return null
  const added: WorkflowNode = { ...step, depends_on: [anchorId], canvas: step.canvas ?? null }
  const rewired = nodes.map(node => ((node.depends_on ?? []).includes(anchorId)
    ? { ...node, depends_on: unique(node.depends_on.map(dep => (dep === anchorId ? added.id : dep))) }
    : node))
  return [...rewired.slice(0, at + 1), added, ...rewired.slice(at + 1)]
}

/** A step dropped on the wire `source → target` (4.7 Q02): it waits for `source`, and `target` waits for it instead of
 *  for `source`. Only that one wire is split -- `source`'s other followers and `target`'s other inputs stay as they
 *  were. Null when there is no such wire or the step's id is taken. */
export function insertOnWire(nodes: readonly WorkflowNode[], source: string, target: string, step: WorkflowNode): WorkflowNode[] | null {
  const at = nodes.findIndex(node => node.id === target)
  if (at < 0 || source === target || !nodes.some(node => node.id === source) || nodes.some(node => node.id === step.id)) return null
  if (!(nodes[at].depends_on ?? []).includes(source)) return null
  const added: WorkflowNode = { ...step, depends_on: [source], canvas: step.canvas ?? null }
  const rewired = nodes.map(node => (node.id === target
    ? { ...node, depends_on: unique(node.depends_on.map(dep => (dep === source ? added.id : dep))) }
    : node))
  return [...rewired.slice(0, at), added, ...rewired.slice(at)]
}

/** A step placed before `anchorId`: it takes over what the anchor waited for, and the anchor waits for it. */
export function insertBefore(nodes: readonly WorkflowNode[], anchorId: string, step: WorkflowNode): WorkflowNode[] | null {
  const at = nodes.findIndex(node => node.id === anchorId)
  if (at < 0 || nodes.some(node => node.id === step.id)) return null
  const anchor = nodes[at]
  const added: WorkflowNode = { ...step, depends_on: [...(anchor.depends_on ?? [])], canvas: step.canvas ?? null }
  const rewired = nodes.map(node => (node.id === anchorId ? { ...node, depends_on: [added.id] } : node))
  return [...rewired.slice(0, at), added, ...rewired.slice(at)]
}

export interface DeleteImpact {
  /** Steps that wait for this one directly: after the delete they wait for what it waited for. */
  dependents: string[]
  /** What those steps will wait for instead (this step's own dependencies). */
  bridgedTo: string[]
  /** Inputs, anywhere downstream, that this step was the only one to deliver. */
  lostInputs: Array<{ nodeId: string; input: string }>
  /** The sign-off this step carried ('none' is no sign-off). */
  gate: string
  /** The flow this step hands the order on to, when it is a call. */
  invokes: string
}

/** The table without `id`, and the gap closed: every step that waited for it waits for what it waited for. */
export function removeNodeBridging(nodes: readonly WorkflowNode[], id: string): WorkflowNode[] {
  const gone = nodes.find(node => node.id === id)
  const bridge = (gone?.depends_on ?? []).filter(dep => dep !== id)
  return nodes
    .filter(node => node.id !== id)
    .map(node => ((node.depends_on ?? []).includes(id)
      ? { ...node, depends_on: unique(node.depends_on.flatMap(dep => (dep === id ? bridge : [dep])).filter(dep => dep !== node.id)) }
      : node))
}

/** What deleting `id` (with the gap closed) takes away, for the confirmation a person reads first. */
export function deleteImpact(nodes: readonly WorkflowNode[], id: string): DeleteImpact | null {
  const gone = nodes.find(node => node.id === id)
  if (!gone) return null
  const after = removeNodeBridging(nodes, id)
  const lostInputs: DeleteImpact['lostInputs'] = []
  for (const node of after) {
    const before = nodes.find(item => item.id === node.id)
    if (!before) continue
    const missingBefore = new Set(undeliveredInputs(nodes, before))
    for (const input of undeliveredInputs(after, node)) {
      if (!missingBefore.has(input)) lostInputs.push({ nodeId: node.id, input })
    }
  }
  return {
    dependents: nodes.filter(node => (node.depends_on ?? []).includes(id)).map(node => node.id),
    bridgedTo: [...(gone.depends_on ?? [])],
    lostInputs,
    gate: String(gone.gate ?? '').trim() || 'none',
    invokes: gone.type === 'invoke_workflow' ? String(gone.invoke?.workflow ?? gone.invoke?.workflow_id ?? '') : '',
  }
}

// ── 一键对齐 (4.7 A02): by dependency layer, left to right, branches centred on the step they leave ──────────────

/** Space between two layers' cards, and between two cards stacked in one layer. */
export const ALIGN_COLUMN_GAP = 96
export const ALIGN_ROW_GAP = 36
/** A card whose height the caller did not say (a node test, a step not drawn yet). */
const DEFAULT_CARD_HEIGHT = 160

type Wired = Pick<WorkflowNode, 'id' | 'depends_on'>

/** Each step's upstream steps in the table: named once, never itself, never a step that is not there. */
function upstreamOf(nodes: readonly Wired[]): Map<string, string[]> {
  const ids = new Set(nodes.map(node => node.id))
  return new Map(nodes.map(node => [node.id, unique((node.depends_on ?? []).filter(dep => ids.has(dep) && dep !== node.id))]))
}

/** The dependency layer of every step: nothing upstream is layer 0, otherwise one past its furthest upstream step. A
 *  loop (the save refuses one) cannot spin: at most one pass per step. */
export function dependencyLayers(nodes: readonly Wired[]): Map<string, number> {
  const upstream = upstreamOf(nodes)
  const layers = new Map(nodes.map(node => [node.id, 0]))
  for (let pass = 0; pass < nodes.length; pass += 1) {
    let changed = false
    for (const node of nodes) {
      const layer = Math.max(0, ...(upstream.get(node.id) ?? []).map(dep => (layers.get(dep) ?? 0) + 1))
      if (layer > (layers.get(node.id) ?? 0)) { layers.set(node.id, layer); changed = true }
    }
    if (!changed) break
  }
  return layers
}

export interface LayeredLayout {
  layers: Map<string, number>
  /** Each card's top-left: the first layer at x 0, the topmost card at y 0. */
  positions: Map<string, { x: number; y: number }>
}

/**
 * Where every card goes when the flow is laid out by dependency: one column per layer, left to right; the cards of a
 * layer stacked top to bottom; each card centred on the steps it continues from, so the branches that leave one step
 * sit evenly above and below it and a join sits level with what it joins. Then once backwards (a step centred on what
 * continues from it, so two steps' branches do not squeeze each other off-centre) and once forwards again. Cards never
 * overlap: cards that would are moved apart together, as close to where each wanted to be as they can be. The table
 * order breaks ties, so the same flow always lays out the same way. Positions only -- nothing here reads or writes a
 * dependency.
 */
export function layeredLayout(nodes: readonly Wired[], heights?: ReadonlyMap<string, number>): LayeredLayout {
  const layers = dependencyLayers(nodes)
  const upstream = upstreamOf(nodes)
  const downstream = new Map<string, string[]>()
  for (const [id, deps] of upstream) {
    for (const dep of deps) {
      const list = downstream.get(dep)
      if (list) list.push(id)
      else downstream.set(dep, [id])
    }
  }
  const order = new Map<string, number>()
  const columns: string[][] = []
  for (const node of nodes) {
    if (order.has(node.id)) continue // an id listed twice is laid out once
    order.set(node.id, order.size)
    const layer = layers.get(node.id) ?? 0
    ;(columns[layer] ??= []).push(node.id)
  }
  const height = (id: string) => {
    const value = heights?.get(id)
    return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : DEFAULT_CARD_HEIGHT
  }
  const top = new Map<string, number>()
  const centre = (id: string) => (top.get(id) ?? 0) + height(id) / 2
  const mean = (values: readonly number[]) => values.reduce((sum, value) => sum + value, 0) / values.length

  // One column, in the order given: every card as near its wished centre as it can be without touching the one above.
  // Cards that collide form a block that moves as one, centred on what its cards wished for together.
  const pack = (ids: readonly string[], wish: (id: string) => number) => {
    type Block = { ids: string[]; span: number; sum: number }
    const blocks: Block[] = []
    for (const id of ids) {
      let block: Block = { ids: [id], span: height(id), sum: wish(id) - height(id) / 2 }
      while (blocks.length) {
        const before = blocks[blocks.length - 1]
        if (block.sum / block.ids.length >= before.sum / before.ids.length + before.span + ALIGN_ROW_GAP) break
        blocks.pop()
        const shift = before.span + ALIGN_ROW_GAP
        block = { ids: [...before.ids, ...block.ids], span: shift + block.span, sum: before.sum + block.sum - shift * block.ids.length }
      }
      blocks.push(block)
    }
    for (const block of blocks) {
      let y = block.sum / block.ids.length
      for (const id of block.ids) { top.set(id, y); y += height(id) + ALIGN_ROW_GAP }
    }
  }
  // The centre a card wants: the middle of the given neighbours already placed, or where it is (or, in a loop's
  // leftovers, just below the card before it).
  const wishes = (ids: readonly string[], neighbours: Map<string, string[]>) => {
    const wished = new Map<string, number>()
    let last = Number.NEGATIVE_INFINITY
    for (const id of ids) {
      const placed = (neighbours.get(id) ?? []).filter(other => top.has(other))
      const value = placed.length ? mean(placed.map(centre)) : top.has(id) ? centre(id) : Number.isFinite(last) ? last + DEFAULT_CARD_HEIGHT + ALIGN_ROW_GAP : 0
      wished.set(id, value)
      last = value
    }
    return wished
  }

  // The first layer as the table lists it, one card under the other.
  let y = 0
  for (const id of columns[0] ?? []) { top.set(id, y); y += height(id) + ALIGN_ROW_GAP }
  // Forwards: each layer ordered by where its upstream steps are (the table order breaks ties), then placed. A step
  // none of whose upstream steps is placed yet (only in a loop) goes last.
  for (let layer = 1; layer < columns.length; layer += 1) {
    const column = columns[layer]
    if (!column) continue
    const first = wishes(column, upstream)
    const known = (id: string) => (upstream.get(id) ?? []).some(dep => top.has(dep))
    column.sort((a, b) => Number(known(b)) - Number(known(a)) || (known(a) ? first.get(a)! - first.get(b)! : 0) || order.get(a)! - order.get(b)!)
    const wished = wishes(column, upstream)
    pack(column, id => wished.get(id)!)
  }
  // Backwards, then forwards again: the order within a layer stays; only the heights move.
  for (let layer = columns.length - 2; layer >= 0; layer -= 1) {
    const column = columns[layer]
    if (!column) continue
    const wished = wishes(column, downstream)
    pack(column, id => wished.get(id)!)
  }
  for (let layer = 1; layer < columns.length; layer += 1) {
    const column = columns[layer]
    if (!column) continue
    const wished = wishes(column, upstream)
    pack(column, id => wished.get(id)!)
  }

  let highest = Number.POSITIVE_INFINITY
  for (const value of top.values()) highest = Math.min(highest, value)
  if (!Number.isFinite(highest)) highest = 0
  const positions = new Map<string, { x: number; y: number }>()
  for (const [layer, column] of columns.entries()) {
    for (const id of column ?? []) positions.set(id, { x: layer * (NODE_WIDTH + ALIGN_COLUMN_GAP), y: (top.get(id) ?? 0) - highest })
  }
  return { layers, positions }
}

/** What 一键对齐 needs to know about the canvas it lays out on. */
export interface AlignFrame {
  /** Each step's card height, so tall cards are not stacked into their neighbours. */
  heights?: ReadonlyMap<string, number>
  /** Where the top-left of the aligned flow goes on the canvas. */
  start?: { x: number; y: number }
}

/** 一键对齐: every step gets the position the dependency layout gives it (layeredLayout), saved as an absolute canvas
 *  position (absoluteCanvas). Only `canvas` changes -- a layout-only save keeps the version -- and a new table is
 *  returned, so it is one undo step. `moved` names the steps whose position changed: empty when the flow is already
 *  laid out this way. */
export function alignAll(nodes: readonly WorkflowNode[], frame: AlignFrame = {}): { nodes: WorkflowNode[]; moved: string[] } {
  const { positions } = layeredLayout(nodes, frame.heights)
  const start = frame.start ?? { x: 0, y: 0 }
  const moved: string[] = []
  const aligned = nodes.map(node => {
    const at = positions.get(node.id)
    if (!at) return node
    const canvas = absoluteCanvas({ x: start.x + at.x, y: start.y + at.y })
    const before = placedPosition(node)
    if (before && before.x === canvas.x && before.y === canvas.y) return node
    moved.push(node.id)
    return { ...node, canvas }
  })
  return { nodes: moved.length ? aligned : [...nodes], moved }
}

// ── Where the cards sit (4.7 AQ): role lanes for a flow nobody arranged, absolute positions once anybody did ──────

type Point = { x: number; y: number }
type Card = Point & { height: number }

/**
 * How a saved position is written. The package keeps a step's position as {x, y} and nothing else (whole numbers:
 * workflow_packages._canvas_position). Positions used to be saved relative to the step's lane in the role-lane layout,
 * and that lane moves whenever a step is added, removed, rewired or given another role -- taking every arranged card
 * with it. A position is saved as an absolute canvas position now; to tell one from an older lane-relative one with
 * only {x, y} to go on, an absolute x is written ABSOLUTE_X past the real one. No lane-relative x comes near it (a lane
 * is 316 px wide), and nothing else reads the number: the runtime never looks at `canvas`, and a version compares
 * steps without it.
 */
export const ABSOLUTE_X = 1_000_000

/** A canvas position as it is saved: absolute, whole pixels, marked (ABSOLUTE_X). */
export function absoluteCanvas(point: Point): Point {
  return { x: Math.round(point.x) + ABSOLUTE_X, y: Math.round(point.y) }
}

/** A step's saved position read back: absolute, or an older one relative to its lane; null when it has none. */
export function savedPoint(node: Pick<WorkflowNode, 'canvas'>): (Point & { absolute: boolean }) | null {
  const at = placedPosition(node)
  if (!at) return null
  return at.x >= ABSOLUTE_X / 2 ? { x: at.x - ABSOLUTE_X, y: at.y, absolute: true } : { x: at.x, y: at.y, absolute: false }
}

/** A step's place in the role-lane layout: its lane's top-left, and its own spot on the canvas (the lane plus its
 *  place in the lane). */
export interface LaneSpot { origin: Point; spot: Point }

/** Every step's LaneSpot in a role-lane layout (layoutWithStaging): what boardLayout reads positions against. */
export function laneSpots(layout: RunGraphLayout, ids: readonly string[]): Map<string, LaneSpot> {
  const lanes = new Map(layout.groups.map(group => [group.key, { x: group.x, y: group.y }]))
  return new Map(ids.map(id => {
    const laid = layout.positions.get(id)
    const origin = (laid && lanes.get(laid.groupKey)) || { x: 0, y: 0 }
    return [id, { origin: { ...origin }, spot: { x: origin.x + (laid?.x ?? 0), y: origin.y + (laid?.y ?? 0) } }]
  }))
}

export interface BoardLayout {
  /** Some step has a saved position (dragged, 一键对齐): the flow is laid out as arranged, and no role lane is drawn --
   *  a lane box drawn around cards arranged by dependency lands on the other lanes. The role stays on each card. */
  free: boolean
  /** Every card's top-left on the canvas. */
  positions: Map<string, Point>
  /** Every step's position is saved absolute already: nothing to write down before the next change. */
  settled: boolean
}

const heightIn = (heights: ReadonlyMap<string, number> | undefined, id: string) => {
  const value = heights?.get(id)
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : DEFAULT_CARD_HEIGHT
}

/** How far a card put down keeps from the cards around it. */
const CLEARANCE = ALIGN_ROW_GAP / 2

/** Where a card of this height goes when it is put down at `wish`: there, or -- when that lands on a card already on the
 *  canvas -- moved down to a row under that card, and on down until it lands on none. */
export function freeSpot(wish: Point, height: number, cards: Iterable<Card>, width = NODE_WIDTH): Point {
  const list = [...cards]
  let y = wish.y
  // y only grows, and each move goes past one card for good: at most one move per card.
  for (let guard = 0; guard <= list.length; guard += 1) {
    const hit = list.find(card => wish.x < card.x + width + CLEARANCE && card.x < wish.x + width + CLEARANCE
      && y < card.y + card.height + CLEARANCE && card.y < y + height + CLEARANCE)
    if (!hit) break
    y = hit.y + hit.height + ALIGN_ROW_GAP
  }
  return { x: wish.x, y }
}

/**
 * Where every card sits. A flow nobody arranged is drawn in its role lanes, exactly as the lane layout puts it. Once
 * any step has a saved position the flow is `free`: a step saved absolute is where it was saved; an older lane-relative
 * position is read against its lane as the lane layout draws it now; and a step with no position yet (added since)
 * starts where the lane layout would put it, moved down off any card already there. Table order decides who moves.
 */
export function boardLayout(
  nodes: ReadonlyArray<Pick<WorkflowNode, 'id' | 'canvas'>>,
  lanes: ReadonlyMap<string, LaneSpot>,
  heights?: ReadonlyMap<string, number>,
): BoardLayout {
  const spot = (id: string) => ({ ...(lanes.get(id)?.spot ?? { x: 0, y: 0 }) })
  const positions = new Map<string, Point>()
  const free = nodes.some(node => placedPosition(node))
  if (!free) {
    for (const node of nodes) positions.set(node.id, spot(node.id))
    return { free, positions, settled: true }
  }
  const cards: Card[] = []
  for (const node of nodes) {
    const saved = savedPoint(node)
    if (!saved || positions.has(node.id)) continue
    const origin = lanes.get(node.id)?.origin ?? { x: 0, y: 0 }
    const at = saved.absolute ? { x: saved.x, y: saved.y } : { x: origin.x + saved.x, y: origin.y + saved.y }
    positions.set(node.id, at)
    cards.push({ ...at, height: heightIn(heights, node.id) })
  }
  for (const node of nodes) {
    if (positions.has(node.id)) continue
    const at = freeSpot(spot(node.id), heightIn(heights, node.id), cards)
    positions.set(node.id, at)
    cards.push({ ...at, height: heightIn(heights, node.id) })
  }
  return { free, positions, settled: nodes.every(node => savedPoint(node)?.absolute === true) }
}

/**
 * The table with every step that is on the canvas saved where it is shown, as an absolute position: an older
 * lane-relative position is converted, once, and a step the layout placed is kept where it was placed -- so the next
 * change (a step added, a wire, a role) moves no card that is already there. A step saved absolute already, and one
 * that is not on the canvas yet, is left as it is. Only positions change.
 */
export function pinnedToBoard<T extends Pick<WorkflowNode, 'id' | 'canvas'>>(nodes: readonly T[], shown: ReadonlyMap<string, Point>): T[] {
  return nodes.map(node => {
    const at = shown.get(node.id)
    return !at || savedPoint(node)?.absolute ? node : { ...node, canvas: absoluteCanvas(at) }
  })
}

/** Where a new step goes relative to the step it was added from: 后加 (after it), 前加 (before it), 复制 (a copy of it). */
export type NewStepPlace = 'after' | 'before' | 'copy'

/**
 * Where a step added on an arranged flow goes, by its place in the graph (4.7 AQ): after a step, one layer to its
 * right; before a step, one layer to its left; a copy, one row under the original. Where that lands on a card already
 * there (a layer that already has a card at that height), it moves down a row under it, and on until it lands on none.
 * `positions` are the cards on the canvas now. Null when the step it was added from is not one of them.
 */
export function placeNewStep(
  place: NewStepPlace, anchorId: string, positions: ReadonlyMap<string, Point>, heights: ReadonlyMap<string, number> | undefined, height: number,
): Point | null {
  const anchor = positions.get(anchorId)
  if (!anchor) return null
  const pitch = NODE_WIDTH + ALIGN_COLUMN_GAP
  const wish = place === 'after' ? { x: anchor.x + pitch, y: anchor.y }
    : place === 'before' ? { x: anchor.x - pitch, y: anchor.y }
      : { x: anchor.x, y: anchor.y + heightIn(heights, anchorId) + ALIGN_ROW_GAP }
  const cards = [...positions].map(([id, at]) => ({ ...at, height: heightIn(heights, id) }))
  return freeSpot(wish, height, cards)
}

/** A step as a save weighs it, without its position: keys in a fixed order, an empty value the same as none (the save
 *  writes no key for an empty inputs, model, tier, rework_to, branch_when or invoke). */
function withoutPosition(node: object): string {
  const empty = (value: unknown) => value === undefined || value === null || value === ''
    || (Array.isArray(value) ? value.length === 0 : typeof value === 'object' && Object.keys(value as object).length === 0)
  const stable = (value: unknown): unknown => (Array.isArray(value) ? value.map(stable)
    : value && typeof value === 'object'
      ? Object.fromEntries(Object.keys(value).sort().map(key => [key, stable((value as Record<string, unknown>)[key])]))
      : value)
  return JSON.stringify(stable(Object.fromEntries(Object.entries(node).filter(([key, value]) => key !== 'canvas' && !empty(value)))))
}

/** Whether `after` differs from `before` in nothing but where its cards sit: the same steps in the same order, each the
 *  same apart from its position. Such a change (一键对齐, a drag, positions written down) still opens a draft -- the
 *  positions are saved -- but it is a layout-only save: no new version is suggested for it (4.7 AQ). */
export function onlyPositionsChanged(before: readonly object[], after: readonly object[]): boolean {
  return before.length === after.length && before.every((node, index) => withoutPosition(node) === withoutPosition(after[index]))
}

// ── The wires the canvas draws, by id (4.7 Q02: a step dropped on a wire goes in between) ─────────────────────────

/** The id the canvas gives the wire `source → target` (a dependency: target waits for source). */
export function wireId(source: string, target: string): string {
  return `dep:${source}->${target}`
}

/** The wire an id names, read back; null for anything that is not a wire id. */
export function wireOf(id: string): { source: string; target: string } | null {
  const match = /^dep:(.+)->(.+)$/.exec(id)
  return match ? { source: match[1], target: match[2] } : null
}

/** Every wire the canvas draws for a table, by id: one per dependency on a step that is there, never to itself. */
export function wireIds(nodes: readonly Wired[]): string[] {
  return [...upstreamOf(nodes)].flatMap(([id, deps]) => deps.map(dep => wireId(dep, id)))
}

// ── 分支条件 on the canvas (4.7 A02) ─────────────────────────────────────────────────────────────────────────────

/** How much of a 分支条件 the card shows; the rest is behind an ellipsis, and the whole text on hover. */
export const BRANCH_TEXT_MAX = 40

/** A step's 分支条件 (branch_when: the words that choose this branch at the decision before it), trimmed; '' when the
 *  step is not a branch. The server hands it with every step (node_payload, read-only there), and a proposal keeps it
 *  (nodeProposal nodeFrom); anything that is not text reads as no condition. */
export function branchCondition(node: object): string {
  const value = (node as { branch_when?: unknown }).branch_when
  return typeof value === 'string' ? value.trim() : ''
}

/** The card's 分支条件 row: at most BRANCH_TEXT_MAX characters (a Chinese character counts as one), and the whole
 *  condition for the hover. Null when the step is not a branch. */
export function branchRowText(node: object): { value: string; title: string } | null {
  const text = branchCondition(node)
  if (!text) return null
  const chars = [...text]
  return { value: chars.length > BRANCH_TEXT_MAX ? `${chars.slice(0, BRANCH_TEXT_MAX).join('')}…` : text, title: text }
}

// ── 节点内组件重排 (4.7 A02) ───────────────────────────────────────────────────────────────────────────────────────

export type OrderedComponentKind = 'skill' | 'tool'
type ComponentInstance = { kind?: unknown; ref?: unknown } & Record<string, unknown>
type WithInstances = Pick<WorkflowNode, 'skills' | 'tools'> & { components?: unknown }

/**
 * One of a step's skills (or tools) moved one place up (-1) or down (+1): the patch that says so, or null when it
 * cannot move that way. The order is written where the save keeps it -- the step's own `skills` / `tools` list -- and,
 * when the step carries its component instances (`components`), those follow in the same order, each kept whole (its
 * name, version and parameters): only the instances of the moved kind change places, the others keep theirs. A skill
 * and a tool do not swap: each kind has its own order. The console is not handed `components`, so on a save the server
 * does the same to the stored instances from the reordered list (node_components.reconcile_components).
 */
export function moveComponent(
  node: WithInstances, kind: OrderedComponentKind, index: number, step: -1 | 1,
): (Partial<WorkflowNode> & { components?: ComponentInstance[] }) | null {
  const key = kind === 'skill' ? 'skills' : 'tools'
  const list = [...(node[key] ?? [])]
  const to = index + step
  if (!Number.isInteger(index) || index < 0 || index >= list.length || to < 0 || to >= list.length) return null
  ;[list[index], list[to]] = [list[to], list[index]]
  const patch: Partial<WorkflowNode> & { components?: ComponentInstance[] } = kind === 'skill' ? { skills: list } : { tools: list }
  if (Array.isArray(node.components)) {
    const instances = node.components as ComponentInstance[]
    const ofKind = instances.filter(item => item && typeof item === 'object' && item.kind === kind)
    const byRef = new Map(ofKind.map(item => [String(item.ref ?? ''), item]))
    const listed = unique(list).filter(ref => byRef.has(ref)).map(ref => byRef.get(ref)!)
    // An instance whose component is not in the list (it should not happen) keeps its turn after the listed ones.
    const queue = [...listed, ...ofKind.filter(item => !listed.includes(item))]
    patch.components = instances.map(item => (item && typeof item === 'object' && item.kind === kind ? queue.shift()! : item))
  }
  return patch
}

/** The name a person reads for one of a step's skills (or tools) in its editor (4.7 AQ): its display name from the
 *  component catalogue when the page has one for it, else what the step names it by. */
export function componentLabel(ref: string, names?: ReadonlyMap<string, string>): string {
  return names?.get(ref)?.trim() || ref
}
