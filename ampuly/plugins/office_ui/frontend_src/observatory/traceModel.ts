import type { SocketStatus, VisualEvent } from '../types/visual'

export type TracePhase = 'idle' | 'thinking' | 'searching' | 'executing' | 'collaborating' | 'review' | 'verifying' | 'completed' | 'failed' | 'cancelled' | 'unknown'
export interface TraceReference { kind: 'artifact' | 'source' | 'task'; id: string; label: string }
export interface TraceMetrics { durationMs?: number; inputTokens?: number; outputTokens?: number; totalTokens?: number; costUsd?: number }
export interface TraceEvent {
  id: string; sourceId?: string; time: number; projectId: string; taskId: string; runId: string
  actorId: string; type: string; phase: TracePhase; title: string; summary: string
  references: readonly TraceReference[]; metrics?: TraceMetrics
}
export interface TraceRun {
  id: string; taskId: string; runId: string; phase: TracePhase; events: readonly TraceEvent[]
  actorIds: readonly string[]; startedAt: number; lastAt: number; metrics?: TraceMetrics
}
export interface TraceSnapshot {
  projectId: string; connection: SocketStatus; runs: readonly TraceRun[]
  events: readonly TraceEvent[]; droppedEvents: number; revision: number
}

type Description = readonly [TracePhase, string, string]
const DESCRIPTIONS: Readonly<Record<string, Description>> = {
  turn_started: ['thinking', '任务开始', '正在分析目标并准备执行。'],
  thinking_delta: ['thinking', '分析中', '模型正在处理当前任务；此处不展示原始推理文本。'],
  assistant_delta: ['executing', '生成回复', '正在整理可交付的回复。'],
  tool_started: ['executing', '调用工具', '正在执行已选定的工具。'],
  tool_progress: ['executing', '工具执行中', '工具仍在运行，等待实际结果。'],
  tool_completed: ['thinking', '工具执行结束', '已收到工具结果，继续推进任务。'],
  tool_failed: ['failed', '工具执行失败', '当前工具未能完成，请查看任务中的错误记录。'],
  status_snapshot: ['unknown', '执行状态同步', '已收到引擎的当前执行状态。'],
  permission_requested: ['review', '等待人工确认', '需要获得授权后才能继续相关操作。'],
  permission_resolved: ['thinking', '人工确认已处理', '已收到本次权限请求的处理结果。'],
  subagent_started: ['collaborating', '协作任务开始', '已分派协作任务，等待执行反馈。'],
  subagent_updated: ['collaborating', '协作进行中', '协作任务状态已更新。'],
  subagent_completed: ['thinking', '协作反馈已到达', '已收到协作任务的反馈。'],
  verification_started: ['verifying', '开始核验', '正在核对本次执行结果。'],
  verification_completed: ['thinking', '核验已结束', '本次核验已返回结果，最终状态以任务记录为准。'],
  turn_completed: ['completed', '任务完成', '本次执行已结束，交付情况以实际产物为准。'],
  turn_failed: ['failed', '任务失败', '本次执行未能完成，需要处理后继续。'],
  turn_cancelled: ['cancelled', '任务已取消', '本次执行已取消。'],
  task_cancelled: ['cancelled', '任务已取消', '本次执行已取消。'],
  cost_update: ['unknown', '费用更新', '已收到引擎记录的用量或费用。'],
  context_usage: ['unknown', '上下文用量更新', '已收到引擎记录的上下文状态。'],
  context_warning: ['unknown', '上下文提醒', '当前上下文接近引擎设定的边界。'],
  checkpoint_saved: ['unknown', '执行检查点已保存', '引擎已记录一个执行检查点。'],
  compaction_applied: ['unknown', '上下文整理完成', '引擎已完成一次上下文整理。'],
  reflect_start: ['thinking', '分析中', '正在分析当前任务。'],
  reflect_done: ['thinking', '分析阶段已更新', '当前分析阶段已更新。'],
  tool_start: ['executing', '调用工具', '正在执行已选定的工具。'],
  tool_done: ['thinking', '工具执行结束', '已收到工具结果，继续推进任务。'],
  waiting: ['idle', '等待任务', '当前没有正在执行的操作。'],
}
const SEARCH_TOOLS = new Set(['read', 'search', 'list', 'fetch', 'file_read', 'file_search', 'list_dir', 'web_search', 'web_fetch', 'browser_snapshot', 'browser_observe'])
const TOOL_LABELS: Readonly<Record<string, string>> = {
  read: '读取资料', search: '检索资料', list: '查看目录', fetch: '读取网页',
  file_read: '读取文件', file_search: '检索文件', list_dir: '查看目录',
  web_search: '联网检索', web_fetch: '读取网页', browser_navigate: '打开网页',
  browser_snapshot: '读取页面', browser_observe: '观察页面', browser_click: '页面交互',
  browser_type: '填写页面', browser_submit_enter: '提交页面', browser_take_screenshot: '页面截图',
  file_write: '写入文件', file_edit: '编辑文件', apply_patch: '应用变更',
  shell_exec: '执行命令', python_exec: '运行分析', agent_spawn: '分派协作',
  todo_read: '查看任务单', todo_write: '更新任务单', workflow_run: '运行流程',
}
const PASSIVE_EVENTS = new Set(['cost_update', 'context_usage', 'context_warning', 'checkpoint_saved', 'compaction_applied', 'reflect_done'])
const TERMINAL_EVENTS = new Set(['turn_completed', 'turn_failed', 'turn_cancelled', 'task_cancelled'])

function text(value: unknown, max = 256): string {
  return typeof value === 'string' ? value.trim().slice(0, max) : ''
}

/** Runtime identities must be identifiers, never a path, URL, or contact field. */
export function sanitizeTraceIdentifier(value: unknown): string | undefined {
  const candidate = text(value, 193)
  if (!/^[\p{L}\p{N}_.:-]{1,192}$/u.test(candidate) || /^[A-Za-z]:/.test(candidate) || /^sk-[\w-]+$/i.test(candidate) || /^(?:86)?1[3-9]\d{9}$/.test(candidate)) return undefined
  return candidate
}

/** Public summaries are opt-in. No arbitrary event text or tool payload is retained. */
export function sanitizePublicSummary(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  const candidate = value.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim()
  if (!candidate || candidate.length > 240) return undefined
  if (/(?:bearer\s|api[ _-]?key|password|secret|token\s*[:=]|sk-[\w-]+|[A-Za-z]:[\\/]|(?:\/Users\/|\/home\/|\\\\)|(?:^|\s)(?:~?\/|\.{1,2}[\\/])[\w./\\-]+|https?:\/\/|[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?:\+?86[ -]?)?1[3-9](?:[ -]?\d){9}\b|[A-Za-z0-9+/_=-]{40,})/i.test(candidate)) return undefined
  return candidate
}

function finiteNumber(...values: unknown[]): number | undefined {
  for (const value of values) {
    if (typeof value === 'number' && Number.isFinite(value) && value >= 0) return value
  }
  return undefined
}

function eventTime(event: VisualEvent): number | undefined {
  const source = event.source_timestamp
  if (typeof source === 'string') {
    const parsed = Date.parse(source)
    if (Number.isFinite(parsed)) return parsed
  }
  const numeric = finiteNumber(source, event.timestamp)
  return numeric === undefined ? undefined : numeric < 100_000_000_000 ? numeric * 1000 : numeric
}

function metrics(data: Record<string, unknown>): TraceMetrics | undefined {
  const result: TraceMetrics = {}
  const values: [keyof TraceMetrics, number | undefined][] = [
    ['durationMs', finiteNumber(data.duration_ms, data.elapsed_ms, data.tool_elapsed_ms)],
    ['inputTokens', finiteNumber(data.input_tokens)],
    ['outputTokens', finiteNumber(data.output_tokens)],
    ['totalTokens', finiteNumber(data.total_tokens)],
    ['costUsd', finiteNumber(data.turn_cost_usd, data.cost_usd)],
  ]
  for (const [key, value] of values) if (value !== undefined) result[key] = value
  return Object.keys(result).length ? Object.freeze(result) : undefined
}

function publicReferences(data: Record<string, unknown>): readonly TraceReference[] {
  if (data.summary_visibility !== 'public' || !Array.isArray(data.public_references)) return Object.freeze([])
  const references: TraceReference[] = []
  for (const value of data.public_references.slice(0, 8)) {
    if (!value || typeof value !== 'object') continue
    const row = value as Record<string, unknown>
    const kind = row.kind
    const id = text(row.id)
    const label = sanitizePublicSummary(row.label)
    const validId = kind === 'task' ? sanitizeTraceIdentifier(id) : /^[\p{L}\p{N}_.-]{1,128}$/u.test(id)
    if ((kind === 'artifact' || kind === 'source' || kind === 'task') && validId && label) {
      references.push(Object.freeze({ kind, id, label }))
    }
  }
  return Object.freeze(references)
}

/** A display projection of real runtime events, not a model's hidden reasoning. */
export function projectVisualEvent(event: VisualEvent, projectId: string): TraceEvent | null {
  const data = event.data ?? {}
  const envelopeProject = text((event as VisualEvent & { project_id?: string }).project_id)
  const dataProject = text(data.project_id)
  if (!projectId || (envelopeProject && envelopeProject !== projectId) || (dataProject && dataProject !== projectId)) return null
  const type = text(event.type)
  const description = Object.hasOwn(DESCRIPTIONS, type) ? DESCRIPTIONS[type] : undefined
  if (!description) return null
  const id = sanitizeTraceIdentifier(event.event_id)
  const time = eventTime(event)
  const taskId = sanitizeTraceIdentifier(data.task_id) || sanitizeTraceIdentifier(data.work_item_id) || sanitizeTraceIdentifier(data.runtime_task_id)
  if (!id || time === undefined || !taskId) return null
  const runId = sanitizeTraceIdentifier(data.run_id) || sanitizeTraceIdentifier(data.canonical_turn_id) || sanitizeTraceIdentifier(data.conversation_turn_id) || sanitizeTraceIdentifier(data.turn_id) || sanitizeTraceIdentifier(data.execution_turn_id) || sanitizeTraceIdentifier(data.runtime_session_id) || taskId
  const sourceId = sanitizeTraceIdentifier(event.source_event_id) || sanitizeTraceIdentifier(data.source_event_id)
  let [phase, title, summary] = description
  if ((type === 'tool_started' || type === 'tool_progress' || type === 'tool_start') && SEARCH_TOOLS.has(text(data.tool_name))) {
    phase = 'searching'; title = '检索与读取'; summary = '正在检索或读取任务所需的信息。'
  }
  if (['tool_started', 'tool_progress', 'tool_completed', 'tool_failed', 'tool_start', 'tool_done'].includes(type)) {
    const toolName = text(data.tool_name)
    const label = Object.hasOwn(TOOL_LABELS, toolName) ? TOOL_LABELS[toolName] : undefined
    if ((type === 'tool_completed' || type === 'tool_done') && (data.success === false || ['failed', 'failure', 'error'].includes(text(data.status).toLowerCase()))) {
      phase = 'failed'; title = '工具执行失败'; summary = '工具报告执行失败，需要处理或调整后继续。'
    }
    if (label) title = `${title} · ${label}`
  }
  if (type === 'status_snapshot') {
    const status = text(data.status)
    if (finiteNumber(data.pending_permission_count) && Number(data.pending_permission_count) > 0) phase = 'review'
    else if (status === 'failed' || status === 'error') phase = 'failed'
    else if (status === 'cancelled' || status === 'canceled') phase = 'cancelled'
    else if (status === 'completed' || status === 'done') phase = 'completed'
    else if (status === 'awaiting_review' || status === 'awaiting_human') phase = 'review'
    else if (status === 'running' || status === 'thinking' || status === 'reflecting') phase = 'thinking'
    else if (status === 'idle') phase = 'idle'
    if (phase === 'thinking' && text(data.current_tool)) phase = 'executing'
  }
  if (type === 'permission_resolved' && (finiteNumber(data.pending_permission_count) ?? 0) > 0) phase = 'review'
  // Only an explicitly public summary enters the projection. Never fall back to
  // text, thinking, arguments, result_summary, paths, or arbitrary JSON.
  if (data.summary_visibility === 'public') summary = sanitizePublicSummary(data.public_summary) ?? summary
  const projectedMetrics = metrics(data)
  return Object.freeze({
    id, ...(sourceId ? { sourceId } : {}), time, projectId, taskId, runId,
    actorId: sanitizeTraceIdentifier(event.agent_id) || sanitizeTraceIdentifier(data.role_id) || 'runtime', type, phase, title, summary,
    references: publicReferences(data), ...(projectedMetrics ? { metrics: projectedMetrics } : {}),
  })
}

export function traceRunKey(event: Pick<TraceEvent, 'taskId' | 'runId'>): string {
  return JSON.stringify([event.taskId, event.runId])
}

export function compareTraceEvents(left: TraceEvent, right: TraceEvent): number {
  return left.time - right.time || left.id.localeCompare(right.id)
}

export function isTerminalTraceEvent(event: TraceEvent): boolean {
  return TERMINAL_EVENTS.has(event.type) || (event.type === 'status_snapshot' && ['completed', 'failed', 'cancelled'].includes(event.phase))
}

export function isToolFailureTraceEvent(event: TraceEvent): boolean {
  return event.type === 'tool_failed' || ((event.type === 'tool_completed' || event.type === 'tool_done') && event.phase === 'failed')
}

/** Rebuild from ordered retained events. Terminal states and review holds are sticky. */
export function buildTraceRun(events: readonly TraceEvent[]): TraceRun {
  if (!events.length) throw new Error('A trace run requires at least one event')
  const ordered = [...events].sort(compareTraceEvents)
  const first = ordered[0]
  let phase: TracePhase = 'unknown'
  let terminal: TracePhase | undefined
  let review = 0
  let failed = false
  let runMetrics: TraceMetrics | undefined
  for (const event of ordered) {
    if (event.type === 'permission_requested') review += 1
    if (event.type === 'permission_resolved') review = Math.max(event.phase === 'review' ? 1 : 0, review - 1)
    if (event.type === 'status_snapshot' && event.phase === 'review') review = Math.max(1, review)
    if (isToolFailureTraceEvent(event)) failed = true
    if (isTerminalTraceEvent(event)) {
      if (!terminal || event.phase === 'failed' || event.phase === 'cancelled') terminal = event.phase
    }
    if (terminal) phase = terminal
    else if (review > 0) phase = 'review'
    else if (failed) phase = 'failed'
    else if (!PASSIVE_EVENTS.has(event.type) && event.phase !== 'unknown') phase = event.phase
    // Keep latest sourced turn usage, never sum repeated cumulative updates.
    // Tool durations belong to the individual event, not the whole run.
    if (event.metrics && (event.type === 'cost_update' || event.type === 'context_usage' || isTerminalTraceEvent(event))) {
      const { durationMs, ...usage } = event.metrics
      runMetrics = { ...runMetrics, ...usage, ...(isTerminalTraceEvent(event) && durationMs !== undefined ? { durationMs } : {}) }
    }
  }
  return Object.freeze({
    id: traceRunKey(first), taskId: first.taskId, runId: first.runId, phase,
    events: Object.freeze(ordered), actorIds: Object.freeze([...new Set(ordered.map(event => event.actorId))]),
    startedAt: first.time, lastAt: ordered[ordered.length - 1].time,
    ...(runMetrics && Object.keys(runMetrics).length ? { metrics: Object.freeze(runMetrics) } : {}),
  })
}
