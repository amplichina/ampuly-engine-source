import type { OrgEmployee, OrgInfoPayload, OrgRole } from '../../types/visual'

/** A workflow runtime can differ from the console's default organization.
 * Prefer its persisted seats and employee assignments; never count unrelated default staff. */
export function runtimeOrganization(org: OrgInfoPayload | null): OrgInfoPayload | null {
  const runId = org?.project_run?.run_id
  if (!org || !runId) return org
  const seats = (org.runtime_seats ?? []).filter(seat => seat.role_session_id.startsWith(`role-runtime::${runId}::`))
  if (!seats.length || seats.every(seat => org.roles.some(role => role.role_id === seat.role_id))) return org
  const roleIds = new Set(seats.map(seat => seat.role_id))
  const roles: OrgRole[] = []
  const employees: OrgEmployee[] = []
  for (const seat of seats) {
    const existing = org.employees.find(person => person.employee_id === seat.employee_id)
    const assignment = (org.work_items ?? []).map(item => item.metadata?.employee_assignment)
      .find(value => value && typeof value === 'object' && (value as Record<string, unknown>).employee_id === seat.employee_id) as Record<string, unknown> | undefined
    const name = existing?.name || (typeof assignment?.name === 'string' ? assignment.name : '') || seat.role_id
    const manager = (org.runtime_teams ?? []).find(team => team.member_role_ids.includes(seat.role_id)
      && team.manager_role_id !== seat.role_id && roleIds.has(team.manager_role_id))?.manager_role_id || ''
    if (!roles.some(role => role.role_id === seat.role_id)) roles.push({ role_id: seat.role_id, name,
      responsibility: typeof assignment?.description === 'string' ? assignment.description : '',
      status: seat.status, reports_to: manager, can_spawn: [], tools: [] })
    if (seat.employee_id && !employees.some(person => person.employee_id === seat.employee_id)) employees.push(existing ?? {
      employee_id: seat.employee_id, name, role_id: seat.role_id, kind: 'ai', category: '', domains: [],
      seniority: typeof assignment?.seniority === 'string' ? assignment.seniority : '', status: seat.status,
      tags: [], skill_refs: [], learned_skill_refs: [], experience_score: 0,
    })
  }
  return { ...org, roles, employees }
}
