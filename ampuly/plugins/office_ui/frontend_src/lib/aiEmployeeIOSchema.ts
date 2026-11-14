/** Bounded draft-only schema editing. Never serializes the containing manifest. */
export type EmployeeIOSchemaField = 'input_schema' | 'output_schema'
export interface EmployeeIOSchemaState {
  readonly kind: 'missing' | 'invalid' | 'present'
  readonly text: string
}
export interface EmployeeIOSchemas {
  readonly input_schema: EmployeeIOSchemaState
  readonly output_schema: EmployeeIOSchemaState
}
export interface EmployeeIOSchemaProblem {
  readonly path: EmployeeIOSchemaField
  readonly message: string
  readonly severity: 'warning' | 'error'
}
type ErrorCode = 'employee_io_schema_invalid' | 'employee_io_schema_too_large' | 'employee_io_schema_removal_unsupported'
type JSONValue = null | boolean | number | string | readonly JSONValue[] | { readonly [key: string]: JSONValue }
type SchemaValue = Readonly<Record<string, JSONValue>>
const MAX_BYTES = 128 * 1024
const MAX_DEPTH = 32
const MAX_NODES = 10_000
const SCHEMA_URI = 'https://json-schema.org/draft/2020-12/schema'
const IO_FIELDS: readonly EmployeeIOSchemaField[] = Object.freeze(['input_schema', 'output_schema'])
const emptyChanges: Readonly<Record<string, unknown>> = Object.freeze({})
export class EmployeeIOSchemaError extends Error {
  readonly code: ErrorCode
  constructor(code: ErrorCode = 'employee_io_schema_invalid') {
    const safe = code === 'employee_io_schema_too_large' || code === 'employee_io_schema_removal_unsupported' ? code : 'employee_io_schema_invalid'
    super(safe === 'employee_io_schema_too_large' ? '内容太多，请缩短后再试。'
      : safe === 'employee_io_schema_removal_unsupported' ? '不能取消已保存的结构；可以修改或恢复打开时的内容。'
        : '请检查输入结构和输出结构。')
    this.name = 'EmployeeIOSchemaError'
    this.code = safe
  }
}
function guard<T>(operation: () => T): T {
  let code: ErrorCode = 'employee_io_schema_invalid'
  try { return operation() } catch (error) {
    try {
      if (error instanceof EmployeeIOSchemaError) {
        const descriptor = Object.getOwnPropertyDescriptor(error, 'code')
        if (descriptor && Object.hasOwn(descriptor, 'value')) code = descriptor.value as ErrorCode
      }
    } catch { /* Do not inspect or attach a hostile exception. */ }
  }
  throw new EmployeeIOSchemaError(code)
}
function record(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}
function data(value: object, key: string): unknown {
  const descriptor = Object.getOwnPropertyDescriptor(value, key)
  if (!descriptor || !descriptor.enumerable || !Object.hasOwn(descriptor, 'value')) throw new EmployeeIOSchemaError()
  return descriptor.value
}
function fields(value: unknown, expected: readonly string[]): Record<string, unknown> {
  if (!record(value)) throw new EmployeeIOSchemaError()
  const keys = Reflect.ownKeys(value)
  if (keys.length !== expected.length || keys.some(key => typeof key !== 'string' || !expected.includes(key))) throw new EmployeeIOSchemaError()
  const output: Record<string, unknown> = {}
  for (const key of expected) output[key] = data(value, key)
  return output
}
/** Counts UTF8 and JSON string bytes without invoking external encoders/coercion. */
function stringSizes(value: unknown): { raw: number; json: number } {
  if (typeof value !== 'string') throw new EmployeeIOSchemaError()
  if (value.length > MAX_BYTES) throw new EmployeeIOSchemaError('employee_io_schema_too_large')
  let raw = 0, json = 2
  for (let index = 0; index < value.length; index++) {
    const unit = value.charCodeAt(index)
    if (unit >= 0xD800 && unit <= 0xDBFF) {
      const next = value.charCodeAt(++index)
      if (!(next >= 0xDC00 && next <= 0xDFFF)) throw new EmployeeIOSchemaError()
      raw += 4; json += 4
    } else if (unit >= 0xDC00 && unit <= 0xDFFF) throw new EmployeeIOSchemaError()
    else {
      const width = unit <= 0x7F ? 1 : unit <= 0x7FF ? 2 : 3
      raw += width
      json += unit === 34 || unit === 92 || [8, 9, 10, 12, 13].includes(unit) ? 2 : unit < 32 ? 6 : width
    }
    if (raw > MAX_BYTES) throw new EmployeeIOSchemaError('employee_io_schema_too_large')
  }
  return { raw, json }
}
interface Budget { nodes: number; bytes: number; active: WeakSet<object> }
function charge(budget: Budget, bytes: number): void {
  budget.bytes += bytes
  if (budget.bytes > MAX_BYTES) throw new EmployeeIOSchemaError('employee_io_schema_too_large')
}
function clone(value: unknown, depth: number, budget: Budget): JSONValue {
  if (depth > MAX_DEPTH || ++budget.nodes > MAX_NODES) throw new EmployeeIOSchemaError('employee_io_schema_too_large')
  if (value === null || typeof value === 'boolean') { charge(budget, value === null ? 4 : value ? 4 : 5); return value }
  if (typeof value === 'string') { charge(budget, stringSizes(value).json); return value }
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value))) throw new EmployeeIOSchemaError()
    charge(budget, String(value).length)
    return value
  }
  if (typeof value !== 'object' || !value) throw new EmployeeIOSchemaError()
  if (budget.active.has(value)) throw new EmployeeIOSchemaError()
  budget.active.add(value)
  if (Array.isArray(value)) {
    if (Object.getPrototypeOf(value) !== Array.prototype) throw new EmployeeIOSchemaError()
    const lengthDescriptor = Object.getOwnPropertyDescriptor(value, 'length')
    if (!lengthDescriptor || !Object.hasOwn(lengthDescriptor, 'value') || typeof lengthDescriptor.value !== 'number'
      || !Number.isInteger(lengthDescriptor.value) || lengthDescriptor.value < 0) throw new EmployeeIOSchemaError()
    const length = lengthDescriptor.value
    if (length > MAX_NODES) throw new EmployeeIOSchemaError('employee_io_schema_too_large')
    const keys = Reflect.ownKeys(value)
    if (keys.length !== length + 1 || keys.some(key => typeof key !== 'string'
      || (key !== 'length' && !/^(0|[1-9][0-9]*)$/.test(key)))) throw new EmployeeIOSchemaError()
    charge(budget, 2 + Math.max(0, length - 1))
    const output: JSONValue[] = []
    // Fixed initial length, not a live iterator.
    for (let index = 0; index < length; index++) output.push(clone(data(value, String(index)), depth + 1, budget))
    if (Object.getOwnPropertyDescriptor(value, 'length')?.value !== length) throw new EmployeeIOSchemaError()
    budget.active.delete(value)
    return Object.freeze(output)
  }
  if (!record(value)) throw new EmployeeIOSchemaError()
  const keys = Reflect.ownKeys(value)
  if (keys.length > MAX_NODES) throw new EmployeeIOSchemaError('employee_io_schema_too_large')
  charge(budget, 2 + Math.max(0, keys.length - 1))
  const output: Record<string, JSONValue> = {}
  for (const key of keys) {
    if (typeof key !== 'string') throw new EmployeeIOSchemaError()
    charge(budget, stringSizes(key).json + 1)
    const child = clone(data(value, key), depth + 1, budget)
    Object.defineProperty(output, key, { value: child, enumerable: true, configurable: false, writable: false })
  }
  budget.active.delete(value)
  return Object.freeze(output)
}
/** Only a local safety precheck; the server owns full metaschema/ref/privacy validation. */
function schemaPrecheck(value: SchemaValue): void {
  const maps = ['properties', 'patternProperties', '$defs', 'definitions', 'dependentSchemas']
  const lists = ['allOf', 'anyOf', 'oneOf', 'prefixItems']
  const singles = ['additionalProperties', 'unevaluatedProperties', 'propertyNames', 'items', 'contains', 'unevaluatedItems', 'not', 'if', 'then', 'else', 'contentSchema']
  const walk = (node: JSONValue): void => {
    if (!record(node)) return
    if (Object.hasOwn(node, '$schema') && node.$schema !== SCHEMA_URI && node.$schema !== SCHEMA_URI + '#') throw new EmployeeIOSchemaError()
    for (const key of ['$ref', '$dynamicRef', '$recursiveRef']) {
      if (Object.hasOwn(node, key) && (typeof node[key] !== 'string' || !node[key].startsWith('#'))) throw new EmployeeIOSchemaError()
    }
    for (const key of maps) if (record(node[key])) for (const child of Object.values(node[key])) walk(child as JSONValue)
    for (const key of lists) if (Array.isArray(node[key])) for (const child of node[key]) walk(child as JSONValue)
    for (const key of singles) if (Object.hasOwn(node, key)) walk(node[key] as JSONValue)
  }
  walk(value)
}
function schema(value: unknown): { value: SchemaValue; bytes: number } {
  if (!record(value)) throw new EmployeeIOSchemaError()
  const budget: Budget = { nodes: 0, bytes: 0, active: new WeakSet() }
  const copied = clone(value, 0, budget) as SchemaValue
  schemaPrecheck(copied)
  return { value: copied, bytes: budget.bytes }
}
function state(kind: EmployeeIOSchemaState['kind'], text = ''): EmployeeIOSchemaState { return Object.freeze({ kind, text }) }
function local(text: string): { state: EmployeeIOSchemaState; value?: SchemaValue; bytes?: number } {
  stringSizes(text) // Limit the text before parsing, even invalid text.
  try {
    const parsed: unknown = JSON.parse(text)
    const copied = schema(parsed)
    return { state: state('present', text), value: copied.value, bytes: copied.bytes }
  } catch { return { state: state('invalid', text) } }
}
function checked(value: EmployeeIOSchemaState): { state: EmployeeIOSchemaState; value?: SchemaValue; bytes?: number } {
  const input = fields(value, ['kind', 'text'])
  if (typeof input.text !== 'string') throw new EmployeeIOSchemaError()
  if (input.kind === 'missing') {
    if (input.text !== '') throw new EmployeeIOSchemaError()
    return { state: state('missing') }
  }
  if (input.kind !== 'present' && input.kind !== 'invalid') throw new EmployeeIOSchemaError()
  const result = local(input.text)
  if (result.state.kind !== input.kind) throw new EmployeeIOSchemaError()
  return result
}
function pair(value: EmployeeIOSchemas): EmployeeIOSchemas {
  const input = fields(value, IO_FIELDS)
  return Object.freeze({ input_schema: checked(input.input_schema as EmployeeIOSchemaState).state,
    output_schema: checked(input.output_schema as EmployeeIOSchemaState).state })
}
function readField(manifest: Record<string, unknown>, field: EmployeeIOSchemaField): EmployeeIOSchemaState {
  try {
    const descriptor = Object.getOwnPropertyDescriptor(manifest, field)
    if (!descriptor) return state('missing')
    if (!descriptor.enumerable || !Object.hasOwn(descriptor, 'value')) return state('invalid')
    const copied = schema(descriptor.value)
    const text = JSON.stringify(copied.value)
    if (typeof text !== 'string') throw new EmployeeIOSchemaError()
    stringSizes(text)
    return state('present', text)
  } catch { return state('invalid') }
}
export function readEmployeeIOSchemas(manifest: unknown): EmployeeIOSchemas {
  try {
    if (record(manifest)) return Object.freeze({ input_schema: readField(manifest, 'input_schema'), output_schema: readField(manifest, 'output_schema') })
  } catch { /* Unknown host content is not echoed. */ }
  return Object.freeze({ input_schema: state('invalid'), output_schema: state('invalid') })
}
export function copyEmployeeIOSchemas(value: EmployeeIOSchemas): EmployeeIOSchemas { return guard(() => pair(value)) }
export function sameEmployeeIOSchemas(a: EmployeeIOSchemas, b: EmployeeIOSchemas): boolean {
  return guard(() => { const left = pair(a), right = pair(b); return IO_FIELDS.every(field => left[field].kind === right[field].kind && left[field].text === right[field].text) })
}
export function employeeIOSchemaState(text: string): EmployeeIOSchemaState { return guard(() => local(text).state) }
export function employeeIOSchemaValue(value: EmployeeIOSchemaState): Readonly<Record<string, unknown>> {
  return guard(() => {
    const copied = checked(value)
    if (copied.state.kind !== 'present' || !copied.value) throw new EmployeeIOSchemaError()
    return copied.value
  })
}
export function employeeIOSchemaChanges(current: EmployeeIOSchemas, baseline: EmployeeIOSchemas): Readonly<Record<string, unknown>> {
  return guard(() => {
    const next = pair(current), original = pair(baseline), output: Record<string, unknown> = {}
    for (const field of IO_FIELDS) {
      if (next[field].kind === original[field].kind && next[field].text === original[field].text) continue
      if (next[field].kind === 'missing') throw new EmployeeIOSchemaError('employee_io_schema_removal_unsupported')
      if (next[field].kind !== 'present') throw new EmployeeIOSchemaError()
      output[field] = employeeIOSchemaValue(next[field])
    }
    return Object.keys(output).length ? Object.freeze(output) : emptyChanges
  })
}
export function employeeIOSchemaEncodedBytes(value: EmployeeIOSchemas): number {
  return guard(() => {
    const copied = pair(value)
    let count = 0, bytes = 2
    for (const field of IO_FIELDS) {
      if (copied[field].kind === 'missing') continue
      const item = checked(copied[field])
      bytes += (count++ ? 1 : 0) + stringSizes(field).json + 1
        + (item.state.kind === 'present' ? item.bytes! : stringSizes(item.state.text).json)
    }
    return count ? bytes : 0
  })
}
export function employeeIOSchemaProblems(value: EmployeeIOSchemas): readonly EmployeeIOSchemaProblem[] {
  return guard(() => {
    const copied = pair(value), output: EmployeeIOSchemaProblem[] = []
    for (const field of IO_FIELDS) {
      if (copied[field].kind === 'missing') output.push(Object.freeze({ path: field, severity: 'warning', message: '还没有填写这项结构；保存其他内容不会自动补上。' }))
      if (copied[field].kind === 'invalid') output.push(Object.freeze({ path: field, severity: 'error', message: '这项内容暂时无法保存；请检查格式或重新填写。' }))
    }
    return Object.freeze(output)
  })
}
