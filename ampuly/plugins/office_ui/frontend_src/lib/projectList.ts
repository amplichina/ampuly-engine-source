// 项目中心's list of all projects (frontend refactor U3). Pure, over the project index the console already holds.
// It shows what the index says -- name, customer, lifecycle -- and nothing it would have to ask every project for:
// reading each project's orders writes an audit row per read and can be refused by customer scope, so counts are
// shown for the project one is in (the strip above), not invented for the others.

type ProjectLike = { id: string; name?: string; customer_id?: string; lifecycle_status?: string }

export type ProjectRow = { id: string; name: string; customer: string; lifecycle: string; current: boolean; enterable: boolean }

const ORDER: Record<string, number> = { active: 0, archived: 1, destroyed: 2 }

export const LIFECYCLE_LABELS: Record<string, readonly [string, string]> = {
  active: ['进行中', 'Active'],
  archived: ['已归档', 'Archived'],
  destroyed: ['已销毁', 'Destroyed'],
}

export function lifecycleLabel(lifecycle: string, zh: boolean): string {
  const known = LIFECYCLE_LABELS[lifecycle]
  return known ? known[zh ? 0 : 1] : lifecycle
}

/** The project one is in first, then the rest by lifecycle and id. A destroyed project is listed (it happened) but
 *  cannot be entered; an archived one can -- it is read-only on the server's side, not hidden. */
export function projectRows(projects: readonly ProjectLike[], activeId: string): ProjectRow[] {
  return projects
    .map(project => {
      const lifecycle = (project.lifecycle_status || 'active').trim() || 'active'
      return {
        id: project.id,
        name: project.name && project.name !== project.id ? project.name : project.id,
        customer: (project.customer_id ?? '').trim(),
        lifecycle,
        current: project.id === activeId,
        enterable: project.id !== activeId && lifecycle !== 'destroyed',
      }
    })
    .sort((a, b) => Number(b.current) - Number(a.current)
      || (ORDER[a.lifecycle] ?? 1) - (ORDER[b.lifecycle] ?? 1)
      // by id, not by display name: ids are plain ASCII, so the order is the same on every machine
      || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
}
