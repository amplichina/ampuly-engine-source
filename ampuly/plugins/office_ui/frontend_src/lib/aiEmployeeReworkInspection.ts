/** Strict M4 observation wire only. No permission, durable revision, confirmation or dispatch. */
export const REWORK_INSPECT_ACTION = 'human_review_rework_inspect' as const
const ID = /^[A-Za-z0-9_.:-]{1,256}$/
const SHA = /^[a-f0-9]{64}$/
const PRODUCER_GAPS = ['authoritative_attempt_source_unavailable', 'permanent_request_mapping_unavailable'] as const
const IDS = ['record_id', 'checkpoint_id', 'review_node_id', 'node_id', 'request_id'] as const
const HASHES = ['payload_sha256', 'revision_note_hash'] as const
const ARTIFACT_FIELDS = ['reviewed_artifact_id', 'reviewed_artifact_version', 'reviewed_content_hash'] as const
const FIELD_NAMES = [...IDS, ...HASHES, 'attempt', ...ARTIFACT_FIELDS, 'dispatched'] as const
const GAPS = new Set<string>([...PRODUCER_GAPS, 'rework_record_missing',
  ...FIELD_NAMES.flatMap(name => [`${name}_missing`, `${name}_invalid`])])
type Row = Record<string, unknown>
type JSONValue = null | boolean | number | string | JSONValue[] | { [key: string]: JSONValue }

export interface ReworkInspectionRequest {
  readonly type: typeof REWORK_INSPECT_ACTION
  readonly project_id: string
  readonly order_id: string
  readonly task_id: string
  readonly request_id: string
}
export interface ReworkInspectionProjection {
  readonly project_id: string
  readonly order_id: string
  readonly task_id: string
  /** order.launched_task_id; not an EmployeeRun/DelegationRun ID. */
  readonly order_run_task_id: string
  readonly record_id: string | null
  readonly checkpoint_id: string | null
  readonly review_node_id: string | null
  readonly node_id: string | null
  /** Original record request, NEVER the current read correlation. */
  readonly request_id: string | null
  readonly payload_sha256: string | null
  readonly revision_note_hash: string | null
  readonly record_attempt: number | null
  readonly reviewed_artifact: Readonly<{ artifact_id: string; version: number; content_hash: string }> | null
  readonly dispatch_observation: 'no_record' | 'unknown' | 'dispatch_recorded' | 'not_dispatched'
  readonly missing_evidence: readonly string[]
  readonly allowed_actions: readonly [typeof REWORK_INSPECT_ACTION]
  readonly confirm_available: false
  /** Finite public projection hash only: not persistence, permission or source authentication. */
  readonly projection_revision: string
}
export type ReworkInspectionDecision =
  | { readonly kind: 'success'; readonly inspection: ReworkInspectionProjection }
  | { readonly kind: 'failed'; readonly message: string }
  | { readonly kind: 'ignored' | 'invalid' }
export type ReworkInspectionAckPayload =
  | { readonly action: typeof REWORK_INSPECT_ACTION; readonly request_id: string; readonly ok: true;
    readonly effects: false; readonly inspection: ReworkInspectionProjection }
  | { readonly action: typeof REWORK_INSPECT_ACTION; readonly request_id: string | null; readonly ok: false;
    readonly effects: false; readonly error: string; readonly message: string }
export interface ReworkInspectionEnvelope { readonly type: 'ack'; readonly payload: ReworkInspectionAckPayload }
type NormalizedInspectionAck = { readonly type: 'ack' } & ReworkInspectionAckPayload

export class ReworkInspectionDTOError extends Error {
  constructor(readonly code: 'inspection_request_invalid' | 'inspection_projection_invalid') {
    super('返工观察字段尚不能核实；未回显原记录。')
    this.name = 'ReworkInspectionDTOError'
  }
}
function fail(): never { throw new ReworkInspectionDTOError('inspection_projection_invalid') }
const row = (value: unknown): value is Row => value !== null && typeof value === 'object' && !Array.isArray(value)
const valid = (value: unknown, pattern: RegExp): value is string => typeof value === 'string' && pattern.exec(value)?.[0] === value
const integer = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 1
const exact = (value: Row, keys: readonly string[]): boolean => Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key))

/** Detached, bounded JSON copy. Accessors, sparse arrays, symbols and exotic prototypes are rejected. */
function copyJSON(value: unknown): JSONValue {
  let nodes = 0, characters = 0
  const ancestors = new Set<object>()
  const charge = (text: string): void => { if ((characters += text.length) > 2 * 1024 * 1024) fail() }
  function copy(item: unknown, depth: number): JSONValue {
    if (++nodes > 50_000 || depth > 64) return fail()
    if (typeof item === 'string') { charge(item); return item }
    if (item === null || typeof item === 'boolean') return item
    if (typeof item === 'number') return Number.isFinite(item) ? item : fail()
    if (!row(item) && !Array.isArray(item)) return fail()
    if (ancestors.has(item as object)) return fail()
    const proto = Object.getPrototypeOf(item)
    if (!Array.isArray(item) && proto !== Object.prototype && proto !== null) return fail()
    if (Array.isArray(item) && item.length > 50_000) return fail()
    const keys = Reflect.ownKeys(item)
    if (keys.length > 50_001 || keys.some(key => typeof key !== 'string')) return fail()
    ancestors.add(item as object)
    let output: JSONValue
    if (Array.isArray(item)) {
      if (keys.length !== item.length + 1) return fail()
      output = []
      for (let index = 0; index < item.length; index++) {
        const field = Object.getOwnPropertyDescriptor(item, String(index))
        if (!field?.enumerable || !('value' in field)) return fail()
        output.push(copy(field.value, depth + 1))
      }
    } else {
      const result: { [key: string]: JSONValue } = Object.create(null)
      for (const key of keys as string[]) {
        charge(key)
        const field = Object.getOwnPropertyDescriptor(item, key)
        if (!field?.enumerable || !('value' in field)) return fail()
        result[key] = copy(field.value, depth + 1)
      }
      output = result
    }
    ancestors.delete(item as object)
    return output
  }
  const result = copy(value, 0)
  if (new TextEncoder().encode(JSON.stringify(result)).byteLength > 2 * 1024 * 1024) return fail()
  return result
}
function frozen<T>(value: T): T {
  if (value !== null && typeof value === 'object') {
    Object.values(value).forEach(frozen)
    Object.freeze(value)
  }
  return value
}

export function buildReworkInspectionRequest(value: unknown): ReworkInspectionRequest {
  try {
    const request = copyJSON(value)
    if (!row(request) || !exact(request, ['type', 'project_id', 'order_id', 'task_id', 'request_id'])
      || request.type !== REWORK_INSPECT_ACTION || !['project_id', 'order_id', 'task_id', 'request_id'].every(key => valid(request[key], ID))) fail()
    return frozen(request) as unknown as ReworkInspectionRequest
  } catch { throw new ReworkInspectionDTOError('inspection_request_invalid') }
}

export function parseReworkInspectionProjection(value: unknown): ReworkInspectionProjection {
  try {
    const data = copyJSON(value)
    const keys = ['project_id', 'order_id', 'task_id', 'order_run_task_id', ...IDS, ...HASHES,
      'record_attempt', 'reviewed_artifact', 'dispatch_observation', 'missing_evidence', 'allowed_actions', 'confirm_available', 'projection_revision']
    if (!row(data) || !exact(data, keys) || !['project_id', 'order_id', 'task_id', 'order_run_task_id'].every(key => valid(data[key], ID))
      || data.task_id !== data.order_run_task_id || !valid(data.projection_revision, SHA) || data.confirm_available !== false
      || !Array.isArray(data.allowed_actions) || data.allowed_actions.length !== 1 || data.allowed_actions[0] !== REWORK_INSPECT_ACTION) fail()
    for (const name of IDS) if (data[name] !== null && !valid(data[name], ID)) fail()
    for (const name of HASHES) if (data[name] !== null && !valid(data[name], SHA)) fail()
    if (data.record_attempt !== null && !integer(data.record_attempt)) fail()
    const artifact = data.reviewed_artifact
    if (artifact !== null && (!row(artifact) || !exact(artifact, ['artifact_id', 'version', 'content_hash'])
      || !valid(artifact.artifact_id, ID) || !integer(artifact.version) || !valid(artifact.content_hash, SHA))) fail()
    const evidence = data.missing_evidence
    if (!Array.isArray(evidence) || evidence.length > GAPS.size
      || evidence.some(gap => typeof gap !== 'string' || !GAPS.has(gap))
      || new Set(evidence).size !== evidence.length
      || PRODUCER_GAPS.some((gap, index) => evidence[index] !== gap)
      || typeof data.dispatch_observation !== 'string'
      || !['no_record', 'unknown', 'dispatch_recorded', 'not_dispatched'].includes(data.dispatch_observation)) fail()
    const gaps = new Set(evidence as string[])
    const count = (name: string): number => Number(gaps.has(`${name}_missing`)) + Number(gaps.has(`${name}_invalid`))
    if (data.dispatch_observation === 'no_record') {
      if ([...IDS, ...HASHES, 'record_attempt', 'reviewed_artifact'].some(key => data[key] !== null)
        || evidence.length !== 3 || evidence[2] !== 'rework_record_missing') fail()
    } else {
      if (gaps.has('rework_record_missing')) fail()
      for (const name of [...IDS, ...HASHES]) if (count(name) !== (data[name] === null ? 1 : 0)) fail()
      if (count('attempt') !== (data.record_attempt === null ? 1 : 0)) fail()
      if (ARTIFACT_FIELDS.some(name => count(name) > 1)
        || (artifact === null ? !ARTIFACT_FIELDS.some(name => count(name) === 1) : ARTIFACT_FIELDS.some(name => count(name) !== 0))) fail()
      if (data.dispatch_observation === 'unknown') { if (count('dispatched') !== 1) fail() }
      else if (count('dispatched') !== 0) fail()
    }
    return frozen(data) as unknown as ReworkInspectionProjection
  } catch { throw new ReworkInspectionDTOError('inspection_projection_invalid') }
}

const FAILURE_WORDS: Record<string, string> = {
  owner_required: '当前账户无权核对这份返工记录。',
  employee_rework_inspection_scope_invalid: '当前订单与任务的读取范围尚不能核实。',
  employee_rework_observation_invalid: '原返工记录字段尚不能核实。',
}
function parseAckPayload(value: unknown): ReworkInspectionAckPayload {
  const payload = copyJSON(value)
  if (!row(payload) || payload.action !== REWORK_INSPECT_ACTION || payload.effects !== false || typeof payload.ok !== 'boolean'
    || !(valid(payload.request_id, ID) || (payload.ok === false && payload.request_id === null))) return fail()
  if (payload.ok) {
    if (!exact(payload, ['action', 'request_id', 'ok', 'effects', 'inspection'])) return fail()
    const inspection = parseReworkInspectionProjection(payload.inspection)
    return frozen({ ...payload, inspection }) as unknown as ReworkInspectionAckPayload
  } else if (!exact(payload, ['action', 'request_id', 'ok', 'effects', 'error', 'message'])
    || !valid(payload.error, ID) || typeof payload.message !== 'string') return fail()
  return frozen(payload) as unknown as ReworkInspectionAckPayload
}
/** Strict real WS envelope -> private normalized reply. Never accept a flat wire.
 * Error strings remain input data here; only acceptInspectionAck supplies safe display copy.
 */
export function parseInspectionEnvelope(value: unknown): NormalizedInspectionAck {
  try {
    const envelope = copyJSON(value)
    if (!row(envelope) || !exact(envelope, ['type', 'payload']) || envelope.type !== 'ack') return fail()
    const payload = parseAckPayload(envelope.payload)
    return frozen({ type: 'ack' as const, ...payload })
  } catch { return fail() }
}
/** For the existing onAck(payload) hook only. Fixed payload keys, no guessed type or network. */
export function envelopeFromAckPayload(value: unknown): ReworkInspectionEnvelope {
  try { return frozen({ type: 'ack' as const, payload: parseAckPayload(value) }) }
  catch { return fail() }
}
/** Private normalized host reply parser; NOT a WS entry point and deliberately not exported. */
function acceptNormalizedInspectionAck(request: ReworkInspectionRequest, value: unknown): ReworkInspectionDecision {
  try {
    const current = buildReworkInspectionRequest(request)
    const ack = copyJSON(value)
    if (!row(ack) || ack.type !== 'ack' || ack.action !== REWORK_INSPECT_ACTION) return { kind: 'invalid' }
    if (ack.request_id !== current.request_id) return { kind: 'ignored' }
    if (ack.effects !== false || typeof ack.ok !== 'boolean') return { kind: 'invalid' }
    if (ack.ok) {
      if (!exact(ack, ['type', 'action', 'request_id', 'ok', 'effects', 'inspection'])) return { kind: 'invalid' }
      const inspection = parseReworkInspectionProjection(ack.inspection)
      if (inspection.project_id !== current.project_id || inspection.order_id !== current.order_id || inspection.task_id !== current.task_id) return { kind: 'ignored' }
      return { kind: 'success', inspection }
    }
    if (!exact(ack, ['type', 'action', 'request_id', 'ok', 'effects', 'error', 'message'])
      || !valid(ack.error, ID) || typeof ack.message !== 'string') return { kind: 'invalid' }
    return { kind: 'failed', message: Object.hasOwn(FAILURE_WORDS, ack.error) ? FAILURE_WORDS[ack.error]
      : '原返工记录尚不能核对；本次读取未证明记录存在或任何重派结果。' }
  } catch { return { kind: 'invalid' } }
}

/** Public never-throw WS entry: strict envelope, exact read correlation, no private error echo. */
export function acceptInspectionAck(request: ReworkInspectionRequest, value: unknown): ReworkInspectionDecision {
  try { return acceptNormalizedInspectionAck(request, parseInspectionEnvelope(value)) }
  catch { return { kind: 'invalid' } }
}
