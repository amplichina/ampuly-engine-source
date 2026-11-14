/** Explicit report focus affects navigation only; runtime facts and normal views are unchanged. */
export function reportProject(search: string): string {
  const q = new URLSearchParams(search)
  const project = q.get('project_id') || ''
  return q.get('briefing') === '1' && /^[a-z0-9][a-z0-9-]{0,63}$/.test(project) ? project : ''
}
export function reportWorkflows<T extends { project_id?: string }>(rows: T[], project: string): T[] {
  return project ? rows.filter(row => row.project_id === project) : rows
}
