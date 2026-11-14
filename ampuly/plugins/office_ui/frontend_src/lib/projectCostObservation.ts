/** Pure, bounded display DTO. No billing inference, currency conversion or I/O. */
export const PROJECT_COST_BASE_GAPS = Object.freeze([
  'pricing_status_not_persisted', 'call_coverage_unverified',
  'not_supplier_reconciled', 'actual_currency_unverified',
] as const)
export const PROJECT_COST_OPTIONAL_GAPS = Object.freeze([
  'unreadable', 'no_records', 'legacy_zero_ambiguous', 'invalid_record',
  'record_limit_exceeded', 'invalid_payload',
] as const)
export type ProjectCostObservationGap = typeof PROJECT_COST_BASE_GAPS[number]
  | typeof PROJECT_COST_OPTIONAL_GAPS[number]
export type ProjectCostObservationUnknownReason = Exclude<typeof PROJECT_COST_OPTIONAL_GAPS[number], 'legacy_zero_ambiguous'>
export interface ProjectCostRun {
  readonly run_index: number
  readonly recorded_subtotal_text: string
  readonly calls: number
}
export interface ProjectCostObservation {
  readonly project_id: string
  readonly state: 'legacy_recorded' | 'unknown'
  readonly recorded_subtotal_text: string | null
  /** Historical field label only, never an asserted supplier currency. */
  readonly currency_label: 'USD'
  readonly currency_basis: 'legacy_field_label_unverified'
  readonly basis: 'legacy_cost_record'
  readonly actual_currency: null
  readonly actual_total: null
  readonly calls: number | null
  readonly task_count: number | null
  readonly gaps: readonly ProjectCostObservationGap[]
  /** Same walk as the subtotal. No task key and no second calculator. */
  readonly runs: readonly ProjectCostRun[] | null
}

type Row = Record<string, unknown>
const FIELDS = Object.freeze(['project_id', 'state', 'recorded_subtotal_text', 'currency_label',
  'currency_basis', 'basis', 'actual_currency', 'actual_total', 'calls', 'task_count', 'gaps', 'runs'] as const)
const RUN_FIELDS = Object.freeze(['run_index', 'recorded_subtotal_text', 'calls'] as const)
const GAP_ORDER = Object.freeze([...PROJECT_COST_BASE_GAPS, ...PROJECT_COST_OPTIONAL_GAPS])
const GAP_SET: ReadonlySet<string> = new Set(GAP_ORDER)
const FATAL_GAPS: ReadonlySet<string> = new Set(['unreadable', 'no_records', 'invalid_record', 'record_limit_exceeded', 'invalid_payload'])
const ID = /^[A-Za-z0-9_.:-]{1,256}$/
const DECIMAL = /^(?:0|[1-9][0-9]*)(?:\.[0-9]{0,17}[1-9])?$/
const validProject = (value: unknown): value is string => typeof value === 'string'
  && ID.exec(value)?.[0] === value
const count = (value: unknown, maximum: number): value is number => typeof value === 'number'
  && Number.isSafeInteger(value) && value >= 0 && value <= maximum

function row(value: unknown): Row | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null
  const prototype = Object.getPrototypeOf(value)
  if (prototype !== Object.prototype && prototype !== null) return null
  const keys = Reflect.ownKeys(value)
  if (keys.length !== FIELDS.length || !FIELDS.every(name => keys.includes(name))) return null
  const result: Row = Object.create(null)
  for (const name of FIELDS) {
    const descriptor = Object.getOwnPropertyDescriptor(value, name)
    if (!descriptor?.enumerable || !('value' in descriptor)) return null
    result[name] = descriptor.value
  }
  return result
}

function amount(value: unknown): value is string {
  // The maximum canonical amount has 13 integer digits and 18 fractional digits.
  if (typeof value !== 'string' || value.length < 1 || value.length > 32 || DECIMAL.exec(value)?.[0] !== value) return false
  const [integer, fraction] = value.split('.')
  return integer.length < 13 || (integer === '1000000000000' && fraction === undefined)
}

function runs(value: unknown, taskCount: number | null, state: string): readonly ProjectCostRun[] | null | undefined {
  if (state === 'unknown') return value === null ? null : undefined
  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) return undefined
  const lengthDescriptor = Object.getOwnPropertyDescriptor(value, 'length')
  const length: unknown = lengthDescriptor && 'value' in lengthDescriptor ? lengthDescriptor.value : null
  if (!count(length, 5000) || length !== taskCount || Reflect.ownKeys(value).length !== length + 1) return undefined
  const parsed: ProjectCostRun[] = []
  for (let index = 0; index < length; index++) {
    const descriptor = Object.getOwnPropertyDescriptor(value, String(index))
    const row = descriptor?.enumerable && descriptor && 'value' in descriptor ? descriptor.value : null
    if (row === null || typeof row !== 'object' || Array.isArray(row)) return undefined
    if (Object.getPrototypeOf(row) !== Object.prototype && Object.getPrototypeOf(row) !== null) return undefined
    const keys = Reflect.ownKeys(row)
    if (keys.length !== RUN_FIELDS.length || !RUN_FIELDS.every(name => keys.includes(name))) return undefined
    const fields: Record<string, unknown> = Object.create(null)
    for (const name of RUN_FIELDS) {
      const field = Object.getOwnPropertyDescriptor(row, name)
      if (!field?.enumerable || !('value' in field)) return undefined
      fields[name] = field.value
    }
    if (fields.run_index !== index + 1 || !amount(fields.recorded_subtotal_text) || !count(fields.calls, Number.MAX_SAFE_INTEGER)) return undefined
    parsed.push(Object.freeze({
      run_index: fields.run_index,
      recorded_subtotal_text: fields.recorded_subtotal_text,
      calls: fields.calls,
    }))
  }
  return Object.freeze(parsed)
}

function gaps(value: unknown): readonly ProjectCostObservationGap[] | null {
  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) return null
  const lengthDescriptor = Object.getOwnPropertyDescriptor(value, 'length')
  const length: unknown = lengthDescriptor && 'value' in lengthDescriptor ? lengthDescriptor.value : null
  if (!count(length, GAP_ORDER.length) || length < PROJECT_COST_BASE_GAPS.length
    || Reflect.ownKeys(value).length !== length + 1) return null
  const seen = new Set<string>()
  for (let index = 0; index < length; index++) {
    const descriptor = Object.getOwnPropertyDescriptor(value, String(index))
    if (!descriptor?.enumerable || !('value' in descriptor) || typeof descriptor.value !== 'string'
      || !GAP_SET.has(descriptor.value) || seen.has(descriptor.value)) return null
    seen.add(descriptor.value)
  }
  if (!PROJECT_COST_BASE_GAPS.every(gap => seen.has(gap))) return null
  return Object.freeze(GAP_ORDER.filter(gap => seen.has(gap)))
}

/** Invalid local scope has no DTO: never echo it or invent a default project. */
export function unknownProjectCostObservation(projectId: unknown,
  reason: ProjectCostObservationUnknownReason = 'invalid_payload'): ProjectCostObservation | null {
  if (!validProject(projectId)) return null
  const safeReason = typeof reason === 'string' && FATAL_GAPS.has(reason) ? reason : 'invalid_payload'
  return Object.freeze({ project_id: projectId, state: 'unknown', recorded_subtotal_text: null,
    currency_label: 'USD', currency_basis: 'legacy_field_label_unverified', basis: 'legacy_cost_record',
    actual_currency: null, actual_total: null, calls: null, task_count: null,
    gaps: Object.freeze([...PROJECT_COST_BASE_GAPS, safeReason as ProjectCostObservationUnknownReason]),
    runs: null })
}

/** A malformed or cross-project explicit observation clears the old display. */
export function parseProjectCostObservation(value: unknown, projectId: unknown): ProjectCostObservation | null {
  const unknown = unknownProjectCostObservation(projectId)
  if (!unknown) return null
  try {
    const data = row(value)
    if (!data || data.project_id !== projectId || !validProject(data.project_id)
      || (data.state !== 'legacy_recorded' && data.state !== 'unknown')
      || data.currency_label !== 'USD' || data.currency_basis !== 'legacy_field_label_unverified'
      || data.basis !== 'legacy_cost_record' || data.actual_currency !== null || data.actual_total !== null) return unknown
    const safeGaps = gaps(data.gaps)
    if (!safeGaps) return unknown
    let taskCount: number | null
    if (data.state === 'unknown') {
      if (data.recorded_subtotal_text !== null || data.calls !== null || data.task_count !== null) return unknown
      taskCount = null
    } else {
      if (!amount(data.recorded_subtotal_text) || !count(data.calls, Number.MAX_SAFE_INTEGER)
        || !count(data.task_count, 5000) || safeGaps.some(gap => FATAL_GAPS.has(gap))
        || (data.recorded_subtotal_text === '0' && !safeGaps.includes('legacy_zero_ambiguous'))) return unknown
      taskCount = data.task_count
    }
    const safeRuns = runs(data.runs, taskCount, data.state)
    if (safeRuns === undefined) return unknown
    return Object.freeze({ project_id: data.project_id, state: data.state,
      recorded_subtotal_text: data.recorded_subtotal_text as string | null,
      currency_label: 'USD', currency_basis: 'legacy_field_label_unverified', basis: 'legacy_cost_record',
      actual_currency: null, actual_total: null, calls: data.calls as number | null,
      task_count: data.task_count as number | null, gaps: safeGaps, runs: safeRuns })
  } catch { return unknown }
}

/** Missing keys preserve partial-reply semantics; snake presence wins even when null. */
export function recordCostObservationFrom(payload: unknown, projectId: unknown): ProjectCostObservation | null | undefined {
  try {
    if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) return parseProjectCostObservation(null, projectId)
    const prototype = Object.getPrototypeOf(payload)
    if (prototype !== Object.prototype && prototype !== null) return parseProjectCostObservation(null, projectId)
    for (const key of ['recorded_cost_observation', 'recordedCostObservation'] as const) {
      const descriptor = Object.getOwnPropertyDescriptor(payload, key)
      if (descriptor) return parseProjectCostObservation(descriptor.enumerable && 'value' in descriptor ? descriptor.value : null, projectId)
    }
    return undefined
  } catch { return parseProjectCostObservation(null, projectId) }
}

function sameRuns(stored: unknown, validated: readonly ProjectCostRun[] | null): boolean {
  if (validated === null) return stored === null
  if (!Array.isArray(stored) || !Object.isFrozen(stored) || stored.length !== validated.length) return false
  return validated.every((run, index) => {
    const item: unknown = stored[index]
    if (item === null || typeof item !== 'object') return false
    const fields = item as ProjectCostRun
    return fields.run_index === run.run_index
      && fields.recorded_subtotal_text === run.recorded_subtotal_text
      && fields.calls === run.calls
  })
}

/** Absence retains only this project's prior observation; explicit unknown clears success. */
export function applyProjectCostObservation(previous: ProjectCostObservation | null | undefined,
  next: ProjectCostObservation | null | undefined, projectId: unknown): ProjectCostObservation | null {
  if (!validProject(projectId)) return null
  if (next !== undefined) return parseProjectCostObservation(next, projectId)
  try {
    const data = row(previous)
    if (!data || data.project_id !== projectId) return null
    const validated = parseProjectCostObservation(previous, projectId)!
    // Keep stable internal state for unrelated partial replies. Mutable callers
    // receive a detached snapshot, and invalid prior values are never retained.
    if (Object.isFrozen(previous) && Object.isFrozen(data.gaps)
      && FIELDS.every(key => key === 'gaps' || key === 'runs' || data[key] === validated[key])
      && Array.isArray(data.gaps) && data.gaps.length === validated.gaps.length
      && validated.gaps.every((gap, index) => data.gaps instanceof Array && data.gaps[index] === gap)
      && sameRuns(data.runs, validated.runs)) return previous!
    return validated
  } catch { return null }
}
