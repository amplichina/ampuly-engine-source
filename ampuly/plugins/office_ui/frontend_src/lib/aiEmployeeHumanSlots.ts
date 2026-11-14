/** Bounded template responsibilities, never people, contacts, or runtime grants. */
export interface EmployeeHumanSlot {
  readonly slot: string
  readonly required: boolean
  readonly decides: readonly string[]
  readonly channel_options?: readonly string[]
}
export interface EmployeeHumanSlotsState { readonly kind: 'missing' | 'present' | 'invalid'; readonly items: readonly EmployeeHumanSlot[] }
export interface EmployeeHumanSlotsProblem { readonly path: string; readonly message: string; readonly severity: 'warning' | 'error' }
type ErrorCode = 'employee_human_slots_invalid' | 'employee_human_slots_too_large' | 'employee_human_slots_removal_unsupported'
export const EMPLOYEE_HUMAN_ROLES = Object.freeze(['submitter', 'rule_confirmer', 'reviewer', 'approver', 'escalation', 'backup', 'delivery_authorizer'] as const)
export const EMPLOYEE_HUMAN_DECISIONS = Object.freeze(['release', 'reject', 'confirm_fact', 'authorize_send'] as const)
export const EMPLOYEE_HUMAN_CHANNELS = Object.freeze(['console', 'feishu', 'dingtalk', 'wecom', 'email'] as const)
export const employeeHumanRoleLabels: Readonly<Record<string, string>> = Object.freeze({
  submitter: '需求提交人', rule_confirmer: '规则确认人', reviewer: '审核人', approver: '审批人',
  escalation: '异常接手人', backup: '备用人', delivery_authorizer: '交付授权人',
})
export const employeeHumanDecisionLabels: Readonly<Record<string, string>> = Object.freeze({ release: '放行', reject: '退回修改', confirm_fact: '确认事实', authorize_send: '允许外发' })
export const employeeHumanChannelLabels: Readonly<Record<string, string>> = Object.freeze({ console: '控制台', feishu: '飞书', dingtalk: '钉钉', wecom: '企业微信', email: '邮箱' })
const MAX_ROWS = 7, MAX_ENUM_ITEMS = 500, MAX_ENUM_CHARACTERS = 64, MAX_BYTES = 2 * 1024 * 1024
const emptyItems: readonly EmployeeHumanSlot[] = Object.freeze([])
const emptyChanges: Readonly<Record<string, unknown>> = Object.freeze({})
export class EmployeeHumanSlotsError extends Error {
  readonly code: ErrorCode
  constructor(code: ErrorCode = 'employee_human_slots_invalid') {
    const safe = code === 'employee_human_slots_too_large' || code === 'employee_human_slots_removal_unsupported' ? code : 'employee_human_slots_invalid'
    super(safe === 'employee_human_slots_too_large' ? '人工职责内容太多，请减少后再试。'
      : safe === 'employee_human_slots_removal_unsupported' ? '不能取消已保存的人工职责约定；可以修改内容或恢复打开时的内容。'
        : '请检查人工职责、判断事项和反馈方式。')
    this.name = 'EmployeeHumanSlotsError'
    this.code = safe
  }
}
function guard<T>(operation: () => T): T {
  let code: ErrorCode = 'employee_human_slots_invalid'
  try { return operation() } catch (error) {
    try {
      if (error instanceof EmployeeHumanSlotsError) {
        const descriptor = Object.getOwnPropertyDescriptor(error, 'code')
        if (descriptor && Object.hasOwn(descriptor, 'value')) code = descriptor.value as ErrorCode
      }
    } catch { /* No hostile original exception escapes the fixed-error boundary. */ }
  }
  throw new EmployeeHumanSlotsError(code)
}
function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null)
}
function fields(value: unknown, required: readonly string[], optional: readonly string[] = []): Record<string, unknown> {
  if (!record(value)) throw new EmployeeHumanSlotsError()
  const keys = Reflect.ownKeys(value)
  if (keys.length < required.length || keys.length > required.length + optional.length
    || keys.some(key => typeof key !== 'string' || (!required.includes(key) && !optional.includes(key)))) throw new EmployeeHumanSlotsError()
  const result: Record<string, unknown> = {}
  for (const key of [...required, ...optional]) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key)
    if (!descriptor && optional.includes(key)) continue
    if (!descriptor || !descriptor.enumerable || !Object.hasOwn(descriptor, 'value')) throw new EmployeeHumanSlotsError()
    result[key] = descriptor.value
  }
  return result
}
function length(value: readonly unknown[], maximum: number): number {
  const descriptor = Object.getOwnPropertyDescriptor(value, 'length')
  if (!descriptor || !Object.hasOwn(descriptor, 'value') || typeof descriptor.value !== 'number'
    || !Number.isInteger(descriptor.value) || descriptor.value < 0) throw new EmployeeHumanSlotsError()
  if (descriptor.value > maximum) throw new EmployeeHumanSlotsError('employee_human_slots_too_large')
  return descriptor.value
}
function array(value: unknown, maximum: number): { value: readonly unknown[]; length: number } {
  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) throw new EmployeeHumanSlotsError()
  const count = length(value, maximum), keys = Reflect.ownKeys(value)
  if (keys.length !== count + 1 || keys.some(key => typeof key !== 'string' || (key !== 'length' && !/^(0|[1-9][0-9]*)$/.test(key)))) throw new EmployeeHumanSlotsError()
  return { value, length: count }
}
function item(value: readonly unknown[], index: number): unknown {
  const descriptor = Object.getOwnPropertyDescriptor(value, String(index))
  if (!descriptor || !descriptor.enumerable || !Object.hasOwn(descriptor, 'value')) throw new EmployeeHumanSlotsError()
  return descriptor.value
}
function enumeration(value: unknown, options: readonly string[], blank = false): string {
  if (typeof value !== 'string') throw new EmployeeHumanSlotsError()
  if (value.length > MAX_ENUM_CHARACTERS) throw new EmployeeHumanSlotsError('employee_human_slots_too_large')
  if ((!blank || value !== '') && !options.includes(value)) throw new EmployeeHumanSlotsError()
  return value
}
function enumList(value: unknown, options: readonly string[]): readonly string[] {
  const source = array(value, MAX_ENUM_ITEMS), result: string[] = []
  for (let index = 0; index < source.length; index++) result.push(enumeration(item(source.value, index), options))
  if (length(source.value, MAX_ENUM_ITEMS) !== source.length) throw new EmployeeHumanSlotsError()
  return Object.freeze(result)
}
function absent(kind: 'missing' | 'invalid'): EmployeeHumanSlotsState { return Object.freeze({ kind, items: emptyItems }) }
function clone(value: unknown): { state: EmployeeHumanSlotsState; bytes: number } {
  const source = array(value, MAX_ROWS), rows: EmployeeHumanSlot[] = []
  for (let index = 0; index < source.length; index++) {
    const row = fields(item(source.value, index), ['slot', 'required', 'decides'], ['channel_options'])
    const role = enumeration(row.slot, EMPLOYEE_HUMAN_ROLES, true)
    if (typeof row.required !== 'boolean') throw new EmployeeHumanSlotsError()
    const decides = enumList(row.decides, EMPLOYEE_HUMAN_DECISIONS)
    const result: EmployeeHumanSlot = Object.hasOwn(row, 'channel_options')
      ? { slot: role, required: row.required, decides, channel_options: enumList(row.channel_options, EMPLOYEE_HUMAN_CHANNELS) }
      : { slot: role, required: row.required, decides }
    rows.push(Object.freeze(result))
  }
  if (length(source.value, MAX_ROWS) !== source.length) throw new EmployeeHumanSlotsError()
  const copied = Object.freeze(rows)
  // Only bounded, detached known scalars are serialized, never original input.
  const bytes = new TextEncoder().encode(JSON.stringify(copied)).byteLength
  if (bytes > MAX_BYTES) throw new EmployeeHumanSlotsError('employee_human_slots_too_large')
  return { state: Object.freeze({ kind: 'present', items: copied }), bytes }
}
function checked(value: EmployeeHumanSlotsState): { state: EmployeeHumanSlotsState; bytes: number } {
  const input = fields(value, ['kind', 'items'])
  if (input.kind === 'present') return clone(input.items)
  if ((input.kind !== 'missing' && input.kind !== 'invalid') || array(input.items, MAX_ROWS).length) throw new EmployeeHumanSlotsError()
  return { state: absent(input.kind), bytes: 0 }
}
export function readEmployeeHumanSlots(manifest: Readonly<Record<string, unknown>>): EmployeeHumanSlotsState {
  try {
    if (!record(manifest)) return absent('invalid')
    const descriptor = Object.getOwnPropertyDescriptor(manifest, 'human_slots')
    if (!descriptor) return absent('missing')
    if (!descriptor.enumerable || !Object.hasOwn(descriptor, 'value')) return absent('invalid')
    return clone(descriptor.value).state
  } catch { return absent('invalid') }
}
export function employeeHumanSlotsState(items: readonly EmployeeHumanSlot[]): EmployeeHumanSlotsState { return guard(() => clone(items).state) }
function sameList(a: readonly string[], b: readonly string[]): boolean { return a.length === b.length && a.every((value, index) => value === b[index]) }
function equal(a: EmployeeHumanSlotsState, b: EmployeeHumanSlotsState): boolean {
  return a.kind === b.kind && a.items.length === b.items.length && a.items.every((row, index) => {
    const other = b.items[index]
    return row.slot === other.slot && row.required === other.required && sameList(row.decides, other.decides)
      && Object.hasOwn(row, 'channel_options') === Object.hasOwn(other, 'channel_options')
      && (!Object.hasOwn(row, 'channel_options') || sameList(row.channel_options!, other.channel_options!))
  })
}
export function sameEmployeeHumanSlots(a: EmployeeHumanSlotsState, b: EmployeeHumanSlotsState): boolean { return guard(() => equal(checked(a).state, checked(b).state)) }
function problems(value: EmployeeHumanSlotsState): readonly EmployeeHumanSlotsProblem[] {
  const result: EmployeeHumanSlotsProblem[] = []
  const add = (path: string, message: string, severity: EmployeeHumanSlotsProblem['severity'] = 'error'): void => { result.push(Object.freeze({ path, message, severity })) }
  if (value.kind === 'missing') add('human_slots', '还没有填写人工职责；保存其他内容不会自动补上。', 'warning')
  else if (value.kind === 'invalid') add('human_slots', '原有人工职责暂时无法完整显示，需要明确重新配置才会替换。')
  else {
    if (!value.items.length) add('human_slots', '至少添加一项人工职责。')
    if (!value.items.some(row => row.slot === 'reviewer' && row.required)) add('human_slots', '至少需要一位必须参与的审核人。')
    const roles = new Set<string>()
    value.items.forEach((row, index) => {
      const path = 'human_slots[' + index + ']'
      if (row.slot === '') add(path + '.slot', '请选择这项职责由哪种角色负责。')
      if (roles.has(row.slot)) add(path + '.slot', '这个角色与前面的重复，请明确修改或删除。')
      roles.add(row.slot)
      if (row.slot === 'reviewer' && !row.required) add(path + '.required', '审核人需要勾选“必须有人负责”。')
      if (!row.decides.length) add(path + '.decides', '至少勾选一种判断事项。')
      for (const field of ['decides', 'channel_options'] as const) {
        const seen = new Set<string>()
        ;(row[field] ?? []).forEach((choice, here) => {
          if (seen.has(choice)) add(path + '.' + field + '[' + here + ']', field === 'decides'
            ? '判断事项与前面的重复，请明确取消或重新选择。' : '反馈方式与前面的重复，请明确取消或重新选择。')
          seen.add(choice)
        })
      }
    })
  }
  return Object.freeze(result)
}
export function employeeHumanSlotsProblems(state: EmployeeHumanSlotsState): readonly EmployeeHumanSlotsProblem[] { return guard(() => problems(checked(state).state)) }
export function employeeHumanSlotsValue(state: EmployeeHumanSlotsState): readonly EmployeeHumanSlot[] {
  return guard(() => { const next = checked(state).state; if (next.kind !== 'present') throw new EmployeeHumanSlotsError(); return next.items })
}
export function validateEmployeeHumanSlots(value: unknown): readonly EmployeeHumanSlot[] {
  return guard(() => { const next = clone(value).state; if (problems(next).some(problem => problem.severity === 'error')) throw new EmployeeHumanSlotsError(); return next.items })
}
export function employeeHumanSlotsChanges(current: EmployeeHumanSlotsState, baseline: EmployeeHumanSlotsState): Readonly<Record<string, unknown>> {
  return guard(() => {
    const next = checked(current).state, original = checked(baseline).state
    if (equal(next, original)) return emptyChanges
    if (next.kind !== 'present') throw new EmployeeHumanSlotsError('employee_human_slots_removal_unsupported')
    return Object.freeze({ human_slots: validateEmployeeHumanSlots(next.items) })
  })
}
export function employeeHumanSlotsEncodedBytes(state: EmployeeHumanSlotsState): number { return guard(() => checked(state).bytes) }
