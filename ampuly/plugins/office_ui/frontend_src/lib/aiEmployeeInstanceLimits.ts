/** Project-instance settings only; no default, fixed-package comparison or execution grant. */
import {
  EmployeeLoopPolicyError,
  employeeLoopPolicyProblems,
  employeeLoopPolicyState,
  employeeLoopPolicyValue,
  readEmployeeLoopPolicy,
  sameEmployeeLoopPolicy,
  validateEmployeeLoopPolicy,
  type EmployeeLoopField,
  type EmployeeLoopPolicyProblem,
  type EmployeeLoopPolicyState,
  type EmployeeLoopPolicyValue,
} from './aiEmployeeLoopPolicy'

export type EmployeeInstanceLimitField = EmployeeLoopField
export type EmployeeInstanceLimitsState = EmployeeLoopPolicyState
export type EmployeeInstanceLimitsValue = EmployeeLoopPolicyValue
export type EmployeeInstanceLimitsProblem = EmployeeLoopPolicyProblem

type LimitsErrorCode = 'employee_instance_limits_invalid' | 'employee_instance_limits_too_large'
const MESSAGES: Readonly<Record<LimitsErrorCode, string>> = Object.freeze({
  employee_instance_limits_invalid: '本项目执行限额格式无效。',
  employee_instance_limits_too_large: '本项目执行限额输入过长。',
})

export class EmployeeInstanceLimitsError extends Error {
  readonly code: LimitsErrorCode

  constructor(code: LimitsErrorCode = 'employee_instance_limits_invalid') {
    const safeCode = code === 'employee_instance_limits_too_large' ? code : 'employee_instance_limits_invalid'
    super(MESSAGES[safeCode])
    this.name = 'EmployeeInstanceLimitsError'
    this.code = safeCode
  }
}

const EMPTY = Object.freeze({})
const MISSING: EmployeeInstanceLimitsState = Object.freeze({ kind: 'missing', fields: EMPTY })
const INVALID: EmployeeInstanceLimitsState = Object.freeze({ kind: 'invalid', fields: EMPTY })
const own = (value: object, key: PropertyKey): boolean => Object.prototype.hasOwnProperty.call(value, key)

function fail(code: LimitsErrorCode = 'employee_instance_limits_invalid'): never {
  throw new EmployeeInstanceLimitsError(code)
}

function guarded<T>(read: () => T): T {
  let code: LimitsErrorCode = 'employee_instance_limits_invalid'
  try {
    return read()
  } catch (error) {
    try {
      if (error instanceof EmployeeLoopPolicyError || error instanceof EmployeeInstanceLimitsError) {
        const descriptor = Object.getOwnPropertyDescriptor(error, 'code')
        if (descriptor && own(descriptor, 'value') && (
          descriptor.value === 'employee_loop_policy_too_large' || descriptor.value === 'employee_instance_limits_too_large'
        )) code = 'employee_instance_limits_too_large'
      }
    } catch { /* Private reflection failures select only our fixed invalid error. */ }
  }
  return fail(code)
}

function plain(value: unknown): value is object {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}

function copy(value: unknown): EmployeeInstanceLimitsState {
  if (!plain(value)) return fail()
  const keys = Reflect.ownKeys(value)
  if (keys.length !== 2 || !keys.includes('kind') || !keys.includes('fields')) return fail()
  const kind = Object.getOwnPropertyDescriptor(value, 'kind')
  const fields = Object.getOwnPropertyDescriptor(value, 'fields')
  if (!kind || !fields || !own(kind, 'value') || !own(fields, 'value')
    || !['missing', 'present', 'invalid'].includes(kind.value)) return fail()
  const present = employeeLoopPolicyState(fields.value)
  if (kind.value === 'present') return present
  if (Reflect.ownKeys(present.fields).length !== 0) return fail()
  return kind.value === 'missing' ? MISSING : INVALID
}

/** Inspect limits only; unrelated configuration, names and getters are never read. */
export function readEmployeeInstanceLimits(instance: unknown): EmployeeInstanceLimitsState {
  try {
    if (!plain(instance)) return INVALID
    const descriptor = Object.getOwnPropertyDescriptor(instance, 'limits')
    if (!descriptor) return MISSING
    if (!own(descriptor, 'value')) return INVALID
    return readEmployeeLoopPolicy({ loop_policy: descriptor.value })
  } catch {
    return INVALID
  }
}

/** Local bounded text, including erroneous text, remains available for explicit correction. */
export function employeeInstanceLimitsState(fields: unknown): EmployeeInstanceLimitsState {
  return guarded(() => employeeLoopPolicyState(fields))
}

export function copyEmployeeInstanceLimitsState(value: EmployeeInstanceLimitsState): EmployeeInstanceLimitsState {
  return guarded(() => copy(value))
}

export function sameEmployeeInstanceLimits(a: EmployeeInstanceLimitsState, b: EmployeeInstanceLimitsState): boolean {
  return guarded(() => sameEmployeeLoopPolicy(copy(a), copy(b)))
}

/** All seven keys are optional on an instance; package-draft required warnings do not apply. */
export function employeeInstanceLimitsProblems(value: EmployeeInstanceLimitsState): readonly EmployeeInstanceLimitsProblem[] {
  return guarded(() => {
    const current = copy(value)
    if (current.kind !== 'present') return Object.freeze([Object.freeze({
      path: 'limits',
      message: current.kind === 'invalid'
        ? '原本项目限额无法完整显示；请明确配置或恢复打开时的内容。'
        : '尚未提供本项目限额；请明确配置后再保存。',
      severity: current.kind === 'invalid' ? 'error' as const : 'warning' as const,
    })])
    return Object.freeze(employeeLoopPolicyProblems(current)
      .filter(problem => problem.severity === 'error')
      .map(problem => Object.freeze({ ...problem, path: problem.path.replace(/^loop_policy(?=\.|$)/, 'limits') })))
  })
}

/** A complete optional-key object, never a defaults-filled or nested-merge patch. */
export function employeeInstanceLimitsValue(value: EmployeeInstanceLimitsState): EmployeeInstanceLimitsValue {
  return guarded(() => employeeLoopPolicyValue(copy(value)))
}

export function validateEmployeeInstanceLimits(value: unknown): EmployeeInstanceLimitsValue {
  return guarded(() => validateEmployeeLoopPolicy(value))
}
