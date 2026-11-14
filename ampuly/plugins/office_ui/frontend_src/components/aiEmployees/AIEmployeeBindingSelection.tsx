/** Controlled presentation and bounded public snapshots. No socket, timers, retries or write ledger. */
export interface BindingSelectionTarget {
  readonly project_id: string
  readonly workflow_id: string
  readonly node_id: string
  readonly expected_workflow_revision: string
  readonly binding_slot: 'executor'
}
export interface BindingSelectionCandidate {
  readonly instance_id: string
  readonly display_name: string
  readonly package_id: string
  readonly package_version: string
  readonly package_fingerprint: string
  readonly instance_revision: number
  readonly revision_fingerprint: string
  readonly intake_state: 'open' | 'paused'
  readonly execution_state: 'active' | 'paused' | 'disabled'
  readonly ready: boolean
  readonly gaps: readonly string[]
}
/** The host normalizes the correlated ACK into this public data projection. */
export interface BindingSelectionData {
  readonly project_id: string
  readonly workflow_id: string
  readonly node_id: string
  readonly binding_slot: 'executor'
  readonly workflow_revision: string
  readonly workflow_source: 'distribution' | 'runtime'
  readonly workflow_version: string | null
  readonly binding_revision: number
  readonly candidates: readonly BindingSelectionCandidate[]
  readonly selected_instance_id: string | null
  readonly allowed_actions: readonly string[]
}
/** Read token is internal correlation only; the host must omit it from the wire. */
export interface BindingSelectionSave {
  readonly read_request_id: string
  readonly project_id: string
  readonly workflow_id: string
  readonly node_id: string
  readonly binding_slot: 'executor'
  readonly workflow_revision: string
  readonly workflow_source: 'distribution' | 'runtime'
  readonly workflow_version: string | null
  readonly binding_revision: number
  readonly instance_id: string
  readonly instance_revision: number
  readonly revision_fingerprint: string
  readonly package_id: string
  readonly package_version: string
  readonly package_fingerprint: string
}
export interface BindingSelectionReadView {
  readonly state: 'not_loaded' | 'loading' | 'ready' | 'error' | 'stale'
  readonly request: {
    readonly action: string
    readonly project_id: string
    readonly request_id: string
    readonly workflow_id: string | null
    readonly node_id: string | null
    readonly workflow_revision: string | null
    readonly binding_slot: string | null
  } | null
  readonly data: unknown
  readonly receipt?: { readonly message?: string } | null
}
export interface BindingSelectionWriteView {
  readonly state: 'idle' | 'pending' | 'received' | 'unknown' | 'partial' | 'succeeded' | 'failed'
  readonly request_id: string | null
  readonly project_id: string | null
  readonly workflow_id: string | null
  readonly node_id: string | null
  readonly workflow_revision: string | null
  readonly verify_available: boolean
}

/** Only local fixed copy can be displayed; a server/private message is never echoed. */
function boundedReadNotice(view: BindingSelectionReadView | undefined): string | null {
  const receipt = view && Object.getOwnPropertyDescriptor(view, 'receipt')?.value
  if (!receipt || typeof receipt !== 'object') return null
  const message = Object.getOwnPropertyDescriptor(receipt, 'message')?.value
  return message === '候选员工资料超过本次读取范围，未返回不完整清单。请联系管理员处理读取范围。'
    || message === '候选员工清单超过本次显示范围，未截短为部分结果。请联系管理员处理范围。'
    ? message : null
}
export interface AIEmployeeBindingSelectionProps {
  readonly project_id: string | null
  readonly connected: boolean
  readonly target: BindingSelectionTarget | null
  readonly view: BindingSelectionReadView | undefined
  readonly selected_instance_id: string | null
  readonly selected_read_request_id: string | null
  /** All pending/unknown/partial employee writes from the App-owned ledger. */
  readonly writesBlocked: boolean
  readonly write: BindingSelectionWriteView | null
  readonly onLoad: (target: BindingSelectionTarget) => void
  readonly onSelect: (instanceId: string | null, readRequestId: string | null) => void
  readonly onSave: (selection: BindingSelectionSave) => void
  /** Explicit check-only of the retained request; never writes or repairs an unfinished save. */
  readonly onRecoverExplicit?: (requestId: string) => void
}

type Row = Record<string, unknown>
const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/
const INSTANCE = /^ei-[a-f0-9]{16}$/
const PACKAGE = /^[a-z0-9][a-z0-9_-]{2,63}$/
const SHA = /^[a-f0-9]{64}$/
const GAP = /^[a-z][a-z0-9_]{0,127}$/
const TARGET_FIELDS = ['project_id', 'workflow_id', 'node_id', 'expected_workflow_revision', 'binding_slot'] as const
const DATA_FIELDS = ['project_id', 'workflow_id', 'node_id', 'binding_slot', 'workflow_revision', 'workflow_source',
  'workflow_version', 'binding_revision', 'candidates', 'selected_instance_id', 'allowed_actions'] as const
const CANDIDATE_FIELDS = ['instance_id', 'display_name', 'package_id', 'package_version', 'package_fingerprint',
  'instance_revision', 'revision_fingerprint', 'intake_state', 'execution_state', 'ready', 'gaps'] as const
const protectedWrite = (state: BindingSelectionWriteView['state'] | undefined): boolean =>
  state === 'pending' || state === 'received' || state === 'unknown' || state === 'partial'
const valid = (value: unknown, pattern: RegExp): value is string => typeof value === 'string' && pattern.exec(value)?.[0] === value
const text = (value: unknown, maximum: number): value is string => typeof value === 'string'
  && value.length > 0 && value.length <= maximum && value.trim() === value && !/[\u0000-\u001f\u007f]/.test(value)
const displayName = (value: unknown): value is string => typeof value === 'string'
  && value.length <= 256 && !/[\u0000-\u001f\u007f]/.test(value)
const revision = (value: unknown, positive = false): value is number => typeof value === 'number'
  && Number.isSafeInteger(value) && value >= (positive ? 1 : 0)

function fields(value: unknown, names: readonly string[]): Row | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null
  const prototype = Object.getPrototypeOf(value)
  if (prototype !== Object.prototype && prototype !== null) return null
  const keys = Reflect.ownKeys(value)
  if (keys.length !== names.length || !names.every(name => keys.includes(name))) return null
  const result: Row = Object.create(null)
  for (const name of names) {
    const descriptor = Object.getOwnPropertyDescriptor(value, name)
    if (!descriptor?.enumerable || !('value' in descriptor)) return null
    result[name] = descriptor.value
  }
  return result
}
function items(value: unknown, maximum: number): readonly unknown[] | null {
  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) return null
  const descriptor = Object.getOwnPropertyDescriptor(value, 'length')
  const length = descriptor && 'value' in descriptor ? descriptor.value : null
  if (!revision(length) || length > maximum || Reflect.ownKeys(value).length !== length + 1) return null
  const result: unknown[] = []
  for (let index = 0; index < length; index++) {
    const item = Object.getOwnPropertyDescriptor(value, String(index))
    if (!item?.enumerable || !('value' in item)) return null
    result.push(item.value)
  }
  return result
}
function targetSnapshot(projectId: string | null, target: BindingSelectionTarget | null): BindingSelectionTarget | null {
  try {
    const data = fields(target, TARGET_FIELDS)
    if (!data || !valid(projectId, ID) || data.project_id !== projectId || !valid(data.workflow_id, ID)
      || !valid(data.node_id, ID) || !valid(data.expected_workflow_revision, SHA) || data.binding_slot !== 'executor') return null
    return Object.freeze({ project_id: projectId, workflow_id: data.workflow_id, node_id: data.node_id,
      expected_workflow_revision: data.expected_workflow_revision, binding_slot: 'executor' })
  } catch { return null }
}
export function bindingSelectionReadMatches(target: BindingSelectionTarget | null, view: BindingSelectionReadView | undefined): boolean {
  const request = view?.request
  return !!target && !!request && request.action === 'employee_binding_candidates' && valid(request.request_id, ID)
    && request.project_id === target.project_id && request.workflow_id === target.workflow_id
    && request.node_id === target.node_id && request.workflow_revision === target.expected_workflow_revision
    && request.binding_slot === target.binding_slot
}

/** Any scope/connection/malformed-view mismatch hides the complete old candidate projection. */
export function bindingSelectionReadData(projectId: string | null, connected: boolean, target: BindingSelectionTarget | null,
  view: BindingSelectionReadView | undefined): BindingSelectionData | null {
  try {
    const current = targetSnapshot(projectId, target)
    if (connected !== true || !current || view?.state !== 'ready' || !bindingSelectionReadMatches(current, view)) return null
    const data = fields(view.data, DATA_FIELDS)
    if (!data || data.project_id !== current.project_id || data.workflow_id !== current.workflow_id
      || data.node_id !== current.node_id || data.binding_slot !== 'executor' || data.workflow_revision !== current.expected_workflow_revision
      || (data.workflow_source !== 'distribution' && data.workflow_source !== 'runtime')
      || (data.workflow_version !== null && !text(data.workflow_version, 128)) || !revision(data.binding_revision)
      || (data.selected_instance_id !== null && !valid(data.selected_instance_id, INSTANCE))) return null
    const rows = items(data.candidates, 500), actions = items(data.allowed_actions, 2)
    if (!rows || !actions || actions.some(action => action !== 'employee_binding_candidates' && action !== 'employee_binding_resolve_set')
      || new Set(actions).size !== actions.length) return null
    const candidates: BindingSelectionCandidate[] = [], seen = new Set<string>()
    for (const item of rows) {
      const candidate = fields(item, CANDIDATE_FIELDS)
      if (!candidate || !valid(candidate.instance_id, INSTANCE) || seen.has(candidate.instance_id)
        || !displayName(candidate.display_name) || !valid(candidate.package_id, PACKAGE) || !text(candidate.package_version, 128)
        || !valid(candidate.package_fingerprint, SHA) || !revision(candidate.instance_revision, true)
        || !valid(candidate.revision_fingerprint, SHA) || (candidate.intake_state !== 'open' && candidate.intake_state !== 'paused')
        || !['active', 'paused', 'disabled'].includes(typeof candidate.execution_state === 'string' ? candidate.execution_state : '')
        || typeof candidate.ready !== 'boolean') return null
      const gaps = items(candidate.gaps, 500)
      if (!gaps || gaps.some(gap => !valid(gap, GAP)) || new Set(gaps).size !== gaps.length) return null
      if (candidate.ready && (gaps.length !== 0 || candidate.intake_state !== 'open' || candidate.execution_state !== 'active')) return null
      seen.add(candidate.instance_id)
      candidates.push(Object.freeze({ instance_id: candidate.instance_id, display_name: candidate.display_name,
        package_id: candidate.package_id, package_version: candidate.package_version, package_fingerprint: candidate.package_fingerprint,
        instance_revision: candidate.instance_revision, revision_fingerprint: candidate.revision_fingerprint,
        intake_state: candidate.intake_state, execution_state: candidate.execution_state as BindingSelectionCandidate['execution_state'],
        ready: candidate.ready, gaps: Object.freeze(gaps as string[]) }))
    }
    if (data.selected_instance_id !== null && !seen.has(data.selected_instance_id as string)) return null
    return Object.freeze({ project_id: current.project_id, workflow_id: current.workflow_id, node_id: current.node_id,
      binding_slot: 'executor', workflow_revision: current.expected_workflow_revision, workflow_source: data.workflow_source,
      workflow_version: data.workflow_version as string | null, binding_revision: data.binding_revision,
      candidates: Object.freeze(candidates), selected_instance_id: data.selected_instance_id as string | null,
      allowed_actions: Object.freeze(actions as string[]) })
  } catch { return null }
}

type SaveGuard = Pick<AIEmployeeBindingSelectionProps, 'project_id' | 'connected' | 'target' | 'view'
  | 'selected_instance_id' | 'selected_read_request_id' | 'writesBlocked' | 'write'>
/** Re-reading cannot silently upgrade an old selection to a newer revision. */
export function bindingSelectionSave(input: SaveGuard): BindingSelectionSave | null {
  const data = bindingSelectionReadData(input.project_id, input.connected, input.target, input.view)
  const requestId = input.view?.request?.request_id
  if (!data || data.binding_revision === Number.MAX_SAFE_INTEGER || input.writesBlocked !== false || protectedWrite(input.write?.state) || !requestId
    || input.selected_read_request_id !== requestId || !data.allowed_actions.includes('employee_binding_resolve_set')) return null
  const selected = data.candidates.find(candidate => candidate.instance_id === input.selected_instance_id)
  if (!selected) return null
  return Object.freeze({ read_request_id: requestId, project_id: data.project_id, workflow_id: data.workflow_id,
    node_id: data.node_id, binding_slot: 'executor', workflow_revision: data.workflow_revision,
    workflow_source: data.workflow_source, workflow_version: data.workflow_version, binding_revision: data.binding_revision,
    instance_id: selected.instance_id, instance_revision: selected.instance_revision, revision_fingerprint: selected.revision_fingerprint,
    package_id: selected.package_id, package_version: selected.package_version, package_fingerprint: selected.package_fingerprint })
}

export function AIEmployeeBindingSelection(props: AIEmployeeBindingSelectionProps) {
  const current = targetSnapshot(props.project_id, props.target)
  const data = bindingSelectionReadData(props.project_id, props.connected, current, props.view)
  const matches = bindingSelectionReadMatches(current, props.view)
  const selected = data?.candidates.find(candidate => candidate.instance_id === props.selected_instance_id)
  const selectionCurrent = !!selected && props.selected_read_request_id === props.view?.request?.request_id
  const saving = props.write?.state === 'pending' || props.write?.state === 'received'
  const uncertain = props.write?.state === 'unknown' || props.write?.state === 'partial'
  const receiptCurrent = props.connected && !!current && props.write?.project_id === current.project_id
    && props.write?.workflow_id === current.workflow_id && props.write?.node_id === current.node_id
    && props.write?.workflow_revision === current.expected_workflow_revision
  const protectedSelection = props.writesBlocked || protectedWrite(props.write?.state)
  const canLoad = props.connected && !!current && !(matches && props.view?.state === 'loading')
  const save = bindingSelectionSave(props)
  const verifyId = props.write?.request_id
  const canVerify = props.connected && uncertain && props.write?.verify_available === true && valid(verifyId, ID) && !!props.onRecoverExplicit
  return <section className="ai-employee-editor__section" aria-label="选择节点员工" data-testid="employee-binding-selection">
    <h4>选择员工</h4>
    <p>为已保存节点选择项目员工；保存的是项目映射。</p>
    <button type="button" data-action="employee_binding_candidates" disabled={!canLoad}
      onClick={() => { if (canLoad && current) props.onLoad(current) }}>读取可选员工</button>
    {!current && <p role="status">先选择本项目的已保存节点。</p>}
    {!props.connected && <p role="status">连接已断开，请连接后重新读取。</p>}
    {props.connected && current && matches && props.view?.state === 'loading' && <p role="status">正在读取可选员工。</p>}
    {props.connected && current && matches && (props.view?.state === 'error' || props.view?.state === 'stale')
      && <p role="status">{boundedReadNotice(props.view) ?? '可选员工尚未核实，请重新读取保存版本。'}</p>}
    {props.connected && current && matches && props.view?.state === 'ready' && !data
      && <p role="status">候选字段尚未核实，请重新读取。</p>}
    {data && <>
      <p>映射修订 {data.binding_revision} · {data.candidates.length} 位可选员工</p>
      {data.candidates.length === 0 ? <p role="status">本次读取没有合法候选。</p> : <label className="ai-employee-editor__field">
        项目员工<select aria-label="节点员工选择" value={selectionCurrent ? props.selected_instance_id! : ''} disabled={protectedSelection}
          onChange={event => {
            if (!data || protectedSelection) return
            const id = event.target.value
            if (!id) props.onSelect(null, props.view?.request?.request_id ?? null)
            else if (data.candidates.some(candidate => candidate.instance_id === id)) props.onSelect(id, props.view?.request?.request_id ?? null)
          }}>
          <option value="">选择员工</option>{data.candidates.map(candidate => <option key={candidate.instance_id} value={candidate.instance_id}>
            {candidate.display_name.trim() ? `${candidate.display_name} · ` : ''}{candidate.instance_id} · {candidate.package_id}@{candidate.package_version} · 修订 {candidate.instance_revision}
          </option>)}
        </select>
      </label>}
      {selectionCurrent && selected && <p>{selected.ready ? '服务端报告实例就绪。' : '服务端报告实例仍有待核对项。'} 保存选择不代表运行准入。</p>}
      {props.selected_instance_id && !selectionCurrent && <p role="status">原选择不属于本次读取，请重新选择员工。</p>}
    </>}
    <button type="button" data-action="employee_binding_resolve_set" disabled={!save}
      onClick={() => { const frozen = bindingSelectionSave(props); if (frozen) props.onSave(frozen) }}>保存选择</button>
    {saving && <p role="status">正在保存选择，等待原请求回执。</p>}
    {uncertain && <div role="status" data-testid="employee-binding-write-unknown">
      <p>保存结果待核实</p>
      <p>原请求保留在宿主台账中。</p>
      <p>只核实原请求，不写入、不修复未完成保存；请求未到达或尚未完成时仍保留未知。</p>
      <button type="button" disabled={!canVerify} onClick={() => {
        if (canVerify && verifyId) props.onRecoverExplicit?.(verifyId)
      }}>核实原请求</button>
    </div>}
    {!uncertain && props.writesBlocked && <p role="status">已有员工保存尚待核实。</p>}
    {receiptCurrent && props.write?.state === 'succeeded' && <p role="status">服务端已确认选择保存；请读取保存映射核对。</p>}
    {receiptCurrent && props.write?.state === 'failed' && <p role="status">本次选择未保存，请核对保存版本。</p>}
  </section>
}
