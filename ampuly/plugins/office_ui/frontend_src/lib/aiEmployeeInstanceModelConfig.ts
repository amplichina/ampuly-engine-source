/** Public local configuration only. Catalogue rows and provenance are not grants. */
export type EmployeeInstanceModelTier = 'light' | 'standard' | 'strong'
export type EmployeeInstanceModelFields = Readonly<{
  provider?: string; model?: string; tier?: EmployeeInstanceModelTier
}>
export type EmployeeInstanceModelConfigState = Readonly<{
  kind: 'missing' | 'invalid' | 'present'; fields: EmployeeInstanceModelFields
}>
export type EmployeeInstanceModelCandidate = Readonly<{
  provider: string
  model: string
  status: 'active' | 'vendor_routed_alias' | 'retired'
  availability: 'available' | 'off_by_default' | 'not_routable' | 'needs_params'
  availability_reason: '' | 'thinking_default_tool_loop' | 'thinking_default' | 'window_cap'
    | 'no_litellm_route' | 'base_not_applied' | 'overseas_vendor'
  capabilities: Readonly<{
    tools: boolean; vision: boolean; thinking_default: boolean; reasoning_passback_required: boolean
  }>
  selectable: boolean
  gaps: readonly EmployeeInstanceModelGap[]
}>
export type EmployeeInstanceModelGap = 'model_unknown' | 'model_retired' | 'model_overseas_off'
  | 'model_not_routable' | 'model_needs_params' | 'model_needs_trial'
export const EMPLOYEE_INSTANCE_MODEL_PROJECTION_VERSION = 'employee_instance_model_selection.v1' as const
export const EMPLOYEE_INSTANCE_MODEL_MAX_CANDIDATES = 500
export const EMPLOYEE_INSTANCE_MODEL_MAX_BYTES = 2 * 1024 * 1024
export const EMPLOYEE_INSTANCE_MODEL_MAX_NODES = 50000
export const EMPLOYEE_INSTANCE_MODEL_MAX_DEPTH = 64
export type EmployeeInstanceModelSnapshot = Readonly<{
  request_id: string
  projection_version: typeof EMPLOYEE_INSTANCE_MODEL_PROJECTION_VERSION
  model_catalogue_revision: string
  candidates: readonly EmployeeInstanceModelCandidate[]
}>
export type EmployeeInstanceModelSelectionSource = Readonly<{
  request_id: string; model_catalogue_revision: string
}>
export type EmployeeInstanceModelEdit = Readonly<{
  config: EmployeeInstanceModelConfigState; selection_source: EmployeeInstanceModelSelectionSource | null
}>
export type EmployeeInstanceModelProblem = Readonly<{
  path: 'model_config'; severity: 'warning' | 'error'; message: string
}>
type ModelErrorCode = 'employee_instance_model_invalid' | 'employee_instance_model_projection_limit'
  | 'employee_instance_model_selection_stale'
const MESSAGES: Readonly<Record<ModelErrorCode, string>> = Object.freeze({
  employee_instance_model_invalid: '本项目模型配置或候选格式无效。',
  employee_instance_model_projection_limit: '本项目模型候选或配置超过显示限额。',
  employee_instance_model_selection_stale: '模型选择来源已变化，请重新明确选择。',
})
const TIERS: readonly EmployeeInstanceModelTier[] = Object.freeze(['light', 'standard', 'strong'])
const STATUSES = ['active', 'vendor_routed_alias', 'retired'] as const
const AVAILABILITIES = ['available', 'off_by_default', 'not_routable', 'needs_params'] as const
const REASONS = ['', 'thinking_default_tool_loop', 'thinking_default', 'window_cap',
  'no_litellm_route', 'base_not_applied', 'overseas_vendor'] as const
const GAPS: readonly EmployeeInstanceModelGap[] = Object.freeze(['model_unknown', 'model_retired',
  'model_overseas_off', 'model_not_routable', 'model_needs_params', 'model_needs_trial'])
const ROW_KEYS = ['provider', 'model', 'status', 'availability', 'availability_reason',
  'capabilities', 'selectable', 'gaps'] as const
const CAP_KEYS = ['tools', 'vision', 'thinking_default', 'reasoning_passback_required'] as const
const SNAPSHOT_KEYS = ['request_id', 'projection_version', 'model_catalogue_revision', 'candidates'] as const
const EMPTY = Object.freeze({})
const MISSING: EmployeeInstanceModelConfigState = Object.freeze({ kind: 'missing', fields: EMPTY })
const INVALID: EmployeeInstanceModelConfigState = Object.freeze({ kind: 'invalid', fields: EMPTY })
const own = (value: object, key: PropertyKey) => Object.prototype.hasOwnProperty.call(value, key)

export class EmployeeInstanceModelConfigError extends Error {
  readonly code: ModelErrorCode
  constructor(code: ModelErrorCode = 'employee_instance_model_invalid') {
    const safeCode = code === 'employee_instance_model_projection_limit'
      || code === 'employee_instance_model_selection_stale' ? code : 'employee_instance_model_invalid'
    super(MESSAGES[safeCode])
    this.name = 'EmployeeInstanceModelConfigError'
    this.code = safeCode
  }
}
function fail(code: ModelErrorCode = 'employee_instance_model_invalid'): never {
  throw new EmployeeInstanceModelConfigError(code)
}
function guarded<T>(read: () => T): T {
  let code: ModelErrorCode = 'employee_instance_model_invalid'
  try { return read() }
  catch (error) {
    try {
      if (error instanceof EmployeeInstanceModelConfigError) {
        const descriptor = Object.getOwnPropertyDescriptor(error, 'code')
        if (descriptor && own(descriptor, 'value') && (descriptor.value === 'employee_instance_model_projection_limit'
          || descriptor.value === 'employee_instance_model_selection_stale')) code = descriptor.value
      }
    } catch { /* Reflection errors cannot disclose input diagnostics. */ }
  }
  return fail(code)
}
function plain(value: unknown): value is object {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}
function data(value: object, key: PropertyKey): unknown {
  const descriptor = Object.getOwnPropertyDescriptor(value, key)
  if (!descriptor || !own(descriptor, 'value')) return fail()
  return descriptor.value
}
/** Fixed initial key bounds; no accessor, inherited value or live unbounded traversal. */
function record(value: unknown, allowed: readonly string[], required: readonly string[] = allowed): Record<string, unknown> {
  if (!plain(value)) return fail()
  const keys = Reflect.ownKeys(value)
  const length = keys.length
  if (length > allowed.length || length < required.length) return fail()
  const result: Record<string, unknown> = Object.create(null)
  for (let index = 0; index < length; index += 1) {
    const key = keys[index]
    if (typeof key !== 'string' || !allowed.includes(key)) return fail()
    result[key] = data(value, key)
    if (Reflect.ownKeys(value).length !== length) return fail()
  }
  for (let index = 0; index < required.length; index += 1) if (!own(result, required[index])) return fail()
  const finalKeys = Reflect.ownKeys(value)
  if (finalKeys.length !== length) return fail()
  for (let index = 0; index < length; index += 1) if (!finalKeys.includes(keys[index])) return fail()
  return result
}
function list(value: unknown, maximum: number): readonly unknown[] {
  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) return fail()
  const length = data(value, 'length')
  if (typeof length !== 'number' || !Number.isSafeInteger(length) || length < 0) return fail()
  if (length > maximum) return fail('employee_instance_model_projection_limit')
  if (Reflect.ownKeys(value).length !== length + 1) return fail()
  const result: unknown[] = []
  for (let index = 0; index < length; index += 1) {
    if (data(value, 'length') !== length) return fail()
    result.push(data(value, String(index)))
    if (data(value, 'length') !== length || Reflect.ownKeys(value).length !== length + 1) return fail()
  }
  if (data(value, 'length') !== length) return fail()
  return result
}
function text(value: unknown): string {
  if (typeof value !== 'string' || value.length < 1) return fail()
  if (value.length > 256) return fail('employee_instance_model_projection_limit')
  if (value.trim() !== value || /[\u0000-\u001f\u007f-\u009f]/.test(value)) return fail()
  const length = value.length
  for (let index = 0; index < length; index += 1) {
    const unit = value.charCodeAt(index)
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = value.charCodeAt(index + 1)
      if (!(next >= 0xdc00 && next <= 0xdfff)) return fail()
      index += 1
    } else if (unit >= 0xdc00 && unit <= 0xdfff) return fail()
  }
  if (new TextEncoder().encode(value).byteLength > 256) return fail('employee_instance_model_projection_limit')
  return value
}
function enumText<T extends string>(value: unknown, choices: readonly T[]): T {
  if (typeof value !== 'string' || !choices.includes(value as T)) return fail()
  return value as T
}
function digest(value: unknown): string {
  if (typeof value !== 'string' || value.length !== 64 || !/^[0-9a-f]{64}$/.test(value)) return fail()
  return value
}
function fields(value: unknown): EmployeeInstanceModelFields {
  const input = record(value, ['provider', 'model', 'tier'], [])
  const result: { provider?: string; model?: string; tier?: EmployeeInstanceModelTier } = {}
  if (own(input, 'provider')) result.provider = text(input.provider)
  if (own(input, 'model')) result.model = text(input.model)
  if (own(input, 'tier')) result.tier = enumText(input.tier, TIERS)
  return Object.freeze(result)
}
function state(value: unknown): EmployeeInstanceModelConfigState {
  const input = record(value, ['kind', 'fields'])
  const kind = enumText(input.kind, ['missing', 'invalid', 'present'] as const)
  const projected = fields(input.fields)
  if (kind === 'present') return Object.freeze({ kind, fields: projected })
  if (Reflect.ownKeys(projected).length !== 0) return fail()
  return kind === 'missing' ? MISSING : INVALID
}
/** Only this own field is inspected; unrelated manifest values/getters are ignored. */
export function readEmployeeInstanceModelConfig(instance: unknown): EmployeeInstanceModelConfigState {
  try {
    if (!plain(instance)) return INVALID
    const descriptor = Object.getOwnPropertyDescriptor(instance, 'model_config')
    if (!descriptor) return MISSING
    if (!own(descriptor, 'value')) return INVALID
    return Object.freeze({ kind: 'present', fields: fields(descriptor.value) })
  } catch { return INVALID }
}
export function employeeInstanceModelConfigState(value: unknown): EmployeeInstanceModelConfigState {
  return guarded(() => Object.freeze({ kind: 'present', fields: fields(value) }))
}
export function copyEmployeeInstanceModelConfigState(value: EmployeeInstanceModelConfigState): EmployeeInstanceModelConfigState {
  return guarded(() => state(value))
}
export function sameEmployeeInstanceModelConfig(a: EmployeeInstanceModelConfigState, b: EmployeeInstanceModelConfigState): boolean {
  return guarded(() => {
    const left = state(a), right = state(b)
    if (left.kind !== right.kind) return false
    for (const key of ['provider', 'model', 'tier'] as const) {
      if (own(left.fields, key) !== own(right.fields, key) || left.fields[key] !== right.fields[key]) return false
    }
    return true
  })
}
/** Count this known public JSON projection before serializing, not arbitrary input. */
class ProjectionBudget {
  private bytes = 0
  private nodes = 0
  node(depth: number) {
    this.nodes += 1
    if (depth > EMPLOYEE_INSTANCE_MODEL_MAX_DEPTH || this.nodes > EMPLOYEE_INSTANCE_MODEL_MAX_NODES) return fail('employee_instance_model_projection_limit')
  }
  punctuation(bytes: number) {
    this.bytes += bytes
    if (this.bytes > EMPLOYEE_INSTANCE_MODEL_MAX_BYTES) return fail('employee_instance_model_projection_limit')
  }
  string(value: string, depth: number) { this.node(depth); this.punctuation(new TextEncoder().encode(JSON.stringify(value)).byteLength) }
  key(value: string, depth: number) { this.string(value, depth); this.punctuation(1) }
  boolean(value: boolean, depth: number) { this.node(depth); this.punctuation(value ? 4 : 5) }
}
function candidate(value: unknown, budget: ProjectionBudget): EmployeeInstanceModelCandidate {
  const row = record(value, ROW_KEYS)
  const provider = text(row.provider), model = text(row.model)
  const status = enumText(row.status, STATUSES), availability = enumText(row.availability, AVAILABILITIES)
  const availability_reason = enumText(row.availability_reason, REASONS)
  const rawCaps = record(row.capabilities, CAP_KEYS)
  const caps: Record<string, boolean> = Object.create(null)
  for (let index = 0; index < CAP_KEYS.length; index += 1) {
    const key = CAP_KEYS[index]
    if (typeof rawCaps[key] !== 'boolean') return fail()
    caps[key] = rawCaps[key] as boolean
  }
  if (typeof row.selectable !== 'boolean') return fail()
  const rawGaps = list(row.gaps, 1)
  const gaps: EmployeeInstanceModelGap[] = []
  for (let index = 0; index < rawGaps.length; index += 1) gaps.push(enumText(rawGaps[index], GAPS))
  // No policy is inferred from capabilities. Contradictory successful declarations are rejected.
  if (row.selectable && (gaps.length !== 0 || status === 'retired' || availability !== 'available')) return fail()
  if (!row.selectable && gaps.length !== 1) return fail()
  if (gaps.includes('model_unknown')) return fail()
  budget.node(2); budget.punctuation(2 + ROW_KEYS.length - 1)
  const publicText = { provider, model, status, availability, availability_reason }
  for (const key of ['provider', 'model', 'status', 'availability', 'availability_reason'] as const) {
    budget.key(key, 3); budget.string(publicText[key], 3)
  }
  budget.key('capabilities', 3); budget.node(3); budget.punctuation(2 + CAP_KEYS.length - 1)
  for (let index = 0; index < CAP_KEYS.length; index += 1) {
    const key = CAP_KEYS[index]; budget.key(key, 4); budget.boolean(caps[key], 4)
  }
  budget.key('selectable', 3); budget.boolean(row.selectable, 3)
  budget.key('gaps', 3); budget.node(3); budget.punctuation(2)
  for (let index = 0; index < gaps.length; index += 1) budget.string(gaps[index], 4)
  return Object.freeze({ provider, model, status, availability, availability_reason,
    capabilities: Object.freeze({ tools: caps.tools, vision: caps.vision,
      thinking_default: caps.thinking_default, reasoning_passback_required: caps.reasoning_passback_required }),
    selectable: row.selectable, gaps: Object.freeze(gaps) })
}
function snapshot(value: unknown): EmployeeInstanceModelSnapshot {
  const input = record(value, SNAPSHOT_KEYS)
  const request_id = text(input.request_id), model_catalogue_revision = digest(input.model_catalogue_revision)
  if (input.projection_version !== EMPLOYEE_INSTANCE_MODEL_PROJECTION_VERSION) return fail()
  const rows = list(input.candidates, EMPLOYEE_INSTANCE_MODEL_MAX_CANDIDATES)
  const budget = new ProjectionBudget()
  budget.node(0); budget.punctuation(2 + SNAPSHOT_KEYS.length - 1)
  for (const key of ['request_id', 'projection_version', 'model_catalogue_revision'] as const) {
    budget.key(key, 1); budget.string(key === 'request_id' ? request_id
      : key === 'projection_version' ? EMPLOYEE_INSTANCE_MODEL_PROJECTION_VERSION : model_catalogue_revision, 1)
  }
  budget.key('candidates', 1); budget.node(1); budget.punctuation(2 + Math.max(0, rows.length - 1))
  const candidates: EmployeeInstanceModelCandidate[] = [], models = new Set<string>()
  for (let index = 0; index < rows.length; index += 1) {
    const row = candidate(rows[index], budget)
    if (models.has(row.model)) return fail()
    models.add(row.model); candidates.push(row)
  }
  const result = Object.freeze({ request_id, projection_version: EMPLOYEE_INSTANCE_MODEL_PROJECTION_VERSION,
    model_catalogue_revision, candidates: Object.freeze(candidates) })
  if (new TextEncoder().encode(JSON.stringify(result)).byteLength > EMPLOYEE_INSTANCE_MODEL_MAX_BYTES) return fail('employee_instance_model_projection_limit')
  return result
}
/** The caller must first correlate the flat ACK, opened revision, project and connection. */
export function employeeInstanceModelCandidates(value: unknown): EmployeeInstanceModelSnapshot {
  return guarded(() => snapshot(value))
}
export function copyEmployeeInstanceModelSnapshot(value: EmployeeInstanceModelSnapshot): EmployeeInstanceModelSnapshot {
  return guarded(() => snapshot(value))
}
function edit(value: unknown): EmployeeInstanceModelEdit {
  const input = record(value, ['config', 'selection_source'])
  const config = state(input.config)
  if (input.selection_source === null) return Object.freeze({ config, selection_source: null })
  const source = record(input.selection_source, ['request_id', 'model_catalogue_revision'])
  if (config.kind !== 'present' || !own(config.fields, 'provider') || !own(config.fields, 'model')) return fail()
  return Object.freeze({ config, selection_source: Object.freeze({ request_id: text(source.request_id),
    model_catalogue_revision: digest(source.model_catalogue_revision) }) })
}
export function employeeInstanceModelEdit(config: EmployeeInstanceModelConfigState): EmployeeInstanceModelEdit {
  return guarded(() => Object.freeze({ config: state(config), selection_source: null }))
}
export function copyEmployeeInstanceModelEdit(value: EmployeeInstanceModelEdit): EmployeeInstanceModelEdit {
  return guarded(() => edit(value))
}
/** Explicit selection, retaining an existing tier until the user explicitly adjusts/removes it. */
export function selectEmployeeInstanceModel(value: EmployeeInstanceModelEdit, catalogue: EmployeeInstanceModelSnapshot,
  provider: string, model: string): EmployeeInstanceModelEdit {
  return guarded(() => {
    const current = edit(value), directory = snapshot(catalogue), originalProvider = text(provider), originalModel = text(model)
    const row = directory.candidates.find(item => item.provider === originalProvider && item.model === originalModel)
    if (!row || !row.selectable) return fail()
    const selected: { provider: string; model: string; tier?: EmployeeInstanceModelTier } = { provider: row.provider, model: row.model }
    if (own(current.config.fields, 'tier')) selected.tier = current.config.fields.tier
    return Object.freeze({ config: Object.freeze({ kind: 'present', fields: Object.freeze(selected) }),
      selection_source: Object.freeze({ request_id: directory.request_id, model_catalogue_revision: directory.model_catalogue_revision }) })
  })
}
export function employeeInstanceModelTier(value: EmployeeInstanceModelEdit, tier: EmployeeInstanceModelTier | null): EmployeeInstanceModelEdit {
  return guarded(() => {
    const current = edit(value)
    if (current.config.kind !== 'present' || current.selection_source === null) return fail()
    const next: { provider?: string; model?: string; tier?: EmployeeInstanceModelTier } = { ...current.config.fields }
    if (tier === null) delete next.tier
    else next.tier = enumText(tier, TIERS)
    return Object.freeze({ config: Object.freeze({ kind: 'present', fields: Object.freeze(next) }), selection_source: current.selection_source })
  })
}
function selectedValue(current: EmployeeInstanceModelEdit, directory: EmployeeInstanceModelSnapshot): EmployeeInstanceModelFields {
  const source = current.selection_source
  if (!source || source.request_id !== directory.request_id || source.model_catalogue_revision !== directory.model_catalogue_revision) return fail('employee_instance_model_selection_stale')
  const { provider, model } = current.config.fields
  if (!provider || !model) return fail()
  const row = directory.candidates.find(item => item.provider === provider && item.model === model)
  if (!row || !row.selectable) return fail('employee_instance_model_selection_stale')
  return current.config.fields
}
export function employeeInstanceModelConfigValue(value: EmployeeInstanceModelEdit, catalogue: EmployeeInstanceModelSnapshot): EmployeeInstanceModelFields {
  return guarded(() => selectedValue(edit(value), snapshot(catalogue)))
}
/** Untouched legacy state contributes no patch; changed models are whole-object replacements. */
export function employeeInstanceModelConfigChanges(value: EmployeeInstanceModelEdit, baseline: EmployeeInstanceModelConfigState,
  catalogue: EmployeeInstanceModelSnapshot | null): Readonly<Record<string, unknown>> {
  return guarded(() => {
    const current = edit(value)
    if (sameEmployeeInstanceModelConfig(current.config, state(baseline))) return EMPTY
    if (catalogue === null) return fail('employee_instance_model_selection_stale')
    return Object.freeze({ model_config: selectedValue(current, snapshot(catalogue)) })
  })
}
export function employeeInstanceModelConfigEncodedBytes(value: EmployeeInstanceModelEdit): number {
  return guarded(() => {
    const current = edit(value)
    return current.config.kind === 'present'
      ? new TextEncoder().encode(JSON.stringify({ model_config: current.config.fields })).byteLength : 0
  })
}
export function employeeInstanceModelConfigProblems(value: EmployeeInstanceModelEdit,
  catalogue: EmployeeInstanceModelSnapshot | null): readonly EmployeeInstanceModelProblem[] {
  return guarded(() => {
    const current = edit(value)
    let message: string
    if (current.config.kind === 'invalid') message = '原模型配置无法安全显示；请明确选择或恢复打开时的配置。'
    else if (current.config.kind === 'missing') message = '尚未设置本项目模型；不会自动选择默认模型。'
    else if (!current.config.fields.provider || !current.config.fields.model) message = '原配置尚未明确模型；空对象或仅档位可以保留，但不能作为新模型选择。'
    else if (!current.selection_source) message = '当前是原保存配置；新模型选择须手动读取候选并明确选择。'
    else if (catalogue === null) message = MESSAGES.employee_instance_model_selection_stale
    else {
      const directory = snapshot(catalogue)
      if (current.selection_source.request_id === directory.request_id
        && current.selection_source.model_catalogue_revision === directory.model_catalogue_revision
        && directory.candidates.some(row => row.selectable && row.provider === current.config.fields.provider && row.model === current.config.fields.model)) return Object.freeze([])
      message = MESSAGES.employee_instance_model_selection_stale
    }
    return Object.freeze([Object.freeze({ path: 'model_config' as const,
      severity: current.config.kind === 'invalid' || current.selection_source !== null ? 'error' as const : 'warning' as const, message })])
  })
}
