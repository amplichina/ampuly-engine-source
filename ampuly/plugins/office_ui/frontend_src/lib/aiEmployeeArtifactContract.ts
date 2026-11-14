/** Bounded, touched-only artifact editing; never copies the rest of a manifest. */
export interface EmployeeDeliverable { readonly name: string; readonly format: string; readonly required: boolean }
export interface EmployeeArtifactState {
  readonly kind: 'missing' | 'present' | 'invalid'
  readonly deliverables: readonly EmployeeDeliverable[]
  readonly acceptance_criteria: readonly string[]
}
export interface EmployeeArtifactProblem { readonly path: string; readonly message: string; readonly severity: 'warning' | 'error' }
type ErrorCode = 'employee_artifact_invalid' | 'employee_artifact_too_large' | 'employee_artifact_removal_unsupported'
type Contract = Readonly<{ deliverables: readonly EmployeeDeliverable[]; acceptance_criteria: readonly string[] }>
const MAX_ITEMS = 500
const MAX_CHARACTERS = 16_384
const MAX_BYTES = 2 * 1024 * 1024
// Python str.strip whitespace: FEFF deliberately remains nonblank.
const pythonBlank = /^[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]*$/u
const emptyDeliverables: readonly EmployeeDeliverable[] = Object.freeze([])
const emptyCriteria: readonly string[] = Object.freeze([])
const emptyChanges: Readonly<Record<string, unknown>> = Object.freeze({})
export class EmployeeArtifactContractError extends Error {
  readonly code: ErrorCode
  constructor(code: ErrorCode = 'employee_artifact_invalid') {
    const safe = code === 'employee_artifact_too_large' || code === 'employee_artifact_removal_unsupported' ? code : 'employee_artifact_invalid'
    super(safe === 'employee_artifact_too_large' ? '文字太多，请缩短后再试。'
      : safe === 'employee_artifact_removal_unsupported' ? '不能取消已保存的交付要求；可以修改内容或恢复打开时的内容。'
        : '请检查交付物和验收标准。')
    this.name = 'EmployeeArtifactContractError'
    this.code = safe
  }
}
function guard<T>(operation: () => T): T {
  let code: ErrorCode = 'employee_artifact_invalid'
  try { return operation() } catch (error) {
    try {
      if (error instanceof EmployeeArtifactContractError) {
        const descriptor = Object.getOwnPropertyDescriptor(error, 'code')
        if (descriptor && Object.hasOwn(descriptor, 'value')) code = descriptor.value as ErrorCode
      }
    } catch { /* Even a hostile exception object cannot escape this boundary. */ }
  }
  // No original error, cause, or input is attached to a projection failure.
  throw new EmployeeArtifactContractError(code)
}
function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null)
}
function fields(value: unknown, keys: readonly string[]): Record<string, unknown> {
  if (!record(value)) throw new EmployeeArtifactContractError()
  const own = Reflect.ownKeys(value)
  if (own.length !== keys.length || own.some(key => typeof key !== 'string' || !keys.includes(key))) throw new EmployeeArtifactContractError()
  const result: Record<string, unknown> = {}
  for (const key of keys) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key)
    if (!descriptor || !descriptor.enumerable || !Object.hasOwn(descriptor, 'value')) throw new EmployeeArtifactContractError()
    result[key] = descriptor.value
  }
  return result
}
function arrayLength(value: readonly unknown[]): number {
  const descriptor = Object.getOwnPropertyDescriptor(value, 'length')
  if (!descriptor || !Object.hasOwn(descriptor, 'value') || typeof descriptor.value !== 'number'
    || !Number.isInteger(descriptor.value) || descriptor.value < 0) throw new EmployeeArtifactContractError()
  if (descriptor.value > MAX_ITEMS) throw new EmployeeArtifactContractError('employee_artifact_too_large')
  return descriptor.value
}
function array(value: unknown): { value: readonly unknown[]; length: number } {
  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) throw new EmployeeArtifactContractError()
  const length = arrayLength(value)
  const own = Reflect.ownKeys(value)
  if (own.length !== length + 1 || own.some(key => typeof key !== 'string'
    || (key !== 'length' && !/^(0|[1-9][0-9]*)$/.test(key)))) throw new EmployeeArtifactContractError()
  return { value, length }
}
function item(value: readonly unknown[], index: number): unknown {
  const descriptor = Object.getOwnPropertyDescriptor(value, String(index))
  if (!descriptor || !descriptor.enumerable || !Object.hasOwn(descriptor, 'value')) throw new EmployeeArtifactContractError()
  return descriptor.value
}
/** Exact JSON string literal UTF-8 size, checked before any serialization. */
function stringBytes(value: unknown): number {
  if (typeof value !== 'string') throw new EmployeeArtifactContractError()
  if (value.length > MAX_CHARACTERS * 2) throw new EmployeeArtifactContractError('employee_artifact_too_large')
  let bytes = 2, characters = 0
  for (let index = 0; index < value.length; index++) {
    const unit = value.charCodeAt(index)
    if (++characters > MAX_CHARACTERS) throw new EmployeeArtifactContractError('employee_artifact_too_large')
    if (unit >= 0xD800 && unit <= 0xDBFF) {
      const next = value.charCodeAt(++index)
      if (!(next >= 0xDC00 && next <= 0xDFFF)) throw new EmployeeArtifactContractError()
      bytes += 4
    } else if (unit >= 0xDC00 && unit <= 0xDFFF) throw new EmployeeArtifactContractError()
    else if (unit === 34 || unit === 92 || [8, 9, 10, 12, 13].includes(unit)) bytes += 2
    else if (unit < 32) bytes += 6
    else bytes += unit <= 0x7F ? 1 : unit <= 0x7FF ? 2 : 3
  }
  return bytes
}
function absent(kind: 'missing' | 'invalid'): EmployeeArtifactState {
  return Object.freeze({ kind, deliverables: emptyDeliverables, acceptance_criteria: emptyCriteria })
}
function clone(value: unknown): { state: EmployeeArtifactState; bytes: number } {
  const input = fields(value, ['deliverables', 'acceptance_criteria'])
  const sourceDeliverables = array(input.deliverables), sourceCriteria = array(input.acceptance_criteria)
  const deliveryLength = sourceDeliverables.length, criteriaLength = sourceCriteria.length
  const deliverables: EmployeeDeliverable[] = [], criteria: string[] = []
  let bytes = '{"deliverables":['.length + '],"acceptance_criteria":['.length + ']}'.length
  const charge = (count: number): void => {
    bytes += count
    if (bytes > MAX_BYTES) throw new EmployeeArtifactContractError('employee_artifact_too_large')
  }
  // Fixed initial lengths; an accessor/proxy cannot turn a list into unbounded work.
  for (let index = 0; index < deliveryLength; index++) {
    const row = fields(item(sourceDeliverables.value, index), ['name', 'format', 'required'])
    const nameBytes = stringBytes(row.name), formatBytes = stringBytes(row.format)
    if (typeof row.required !== 'boolean') throw new EmployeeArtifactContractError()
    charge((index ? 1 : 0) + '{"name":'.length + nameBytes + ',"format":'.length + formatBytes
      + ',"required":'.length + (row.required ? 4 : 5) + 1)
    deliverables.push(Object.freeze({ name: row.name as string, format: row.format as string, required: row.required }))
  }
  for (let index = 0; index < criteriaLength; index++) {
    const text = item(sourceCriteria.value, index)
    charge((index ? 1 : 0) + stringBytes(text))
    criteria.push(text as string)
  }
  if (arrayLength(sourceDeliverables.value) !== deliveryLength || arrayLength(sourceCriteria.value) !== criteriaLength) throw new EmployeeArtifactContractError()
  return { state: Object.freeze({ kind: 'present', deliverables: Object.freeze(deliverables), acceptance_criteria: Object.freeze(criteria) }), bytes }
}
function checked(value: EmployeeArtifactState): { state: EmployeeArtifactState; bytes: number } {
  const input = fields(value, ['kind', 'deliverables', 'acceptance_criteria'])
  if (input.kind === 'present') return clone({ deliverables: input.deliverables, acceptance_criteria: input.acceptance_criteria })
  if ((input.kind !== 'missing' && input.kind !== 'invalid') || array(input.deliverables).length || array(input.acceptance_criteria).length) throw new EmployeeArtifactContractError()
  return { state: absent(input.kind), bytes: 0 }
}
export function readEmployeeArtifact(manifest: Readonly<Record<string, unknown>>): EmployeeArtifactState {
  try {
    if (!record(manifest)) return absent('invalid')
    const descriptor = Object.getOwnPropertyDescriptor(manifest, 'artifact_contract')
    if (!descriptor) return absent('missing')
    if (!descriptor.enumerable || !Object.hasOwn(descriptor, 'value')) return absent('invalid')
    return clone(descriptor.value).state
  } catch { return absent('invalid') }
}
/** Local empty rows/lists remain editable; complete-save semantics are separate. */
export function employeeArtifactState(deliverables: readonly EmployeeDeliverable[], criteria: readonly string[]): EmployeeArtifactState {
  return guard(() => clone({ deliverables, acceptance_criteria: criteria }).state)
}
function equal(a: EmployeeArtifactState, b: EmployeeArtifactState): boolean {
  return a.kind === b.kind && a.deliverables.length === b.deliverables.length && a.acceptance_criteria.length === b.acceptance_criteria.length
    && a.deliverables.every((row, index) => row.name === b.deliverables[index].name
      && row.format === b.deliverables[index].format && row.required === b.deliverables[index].required)
    && a.acceptance_criteria.every((text, index) => text === b.acceptance_criteria[index])
}
export function sameEmployeeArtifact(a: EmployeeArtifactState, b: EmployeeArtifactState): boolean {
  return guard(() => equal(checked(a).state, checked(b).state))
}
function problems(value: EmployeeArtifactState): readonly EmployeeArtifactProblem[] {
  const result: EmployeeArtifactProblem[] = []
  const add = (path: string, message: string, severity: EmployeeArtifactProblem['severity'] = 'error'): void => { result.push(Object.freeze({ path, message, severity })) }
  if (value.kind === 'missing') add('artifact_contract', '还没有填写交付要求；保存其他内容不会自动补上。', 'warning')
  else if (value.kind === 'invalid') add('artifact_contract', '原有交付要求暂时无法完整显示；需要明确重新填写才会替换。')
  else {
    if (!value.deliverables.length) add('artifact_contract.deliverables', '至少添加一个交付物。')
    if (!value.acceptance_criteria.length) add('artifact_contract.acceptance_criteria', '至少添加一条验收标准。')
    value.deliverables.forEach((row, index) => {
      if (pythonBlank.test(row.name)) add('artifact_contract.deliverables[' + index + '].name', '填写交付物名称，不能只留空白。')
      if (pythonBlank.test(row.format)) add('artifact_contract.deliverables[' + index + '].format', '填写交付物格式，不能只留空白。')
    })
    const seen = new Set<string>()
    value.acceptance_criteria.forEach((text, index) => {
      const path = 'artifact_contract.acceptance_criteria[' + index + ']'
      if (pythonBlank.test(text)) add(path, '填写验收标准，不能只留空白。')
      if (seen.has(text)) add(path, '这条验收标准与前面的完全相同，请修改或删除。')
      seen.add(text)
    })
  }
  return Object.freeze(result)
}
export function employeeArtifactProblems(state: EmployeeArtifactState): readonly EmployeeArtifactProblem[] {
  return guard(() => problems(checked(state).state))
}
function contract(state: EmployeeArtifactState): Contract {
  if (state.kind !== 'present') throw new EmployeeArtifactContractError()
  return Object.freeze({ deliverables: state.deliverables, acceptance_criteria: state.acceptance_criteria })
}
export function employeeArtifactContractValue(state: EmployeeArtifactState): Contract {
  return guard(() => contract(checked(state).state))
}
export function validateEmployeeArtifactContract(value: unknown): Readonly<Record<string, unknown>> {
  return guard(() => {
    const copied = clone(value).state
    if (problems(copied).some(problem => problem.severity === 'error')) throw new EmployeeArtifactContractError()
    return contract(copied)
  })
}
export function employeeArtifactChanges(current: EmployeeArtifactState, baseline: EmployeeArtifactState): Readonly<Record<string, unknown>> {
  return guard(() => {
    const next = checked(current).state, original = checked(baseline).state
    if (equal(next, original)) return emptyChanges
    if (next.kind !== 'present') throw new EmployeeArtifactContractError('employee_artifact_removal_unsupported')
    return Object.freeze({ artifact_contract: validateEmployeeArtifactContract(contract(next)) })
  })
}
export function employeeArtifactEncodedBytes(state: EmployeeArtifactState): number {
  return guard(() => checked(state).bytes)
}
