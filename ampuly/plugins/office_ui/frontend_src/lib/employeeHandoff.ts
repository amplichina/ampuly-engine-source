// 员工交接（4.8 H09/T18）：the owner's view of one project's handoffs. The
// server record is the only fact; this keeps the last list/decision ack.
export interface EmployeeHandoff {
  handoff_id: string
  task_id: string
  from_role: string
  to_role: string
  status: string
  requires_ack: boolean
  summary: string
  artifacts: Array<Record<string, unknown>>
  artifact_materials?: Array<{ name: string; sha256: string; content: string }>
  artifacts_verified?: boolean
  artifacts_error?: string
  source_handoff_id?: string
  rework_of_handoff_id?: string
  attempt?: number
  instance_id?: string
  instance_revision?: string
  package_id?: string
  package_version?: string
  package_fingerprint?: string
  budget_id?: string
  limits?: Record<string, unknown>
  budget?: {
    budget_id?: string
    state?: string
    lifecycle?: string
    limits?: Record<string, number>
    held?: Record<string, number>
    consumed?: Record<string, number>
    available?: Record<string, number | null>
  }
  decision?: string
  decided_by?: string
  created_at: string
  due_at?: string
  entry?: string
  node_id?: string
  workflow_id?: string
  order_id?: string
  acceptance_state?: string
  delivered?: boolean
  origin_check?: {
    role?: string
    check_passed?: boolean
    tool?: string
    tool_passed?: boolean | null
    delivery_status?: string
    code?: string
    acceptance_criteria_count?: number
  }
}

export interface EmployeeHandoffState {
  projectId: string
  handoffs: EmployeeHandoff[]
  counts: Record<string, number>
  offset: number
  hasMore: boolean
  lastAction: string
  lastOk: boolean | null
  error: string
  // Bumps after a successful continue so the card re-reads the list.
  listRefresh: number
  scanTruncated: boolean
}

export const EMPTY_EMPLOYEE_HANDOFFS: EmployeeHandoffState = {
  projectId: '', handoffs: [], counts: {}, offset: 0, hasMore: false,
  lastAction: '', lastOk: null, error: '', listRefresh: 0, scanTruncated: false,
}

/** The post-continue read drops the status filter so the consumed row stays visible. */
export function handoffRefreshQuery(filters: { offset: number; taskId: string }): {
  status?: string
  offset: number
  taskId?: string
} {
  const offset = Number.isFinite(filters.offset) && filters.offset > 0 ? Math.floor(filters.offset) : 0
  const taskId = filters.taskId.trim()
  return { offset, ...(taskId ? { taskId } : {}) }
}

function shiftConsumed(counts: Record<string, number>, from: string): Record<string, number> {
  if (from === 'consumed') return counts
  const next = { ...counts }
  if (typeof next[from] === 'number') next[from] = Math.max(0, next[from] - 1)
  next.consumed = (typeof next.consumed === 'number' ? next.consumed : 0) + 1
  return next
}

export function employeeHandoffsSent(
  prev: EmployeeHandoffState, projectId: string, action: string, sent: boolean,
): EmployeeHandoffState {
  if (!sent) return { ...prev, projectId, lastAction: action, lastOk: false, error: 'send-failed' }
  return { ...prev, projectId, lastAction: action, lastOk: null, error: '' }
}

export function employeeHandoffsAck(
  prev: EmployeeHandoffState, payload: Record<string, unknown>,
): EmployeeHandoffState {
  const projectId = String(payload.project_id ?? prev.projectId ?? '')
  const action = String(payload.action ?? '')
  const ok = payload.ok === true
  const error = String(payload.error ?? '')
  if (!ok) {
    return { ...prev, projectId, lastAction: action, lastOk: false, error: error || 'handoff_operation_failed' }
  }
  if ((action === 'employee_handoff_list' || action === 'employee_handoff_node_list') && Array.isArray(payload.handoffs)) {
    return {
      ...prev, projectId, handoffs: payload.handoffs as EmployeeHandoff[],
      counts: (payload.counts && typeof payload.counts === 'object') ? payload.counts as Record<string, number> : {},
      offset: Number(payload.offset ?? 0) || 0,
      hasMore: payload.has_more === true,
      scanTruncated: payload.truncated === true,
      lastAction: action, lastOk: true, error: '',
    }
  }
  if (action === 'employee_handoff_check') {
    const handoffId = String(payload.handoff_id ?? '')
    const check = payload.origin_check
    return {
      ...prev, projectId,
      handoffs: prev.handoffs.map(item => item.handoff_id === handoffId ? {
        ...item,
        origin_check: check && typeof check === 'object' ? check as EmployeeHandoff['origin_check'] : item.origin_check,
        acceptance_state: String(payload.acceptance_state ?? item.acceptance_state ?? ''),
        delivered: payload.delivered === true,
      } : item),
      lastAction: action, lastOk: true, error: '',
    }
  }
  if (action === 'employee_handoff_continue') {
    const handoffId = String(payload.handoff_id ?? '')
    const current = prev.handoffs.find(item => item.handoff_id === handoffId)
    return {
      ...prev, projectId,
      handoffs: current
        ? prev.handoffs.map(item => item.handoff_id === handoffId ? { ...item, status: 'consumed' } : item)
        : prev.handoffs,
      counts: current && current.status !== 'consumed' ? shiftConsumed(prev.counts, current.status) : prev.counts,
      lastAction: action, lastOk: true, error: '',
      listRefresh: prev.listRefresh + 1,
    }
  }
  const decided = payload.handoff
  if (action === 'employee_handoff_decide' && decided && typeof decided === 'object') {
    const record = decided as EmployeeHandoff
    const found = prev.handoffs.some(item => item.handoff_id === record.handoff_id)
    return {
      ...prev, projectId,
      handoffs: found
        ? prev.handoffs.map(item => item.handoff_id === record.handoff_id ? record : item)
        : [record, ...prev.handoffs],
      lastAction: action, lastOk: true, error: '',
    }
  }
  return { ...prev, projectId, lastAction: action, lastOk: true, error: '' }
}
