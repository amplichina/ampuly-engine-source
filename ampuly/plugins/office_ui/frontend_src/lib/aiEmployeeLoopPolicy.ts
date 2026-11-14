export type EmployeeLoopField =
  | 'max_steps'
  | 'max_model_calls'
  | 'max_wall_clock_s'
  | 'max_rework'
  | 'max_retries'
  | 'max_sub_agents'
  | 'max_cost_cny'

export interface EmployeeLoopPolicyState {
  readonly kind: 'missing' | 'present' | 'invalid'
  readonly fields: Readonly<Partial<Record<EmployeeLoopField, string>>>
}

export interface EmployeeLoopPolicyProblem {
  readonly path: string
  readonly message: string
  readonly severity: 'error' | 'warning'
}

export type EmployeeLoopPolicyValue = Readonly<Partial<Record<Exclude<EmployeeLoopField, 'max_cost_cny'>, number>> & {
  readonly max_cost_cny?: string
}>

type LoopErrorCode = 'employee_loop_policy_invalid' | 'employee_loop_policy_too_large' | 'employee_loop_policy_removal_unsupported'

const MESSAGES: Readonly<Record<LoopErrorCode, string>> = Object.freeze({
  employee_loop_policy_invalid: '自主执行上限格式无效。',
  employee_loop_policy_too_large: '自主执行上限输入过长。',
  employee_loop_policy_removal_unsupported: '不能删除整个自主执行上限；请恢复打开时的内容或明确重新配置。',
})

export class EmployeeLoopPolicyError extends Error {
  readonly code: LoopErrorCode

  constructor(code: LoopErrorCode = 'employee_loop_policy_invalid') {
    const safeCode = code === 'employee_loop_policy_too_large' || code === 'employee_loop_policy_removal_unsupported' ? code : 'employee_loop_policy_invalid'
    super(MESSAGES[safeCode])
    this.name = 'EmployeeLoopPolicyError'
    this.code = safeCode
  }
}

const FIELDS: readonly EmployeeLoopField[] = Object.freeze([
  'max_steps', 'max_model_calls', 'max_wall_clock_s', 'max_rework', 'max_retries', 'max_sub_agents', 'max_cost_cny',
])
const REQUIRED: readonly EmployeeLoopField[] = Object.freeze([
  'max_steps', 'max_model_calls', 'max_wall_clock_s', 'max_rework', 'max_retries',
])
const RANGES: Readonly<Record<Exclude<EmployeeLoopField, 'max_cost_cny'>, readonly [number, number]>> = Object.freeze({
  max_steps: Object.freeze([1, 10000] as const),
  max_model_calls: Object.freeze([1, 10000] as const),
  max_wall_clock_s: Object.freeze([1, 86400] as const),
  max_rework: Object.freeze([0, 100] as const),
  max_retries: Object.freeze([0, 20] as const),
  max_sub_agents: Object.freeze([0, 64] as const),
})
const INTEGER = /^(?:0|[1-9][0-9]*|-0)$/
const COST = /^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?$/
const EMPTY_FIELDS = Object.freeze({})
const MISSING: EmployeeLoopPolicyState = Object.freeze({ kind: 'missing', fields: EMPTY_FIELDS })
const INVALID: EmployeeLoopPolicyState = Object.freeze({ kind: 'invalid', fields: EMPTY_FIELDS })
const own = (value: object, key: PropertyKey): boolean => Object.prototype.hasOwnProperty.call(value, key)

function fail(code: LoopErrorCode = 'employee_loop_policy_invalid'): never {
  throw new EmployeeLoopPolicyError(code)
}

function guarded<T>(read: () => T): T {
  let code: LoopErrorCode = 'employee_loop_policy_invalid'
  try {
    return read()
  } catch (error) {
    // Inspect only our error's own data property; never echo a private thrown message.
    try {
      if (error instanceof EmployeeLoopPolicyError) {
        const descriptor = Object.getOwnPropertyDescriptor(error, 'code')
        if (descriptor && own(descriptor, 'value') && typeof descriptor.value === 'string' && own(MESSAGES, descriptor.value)) code = descriptor.value as LoopErrorCode
      }
    } catch { /* A thrown reflection failure is still only a fixed invalid input error. */ }
  }
  return fail(code)
}

function plainRecord(value: unknown): value is Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}

function dataFields(value: unknown, allowed: readonly string[]): Record<string, unknown> {
  if (!plainRecord(value)) return fail()
  const keys = Reflect.ownKeys(value)
  if (keys.length > allowed.length) return fail()
  const result: Record<string, unknown> = Object.create(null)
  for (const key of keys) {
    if (typeof key !== 'string' || !allowed.includes(key)) return fail()
    const descriptor = Object.getOwnPropertyDescriptor(value, key)
    if (!descriptor || !own(descriptor, 'value')) return fail()
    result[key] = descriptor.value
  }
  return result
}

function textFields(value: unknown): Readonly<Partial<Record<EmployeeLoopField, string>>> {
  const source = dataFields(value, FIELDS)
  const result: Partial<Record<EmployeeLoopField, string>> = {}
  for (const field of FIELDS) {
    if (!own(source, field)) continue
    const text = source[field]
    if (typeof text !== 'string') return fail()
    if (text.length > 64) return fail('employee_loop_policy_too_large')
    result[field] = text
  }
  return Object.freeze(result)
}

function state(value: unknown): EmployeeLoopPolicyState {
  const source = dataFields(value, ['kind', 'fields'])
  if (!own(source, 'kind') || !own(source, 'fields')) return fail()
  const kind = source.kind
  if (kind !== 'missing' && kind !== 'present' && kind !== 'invalid') return fail()
  const fields = textFields(source.fields)
  if (kind !== 'present') {
    if (Reflect.ownKeys(fields).length !== 0) return fail()
    return kind === 'missing' ? MISSING : INVALID
  }
  return Object.freeze({ kind, fields })
}

function fullMatch(pattern: RegExp, text: string): boolean {
  const match = pattern.exec(text)
  return match !== null && match[0] === text
}

function decimalCompare(a: string, b: string): number {
  return a.length !== b.length ? a.length - b.length : a < b ? -1 : a > b ? 1 : 0
}

function numericValue(field: Exclude<EmployeeLoopField, 'max_cost_cny'>, text: string): number {
  if (!fullMatch(INTEGER, text)) return fail()
  const unsigned = text === '-0' ? '0' : text
  const [low, high] = RANGES[field]
  // Compare canonical decimal text before conversion; empty/huge inputs cannot become an accepted number.
  if (decimalCompare(unsigned, String(low)) < 0 || decimalCompare(unsigned, String(high)) > 0) return fail()
  return Number(text)
}

function typedFields(value: unknown): EmployeeLoopPolicyState {
  const source = dataFields(value, FIELDS)
  const fields: Partial<Record<EmployeeLoopField, string>> = {}
  for (const field of FIELDS) {
    if (!own(source, field)) continue
    const item = source[field]
    if (field === 'max_cost_cny') {
      if (typeof item !== 'string' || !fullMatch(COST, item)) return fail()
      fields[field] = item
    } else {
      const [low, high] = RANGES[field]
      if (typeof item !== 'number' || !Number.isFinite(item) || !Number.isInteger(item) || item < low || item > high) return fail()
      fields[field] = Object.is(item, -0) ? '-0' : String(item)
    }
  }
  return Object.freeze({ kind: 'present', fields: Object.freeze(fields) })
}

function valueOf(current: EmployeeLoopPolicyState): EmployeeLoopPolicyValue {
  if (current.kind !== 'present') return fail()
  const result: Partial<Record<Exclude<EmployeeLoopField, 'max_cost_cny'>, number>> & { max_cost_cny?: string } = {}
  for (const field of FIELDS) {
    if (!own(current.fields, field)) continue
    const text = current.fields[field] as string
    if (field === 'max_cost_cny') {
      if (!fullMatch(COST, text)) return fail()
      result.max_cost_cny = text
    } else {
      result[field] = numericValue(field, text)
    }
  }
  return Object.freeze(result)
}

function same(a: EmployeeLoopPolicyState, b: EmployeeLoopPolicyState): boolean {
  return a.kind === b.kind && FIELDS.every(field => own(a.fields, field) === own(b.fields, field) && a.fields[field] === b.fields[field])
}

export function readEmployeeLoopPolicy(manifest: unknown): EmployeeLoopPolicyState {
  try {
    if (!plainRecord(manifest)) return INVALID
    // Do not enumerate the manifest or evaluate loop_policy/other getters.
    const descriptor = Object.getOwnPropertyDescriptor(manifest, 'loop_policy')
    if (!descriptor) return MISSING
    if (!own(descriptor, 'value')) return INVALID
    return typedFields(descriptor.value)
  } catch {
    return INVALID
  }
}

export function employeeLoopPolicyState(fields: unknown): EmployeeLoopPolicyState {
  return guarded(() => Object.freeze({ kind: 'present' as const, fields: textFields(fields) }))
}

export function sameEmployeeLoopPolicy(a: EmployeeLoopPolicyState, b: EmployeeLoopPolicyState): boolean {
  return guarded(() => same(state(a), state(b)))
}

export function employeeLoopPolicyProblems(value: EmployeeLoopPolicyState): readonly EmployeeLoopPolicyProblem[] {
  return guarded(() => {
    const current = state(value)
    const problems: EmployeeLoopPolicyProblem[] = []
    const problem = (path: string, message: string, severity: 'error' | 'warning') => problems.push(Object.freeze({ path, message, severity }))
    if (current.kind === 'invalid') problem('loop_policy', '原自主执行上限无法完整显示。重新配置并保存后将替换原配置；也可恢复打开时的内容。', 'error')
    else if (current.kind === 'missing') problem('loop_policy', '还没有设置自主执行上限；草稿可继续保存。', 'warning')
    else {
      for (const field of FIELDS) {
        if (!own(current.fields, field)) {
          if (REQUIRED.includes(field)) problem(`loop_policy.${field}`, '此上限还没有设置；草稿可继续保存。', 'warning')
          continue
        }
        try {
          const text = current.fields[field] as string
          if (field === 'max_cost_cny') {
            if (!fullMatch(COST, text)) fail()
          } else numericValue(field, text)
        } catch {
          problem(`loop_policy.${field}`, field === 'max_cost_cny' ? '请输入规范金额文本，最多 12 位整数和 6 位小数。' : '请输入该范围内的规范整数文本。', 'error')
        }
      }
    }
    return Object.freeze(problems)
  })
}

export function employeeLoopPolicyValue(value: EmployeeLoopPolicyState): EmployeeLoopPolicyValue {
  return guarded(() => valueOf(state(value)))
}

export function validateEmployeeLoopPolicy(value: unknown): EmployeeLoopPolicyValue {
  return guarded(() => valueOf(typedFields(value)))
}

export function employeeLoopPolicyChanges(current: EmployeeLoopPolicyState, baseline: EmployeeLoopPolicyState): Readonly<{ loop_policy?: EmployeeLoopPolicyValue }> {
  return guarded(() => {
    const next = state(current)
    if (same(next, state(baseline))) return Object.freeze({})
    if (next.kind !== 'present') return fail('employee_loop_policy_removal_unsupported')
    return Object.freeze({ loop_policy: valueOf(next) })
  })
}

export function employeeLoopPolicyEncodedBytes(value: EmployeeLoopPolicyState): number {
  return guarded(() => {
    const current = state(value)
    return current.kind === 'present' ? new TextEncoder().encode(JSON.stringify(current.fields)).byteLength : 0
  })
}
