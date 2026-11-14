import { runtimeOrganization } from './runtimeOrganization'
import type { AgentInfo, OrgEmployee, OrgInfoPayload, OrgRole, RuntimeSeatInfo, RuntimeWorkItemInfo } from '../../types/visual'
import type { TraceEvent } from '../traceModel'

export type ActorKind = 'ai' | 'human' | 'supervised_ai'
export type ActorState = 'working' | 'waiting' | 'blocked' | 'idle' | 'offline' | 'unknown'
export type CollaborationLinkKind = 'reports' | 'shared-task' | 'shared-order' | 'trace' | 'team'
export type DutyMode = 'project' | 'business'

export interface CollaborationTask {
  id: string
  title: string
  status: string
}

export interface CollaborationOrder {
  id: string
  title: string
  taskId?: string
  status: string
  customerName?: string
  brandName?: string
  amount?: number
  currency?: string
  dueAt?: string
  assigneeEmployeeId?: string
  acceptanceStatus?: string
  fdePhase?: string
}

export interface CollaborationWorkItem {
  id: string
  title: string
  phase: string
  state: ActorState
  blockedReason?: string
  handoffStatus?: string
  taskId?: string
  orderId?: string
}

export interface CollaborationActor {
  id: string
  name: string
  roleId: string
  roleName: string
  roleReportsTo: string
  kind: ActorKind
  employeeId?: string
  agentId?: string
  managerId: string | null
  depth: number
  state: ActorState
  stateLabel: string
  currentWorkTitle?: string
  currentTaskId?: string
  currentTool?: string
  workItems: readonly CollaborationWorkItem[]
  activeWorkCount: number
  waitingCount: number
  blockedCount: number
  lastActive?: number
  vacant?: boolean
  isDefault?: boolean
  tone: number
}

export interface CollaborationLink {
  id: string
  source: string
  target: string
  kind: CollaborationLinkKind
  label: string
}

export interface CollaborationKpis {
  total: number
  available: number
  working: number
  waiting: number
  blocked: number
  projects: number
  orders: number
}

export interface CollaborationAttention {
  id: string
  label: string
  detail: string
  actorId?: string
  taskId?: string
}

export interface DutyItem {
  id: string
  title: string
  state: ActorState
  meta: string
}

export interface DutyCard {
  id: string
  kind: 'project' | 'business'
  title: string
  subtitle: string
  status: string
  taskId?: string
  actors: readonly { id: string; name: string; kind: ActorKind }[]
  items: readonly DutyItem[]
  metric: string
}

export interface CollaborationView {
  ownerId: string
  actors: readonly CollaborationActor[]
  links: readonly CollaborationLink[]
  /** Distinct observed relations that were not drawn because MAX_LINKS was reached. */
  truncatedLinks: number
  kpis: CollaborationKpis
  attention: readonly CollaborationAttention[]
  tasks: readonly CollaborationTask[]
  orders: readonly CollaborationOrder[]
  projectCards: readonly DutyCard[]
  businessCards: readonly DutyCard[]
}

export interface CollaborationInput {
  org?: OrgInfoPayload | null
  agents?: readonly AgentInfo[]
  tasks?: readonly CollaborationTask[]
  orders?: readonly CollaborationOrder[]
  traceEvents?: readonly TraceEvent[]
}

const OWNER_ID = 'owner'
const MAX_ACTORS_FOR_PAIRS = 8
export const MAX_LINKS = 40

const STATE_LABEL: Record<ActorState, string> = {
  working: '在办',
  waiting: '等待',
  blocked: '阻塞',
  idle: '空闲',
  offline: '离线',
  unknown: '未提供',
}

const TERMINAL_PHASES = new Set(['done', 'completed', 'complete', 'succeeded', 'success', 'cancelled', 'canceled', 'failed', 'error', 'approved', 'rejected', 'skipped', 'superseded'])
const WAITING_PHASES = new Set(['waiting', 'review', 'pending', 'pending_review', 'awaiting_review', 'awaiting_human', 'gate', 'blocked', 'ready', 'waiting_dependencies', 'waiting_for_peer', 'waiting_for_children', 'awaiting_manager_review', 'queued'])
const WORKING_PHASES = new Set(['working', 'running', 'active', 'doing', 'executing', 'in_progress', 'in-progress'])

function clean(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function stripDefaultSuffix(value: string): string {
  return value
    .replace(/\s*Default Employee\s*$/i, '')
    .replace(/\s*默认员工\s*$/u, '')
    .trim()
}

function phaseState(phase: string, blocked?: string): ActorState {
  const value = phase.toLowerCase().replace(/\s+/g, '_')
  if (value === 'idle') return 'idle'
  if (blocked) return 'blocked'
  if (value.includes('block')) return 'blocked'
  if (value.includes('fail') || value.includes('error')) return 'blocked'
  if (TERMINAL_PHASES.has(value)) return value.includes('cancel') ? 'idle' : 'idle'
  if (WAITING_PHASES.has(value)) return 'waiting'
  if (WORKING_PHASES.has(value)) return 'working'
  return 'unknown'
}

function agentState(agent: AgentInfo | undefined): ActorState {
  if (!agent) return 'unknown'
  const value = `${agent.runtime_status ?? ''} ${agent.status ?? ''}`.toLowerCase()
  if (value.includes('offline') || value.includes('disconnect')) return 'offline'
  if (value.includes('block') || value.includes('fail')) return 'blocked'
  if (value.includes('wait') || value.includes('review')) return 'waiting'
  if (value.includes('tool_active') || value.includes('working') || value.includes('running') || value.includes('executing') || value.includes('reflecting') || value.includes('thinking')) return 'working'
  if (value.includes('idle')) return 'idle'
  return 'unknown'
}

function mergeState(current: ActorState, next: ActorState): ActorState {
  const rank: Record<ActorState, number> = { blocked: 5, working: 4, waiting: 3, unknown: 2, idle: 1, offline: 0 }
  return rank[next] > rank[current] ? next : current
}

function roleOf(employee: OrgEmployee, roles: Map<string, OrgRole>): OrgRole | undefined {
  const ids = employee.role_ids?.length ? employee.role_ids : [employee.role_id]
  for (const id of ids) {
    const role = roles.get(clean(id))
    if (role) return role
  }
  return undefined
}

function employeeAgent(employee: OrgEmployee, agents: readonly AgentInfo[]): AgentInfo | undefined {
  if (employee.linked_agent_id) {
    const linked = agents.find(agent => agent.agent_id === employee.linked_agent_id)
    if (linked) return linked
  }
  return agents.find(agent => agent.employee_id === employee.employee_id)
}

function displayName(employee: OrgEmployee, role: OrgRole | undefined): string {
  const cleaned = stripDefaultSuffix(employee.name)
  if (employee.is_default_employee || !cleaned) return clean(role?.name) || cleaned || employee.employee_id
  return cleaned
}

function agentName(agent: AgentInfo, role: OrgRole | undefined): string {
  const cleaned = stripDefaultSuffix(agent.name)
  if (!cleaned || /default employee/i.test(agent.name)) return clean(role?.name) || cleaned || agent.agent_id
  return cleaned
}

function roleTone(roles: readonly OrgRole[], roleId: string): number {
  const index = roles.findIndex(role => role.role_id === roleId)
  return index < 0 ? 0 : index % 5
}

/** Build a display projection only. It never invents a person, an online state or a relation. */
export function buildCollaborationView(input: CollaborationInput): CollaborationView {
  const org = runtimeOrganization(input.org ?? null)
  const roles = [...(org?.roles ?? [])]
  const roleMap = new Map(roles.map(role => [role.role_id, role]))
  const employees = [...(org?.employees ?? [])]
  const agents = [...(input.agents ?? [])]
  const tasks = [...(input.tasks ?? [])].map(task => ({
    id: clean(task.id), title: clean(task.title) || clean(task.id), status: clean(task.status) || '未提供',
  })).filter(task => task.id)
  const taskMap = new Map(tasks.map(task => [task.id, task]))
  const orders = [...(input.orders ?? [])].filter(order => clean(order.id))
  const traceEvents = input.traceEvents ?? []

  const owner: CollaborationActor = {
    id: OWNER_ID, name: '你（Owner）', roleId: OWNER_ID, roleName: 'Owner', roleReportsTo: '',
    kind: 'human', managerId: null, depth: 0, state: 'idle', stateLabel: STATE_LABEL.idle,
    workItems: [], activeWorkCount: 0, waitingCount: 0, blockedCount: 0, tone: 4,
  }

  const actors: CollaborationActor[] = [owner]
  const usedAgents = new Set<string>()
  const employeeByActor = new Map<string, OrgEmployee>()
  const agentByActor = new Map<string, AgentInfo>()

  for (const employee of employees) {
    const role = roleOf(employee, roleMap)
    if (!role) continue
    const agent = employeeAgent(employee, agents)
    if (agent) usedAgents.add(agent.agent_id)
    const actor: CollaborationActor = {
      id: `emp:${employee.employee_id}`,
      name: displayName(employee, role),
      roleId: role.role_id,
      roleName: role.name,
      roleReportsTo: role.reports_to,
      kind: employee.kind === 'human' || employee.kind === 'supervised_ai' ? employee.kind : 'ai',
      employeeId: employee.employee_id,
      ...(agent ? { agentId: agent.agent_id } : {}),
      managerId: null,
      depth: 1,
      state: agent ? agentState(agent) : phaseState(org?.runtime_seats?.find(seat => seat.employee_id === employee.employee_id)?.status || 'idle'),
      stateLabel: STATE_LABEL.idle,
      ...(agent?.current_task_id ? { currentTaskId: agent.current_task_id, currentWorkTitle: taskMap.get(agent.current_task_id)?.title } : {}),
      ...(agent?.current_tool ? { currentTool: agent.current_tool } : {}),
      workItems: [],
      activeWorkCount: 0,
      waitingCount: 0,
      blockedCount: 0,
      isDefault: Boolean(employee.is_default_employee),
      tone: roleTone(roles, role.role_id),
    }
    actors.push(actor)
    employeeByActor.set(actor.id, employee)
    if (agent) agentByActor.set(actor.id, agent)
  }

  for (const agent of agents) {
    if (usedAgents.has(agent.agent_id)) continue
    const role = roleMap.get(clean(agent.ampuly_role_id))
    if (!role) continue
    const actor: CollaborationActor = {
      id: `agent:${agent.agent_id}`,
      name: agentName(agent, role),
      roleId: role.role_id,
      roleName: role.name,
      roleReportsTo: role.reports_to,
      kind: 'ai',
      ...(agent.employee_id ? { employeeId: agent.employee_id } : {}),
      agentId: agent.agent_id,
      managerId: null,
      depth: 1,
      state: agentState(agent),
      stateLabel: STATE_LABEL.idle,
      ...(agent.current_task_id ? { currentTaskId: agent.current_task_id, currentWorkTitle: taskMap.get(agent.current_task_id)?.title } : {}),
      ...(agent.current_tool ? { currentTool: agent.current_tool } : {}),
      workItems: [],
      activeWorkCount: 0,
      waitingCount: 0,
      blockedCount: 0,
      tone: roleTone(roles, role.role_id),
    }
    actors.push(actor)
    agentByActor.set(actor.id, agent)
  }

  // Only roles with no person at all get a vacant seat. It is labelled, never treated as a person.
  const staffedRoles = new Set(actors.filter(actor => actor.id !== OWNER_ID).map(actor => actor.roleId))
  for (const role of roles) {
    if (staffedRoles.has(role.role_id)) continue
    actors.push({
      id: `vacant:${role.role_id}`, name: role.name, roleId: role.role_id, roleName: role.name,
      roleReportsTo: role.reports_to, kind: 'ai', managerId: null, depth: 1, state: 'unknown',
      stateLabel: '未配置', workItems: [], activeWorkCount: 0, waitingCount: 0, blockedCount: 0,
      vacant: true, tone: roleTone(roles, role.role_id),
    })
  }

  // Seats and work items. Work items are assigned by explicit seat first, then by role.
  const seats: RuntimeSeatInfo[] = [...(org?.runtime_seats ?? [])]
  const actorByEmployee = new Map(actors.filter(actor => actor.employeeId).map(actor => [actor.employeeId as string, actor]))
  const actorsByRole = new Map<string, CollaborationActor[]>()
  for (const actor of actors) {
    if (actor.id === OWNER_ID) continue
    const list = actorsByRole.get(actor.roleId) ?? []
    list.push(actor)
    actorsByRole.set(actor.roleId, list)
  }
  const workItemByActor = new Map<string, CollaborationWorkItem[]>([])
  for (const item of (org?.work_items ?? []) as RuntimeWorkItemInfo[]) {
    const title = clean(item.title) || clean(item.work_item_id)
    const seat = seats.find(row => row.current_work_item_id === item.work_item_id || row.focused_work_item_id === item.work_item_id)
    let actor = seat?.employee_id ? actorByEmployee.get(seat.employee_id) : undefined
    if (!actor) actor = actorsByRole.get(item.role_id)?.[0]
    if (!actor) continue
    const state = phaseState(item.phase, item.blocked_reason)
    const viewItem: CollaborationWorkItem = {
      id: item.work_item_id, title, phase: item.phase, state,
      ...(item.blocked_reason ? { blockedReason: item.blocked_reason } : {}),
      ...(item.handoff_status ? { handoffStatus: item.handoff_status } : {}),
    }
    const list = workItemByActor.get(actor.id) ?? []
    list.push(viewItem)
    workItemByActor.set(actor.id, list)
  }

  const orderIdsByEmployee = new Map<string, string[]>()
  for (const order of orders) {
    const employeeId = clean(order.assigneeEmployeeId)
    if (!employeeId) continue
    const list = orderIdsByEmployee.get(employeeId) ?? []
    list.push(order.id)
    orderIdsByEmployee.set(employeeId, list)
  }

  const withWork: CollaborationActor[] = actors.map(actor => {
    if (actor.id === OWNER_ID) return actor
    const workItems = [...(workItemByActor.get(actor.id) ?? [])]
    const agent = agentByActor.get(actor.id)
    if (!workItems.length && actor.currentTaskId) {
      const task = taskMap.get(actor.currentTaskId)
      workItems.push({
        id: actor.currentTaskId, title: task?.title || actor.currentTaskId,
        phase: agent?.status || 'unknown', state: agentState(agent),
      })
    }
    const orderIds = actor.employeeId ? orderIdsByEmployee.get(actor.employeeId) ?? [] : []
    for (const id of orderIds) {
      const order = orders.find(row => row.id === id)
      if (!order) continue
      workItems.push({
        id: `order:${order.id}`, title: order.title || order.id,
        phase: order.status || order.acceptanceStatus || 'unknown',
        state: order.acceptanceStatus && /pending|review|await/i.test(order.acceptanceStatus) ? 'waiting' : 'working',
        orderId: order.id,
      })
    }
    const priority: Record<ActorState, number> = { blocked: 0, working: 1, waiting: 2, unknown: 3, idle: 4, offline: 5 }
    workItems.sort((a, b) => priority[a.state] - priority[b.state])
    let state = actor.state
    for (const item of workItems) state = mergeState(state, item.state)
    if (actor.vacant) state = 'unknown'
    if (actor.kind === 'human' && state === 'unknown') state = 'idle'
    const activeWorkCount = workItems.filter(item => item.state === 'working').length
    const waitingCount = workItems.filter(item => item.state === 'waiting').length
    const blockedCount = workItems.filter(item => item.state === 'blocked').length
    return {
      ...actor, workItems, state, stateLabel: actor.vacant ? '未配置' : STATE_LABEL[state],
      activeWorkCount, waitingCount, blockedCount,
      currentWorkTitle: workItems.find(item => item.state !== 'idle')?.title || actor.currentWorkTitle,
    }
  })

  // Reporting lines: explicit supervisor first, then role.reports_to.
  const byId = new Map(withWork.map(actor => [actor.id, actor]))
  const firstByRole = new Map<string, CollaborationActor>()
  for (const actor of withWork) {
    if (actor.id === OWNER_ID || actor.vacant) continue
    if (!firstByRole.has(actor.roleId)) firstByRole.set(actor.roleId, actor)
  }
  const resolveManager = (actor: CollaborationActor): string => {
    if (actor.id === OWNER_ID) return ''
    const employee = employeeByActor.get(actor.id)
    const supervisor = clean(employee?.supervisor_employee_id)
    if (supervisor) {
      const found = withWork.find(row => row.employeeId === supervisor)
      if (found && found.id !== actor.id) return found.id
    }
    const reportsTo = clean(actor.roleReportsTo)
    if (!reportsTo || reportsTo === 'owner') return OWNER_ID
    const manager = firstByRole.get(reportsTo)
    if (manager && manager.id !== actor.id) return manager.id
    return OWNER_ID
  }
  const managed = withWork.map(actor => ({ ...actor, managerId: actor.id === OWNER_ID ? null : resolveManager(actor) }))
  const managerById = new Map(managed.map(actor => [actor.id, actor]))
  const withDepth = managed.map(actor => {
    let depth = 0
    let cursor = actor.managerId
    const seen = new Set<string>([actor.id])
    while (cursor && !seen.has(cursor) && depth < 12) {
      seen.add(cursor)
      depth += 1
      cursor = managerById.get(cursor)?.managerId ?? null
    }
    return { ...actor, depth }
  })

  // Collaboration links: only observed/reporting/team relations.
  const links: CollaborationLink[] = []
  const linkKeys = new Set<string>()
  const droppedKeys = new Set<string>()
  const addLink = (source: string, target: string, kind: CollaborationLinkKind, label: string) => {
    if (!source || !target || source === target) return
    if (!byId.has(source) || !byId.has(target)) return
    const a = source
    const b = target
    const key = [a, b].sort().join('|') + '|' + kind
    if (linkKeys.has(key)) return
    // Past the cap the relation is counted, not silently lost, so the page can say
    // how many observed relations are not drawn.
    if (links.length >= MAX_LINKS) { droppedKeys.add(key); return }
    linkKeys.add(key)
    links.push({ id: key, source: a, target: b, kind, label })
  }
  for (const actor of withDepth) {
    if (actor.managerId) addLink(actor.managerId, actor.id, 'reports', '汇报')
  }

  const groupPairs = (groups: Map<string, string[]>, kind: CollaborationLinkKind, label: string) => {
    for (const ids of groups.values()) {
      const unique = [...new Set(ids)].filter(id => byId.has(id)).slice(0, MAX_ACTORS_FOR_PAIRS)
      for (let i = 0; i < unique.length; i += 1) {
        for (let j = i + 1; j < unique.length; j += 1) addLink(unique[i], unique[j], kind, label)
      }
    }
  }
  const taskGroups = new Map<string, string[]>()
  for (const actor of withDepth) {
    const taskId = clean(actor.currentTaskId)
    if (!taskId) continue
    const list = taskGroups.get(taskId) ?? []
    list.push(actor.id)
    taskGroups.set(taskId, list)
  }
  for (const item of (org?.work_items ?? []) as RuntimeWorkItemInfo[]) {
    const seat = seats.find(row => row.current_work_item_id === item.work_item_id)
    const actor = seat?.employee_id ? byId.get(`emp:${seat.employee_id}`) : undefined
    if (!actor) continue
    const metadata = item.metadata as Record<string, unknown> | undefined
    const taskId = clean(metadata?.task_id ?? metadata?.taskId)
    if (!taskId) continue
    const list = taskGroups.get(taskId) ?? []
    list.push(actor.id)
    taskGroups.set(taskId, list)
  }
  groupPairs(taskGroups, 'shared-task', '同一任务')

  const orderGroups = new Map<string, string[]>()
  for (const order of orders) {
    const ids: string[] = []
    if (order.assigneeEmployeeId) {
      const actor = withDepth.find(row => row.employeeId === order.assigneeEmployeeId)
      if (actor) ids.push(actor.id)
    }
    if (order.taskId) {
      for (const actor of withDepth) if (actor.currentTaskId === order.taskId) ids.push(actor.id)
    }
    orderGroups.set(order.id, ids)
  }
  groupPairs(orderGroups, 'shared-order', '同一业务')

  const traceGroups = new Map<string, string[]>()
  for (const event of traceEvents) {
    const taskId = clean(event.taskId)
    if (!taskId) continue
    const actor = withDepth.find(row =>
      (row.agentId && row.agentId === event.actorId)
      || (row.employeeId && row.employeeId === event.actorId)
      || row.id === event.actorId)
    if (!actor) continue
    const list = traceGroups.get(taskId) ?? []
    list.push(actor.id)
    traceGroups.set(taskId, list)
  }
  groupPairs(traceGroups, 'trace', '共用任务')

  // Runtime team managers/members.
  for (const team of org?.runtime_teams ?? []) {
    const manager = firstByRole.get(team.manager_role_id)
    if (!manager) continue
    for (const roleId of team.member_role_ids) {
      const member = firstByRole.get(roleId)
      if (member && member.id !== manager.id) addLink(manager.id, member.id, 'team', '团队')
    }
  }

  const kpis: CollaborationKpis = {
    total: withDepth.length,
    available: withDepth.filter(actor => !actor.vacant && actor.state !== 'offline' && actor.state !== 'unknown').length,
    working: withDepth.filter(actor => actor.state === 'working').length,
    waiting: withDepth.filter(actor => actor.state === 'waiting').length,
    blocked: withDepth.filter(actor => actor.state === 'blocked').length,
    projects: tasks.length,
    orders: orders.length,
  }

  const attention: CollaborationAttention[] = []
  for (const actor of withDepth) {
    for (const item of actor.workItems) {
      if (item.state !== 'waiting' && item.state !== 'blocked') continue
      attention.push({
        id: `${actor.id}:${item.id}`,
        label: item.title,
        detail: `${actor.name} · ${item.state === 'blocked' ? '阻塞' : '待你处理'}`,
        actorId: actor.id,
        ...(item.taskId ? { taskId: item.taskId } : {}),
      })
    }
  }
  for (const order of orders) {
    const waiting = order.acceptanceStatus && /pending|review|await|wait/i.test(order.acceptanceStatus)
    if (!waiting) continue
    attention.push({
      id: `order:${order.id}`, label: order.title || order.id,
      detail: `业务工单 · ${order.acceptanceStatus}`,
      ...(order.taskId ? { taskId: order.taskId } : {}),
    })
  }

  return {
    ownerId: OWNER_ID,
    actors: withDepth,
    links,
    truncatedLinks: droppedKeys.size,
    kpis,
    attention: attention.slice(0, 8),
    tasks,
    orders,
    projectCards: buildProjectCards(withDepth, tasks, traceEvents),
    businessCards: buildBusinessCards(withDepth, orders),
  }
}

function buildProjectCards(
  actors: readonly CollaborationActor[],
  tasks: readonly CollaborationTask[],
  traceEvents: readonly TraceEvent[],
): DutyCard[] {
  return tasks.map(task => {
    const participants = actors.filter(actor => actor.currentTaskId === task.id)
    const recent = traceEvents.filter(event => event.taskId === task.id).slice(-3)
    const items: DutyItem[] = recent.length
      ? recent.map(event => ({
          id: `${task.id}:${event.id}`, title: event.title, state: event.phase === 'failed' ? 'blocked' : event.phase === 'review' ? 'waiting' : 'working',
          meta: event.actorId || '执行者未提供',
        }))
      : participants.slice(0, 3).map(actor => ({
          id: `${task.id}:${actor.id}`, title: actor.currentWorkTitle || actor.roleName,
          state: actor.state, meta: actor.name,
        }))
    return {
      id: `task:${task.id}`, kind: 'project' as const,
      title: task.title, subtitle: `会话 · ${task.status}`,
      status: task.status, taskId: task.id,
      actors: participants.map(actor => ({ id: actor.id, name: actor.name, kind: actor.kind })),
      items,
      metric: `${participants.length} 人`,
    }
  })
}

function buildBusinessCards(
  actors: readonly CollaborationActor[],
  orders: readonly CollaborationOrder[],
): DutyCard[] {
  return orders.map(order => {
    const participants: { id: string; name: string; kind: ActorKind }[] = []
    if (order.assigneeEmployeeId) {
      const assigned = actors.find(actor => actor.employeeId === order.assigneeEmployeeId)
      if (assigned) participants.push({ id: assigned.id, name: assigned.name, kind: assigned.kind })
    }
    if (order.taskId) {
      for (const actor of actors) {
        if (actor.currentTaskId === order.taskId && !participants.some(row => row.id === actor.id)) {
          participants.push({ id: actor.id, name: actor.name, kind: actor.kind })
        }
      }
    }
    const items: DutyItem[] = []
    if (order.acceptanceStatus) items.push({ id: `${order.id}:acceptance`, title: '客户验收', state: /pending|review|await|wait/i.test(order.acceptanceStatus) ? 'waiting' : 'working', meta: order.acceptanceStatus })
    if (order.fdePhase) items.push({ id: `${order.id}:fde`, title: 'FDE 阶段', state: 'working', meta: order.fdePhase })
    if (!items.length) items.push({ id: `${order.id}:status`, title: '工单状态', state: 'unknown', meta: order.status })
    return {
      id: `order:${order.id}`, kind: 'business' as const,
      title: order.title || order.customerName || order.id,
      subtitle: [order.customerName, order.brandName].filter(Boolean).join(' · ') || '业务工单',
      status: order.status, ...(order.taskId ? { taskId: order.taskId } : {}),
      actors: participants,
      items,
      metric: order.amount !== undefined ? money(order.amount, order.currency) : '未提供金额',
    }
  })
}

export function money(amount: number, currency?: string): string {
  const code = (currency || 'CNY').toUpperCase()
  const sign = code === 'CNY' ? '¥' : code === 'USD' ? '$' : code === 'EUR' ? '€' : `${code} `
  return `${sign}${amount.toLocaleString('en-US', { maximumFractionDigits: 2 })}`
}

export function actorStateLabel(state: ActorState): string {
  return STATE_LABEL[state]
}
