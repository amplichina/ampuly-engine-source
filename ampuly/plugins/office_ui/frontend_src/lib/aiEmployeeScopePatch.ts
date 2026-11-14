/** Touched-only, bounded patches. Never serialize the whole manifest or replay untouched fields. */
import {
  employeeArtifactChanges, employeeArtifactEncodedBytes, readEmployeeArtifact,
  validateEmployeeArtifactContract,
} from './aiEmployeeArtifactContract'
import type { EmployeeArtifactState } from './aiEmployeeArtifactContract'
import {
  employeeHumanSlotsChanges, employeeHumanSlotsEncodedBytes, readEmployeeHumanSlots,
  validateEmployeeHumanSlots,
} from './aiEmployeeHumanSlots'
import type { EmployeeHumanSlotsState } from './aiEmployeeHumanSlots'
import {
  employeeLoopPolicyChanges, employeeLoopPolicyEncodedBytes, readEmployeeLoopPolicy,
  validateEmployeeLoopPolicy,
} from './aiEmployeeLoopPolicy'
import type { EmployeeLoopPolicyState } from './aiEmployeeLoopPolicy'
import {
  employeeIOSchemaChanges, employeeIOSchemaEncodedBytes, employeeIOSchemaValue,
  readEmployeeIOSchemas,
} from './aiEmployeeIOSchema'
import type { EmployeeIOSchemas } from './aiEmployeeIOSchema'
export type EmployeeScopeField = 'scope_in' | 'scope_out'
export interface EmployeeScopeList {
  readonly kind: 'missing' | 'present' | 'invalid'
  readonly items: readonly string[]
}
export interface EmployeeScopes { readonly scope_in: EmployeeScopeList; readonly scope_out: EmployeeScopeList }
export interface EmployeeSupportedFields { readonly name: string; readonly responsibility: string; readonly version: string }
export interface EmployeeFieldProblem {
  readonly code: string
  readonly severity: 'error' | 'warning'
  readonly path: string
  readonly message: string
}
export const EMPLOYEE_SCOPE_MAX_ITEMS = 500
export const EMPLOYEE_SCOPE_MAX_CHARACTERS = 16_384
export const EMPLOYEE_EDIT_MAX_BYTES = 2 * 1024 * 1024
const scalarLimits = { name: 256, responsibility: 16_384, version: 256 } as const
const supported = ['name', 'responsibility', 'version', 'scope_in', 'scope_out', 'artifact_contract', 'human_slots', 'loop_policy', 'input_schema', 'output_schema'] as const
const scopes = ['scope_in', 'scope_out'] as const
// Python str.strip whitespace, deliberately NOT JS trim (which also strips FEFF).
const pythonBlank = /^[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]*$/u
const empty = Object.freeze([]) as readonly string[]
const state = (kind: EmployeeScopeList['kind'], items: readonly string[] = empty): EmployeeScopeList => Object.freeze({ kind, items: Object.freeze([...items]) })
const problem = (code: string, severity: EmployeeFieldProblem['severity'], path: string, message: string): EmployeeFieldProblem => Object.freeze({ code, severity, path, message })
export class EmployeeScopePatchError extends Error {
  constructor(readonly code: string = 'employee_package_invalid') {
    super(code === 'employee_edit_too_large' ? '受支持的编辑内容超过 2 MiB，请缩小本次编辑。'
      : code === 'employee_field_removal_unsupported' ? '当前保存接口不能删除已声明字段；请保留声明或重置为读取基准。'
        : code === 'employee_patch_field_unsupported' ? '本入口只保存岗位信息、负责范围、输入输出约定、交付约定、人工职责约定及自主执行上限。'
          : '请先修正受支持字段的格式或范围提示，再保存。')
    this.name = 'EmployeeScopePatchError'
  }
}
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
export function readEmployeeScope(manifest: Readonly<Record<string, unknown>>, field: EmployeeScopeField): EmployeeScopeList {
  if (!Object.hasOwn(manifest, field)) return state('missing')
  const value = manifest[field]
  if (!Array.isArray(value) || value.length > EMPLOYEE_SCOPE_MAX_ITEMS || value.some(item => typeof item !== 'string' || item.length > EMPLOYEE_SCOPE_MAX_CHARACTERS * 2)) return state('invalid')
  return state('present', value as string[])
}
export function readEmployeeScopes(manifest: Readonly<Record<string, unknown>>): EmployeeScopes {
  return Object.freeze({ scope_in: readEmployeeScope(manifest, 'scope_in'), scope_out: readEmployeeScope(manifest, 'scope_out') })
}
/** Local bad/duplicate/blank strings remain visible for repair, never silently normalized. */
export function employeeScopeList(items: readonly string[]): EmployeeScopeList {
  if (!Array.isArray(items) || items.length > EMPLOYEE_SCOPE_MAX_ITEMS
    || items.some(item => typeof item !== 'string' || item.length > EMPLOYEE_SCOPE_MAX_CHARACTERS * 2)) throw new EmployeeScopePatchError()
  return state('present', items)
}
function checkState(value: EmployeeScopeList): void {
  if (!object(value) || !['missing', 'present', 'invalid'].includes(value.kind) || !Array.isArray(value.items)
    || value.items.length > EMPLOYEE_SCOPE_MAX_ITEMS || value.items.some(item => typeof item !== 'string' || item.length > EMPLOYEE_SCOPE_MAX_CHARACTERS * 2)
    || (value.kind !== 'present' && value.items.length !== 0)) throw new EmployeeScopePatchError()
}
export function sameEmployeeScope(a: EmployeeScopeList, b: EmployeeScopeList): boolean {
  checkState(a); checkState(b)
  return a.kind === b.kind && a.items.length === b.items.length && a.items.every((item, index) => item === b.items[index])
}
export function sameEmployeeScopes(a: EmployeeScopes, b: EmployeeScopes): boolean {
  return scopes.every(field => sameEmployeeScope(a[field], b[field]))
}
function hasUnpairedSurrogate(value: string): boolean {
  for (let index = 0; index < value.length; index++) {
    const unit = value.charCodeAt(index)
    if (unit >= 0xD800 && unit <= 0xDBFF) {
      const next = value.charCodeAt(++index)
      if (!(next >= 0xDC00 && next <= 0xDFFF)) return true
    } else if (unit >= 0xDC00 && unit <= 0xDFFF) return true
  }
  return false
}
function stringProblems(value: unknown, path: string, limit: number): EmployeeFieldProblem[] {
  if (typeof value !== 'string' || pythonBlank.test(value) || Array.from(value).length > limit || hasUnpairedSurrogate(value)) {
    return [problem('employee_package_invalid', 'error', path, `须为非空白字符串，最多 ${limit} 个 Unicode 字符，且没有无配对代理字符。`)]
  }
  return []
}
export function employeeScopeProblems(field: EmployeeScopeField, value: EmployeeScopeList): readonly EmployeeFieldProblem[] {
  checkState(value)
  if (value.kind === 'missing') return Object.freeze([problem('employee_package_required', 'warning', field, '字段尚未声明；保存其他字段不会自动补为空数组。')])
  if (value.kind === 'invalid') return Object.freeze([problem('employee_package_invalid', 'error', field, '现有字段不是受支持的有界字符串数组；只有明确替换后才会保存。')])
  const result: EmployeeFieldProblem[] = []
  if (!value.items.length) result.push(field === 'scope_in'
    ? problem('employee_package_invalid', 'error', field, '职责内范围至少需要一项。')
    : problem('employee_package_scope_out_empty', 'warning', field, '已明确声明为空数组；可保存为草稿，但完整岗位仍有此缺口。'))
  const seen = new Set<string>()
  value.items.forEach((item, index) => {
    result.push(...stringProblems(item, `${field}[${index}]`, EMPLOYEE_SCOPE_MAX_CHARACTERS))
    if (seen.has(item)) result.push(problem('employee_package_invalid', 'error', `${field}[${index}]`, '与前面的项完全重复；请明确修改或删除，不会自动去重。'))
    seen.add(item)
  })
  return Object.freeze(result)
}
/** Measure bounded primitives and leaf-owned structured values; not a whole-manifest round trip. */
function checkBudget(fields: Record<string, unknown>, artifact?: EmployeeArtifactState, human?: EmployeeHumanSlotsState,
                     loop?: EmployeeLoopPolicyState, ios?: EmployeeIOSchemas): void {
  let bytes = 2
  const encoder = new TextEncoder()
  const charge = (value: string): void => { bytes += encoder.encode(JSON.stringify(value)).byteLength; if (bytes > EMPLOYEE_EDIT_MAX_BYTES) throw new EmployeeScopePatchError('employee_edit_too_large') }
  for (const [key, value] of Object.entries(fields)) {
    // Only the corresponding leaf may inspect either known structured value. Never serialize
    // an arbitrary manifest/schema or pretend an unmeasured object is small.
    if (key === 'artifact_contract') {
      if (artifact?.kind !== 'present') throw new EmployeeScopePatchError()
      continue
    }
    if (key === 'human_slots') {
      if (human?.kind !== 'present') throw new EmployeeScopePatchError()
      continue
    }
    if (key === 'loop_policy') {
      if (loop?.kind !== 'present') throw new EmployeeScopePatchError()
      continue
    }
    if (key === 'input_schema' || key === 'output_schema') {
      if (ios?.[key].kind !== 'present') throw new EmployeeScopePatchError()
      continue
    }
    charge(key); bytes += 2
    if (typeof value === 'string') charge(value)
    else if (Array.isArray(value)) {
      bytes += 2
      for (const item of value) { charge(item as string); bytes++ }
    }
    if (bytes > EMPLOYEE_EDIT_MAX_BYTES) throw new EmployeeScopePatchError('employee_edit_too_large')
  }
  if (artifact !== undefined) {
    const artifactBytes = employeeArtifactEncodedBytes(artifact)
    if (artifact.kind === 'present') {
      charge('artifact_contract'); bytes += 2 + artifactBytes
      if (bytes > EMPLOYEE_EDIT_MAX_BYTES) throw new EmployeeScopePatchError('employee_edit_too_large')
    }
  }
  if (human !== undefined) {
    const humanBytes = employeeHumanSlotsEncodedBytes(human)
    if (human.kind === 'present') {
      charge('human_slots'); bytes += 2 + humanBytes
      if (bytes > EMPLOYEE_EDIT_MAX_BYTES) throw new EmployeeScopePatchError('employee_edit_too_large')
    }
  }
  if (loop !== undefined) {
    const loopBytes = employeeLoopPolicyEncodedBytes(loop)
    if (loop.kind === 'present') {
      charge('loop_policy'); bytes += 2 + loopBytes
      if (bytes > EMPLOYEE_EDIT_MAX_BYTES) throw new EmployeeScopePatchError('employee_edit_too_large')
    }
  }
  if (ios !== undefined) {
    // The IO leaf measures both present objects and bounded invalid local text.
    // Its result includes the IO keys/markers; never charge those fields twice.
    bytes += employeeIOSchemaEncodedBytes(ios)
    if (bytes > EMPLOYEE_EDIT_MAX_BYTES) throw new EmployeeScopePatchError('employee_edit_too_large')
  }
}
export function assertEmployeeEditorBudget(fields: EmployeeSupportedFields, value: EmployeeScopes,
                                           artifact?: EmployeeArtifactState, human?: EmployeeHumanSlotsState,
                                           loop?: EmployeeLoopPolicyState, ios?: EmployeeIOSchemas): void {
  for (const field of ['name', 'responsibility', 'version'] as const) {
    if (typeof fields[field] !== 'string' || fields[field].length > scalarLimits[field] * 2) throw new EmployeeScopePatchError()
  }
  for (const field of scopes) checkState(value[field])
  checkBudget({ name: fields.name, responsibility: fields.responsibility, version: fields.version,
    ...Object.fromEntries(scopes.filter(field => value[field].kind === 'present').map(field => [field, value[field].items])) }, artifact, human, loop, ios)
}
export function validateEmployeeDraftChanges(changes: unknown): Readonly<Record<string, unknown>> {
  if (!object(changes) || !Object.keys(changes).length || Object.keys(changes).some(key => !supported.includes(key as typeof supported[number]))) throw new EmployeeScopePatchError('employee_patch_field_unsupported')
  const copied: Record<string, unknown> = {}
  let artifact: EmployeeArtifactState | undefined
  let human: EmployeeHumanSlotsState | undefined
  let loop: EmployeeLoopPolicyState | undefined
  for (const key of Object.keys(changes)) {
    const value = changes[key]
    if (key === 'artifact_contract') {
      copied[key] = validateEmployeeArtifactContract(value)
      artifact = readEmployeeArtifact({ artifact_contract: copied[key] })
      if (artifact.kind !== 'present') throw new EmployeeScopePatchError()
    } else if (key === 'human_slots') {
      copied[key] = validateEmployeeHumanSlots(value)
      human = readEmployeeHumanSlots({ human_slots: copied[key] })
      if (human.kind !== 'present') throw new EmployeeScopePatchError()
    } else if (key === 'loop_policy') {
      copied[key] = validateEmployeeLoopPolicy(value)
      loop = readEmployeeLoopPolicy({ loop_policy: copied[key] })
      if (loop.kind !== 'present') throw new EmployeeScopePatchError()
    } else if (key === 'input_schema' || key === 'output_schema') {
      const projected = readEmployeeIOSchemas({ [key]: value })
      copied[key] = employeeIOSchemaValue(projected[key])
    } else if (key === 'scope_in' || key === 'scope_out') {
      if (!Array.isArray(value)) throw new EmployeeScopePatchError()
      const list = employeeScopeList(value as string[])
      if (employeeScopeProblems(key, list).some(item => item.severity === 'error')) throw new EmployeeScopePatchError()
      copied[key] = list.items
    } else {
      const field = key as keyof typeof scalarLimits
      if (typeof value !== 'string' || value.length > scalarLimits[field] * 2 || stringProblems(value, key, scalarLimits[field]).length) throw new EmployeeScopePatchError()
      copied[key] = value
    }
  }
  const ios = Object.hasOwn(copied, 'input_schema') || Object.hasOwn(copied, 'output_schema') ? readEmployeeIOSchemas(copied) : undefined
  checkBudget(copied, artifact, human, loop, ios)
  return Object.freeze(copied)
}
export function employeeDraftChanges(fields: EmployeeSupportedFields, baseline: EmployeeSupportedFields, current: EmployeeScopes, original: EmployeeScopes,
                                     artifact?: EmployeeArtifactState, baseArtifact?: EmployeeArtifactState,
                                     human?: EmployeeHumanSlotsState, baseHuman?: EmployeeHumanSlotsState,
                                     loop?: EmployeeLoopPolicyState, baseLoop?: EmployeeLoopPolicyState,
                                     ios?: EmployeeIOSchemas, baseIOS?: EmployeeIOSchemas): Readonly<Record<string, unknown>> {
  assertEmployeeEditorBudget(fields, current, artifact, human, loop, ios)
  if ((artifact === undefined) !== (baseArtifact === undefined)) throw new EmployeeScopePatchError()
  if ((human === undefined) !== (baseHuman === undefined)) throw new EmployeeScopePatchError()
  if ((loop === undefined) !== (baseLoop === undefined)) throw new EmployeeScopePatchError()
  if ((ios === undefined) !== (baseIOS === undefined)) throw new EmployeeScopePatchError()
  const changes: Record<string, unknown> = {}
  for (const field of ['name', 'responsibility', 'version'] as const) if (fields[field] !== baseline[field]) changes[field] = fields[field]
  for (const field of scopes) if (!sameEmployeeScope(current[field], original[field])) {
    if (current[field].kind !== 'present') throw new EmployeeScopePatchError('employee_field_removal_unsupported')
    changes[field] = current[field].items
  }
  if (artifact !== undefined && baseArtifact !== undefined) Object.assign(changes, employeeArtifactChanges(artifact, baseArtifact))
  if (human !== undefined && baseHuman !== undefined) Object.assign(changes, employeeHumanSlotsChanges(human, baseHuman))
  if (loop !== undefined && baseLoop !== undefined) Object.assign(changes, employeeLoopPolicyChanges(loop, baseLoop))
  if (ios !== undefined && baseIOS !== undefined) Object.assign(changes, employeeIOSchemaChanges(ios, baseIOS))
  return Object.keys(changes).length ? validateEmployeeDraftChanges(changes) : Object.freeze(changes)
}
function requirePresent(value: EmployeeScopeList, index?: number): string[] {
  checkState(value)
  if (value.kind !== 'present' || (index !== undefined && (!Number.isInteger(index) || index < 0 || index >= value.items.length))) throw new EmployeeScopePatchError()
  return [...value.items]
}
export function addEmployeeScopeItem(value: EmployeeScopeList): EmployeeScopeList { return employeeScopeList([...requirePresent(value), '']) }
export function editEmployeeScopeItem(value: EmployeeScopeList, index: number, text: string): EmployeeScopeList {
  const items = requirePresent(value, index); items[index] = text; return employeeScopeList(items)
}
export function removeEmployeeScopeItem(value: EmployeeScopeList, index: number): EmployeeScopeList {
  const items = requirePresent(value, index); items.splice(index, 1); return employeeScopeList(items)
}
export function moveEmployeeScopeItem(value: EmployeeScopeList, index: number, direction: -1 | 1): EmployeeScopeList {
  const items = requirePresent(value, index), target = index + direction
  if ((direction !== -1 && direction !== 1) || target < 0 || target >= items.length) throw new EmployeeScopePatchError()
  ;[items[index], items[target]] = [items[target], items[index]]
  return employeeScopeList(items)
}
