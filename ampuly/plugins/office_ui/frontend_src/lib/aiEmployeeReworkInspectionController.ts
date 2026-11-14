/** One bounded manual observation read. No socket, clock, timer, runtime or write receipt. */
import { acceptInspectionAck, buildReworkInspectionRequest, envelopeFromAckPayload, REWORK_INSPECT_ACTION,
  type ReworkInspectionEnvelope, type ReworkInspectionRequest } from './aiEmployeeReworkInspection'

export const REWORK_INSPECTION_TIMEOUT_MS = 30_000 as const
/** Exact existing VisualSocketClient.send return domain; only sent is a started read here. */
export type ReworkInspectionSendDisposition = 'sent' | 'queued' | 'queue-full' | 'send-failed'
export interface ReworkInspectionScope { readonly project_id: string; readonly order_id: string; readonly task_id: string }
export interface ReworkInspectionContext {
  readonly owner: boolean
  readonly connected: boolean
  readonly scope: ReworkInspectionScope | null
  /** Host-owned opaque identity/scope epoch. Not a credential; never sent or publicly exposed. */
  readonly context_revision: string
}
export interface ReworkInspectionSnapshot {
  readonly generation: number
  readonly owner: boolean
  readonly connected: boolean
  readonly scope: ReworkInspectionScope | null
  readonly state: 'idle' | 'pending' | 'received' | 'unknown'
  readonly request: ReworkInspectionRequest | null
  readonly ack: ReworkInspectionEnvelope | null
  readonly message: string
  readonly disposed: boolean
}
export type ReworkInspectionStart =
  | { readonly kind: 'pending'; readonly request_id: string; readonly generation: number; readonly timeout_ms: 30000 }
  | { readonly kind: 'settled' | 'unknown'; readonly request_id: string; readonly generation: number }
  | { readonly kind: 'blocked' | 'superseded' }
export interface ReworkInspectionControllerOptions {
  send: (request: ReworkInspectionRequest) => ReworkInspectionSendDisposition
  /** Random fresh ASCII prefix; this controller appends its monotonic generation. */
  newRequestId: () => string
  /** May impose a stricter finite lifetime, also useful for synthetic exhaustion cases. */
  generationLimit?: number
}
type Row = Record<string, unknown>
const ID = /^[A-Za-z0-9_.:-]{1,256}$/
const PREFIX = /^[A-Za-z0-9_.:-]{1,239}$/
const SAFE_FAILURES = new Set(['owner_required', 'employee_rework_inspection_scope_invalid', 'employee_rework_observation_invalid'])
const valid = (value: unknown, pattern: RegExp): value is string => typeof value === 'string' && pattern.exec(value)?.[0] === value
function validContextRevision(value: unknown): value is string {
  if (typeof value !== 'string' || value.length < 1 || value.length > 2048 || /[\u0000-\u001f\u007f-\u009f]/.test(value)) return false
  for (let index = 0; index < value.length; index++) {
    const code = value.charCodeAt(index)
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(++index)
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false
    } else if (code >= 0xdc00 && code <= 0xdfff) return false
  }
  return new TextEncoder().encode(value).byteLength <= 4096
}
function fields(value: unknown, names: readonly string[]): Row | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null
  const proto = Object.getPrototypeOf(value)
  if (proto !== Object.prototype && proto !== null) return null
  const keys = Reflect.ownKeys(value)
  if (keys.length !== names.length || !names.every(name => keys.includes(name))) return null
  const result: Row = Object.create(null)
  for (const name of names) {
    const field = Object.getOwnPropertyDescriptor(value, name)
    if (!field?.enumerable || !('value' in field)) return null
    result[name] = field.value
  }
  return result
}
function context(value: unknown): ReworkInspectionContext | null {
  const data = fields(value, ['owner', 'connected', 'scope', 'context_revision'])
  if (!data || typeof data.owner !== 'boolean' || typeof data.connected !== 'boolean' || !validContextRevision(data.context_revision)) return null
  let scope: ReworkInspectionScope | null = null
  if (data.scope !== null) {
    const ids = fields(data.scope, ['project_id', 'order_id', 'task_id'])
    if (!ids || !valid(ids.project_id, ID) || !valid(ids.order_id, ID) || !valid(ids.task_id, ID)) return null
    scope = Object.freeze({ project_id: ids.project_id, order_id: ids.order_id, task_id: ids.task_id })
  }
  return { owner: data.owner, connected: data.connected, scope, context_revision: data.context_revision }
}
function sameScope(left: ReworkInspectionScope | null, right: ReworkInspectionScope | null): boolean {
  return left === null ? right === null : right !== null && left.project_id === right.project_id
    && left.order_id === right.order_id && left.task_id === right.task_id
}

export class AIEmployeeReworkInspectionController {
  private snapshot: ReworkInspectionSnapshot = Object.freeze({ generation: 1, owner: false, connected: false,
    scope: null, state: 'idle', request: null, ack: null, message: '原返工记录尚未读取。', disposed: false })
  private contextRevision: string | null = null
  private launching = false
  private notifying = false
  private readonly listeners = new Set<() => void>()
  private readonly generationLimit: number
  private readonly send: ReworkInspectionControllerOptions['send']
  private readonly newRequestId: ReworkInspectionControllerOptions['newRequestId']
  constructor(options: ReworkInspectionControllerOptions) {
    const limit = options.generationLimit ?? Number.MAX_SAFE_INTEGER
    if (typeof options.send !== 'function' || typeof options.newRequestId !== 'function'
      || !Number.isSafeInteger(limit) || limit < 1 || limit > Number.MAX_SAFE_INTEGER) {
      throw new Error('返工只读控制器依赖无效。')
    }
    this.generationLimit = limit
    this.send = options.send
    this.newRequestId = options.newRequestId
  }
  getSnapshot = (): ReworkInspectionSnapshot => this.snapshot
  subscribe = (listener: () => void): (() => void) => {
    if (this.snapshot.disposed) return () => {}
    if (typeof listener !== 'function' || this.listeners.size >= 32) throw new Error('返工读取订阅不可用。')
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }
  private publish(snapshot: ReworkInspectionSnapshot): void {
    this.snapshot = Object.freeze(snapshot)
    if (this.notifying) return
    this.notifying = true
    try {
      for (const listener of [...this.listeners]) {
        if (this.snapshot.disposed) break
        try { listener() } catch { /* never log or throw through a shared onAck handler */ }
      }
    } finally { this.notifying = false }
  }
  private advance(): number | null {
    if (this.snapshot.disposed) return null
    if (this.snapshot.generation >= this.generationLimit) {
      this.publish({ ...this.snapshot, state: 'idle', request: null, ack: null,
        message: '读取代次已耗尽；此控制器不再发送请求。' })
      return null
    }
    return this.snapshot.generation + 1
  }
  /** Every actor/context/connection/scope change invalidates the old read. Invalid input fails closed. */
  updateContext(value: unknown): boolean {
    if (this.snapshot.disposed) return false
    let next: ReworkInspectionContext | null = null
    try { next = context(value) } catch { /* malformed host values fail closed */ }
    if (this.snapshot.disposed) return false
    const owner = next?.owner ?? false, connected = next?.connected ?? false, scope = next?.scope ?? null
    const revision = next?.context_revision ?? null
    if (owner === this.snapshot.owner && connected === this.snapshot.connected
      && sameScope(scope, this.snapshot.scope) && revision === this.contextRevision) return next !== null
    const generation = this.advance()
    if (this.snapshot.disposed) return false
    this.contextRevision = revision
    this.publish({ generation: generation ?? this.snapshot.generation, owner, connected, scope,
      state: 'idle', request: null, ack: null, disposed: false,
      message: generation === null ? '读取代次已耗尽；此控制器不再发送请求。'
        : next ? '读取范围已更新；仅手动核对，不使用旧观察。' : '当前读取范围尚不能核实。' })
    return next !== null
  }
  private pending(requestId: string, generation: number): boolean {
    return !this.snapshot.disposed && this.snapshot.owner && this.snapshot.connected && this.snapshot.scope !== null
      && this.snapshot.state === 'pending' && this.snapshot.generation === generation && this.snapshot.request?.request_id === requestId
  }
  /** Explicit user read only. Prefix/transport exceptions are fixed local states, never retries. */
  inspect(): ReworkInspectionStart {
    if (this.launching) return { kind: 'blocked' }
    this.launching = true
    try { return this.inspectChecked() } finally { this.launching = false }
  }
  private inspectChecked(): ReworkInspectionStart {
    const before = this.snapshot
    if (before.disposed || !before.owner || !before.connected || !before.scope || before.state === 'pending') return { kind: 'blocked' }
    const generation = this.advance()
    if (generation === null) return { kind: 'blocked' }
    this.publish({ ...before, generation, state: 'idle', request: null, ack: null, message: '正在准备原记录读取。' })
    if (this.snapshot.disposed || this.snapshot.generation !== generation) return { kind: 'superseded' }
    let request: ReworkInspectionRequest
    try {
      const prefix = this.newRequestId()
      if (!valid(prefix, PREFIX)) throw new Error()
      request = buildReworkInspectionRequest({ type: REWORK_INSPECT_ACTION, ...before.scope, request_id: `${prefix}:${generation}` })
    } catch {
      if (!this.snapshot.disposed && this.snapshot.generation === generation) this.publish({ ...this.snapshot,
        state: 'idle', request: null, ack: null, message: '无法建立本次读取关联；未发送请求。' })
      return { kind: 'blocked' }
    }
    if (this.snapshot.disposed || this.snapshot.generation !== generation) return { kind: 'superseded' }
    this.publish({ ...this.snapshot, state: 'pending', request, ack: null, message: '正在核对原返工记录。' })
    if (!this.pending(request.request_id, generation)) return { kind: 'superseded' }
    let disposition: ReworkInspectionSendDisposition | null = null
    try { disposition = this.send(request) } catch { /* no private exception or implicit resend */ }
    if (!this.pending(request.request_id, generation)) {
      return this.snapshot.generation === generation && this.snapshot.state === 'received'
        ? { kind: 'settled', request_id: request.request_id, generation } : { kind: 'superseded' }
    }
    if (disposition !== 'sent') {
      this.publish({ ...this.snapshot, state: 'unknown', ack: null, message: '本次读取未确认为已发送；本控制器不会自动重发。' })
      return { kind: 'unknown', request_id: request.request_id, generation }
    }
    return { kind: 'pending', request_id: request.request_id, generation, timeout_ms: REWORK_INSPECTION_TIMEOUT_MS }
  }
  /** Existing onAck(payload) hook; malformed/stale input never throws or clears the current pending read. */
  receiveAckPayload(payload: unknown): boolean {
    if (this.snapshot.disposed || this.snapshot.state !== 'pending' || !this.snapshot.request) return false
    try {
      const request = this.snapshot.request, generation = this.snapshot.generation
      const envelope = envelopeFromAckPayload(payload)
      const decision = acceptInspectionAck(request, envelope)
      if (decision.kind !== 'success' && decision.kind !== 'failed') return false
      if (!this.pending(request.request_id, generation)) return false
      let ack = envelope
      if (decision.kind === 'failed' && !envelope.payload.ok) {
        // Retain only finite local error copy; never expose a raw private server message/code via snapshot.
        ack = envelopeFromAckPayload({ action: REWORK_INSPECT_ACTION, request_id: request.request_id, ok: false, effects: false,
          error: SAFE_FAILURES.has(envelope.payload.error) ? envelope.payload.error : 'inspection_read_failed', message: decision.message })
      }
      this.publish({ ...this.snapshot, state: 'received', ack,
        message: decision.kind === 'success' ? '已读取有限原记录观察；不是确认重派或派发成功证明。' : decision.message })
      return true
    } catch { return false }
  }
  /** Host schedules once at 30s and cancels on context/settlement/dispose. No clock/timer in this leaf. */
  timeout(requestId: string, generation: number): boolean {
    if (!Number.isSafeInteger(generation) || generation < 1 || !this.pending(requestId, generation)) return false
    this.publish({ ...this.snapshot, state: 'unknown', ack: null, message: '读取已超时，结果未确认；旧观察已隐藏，请手动重新核对。' })
    return true
  }
  dispose(): void {
    if (this.snapshot.disposed) return
    this.listeners.clear()
    this.contextRevision = null
    this.snapshot = Object.freeze({ ...this.snapshot, owner: false, connected: false, scope: null,
      state: 'idle', request: null, ack: null, message: '读取控制器已关闭。', disposed: true })
  }
}
