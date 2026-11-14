// What the all-projects level (and a project's header) says about each project: the four numbers, as the server
// counts them. Since S0-A (2026-09-27) `project_order_overview` carries, per project, `standing` (the four numbers,
// `truncated`, `partial`) and `flows` (the same four per flow), judged by one rule on the server
// (order_attention.count_standing); the console no longer counts anything itself -- it reads, and where the server sent
// no standing (an older server, a project it could not read) the page says nothing rather than zero. Pure.

import { standingFrom, type Standing } from './projectPanorama'

export interface OverviewOrder { order_id: string; status: string; workflow_id: string; acceptance?: { status?: string } | null; attention?: Record<string, unknown> | null }
export interface ProjectOverviewEntry {
  orders: OverviewOrder[]
  truncated?: boolean
  /** The four numbers of the project; null or absent when the server sent none. */
  standing?: Standing | null
  /** The four numbers per flow (by workflow id); {} or absent when the server sent none. */
  flows?: Record<string, Standing>
}
export type ProjectOverview = Record<string, ProjectOverviewEntry>

/** A project's standing as the server counted it, or null when the overview has nothing for it (not read, not
 *  visible, refused, or an older server): the row then says nothing -- no orders and "not known" are different. */
export function standingOf(overview: ProjectOverview | null | undefined, projectId: string): Standing | null {
  return overview?.[projectId]?.standing ?? null
}

/** One flow's four numbers. The server lists a flow under `flows` only when it has an order (order_attention.flows_of),
 *  so under a project whose standing was read, a flow with no entry is a flow with no orders: four zeros, not "not
 *  read". The project's `truncated` (every number a floor) and `partial` (waiting and abnormal unknown) hold for its
 *  flows as well -- the per-flow counts come from the same cut rows and the same judgement -- so they are carried onto
 *  every flow's standing. Null only when the project itself has no standing. */
export function flowStandingOf(overview: ProjectOverview | null | undefined, projectId: string, flowId: string): Standing | null {
  const project = standingOf(overview, projectId)
  const own = overview?.[projectId]?.flows?.[flowId] ?? null
  if (!own) {
    return project ? { waiting_on_me: 0, abnormal_or_overdue: 0, in_flight: 0, delivered: 0, truncated: project.truncated, partial: project.partial } : null
  }
  if (!project) return own
  return { ...own, truncated: own.truncated || project.truncated, partial: own.partial || project.partial }
}

/** The server's reply, kept only when it is the shape above; anything else is no overview. */
export function overviewFrom(payload: unknown): ProjectOverview | null {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null
  const out: ProjectOverview = {}
  for (const [projectId, entry] of Object.entries(payload as Record<string, unknown>)) {
    const raw = entry as { orders?: unknown; truncated?: unknown; standing?: unknown; flows?: unknown } | null
    const orders = raw?.orders
    if (!Array.isArray(orders)) continue
    const flows: Record<string, Standing> = {}
    if (raw?.flows && typeof raw.flows === 'object' && !Array.isArray(raw.flows)) {
      for (const [flowId, value] of Object.entries(raw.flows as Record<string, unknown>)) {
        const standing = standingFrom(value)
        if (standing) flows[flowId] = standing
      }
    }
    out[projectId] = {
      orders: orders.filter((row): row is OverviewOrder => Boolean(row) && typeof row === 'object' && typeof (row as OverviewOrder).status === 'string'),
      truncated: raw?.truncated === true,
      standing: standingFrom(raw?.standing),
      flows,
    }
  }
  return out
}
