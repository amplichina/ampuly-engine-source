/** Exact child-review reads and one owner confirmation on the existing socket. */
export type ReworkScope = Readonly<{ project_id: string; order_id: string; task_id: string }>
type Row = Record<string, unknown>
export interface ReworkPendingRecord {
  record_id: string; review_task_id: string; checkpoint_id: string; revision: string
  request: Readonly<Row>; allowed_actions: readonly string[]; consequence: string
}
export interface ReworkPendingSnapshot {
  records: readonly ReworkPendingRecord[]; readState: 'idle' | 'pending' | 'ready' | 'unknown'
  writeState: 'idle' | 'pending' | 'unknown' | 'confirmed' | 'failed'; checking: boolean; message: string
}
interface Options {
  scope: ReworkScope; send: (request: Readonly<Row>) => 'sent' | 'queued' | 'queue-full' | 'send-failed'
  nextRequestId: () => string; connected: () => boolean
  setTimer: (callback: () => void, ms: number) => unknown; clearTimer: (timer: unknown) => void
}
const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/, SHA = /^[a-f0-9]{64}$/
const fields = ['type', 'project_id', 'order_id', 'task_id', 'review_node_id', 'target_node_id', 'rework_record_id',
  'expected_attempt', 'reviewed_artifact_id', 'reviewed_artifact_version', 'reviewed_content_hash', 'revision_note', 'revision_note_hash']
const row = (value: unknown): value is Row => value !== null && typeof value === 'object' && !Array.isArray(value)
const id = (value: unknown): value is string => typeof value === 'string' && ID.test(value)
const sha = (value: unknown): value is string => typeof value === 'string' && SHA.test(value)
const number = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) > 0
const sameScope = (value: Row, scope: ReworkScope) => value.project_id === scope.project_id && value.order_id === scope.order_id && value.task_id === scope.task_id

function records(value: unknown, scope: ReworkScope): readonly ReworkPendingRecord[] | null {
  if (!Array.isArray(value) || value.length > 32) return null
  const seen = new Set<string>(), result: ReworkPendingRecord[] = []
  for (const item of value) {
    if (!row(item) || !id(item.record_id) || seen.has(item.record_id) || !id(item.review_task_id) || !id(item.checkpoint_id)
      || !sha(item.revision) || typeof item.consequence !== 'string' || !item.consequence.trim() || item.consequence.length > 1000
      || !Array.isArray(item.allowed_actions) || item.allowed_actions.length > 1 || item.allowed_actions.some(action => action !== 'human_review_rework_confirm')
      || !row(item.request) || Object.keys(item.request).length !== fields.length || !fields.every(key => Object.prototype.hasOwnProperty.call(item.request, key))) return null
    const body = item.request
    if (body.type !== 'human_review_rework_confirm' || !sameScope(body, scope) || body.rework_record_id !== item.record_id
      || !['project_id', 'order_id', 'task_id', 'review_node_id', 'target_node_id', 'rework_record_id', 'reviewed_artifact_id'].every(key => id(body[key]))
      || !number(body.expected_attempt) || !number(body.reviewed_artifact_version) || !sha(body.reviewed_content_hash) || !sha(body.revision_note_hash)
      || typeof body.revision_note !== 'string' || !body.revision_note.trim() || new TextEncoder().encode(body.revision_note).byteLength > 16384) return null
    seen.add(item.record_id)
    result.push(Object.freeze({ record_id: item.record_id, review_task_id: item.review_task_id, checkpoint_id: item.checkpoint_id,
      revision: item.revision, request: Object.freeze({ ...body }), allowed_actions: Object.freeze([...item.allowed_actions]), consequence: item.consequence }))
  }
  return Object.freeze(result)
}

export class AIEmployeeReworkPendingController {
  private snapshot: ReworkPendingSnapshot = Object.freeze({ records: [], readState: 'idle', writeState: 'idle', checking: false, message: '' })
  private listeners = new Set<() => void>()
  private read: Row | null = null
  private original: Readonly<Row> | null = null
  private timer: unknown
  private disposed = false
  constructor(private readonly options: Options) {}
  getSnapshot = () => this.snapshot
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  private publish(next: ReworkPendingSnapshot) {
    this.snapshot = Object.freeze(next)
    for (const listener of [...this.listeners]) { try { listener() } catch { /* isolate shared socket */ } }
  }
  private clear() { if (this.timer !== undefined) this.options.clearTimer(this.timer); this.timer = undefined }
  private send(body: Readonly<Row>) {
    let sent = false
    try { sent = this.options.send(body) === 'sent' } catch { /* delivery remains unknown */ }
    if (!sent) { this.timeout(); return }
    // An in-process observer can have already settled this request.
    if (this.snapshot.readState === 'pending' || this.snapshot.writeState === 'pending' || this.snapshot.checking) {
      this.timer = this.options.setTimer(() => { this.timer = undefined; this.timeout() }, 30_000)
    }
  }
  private fresh() { const request = this.options.nextRequestId(); if (!id(request)) throw new Error('invalid_request_id'); return request }
  inspect() {
    if (this.disposed || !this.options.connected() || this.snapshot.readState === 'pending' || this.snapshot.writeState === 'pending' || this.snapshot.checking) return false
    this.clear()
    let requestId: string
    try { requestId = this.fresh() } catch { return false }
    this.read = { type: 'human_review_rework_pending', ...this.options.scope, request_id: requestId }
    this.publish({ ...this.snapshot, records: [], readState: 'pending', message: '正在读取原审核节点的返修记录。' })
    this.send(this.read); return true
  }
  confirm(record: ReworkPendingRecord) {
    if (this.disposed || !this.options.connected() || this.snapshot.readState !== 'ready'
      || ['pending', 'unknown', 'confirmed'].includes(this.snapshot.writeState) || this.snapshot.checking
      || !this.snapshot.records.includes(record) || !record.allowed_actions.includes('human_review_rework_confirm')) return false
    this.clear()
    let requestId: string
    try { requestId = this.fresh() } catch { return false }
    this.original = Object.freeze({ ...record.request, request_id: requestId })
    this.publish({ ...this.snapshot, writeState: 'pending', message: '确认已发出，正在等回执。' })
    this.send(this.original); return true
  }
  lookup() {
    if (this.disposed || !this.options.connected() || !this.original || this.snapshot.writeState !== 'unknown' || this.snapshot.checking || this.snapshot.readState === 'pending') return false
    this.clear()
    this.publish({ ...this.snapshot, checking: true, message: '正在只读核对原确认请求。' })
    this.send({ ...this.original, type: 'human_review_rework_lookup' }); return true
  }
  timeout() {
    if (this.disposed) return
    this.clear()
    this.publish({ ...this.snapshot, records: this.snapshot.readState === 'pending' ? [] : this.snapshot.records,
      readState: this.snapshot.readState === 'pending' ? 'unknown' : this.snapshot.readState,
      writeState: this.snapshot.writeState === 'pending' ? 'unknown' : this.snapshot.writeState, checking: false,
      message: '回执尚未确认；确认请求保留原编号，只能先核对，不能再次重派。' })
  }
  connectionLost() {
    if (this.disposed) return
    this.clear(); this.read = null
    this.publish({ ...this.snapshot, records: [], readState: 'idle', checking: false,
      writeState: this.snapshot.writeState === 'pending' ? 'unknown' : this.snapshot.writeState,
      message: '连接中断；已保留原确认编号，恢复后请手动核对。' })
  }
  receive(payload: unknown): boolean {
    if (this.disposed || !row(payload)) return false
    try {
      if (payload.action === 'human_review_rework_pending' && this.snapshot.readState === 'pending' && this.read
        && payload.request_id === this.read.request_id && sameScope(payload, this.options.scope) && payload.effects === false) {
        const parsed = payload.ok === true ? records(payload.records, this.options.scope) : null
        if (payload.ok !== false && parsed === null) return false
        this.clear(); this.read = null
        this.publish({ ...this.snapshot, readState: parsed ? 'ready' : 'unknown', records: parsed ?? [],
          message: parsed ? (parsed.length ? '已读取固定返修记录。' : '这张工单目前没有待确认的返修。') : '原返修记录暂时无法核实。' })
        return true
      }
      const write = this.original
      if (!write || payload.request_id !== write.request_id || payload.project_id !== write.project_id
        || payload.order_id !== write.order_id || payload.rework_record_id !== write.rework_record_id) return false
      if (payload.action === 'human_review_rework_confirm' && this.snapshot.writeState === 'pending') {
        if (payload.ok === true && !['confirmed', 'already_applied'].includes(String(payload.status))) return false
        this.clear()
        const confirmed = payload.ok === true
        // A server error can follow commit or dispatch failure. Retain original
        // ID and require a read, rather than treating failure as replay permission.
        this.publish({ ...this.snapshot, writeState: confirmed ? 'confirmed' : 'unknown',
          message: confirmed ? '原节点的返修确认已记录；执行进展以工单为准。' : '确认结果待核实；请核对原请求。' })
        return true
      }
      if (payload.action === 'human_review_rework_lookup' && this.snapshot.checking && payload.effects === false) {
        this.clear()
        const confirmed = payload.ok === true && payload.status === 'already_applied'
        this.publish({ ...this.snapshot, checking: false, writeState: confirmed ? 'confirmed' : 'unknown',
          message: confirmed ? '已核实原确认记录；不会再次重派。' : '尚未找到可核实的原确认记录；保持待核实。' })
        return true
      }
      return false
    } catch { return false }
  }
  dispose() { this.disposed = true; this.clear(); this.listeners.clear(); this.read = null; this.original = null }
}
