import { useEffect, useState, useSyncExternalStore } from 'react'
import { AIEmployeeShelf } from './AIEmployeeShelf'
import { AIEmployeeBindingSelection, bindingSelectionReadData, type BindingSelectionTarget } from './AIEmployeeBindingSelection'
import type { AIEmployeeInstanceRef, AIEmployeeInstanceView } from './AIEmployeeLibrary'
import type { WorkflowNode } from '../WorkflowsPage'
import { employeeAssetWriteBlocked, projectEmployeeInstance } from '../../lib/aiEmployeeAssetAdapter'
import { currentCanvasEmployee, type CanvasEmployeeDesign } from '../../lib/canvasEmployeeDesign'
import { employeeBindingFrom, type PortableEmployeeBinding } from '../../lib/workflowEmployeeBinding'

export interface AIEmployeeShelfHostProps {
  design: CanvasEmployeeDesign
  workflowId: string
  savedRevision: string
  draftOpen: boolean
  node: WorkflowNode | null
  editable: boolean
  onCandidate: (reference: AIEmployeeInstanceRef, name: string) => void
  onBinding: (nodeId: string, value: PortableEmployeeBinding | null) => void
}

/** All reads/writes use the App-owned controller, including uncertain receipts.
 * Unmounting the shelf neither loses pending writes nor retries them. */
export function AIEmployeeShelfHost(props: AIEmployeeShelfHostProps) {
  const { design } = props
  const { controller } = design
  const snapshot = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  const [query, setQuery] = useState('')
  const [error, setError] = useState('')
  const [choice, setChoice] = useState<{ instance_id: string | null; read_request_id: string | null }>({ instance_id: null, read_request_id: null })
  const current = !!design.project_id && design.project_id === design.workflows_read_project_id
    && snapshot.project_id === design.project_id
  const project = current ? snapshot.projects[design.project_id!] : undefined
  const blocked = snapshot.writes.some(item => employeeAssetWriteBlocked(item.receipt, false))
  const run = (operation: () => unknown) => {
    try { operation(); setError('') } catch { setError('操作未发出，请核对当前项目、连接和待确认请求。') }
  }
  useEffect(() => {
    if (!design.project_id || design.project_id !== design.workflows_read_project_id) return
    try {
      controller.selectProject(design.project_id)
      const now = controller.getSnapshot()
      const list = now.projects[design.project_id]?.instances
      if (now.connected && list?.state === 'not_loaded') controller.loadInstances()
    } catch { setError('项目实例尚未读取，请手动刷新。') }
  }, [controller, design.project_id, design.workflows_read_project_id, snapshot.connected])
  useEffect(() => { setChoice({ instance_id: null, read_request_id: null }) },
    [design.project_id, props.workflowId, props.savedRevision, props.node?.id])
  let instances: AIEmployeeInstanceView[] = []
  let listError = ''
  try {
    if (project?.instances.state === 'ready' && Array.isArray(project.instances.data?.instances)) {
      instances = project.instances.data.instances.map(raw => projectEmployeeInstance(raw, design.project_id!))
    }
  } catch { listError = '实例清单无法核对，请重新读取。' }
  if (project?.instances.state === 'error' || project?.instances.state === 'stale') listError = '实例清单已失效，请重新读取。'
  const target: BindingSelectionTarget | null = current && props.node?.employee_binding && !props.draftOpen
    && /^[a-f0-9]{64}$/.test(props.savedRevision) ? {
      project_id: design.project_id!, workflow_id: props.workflowId, node_id: props.node.id,
      expected_workflow_revision: props.savedRevision, binding_slot: 'executor',
    } : null
  const candidateView = project?.binding_candidates
  const raw = candidateView?.state === 'ready' ? candidateView.data : null
  const data = raw ? { project_id: raw.project_id, workflow_id: raw.workflow_id, node_id: raw.node_id,
    binding_slot: raw.binding_slot, workflow_revision: raw.workflow_revision, workflow_source: raw.workflow_source,
    workflow_version: raw.workflow_version, binding_revision: raw.binding_revision, candidates: raw.candidates,
    selected_instance_id: raw.selected_instance_id, allowed_actions: raw.allowed_actions } : null
  const selectedData = bindingSelectionReadData(current ? design.project_id : null, snapshot.connected && current,
    target, candidateView ? { ...candidateView, data } : undefined)
  const writes = snapshot.writes.filter(item => item.request.action === 'employee_binding_resolve_set')
  const original = writes.find(item => employeeAssetWriteBlocked(item.receipt, false))
    ?? writes.filter(item => !!target && item.request.project_id === target.project_id && item.request.workflow_id === target.workflow_id
      && item.request.node_id === target.node_id && item.request.workflow_revision === target.expected_workflow_revision).at(-1)
  return <div className="canvas-employee-host" data-testid="canvas-employee-host">
    <AIEmployeeShelf project_id={current ? design.project_id : null} instances={instances} query={query}
      selected_instance_id={choice.instance_id} connected={snapshot.connected && current} busy={blocked || !props.editable}
      result_state="idle" loading={project?.instances.state === 'loading' || project?.instances.state === 'not_loaded'}
      error={listError} owner_design_mode drop_chooser_connected={props.editable && current}
      onQueryChange={setQuery} onUseCandidate={reference => {
        const item = currentCanvasEmployee(design, reference)
        if (props.editable && item) props.onCandidate(reference, item.name || item.package_id)
      }} onInspect={reference => run(() => {
        if (!currentCanvasEmployee(design, reference)) throw new Error('stale')
        controller.loadInstance(reference.instance_id)
        design.onOpenLibrary()
      })} />
    <button type="button" disabled={!current || !snapshot.connected || project?.instances.state === 'loading'}
      onClick={() => run(() => controller.loadInstances())}>刷新项目员工</button>
    <button type="button" onClick={design.onOpenLibrary}>到组件库配置</button>
    {error && <p role="alert">{error}</p>}
    {props.node?.employee_binding && <CanvasEmployeeBindingEditor key={props.node.id + JSON.stringify(props.node.employee_binding)}
      node={props.node} editable={props.editable && !blocked} onBinding={props.onBinding} />}
    {props.node?.employee_binding && <>
      {selectedData && <p role="status">{selectedData.selected_instance_id
        ? `已读取的项目执行员工：${selectedData.candidates.find(item => item.instance_id === selectedData.selected_instance_id)?.display_name || selectedData.selected_instance_id}`
        : '此保存版本尚未指定项目执行员工。'}</p>}
      {props.draftOpen && <p role="status">先保存草稿，再确认这一步使用哪位项目员工。</p>}
      <AIEmployeeBindingSelection project_id={current ? design.project_id : null}
        connected={snapshot.connected && current} target={target}
        view={candidateView ? { ...candidateView, data } : undefined}
        selected_instance_id={choice.instance_id} selected_read_request_id={choice.read_request_id}
        writesBlocked={blocked || !props.editable} write={original ? {
          state: original.receipt.state, request_id: original.request.request_id,
          project_id: original.request.project_id, workflow_id: original.request.workflow_id,
          node_id: original.request.node_id, workflow_revision: original.request.workflow_revision,
          verify_available: snapshot.connected && current && ['unknown', 'partial'].includes(original.receipt.state),
        } : null}
        onLoad={value => run(() => {
          setChoice({ instance_id: null, read_request_id: null })
          controller.loadBindingCandidates(value.workflow_id, value.node_id, value.expected_workflow_revision)
        })} onSelect={(instance_id, read_request_id) => setChoice({ instance_id, read_request_id })}
        onSave={value => run(() => controller.saveBindingSelection(value))}
        onRecoverExplicit={requestId => run(() => controller.recoverWrite(requestId))} />
    </>}
  </div>
}

/** Deliberate local draft edits; human roles/gates stay on the enclosing node. */
export function CanvasEmployeeBindingEditor({ node, editable, onBinding }: {
  node: WorkflowNode; editable: boolean; onBinding: AIEmployeeShelfHostProps['onBinding']
}) {
  const value = node.employee_binding!
  const [editing, setEditing] = useState(false)
  const [remove, setRemove] = useState(false)
  const [input, setInput] = useState(JSON.stringify(value.input_mapping ?? {}, null, 2))
  const [output, setOutput] = useState(JSON.stringify(value.output_mapping ?? {}, null, 2))
  const [parameters, setParameters] = useState(JSON.stringify(value.param_overrides ?? {}, null, 2))
  const [note, setNote] = useState(value.binding_note ?? '')
  const [error, setError] = useState('')
  const apply = () => {
    if (!editable || input.length + output.length + parameters.length > 100_000) return
    try {
      const next = employeeBindingFrom({ ...value, input_mapping: JSON.parse(input), output_mapping: JSON.parse(output),
        param_overrides: JSON.parse(parameters), binding_note: note })
      if (!next) throw new Error('invalid')
      onBinding(node.id, next)
      setEditing(false)
    } catch { setError('映射需为字段与来源路径的 JSON 对象，参数需为 JSON 对象；修改仍保留。') }
  }
  return <section className="canvas-employee-binding" aria-label="本节点 AI 员工">
    <h4>{node.name || node.id} · AI 员工</h4>
    <p>{value.package_id} · {value.version_constraint.version ?? '项目采用版本'}</p>
    <p>真人负责人和审核规则沿用本节点。</p>
    <button type="button" disabled={!editable} onClick={() => { setEditing(!editing); setRemove(false) }}>编辑绑定</button>
    <button type="button" disabled={!editable} onClick={() => setRemove(true)}>移除 AI 员工</button>
    {remove && <div role="alert"><p>仅移除本步骤的 AI 员工绑定，保留步骤、连线和人审。保存草稿后生效。</p>
      <button type="button" disabled={!editable} onClick={() => onBinding(node.id, null)}>确认移除</button>
      <button type="button" onClick={() => setRemove(false)}>取消</button></div>}
    {editing && <fieldset disabled={!editable}>
      <label>输入映射<textarea value={input} maxLength={40000} onChange={event => setInput(event.target.value)} placeholder={'{"material": "order.inputs.materials"}'} /></label>
      <label>输出映射<textarea value={output} maxLength={40000} onChange={event => setOutput(event.target.value)} /></label>
      <label>本步骤参数<textarea value={parameters} maxLength={40000} onChange={event => setParameters(event.target.value)} /></label>
      <label>用途说明<input value={note} maxLength={4096} onChange={event => setNote(event.target.value)} /></label>
      <button type="button" onClick={apply}>应用到草稿</button>
      {error && <p role="alert">{error}</p>}
    </fieldset>}
  </section>
}
