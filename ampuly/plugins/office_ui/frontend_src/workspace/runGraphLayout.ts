/**
 * Layout for the run graph, ComfyUI-style: one group panel per role, panels
 * ordered left to right by where the role first appears in the pipeline,
 * the role's work items stacked inside. Pure functions, no React.
 */

export interface LayoutItem {
  id: string
  /** Group key — the role this item belongs to. */
  groupKey: string
  groupName: string
  createdAt: number
  /** Ids this item waits for (dependency edges). */
  deps: string[]
  /** The item that delegated this one (tree edge), if known. */
  parent?: string
  /** Rendered card height in px; the caller knows how many rows it drew. */
  height: number
}

export interface LayoutGroup {
  key: string
  name: string
  color: string
  x: number
  y: number
  width: number
  height: number
  count: number
}

export interface LayoutPosition {
  /** Position relative to the group panel's top-left corner. */
  x: number
  y: number
  groupKey: string
}

export interface RunGraphLayout {
  groups: LayoutGroup[]
  positions: Map<string, LayoutPosition>
  ranks: Map<string, number>
}

export const NODE_WIDTH = 232
export const GROUP_PADDING = 14
export const GROUP_TITLE_HEIGHT = 32
export const GROUP_GAP = 56
export const NODE_GAP = 18
export const GROUP_WIDTH = NODE_WIDTH + GROUP_PADDING * 2

/** Distinct hues for role panels; assigned in pipeline order. */
export const ROLE_PALETTE = [
  '#3d7ddb', '#d9883a', '#8f5fd9', '#3ab47b', '#d94d6c', '#3ab1c9', '#c7b03a', '#a8704f',
]

/**
 * A manager's own item (the intake, the final wrap-up) lists the stations it
 * delegated as dependencies: it waits for its children. Read as edges that is
 * a cycle — parent delegates child, parent waits on child — and it is not
 * what the picture should say. Those "waits for my own children" edges are
 * dropped everywhere; the tree edge already carries the relationship.
 */
export function waitsOnOwnChild(item: LayoutItem, dep: string, byId: Map<string, LayoutItem>): boolean {
  return byId.get(dep)?.parent === item.id
}

/**
 * Longest-path rank from the roots, over dependency and tree edges. A cycle
 * (which the runtime forbids, but a projection may lag) cannot loop forever:
 * relaxation is bounded by the item count.
 */
export function computeRanks(items: LayoutItem[]): Map<string, number> {
  const ids = new Set(items.map(item => item.id))
  const byId = new Map(items.map(item => [item.id, item]))
  const preds = new Map<string, string[]>()
  for (const item of items) {
    const incoming = item.deps.filter(dep => ids.has(dep) && dep !== item.id && !waitsOnOwnChild(item, dep, byId))
    if (item.parent && ids.has(item.parent) && item.parent !== item.id) incoming.push(item.parent)
    preds.set(item.id, incoming)
  }
  const ranks = new Map<string, number>(items.map(item => [item.id, 0]))
  for (let pass = 0; pass < items.length; pass += 1) {
    let changed = false
    for (const item of items) {
      const best = Math.max(0, ...(preds.get(item.id) ?? []).map(pred => (ranks.get(pred) ?? 0) + 1))
      if (best > (ranks.get(item.id) ?? 0)) {
        ranks.set(item.id, best)
        changed = true
      }
    }
    if (!changed) break
  }
  return ranks
}

export function computeRunGraphLayout(items: LayoutItem[]): RunGraphLayout {
  const ranks = computeRanks(items)
  const byGroup = new Map<string, LayoutItem[]>()
  for (const item of items) {
    const list = byGroup.get(item.groupKey) ?? []
    list.push(item)
    byGroup.set(item.groupKey, list)
  }
  const groupOrder = Array.from(byGroup.entries())
    .map(([key, list]) => ({
      key,
      name: list[0]?.groupName || key,
      list,
      minRank: Math.min(...list.map(item => ranks.get(item.id) ?? 0)),
      firstSeen: Math.min(...list.map(item => item.createdAt || 0)),
    }))
    .sort((a, b) => a.minRank - b.minRank || a.firstSeen - b.firstSeen || a.key.localeCompare(b.key))

  const groups: LayoutGroup[] = []
  const positions = new Map<string, LayoutPosition>()
  let x = 0
  groupOrder.forEach((group, index) => {
    const ordered = [...group.list].sort(
      (a, b) => (ranks.get(a.id) ?? 0) - (ranks.get(b.id) ?? 0) || (a.createdAt || 0) - (b.createdAt || 0) || a.id.localeCompare(b.id),
    )
    let y = GROUP_TITLE_HEIGHT + GROUP_PADDING
    for (const item of ordered) {
      positions.set(item.id, { x: GROUP_PADDING, y, groupKey: group.key })
      y += item.height + NODE_GAP
    }
    const height = y - NODE_GAP + GROUP_PADDING
    // Stagger panels a little by rank so a long pipeline reads as a slope
    // rather than a flat wall, the way ComfyUI graphs tend to drift.
    groups.push({
      key: group.key,
      name: group.name,
      color: ROLE_PALETTE[index % ROLE_PALETTE.length],
      x,
      y: group.minRank * 24,
      width: GROUP_WIDTH,
      height,
      count: ordered.length,
    })
    x += GROUP_WIDTH + GROUP_GAP
  })
  return { groups, positions, ranks }
}
