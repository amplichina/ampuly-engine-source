import type { TraceEvent, TracePhase } from './traceModel'

type Send = (message: Record<string, unknown>) => string
interface PendingRead { projectId: string; orderId: string; taskId: string; resolve: (events: readonly TraceEvent[]) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }
const id = (value: unknown): string => typeof value === 'string' && /^[\p{L}\p{N}_.:-]{1,160}$/u.test(value) ? value : ''
const RUNTIME_TERMINALS = new Set(['turn_completed', 'turn_failed', 'turn_cancelled'])
const LABELS: Record<string, readonly [TracePhase, string]> = {
  work_item_created: ['collaborating', '派出工作项'], delivery_work_item_created: ['collaborating', '派出交付工作项'],
  work_item_claimed: ['executing', '工作项已认领'], work_item_completed: ['unknown', '工作项完成'],
  work_item_failed: ['failed', '工作项失败'], report_work_item_created: ['collaborating', '派出汇报回合'],
  review_work_item_created: ['review', '派出评审回合'], gate_published: ['review', '等待人工确认'],
  gate_decided: ['unknown', '人工决策已记录'], turn_started: ['thinking', '执行开始'],
  turn_completed: ['completed', '本次执行完成'], turn_failed: ['failed', '本次执行失败'],
  turn_cancelled: ['cancelled', '本次执行取消'], tool_started: ['executing', '开始调用工具'],
  tool_completed: ['thinking', '工具已返回'], verification_completed: ['unknown', '校验结果已记录'],
  effect_proposed: ['unknown', '提出外部动作'], effect_approval_requested: ['review', '外部动作等待授权'],
  effect_authorized: ['unknown', '外部动作已授权'], effect_rejected: ['unknown', '外部动作被拒绝'],
  effect_claimed: ['executing', '执行外部动作'], effect_applied: ['unknown', '外部动作执行已记录'],
  effect_failed: ['failed', '外部动作失败'], effect_outcome_unknown: ['unknown', '外部动作结果未知'],
  effect_simulated: ['unknown', '外部动作模拟已记录'], effect_verified: ['unknown', '外部动作核验已记录'],
  order_created: ['unknown', '业务工单已创建'], created: ['unknown', '业务工单已创建'],
  order_closed: ['completed', '业务工单已结案'], closed: ['completed', '业务工单已结案'],
}

/** Converts an already authorized order read into a small display projection.
 * Raw detail, tool arguments, file paths and model text never enter the store.
 * Runtime and business integrity are described separately, never generalized.
 */
export function projectOrderHistory(payload: Record<string, unknown>, projectId: string, orderId: string, taskId: string): readonly TraceEvent[] {
  if (payload.project_id !== projectId || payload.order_id !== orderId || !Array.isArray(payload.entries)) return []
  const launchedTask = id(taskId)
  const task = launchedTask || `order:${id(orderId)}`
  if (!id(projectId) || !id(orderId) || !id(task)) return []
  return payload.entries.slice(-600).flatMap((value, index): TraceEvent[] => {
    if (!value || typeof value !== 'object') return []
    const entry = value as Record<string, unknown>
    const time = typeof entry.at === 'string' ? Date.parse(entry.at) : NaN
    if (!Number.isFinite(time)) return []
    const kind = id(entry.kind)
    const source = ['order', 'gate', 'runtime', 'effect', 'browser'].includes(String(entry.source)) ? String(entry.source) : 'record'
    const [phase, title] = Object.hasOwn(LABELS, kind) ? LABELS[kind] : ['unknown', '历史事件已记录'] as const
    const sourceId = id(entry.event_id)
    const actor = id(entry.actor) || 'engine'
    // Closing a business order says nothing about completion of its runtime
    // turns. Keep order/gate history in its own display group even if a future
    // server starts attaching a related runtime run_id to those records.
    const businessSource = source === 'order' || source === 'gate'
    const run = businessSource ? `business-history:${orderId}` : id(entry.run_id) || `order-history:${orderId}`
    // Only an explicitly terminal runtime turn can lock that run's state.
    // A failed work item/effect is a local event, not proof the whole turn failed.
    const type = source === 'runtime' && RUNTIME_TERMINALS.has(kind) ? kind : `history_${kind || source}`
    const summary = source === 'order' || source === 'gate'
      ? (entry.chain_verified === true ? '来自已校验的业务事件记录。' : '来自业务事件记录；未确认链校验通过。')
      : '来自系统运行记录；不代表已通过业务审计链校验。'
    return [{ id: `history:${orderId}:${source}:${sourceId || `${time}:${index}`}`, sourceId: sourceId || undefined,
      time, projectId, taskId: task, runId: run, actorId: actor, type, phase, title, summary,
      // Synthetic order groups are browsable history, not launchable tasks.
      references: launchedTask ? [{ kind: 'task', id: launchedTask, label: '查看对应执行记录' }] : [] }]
  })
}

/** Single-flight, read-only request over the existing authenticated connection.
 * Legacy order_timeline replies do not echo request_id. Accept only the exact
 * project/order pair while this read is pending. Never retry or write state.
 */
export class ObservatoryHistoryReader {
  private pending: PendingRead | null = null
  constructor(private readonly send: Send, private readonly timeoutMs = 15_000) {}

  read(projectId: string, orderId: string, taskId = ''): Promise<readonly TraceEvent[]> {
    this.cancel()
    if (!id(projectId) || !id(orderId)) return Promise.reject(new Error('历史记录范围无效。'))
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending = null; reject(new Error('读取历史记录超时，请手动重试。')) }, this.timeoutMs)
      this.pending = { projectId, orderId, taskId, resolve, reject, timer }
      try {
        const disposition = this.send({ type: 'order_timeline', project_id: projectId, order_id: orderId })
        if (disposition !== 'sent') this.cancel('当前未连接，历史记录未读取。')
      } catch { this.cancel('历史记录读取失败，请检查连接。') }
    })
  }

  receive(payload: Record<string, unknown>, currentProjectId: string): void {
    const pending = this.pending
    if (!pending || payload.action !== 'order_timeline' || currentProjectId !== pending.projectId
      || payload.project_id !== pending.projectId || payload.order_id !== pending.orderId) return
    clearTimeout(pending.timer); this.pending = null
    if (payload.ok !== true) { pending.reject(new Error('未能读取这张工单的历史记录。')); return }
    if (!Array.isArray(payload.entries)) { pending.reject(new Error('历史记录格式无效，请手动重试。')); return }
    pending.resolve(projectOrderHistory(payload, pending.projectId, pending.orderId, pending.taskId))
  }

  cancel(message = '历史记录读取已取消。'): void {
    const pending = this.pending
    if (!pending) return
    this.pending = null; clearTimeout(pending.timer); pending.reject(new Error(message))
  }
}
