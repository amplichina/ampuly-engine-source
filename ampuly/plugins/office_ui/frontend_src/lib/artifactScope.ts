// Grouping deliverables by the order they belong to (frontend refactor U3). Pure, over the file index as it arrives.
//
// On a real workspace every file lives under `orders/<order id>/…`, so the panel's one grouping -- the first path
// segment -- collapsed into a single useless chip reading "orders", and three files all named 需求单.md could only be
// told apart by a truncated directory line. The order id is right there in the path: use it.

export type FileLike = { path: string; group?: string }

export type Scope = { id: string; label: string; count: number }

const ORDERS = 'orders/'

/** The order a file belongs to, or '' when it is not under an order. */
export function orderOfPath(path: string): string {
  const clean = String(path || '').replace(/^\.?\//, '')
  if (!clean.startsWith(ORDERS)) return ''
  const rest = clean.slice(ORDERS.length)
  const slash = rest.indexOf('/')
  return (slash === -1 ? rest : rest.slice(0, slash)).trim()
}

/** What is inside the order's own folder -- intake, output, feedback -- or '' at the order's root. */
export function areaOfPath(path: string): string {
  const order = orderOfPath(path)
  if (!order) return ''
  const rest = String(path || '').replace(/^\.?\//, '').slice(ORDERS.length + order.length + 1)
  const slash = rest.indexOf('/')
  return slash === -1 ? '' : rest.slice(0, slash).trim()
}

/**
 * The chips to offer, given the files. Grouping by order when the files really are under orders, and by the first
 * path segment otherwise -- which is what the panel did before, and is still right for a workspace laid out
 * differently. `labelFor` turns an order id into the name people use for it; the id is kept when it cannot.
 */
export function scopesOf(files: readonly FileLike[], labelFor: (orderId: string) => string = id => id): { kind: 'order' | 'group'; scopes: Scope[] } {
  const byOrder = new Map<string, number>()
  let outside = 0
  for (const file of files) {
    const order = orderOfPath(file.path)
    if (order) byOrder.set(order, (byOrder.get(order) ?? 0) + 1)
    else outside += 1
  }
  // Only worth switching when the orders really are how these files are laid out.
  if (byOrder.size > 1 || (byOrder.size === 1 && outside === 0 && files.length > 0)) {
    const scopes = [...byOrder.entries()]
      .sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1))
      .map(([id, count]) => ({ id, label: labelFor(id) || id, count }))
    if (outside > 0) scopes.push({ id: '', label: '', count: outside })
    return { kind: 'order', scopes }
  }
  const byGroup = new Map<string, number>()
  for (const file of files) {
    const group = String(file.group ?? '').trim()
    if (group) byGroup.set(group, (byGroup.get(group) ?? 0) + 1)
  }
  return {
    kind: 'group',
    scopes: [...byGroup.entries()].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)).map(([id, count]) => ({ id, label: id, count })),
  }
}

/** The files a chosen chip shows. An empty choice is everything, as it was. */
export function filesInScope<T extends FileLike>(files: readonly T[], kind: 'order' | 'group', chosen: string): T[] {
  if (!chosen) return [...files]
  return files.filter(file => (kind === 'order' ? orderOfPath(file.path) === chosen : String(file.group ?? '') === chosen))
}
