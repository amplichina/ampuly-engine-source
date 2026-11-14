// Pure data shaping for the 关系 (relations) view and the >40-person branch folding.
//
// Rendering is done by react-force-graph-2d / force-graph (MIT, Vasco Asturiano),
// see RelationsGraph.tsx and THIRD_PARTY_NOTICES.md. This file is original Ampuly
// code: it only decides which observed actors/links are shown, which ones are
// "active" (flowing particles), and where nodes start before the force engine runs.
// It never invents a person, a relation or a state.
import type { ActorKind, ActorState, CollaborationActor, CollaborationLink, CollaborationLinkKind } from './model'

export const FOLD_THRESHOLD = 40

export interface CollaborationFold {
  /** Stable id: `fold:<managerId>`. */
  id: string
  managerId: string
  memberIds: readonly string[]
  count: number
  label: string
}

export interface FoldResult {
  actors: readonly CollaborationActor[]
  links: readonly CollaborationLink[]
  folds: readonly CollaborationFold[]
}

export interface FoldOptions {
  threshold?: number
  /** Manager ids whose folded children the user expanded. */
  expanded?: ReadonlySet<string>
  /** Actor ids that must stay visible (e.g. the selected person). */
  keepVisible?: ReadonlySet<string>
}

const ACTIVE_STATES: ReadonlySet<ActorState> = new Set(['working', 'waiting', 'blocked'])

export function isActiveActor(actor: Pick<CollaborationActor, 'state'>): boolean {
  return ACTIVE_STATES.has(actor.state)
}

export function foldId(managerId: string): string {
  return `fold:${managerId}`
}

/**
 * Above `threshold` people (owner excluded), collapse each manager's children whose
 * whole subtree is inactive into one fold node. A manager keeps its own card; only
 * groups of two or more inactive people are folded, and anything in `keepVisible`
 * (plus its subtree) is never hidden. Links touching hidden people are re-pointed to
 * the fold node and de-duplicated; links inside a fold disappear with it.
 */
export function foldInactiveBranches(
  actors: readonly CollaborationActor[],
  links: readonly CollaborationLink[],
  options: FoldOptions = {},
): FoldResult {
  const threshold = options.threshold ?? FOLD_THRESHOLD
  const people = actors.filter(actor => actor.id !== 'owner').length
  if (people <= threshold) return { actors, links, folds: [] }

  const expanded = options.expanded ?? new Set<string>()
  const keep = options.keepVisible ?? new Set<string>()
  const ids = new Set(actors.map(actor => actor.id))
  const children = new Map<string, CollaborationActor[]>()
  for (const actor of actors) {
    if (!actor.managerId || !ids.has(actor.managerId)) continue
    const list = children.get(actor.managerId) ?? []
    list.push(actor)
    children.set(actor.managerId, list)
  }

  // Subtree of each actor (itself included), cycle-safe.
  const subtreeCache = new Map<string, string[]>()
  const subtree = (id: string, seen = new Set<string>()): string[] => {
    const cached = subtreeCache.get(id)
    if (cached) return cached
    if (seen.has(id)) return []
    seen.add(id)
    const out = [id]
    for (const child of children.get(id) ?? []) out.push(...subtree(child.id, seen))
    subtreeCache.set(id, out)
    return out
  }
  const byId = new Map(actors.map(actor => [actor.id, actor]))
  const quiet = (id: string): boolean => subtree(id).every(member => {
    const actor = byId.get(member)
    return !!actor && !isActiveActor(actor) && !keep.has(member)
  })

  const hiddenTo = new Map<string, string>()
  const folds: CollaborationFold[] = []
  // Walk top-down so that an outer fold wins over inner ones.
  const order = [...actors].sort((a, b) => a.depth - b.depth)
  for (const manager of order) {
    if (hiddenTo.has(manager.id) || expanded.has(manager.id)) continue
    const quietChildren = (children.get(manager.id) ?? []).filter(child => !hiddenTo.has(child.id) && quiet(child.id))
    const members = quietChildren.flatMap(child => subtree(child.id)).filter(id => !hiddenTo.has(id))
    if (members.length < 2) continue
    const id = foldId(manager.id)
    for (const member of members) hiddenTo.set(member, id)
    folds.push({ id, managerId: manager.id, memberIds: members, count: members.length, label: `${members.length} 人未在办` })
  }
  if (!folds.length) return { actors, links, folds }

  const visibleActors = actors.filter(actor => !hiddenTo.has(actor.id))
  const out: CollaborationLink[] = []
  const keys = new Set<string>()
  const push = (link: CollaborationLink) => {
    if (link.source === link.target) return
    const key = [link.source, link.target].sort().join('|') + '|' + link.kind
    if (keys.has(key)) return
    keys.add(key)
    out.push({ ...link, id: key })
  }
  for (const fold of folds) push({ id: '', source: fold.managerId, target: fold.id, kind: 'reports', label: '汇报' })
  for (const link of links) {
    push({ ...link, source: hiddenTo.get(link.source) ?? link.source, target: hiddenTo.get(link.target) ?? link.target })
  }
  return { actors: visibleActors, links: out, folds }
}

// ---------------------------------------------------------------------------
// Relation graph data for the force view.

export interface RelationNode {
  id: string
  label: string
  initials: string
  kind: ActorKind | 'fold'
  state: ActorState
  active: boolean
  isOwner: boolean
  count?: number
  /** Observed in-progress work items (drives glow/pulse strength). */
  activity: number
  /** Observed current task (colours the agent's ion cloud per task). */
  taskId?: string
  radius: number
  /** Seed position (radial by depth) so the first frame is already readable. */
  x: number
  y: number
}

export interface RelationLink {
  id: string
  source: string
  target: string
  kind: CollaborationLinkKind
  label: string
  active: boolean
  human: boolean
  particles: number
  speed: number
  /** 0..1 from the endpoints' observed work count; scales particle density/speed. */
  intensity: number
  /** Set when both ends work on the same observed task: the relation carries that task's ions. */
  taskId?: string
}

export interface RelationGraphData {
  nodes: RelationNode[]
  links: RelationLink[]
  activeLinks: number
}

export function actorInitials(name: string, kind: ActorKind): string {
  const cleaned = name.replace(/[（(].*$/, '').trim()
  if (kind === 'human' && !cleaned) return '人'
  return [...cleaned].slice(0, 2).join('') || '—'
}

const PARTICLES: Record<CollaborationLinkKind, number> = {
  trace: 4, 'shared-task': 3, 'shared-order': 2, team: 2, reports: 1,
}
const SPEED: Record<CollaborationLinkKind, number> = {
  trace: 0.012, 'shared-task': 0.009, 'shared-order': 0.007, team: 0.006, reports: 0.005,
}

/** A relation flows only when someone on it is actually working (observed state). */
export function isActiveLink(source: Pick<RelationNode, 'state'> | undefined, target: Pick<RelationNode, 'state'> | undefined): boolean {
  return source?.state === 'working' || target?.state === 'working'
}

export function buildRelationGraph(
  actors: readonly CollaborationActor[],
  links: readonly CollaborationLink[],
  folds: readonly CollaborationFold[] = [],
): RelationGraphData {
  const nodes: RelationNode[] = []
  const depthOf = new Map<string, number>()
  const rings = new Map<number, string[]>()
  const place = (id: string, depth: number) => {
    depthOf.set(id, depth)
    const list = rings.get(depth) ?? []
    list.push(id)
    rings.set(depth, list)
  }
  for (const actor of actors) place(actor.id, actor.id === 'owner' ? 0 : Math.max(1, actor.depth))
  const actorById = new Map(actors.map(actor => [actor.id, actor]))
  for (const fold of folds) place(fold.id, (actorById.get(fold.managerId)?.depth ?? 0) + 1)

  const seed = new Map<string, { x: number; y: number }>()
  for (const [depth, ids] of rings) {
    const radius = depth * 150
    ids.forEach((id, index) => {
      if (depth === 0) { seed.set(id, { x: 0, y: 0 }); return }
      const angle = (index / ids.length) * Math.PI * 2 + depth * 0.6
      seed.set(id, { x: Math.round(Math.cos(angle) * radius), y: Math.round(Math.sin(angle) * radius) })
    })
  }

  for (const actor of actors) {
    const isOwner = actor.id === 'owner'
    const position = seed.get(actor.id) ?? { x: 0, y: 0 }
    nodes.push({
      id: actor.id,
      label: actor.name,
      initials: actorInitials(actor.name, actor.kind),
      kind: actor.kind,
      state: actor.state,
      active: isActiveActor(actor),
      isOwner,
      activity: actor.activeWorkCount,
      ...(actor.currentTaskId ? { taskId: actor.currentTaskId } : {}),
      radius: isOwner ? 20 : actor.depth <= 1 ? 16 : 13,
      ...position,
    })
  }
  for (const fold of folds) {
    const position = seed.get(fold.id) ?? { x: 0, y: 0 }
    nodes.push({
      id: fold.id, label: fold.label, initials: String(fold.count), kind: 'fold', state: 'idle',
      active: false, isOwner: false, count: fold.count, activity: 0, radius: 14, ...position,
    })
  }

  const nodeById = new Map(nodes.map(node => [node.id, node]))
  const out: RelationLink[] = []
  let activeLinks = 0
  for (const link of links) {
    const source = nodeById.get(link.source)
    const target = nodeById.get(link.target)
    if (!source || !target) continue
    const active = isActiveLink(source, target)
    if (active) activeLinks += 1
    const intensity = active ? Math.min(1, (source.activity + target.activity) / 4) : 0
    out.push({
      id: link.id,
      source: link.source,
      target: link.target,
      kind: link.kind,
      label: link.label,
      active,
      human: source.kind === 'human' || target.kind === 'human',
      particles: active ? Math.round(PARTICLES[link.kind] * (1 + intensity)) : 0,
      speed: SPEED[link.kind] * (1 + 0.6 * intensity),
      intensity,
      ...(source.taskId && source.taskId === target.taskId ? { taskId: source.taskId } : {}),
    })
  }
  return { nodes, links: out, activeLinks }
}

// ---------------------------------------------------------------------------
// Trace pulses: real trace events mapped onto graph actors, used to fire
// ripples/bursts in the particle views. Never synthesised.

export interface RelationPulse {
  id: string
  actorId: string
  time: number
  /** Display title of the trace event (already safe-projected by the trace store). */
  title?: string
  type?: string
}

export function tracePulses(
  actors: readonly Pick<CollaborationActor, 'id' | 'agentId' | 'employeeId'>[],
  events: readonly { id: string; actorId: string; time: number; title?: string; type?: string }[],
  limit = 24,
): RelationPulse[] {
  const out: RelationPulse[] = []
  for (const event of events.slice(-limit)) {
    const actor = actors.find(row =>
      (row.agentId && row.agentId === event.actorId)
      || (row.employeeId && row.employeeId === event.actorId)
      || row.id === event.actorId)
    if (actor) {
      out.push({
        id: event.id, actorId: actor.id, time: event.time,
        ...(event.title ? { title: event.title } : {}), ...(event.type ? { type: event.type } : {}),
      })
    }
  }
  return out
}
