import { useCallback, useEffect, useState, useSyncExternalStore } from 'react'
import { AIEmployeeLibrary } from './AIEmployeeLibrary'
import { AIEmployeeProjectPanel } from './AIEmployeeProjectPanel'
import { ScopedStringList } from './ScopedStringList'
import { EmployeeArtifactContractEditor } from './EmployeeArtifactContractEditor'
import { EmployeeArtifactContractError } from '../../lib/aiEmployeeArtifactContract'
import { EmployeeHumanSlotsEditor } from './EmployeeHumanSlotsEditor'
import { EmployeeHumanSlotsError } from '../../lib/aiEmployeeHumanSlots'
import { EmployeeLoopPolicyEditor } from './EmployeeLoopPolicyEditor'
import { EmployeeLifecyclePanel } from './EmployeeLifecyclePanel'
import { EmployeeInstanceAdoptionPanel } from './EmployeeInstanceAdoptionPanel'
import { EmployeeLoopPolicyError } from '../../lib/aiEmployeeLoopPolicy'
import { EmployeeIOSchemaEditor } from './EmployeeIOSchemaEditor'
import { EmployeeIOSchemaError } from '../../lib/aiEmployeeIOSchema'
import { EmployeeInstanceLimitsEditor } from './EmployeeInstanceLimitsEditor'
import { EmployeeInstanceLimitsError, employeeInstanceLimitsValue, sameEmployeeInstanceLimits } from '../../lib/aiEmployeeInstanceLimits'
import { EmployeeInstanceModelEditor } from './EmployeeInstanceModelEditor'
import { EmployeeInstanceKnowledgeEditor } from './EmployeeInstanceKnowledgeEditor'
import { EmployeeInstanceModelConfigError, employeeInstanceModelConfigValue, sameEmployeeInstanceModelConfig } from '../../lib/aiEmployeeInstanceModelConfig'
import { AIEmployeeStoredBindingPreview } from './AIEmployeeStoredBindingPreview'
import { AIEmployeeAdoptionPreview } from './AIEmployeeAdoptionPreview'
import { AIEmployeeBindingSelection } from './AIEmployeeBindingSelection'
import type { WorkflowSummary } from '../WorkflowsPage'
import { EmployeeAssetAdapterError, employeeAssetWriteBlocked, projectEmployeeInstance, projectEmployeePackage, projectEmployeeProjectRow } from '../../lib/aiEmployeeAssetAdapter'
import { EmployeeAssetControllerError } from '../../lib/aiEmployeeAssetController'
import type { EmployeeAssetController, EmployeeAssetReadView } from '../../lib/aiEmployeeAssetController'
import type { PackageSource } from '../../lib/aiEmployeeAssetAdapter'
import type { AIEmployeePackageRef } from './AIEmployeeLibrary'
import { EmployeeScopePatchError, employeeDraftChanges } from '../../lib/aiEmployeeScopePatch'

export interface AIEmployeeAssetHostProps {
  controller: EmployeeAssetController; project_id: string | null
  workflows?: WorkflowSummary[] | null; workflow_error?: string | null
  workflows_read_project_id?: string | null; open_builder_workflow_id?: string | null
}
type Row = Record<string, unknown>
const row = (value: unknown): value is Row => value !== null && typeof value === 'object' && !Array.isArray(value)
const text = (value: unknown): string => typeof value === 'string' ? value : ''
const scopeErrorWords = (error: EmployeeScopePatchError): string => error.code === 'employee_edit_too_large' ? '文字太多，请缩短后再保存。'
  : error.code === 'employee_field_removal_unsupported' ? '不能取消已保存的这项设置。可以修改内容，或恢复打开时的内容。'
    : '请先修正标出的内容，再保存。'
const readWords = (view: EmployeeAssetReadView | undefined): string => view?.state === 'loading' ? '正在读取员工信息。'
  : view?.state === 'stale' ? '读取已过期；保留旧快照，先重新核对。'
    : view?.state === 'error' ? '读取失败；没有把旧快照改成空列表。' : '员工信息尚未读取。'

/** Leaf-only host. App owns the controller, socket callbacks and connection lifecycle.
 * Mount/project/reconnect triggers at most two initial reads, never interval polling.
  * Only supported design fields, IO schemas, delivery, human duties and loop limits are editable; full B editor has
 * no per-field disabling contract, so it is intentionally not used as a manifest form.
 */
export function AIEmployeeAssetHost({ controller, project_id, workflows = null, workflow_error = null,
  workflows_read_project_id = null, open_builder_workflow_id = null }: AIEmployeeAssetHostProps) {
  const snapshot = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  const [source, setSource] = useState<PackageSource>('published')
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<AIEmployeePackageRef | null>(null)
  const [selectedInstance, setSelectedInstance] = useState<string | null>(null)
  const [bindingChoice, setBindingChoice] = useState<{ instance_id: string | null; read_request_id: string | null }>({ instance_id: null, read_request_id: null })
  const [message, setMessage] = useState('')
  const [templateId, setTemplateId] = useState('')
  const [templateBudget, setTemplateBudget] = useState('')
  const [openAfterRead, setOpenAfterRead] = useState<string | null>(null)
  const [openInstanceAfterRead, setOpenInstanceAfterRead] = useState<string | null>(null)
  const [draftStep, setDraftStep] = useState<'identity' | 'rules' | 'capabilities' | 'release'>('identity')
  const scopeCurrent = project_id !== null && snapshot.project_id === project_id && Object.hasOwn(snapshot.projects, project_id)
  const project = scopeCurrent && project_id !== null ? snapshot.projects[project_id] : undefined
  const newDraft = project?.creation ?? { package_id: '', name: '', responsibility: '', version: '' }
  const view = source === 'draft' ? project?.drafts : project?.published
  const blocked = snapshot.writes.some(item => employeeAssetWriteBlocked(item.receipt, false)) || controller.lifecycle.hasBlockingWrite() || controller.instanceAdoption.hasBlockingWrite() || controller.knowledge.hasBlockingWrite()
  const adoptionCandidates = controller.instanceAdoptionCandidates()
  const lifecycleCandidate = controller.lifecycleCandidate()
  const instanceLifecycleCandidate = controller.instanceLifecycleCandidate()
  const clearBinding = useCallback(() => {
    controller.clearBindingPreview(); controller.clearBindingCandidates()
    setBindingChoice({ instance_id: null, read_request_id: null })
  }, [controller])
  const run = (operation: () => unknown): void => {
    try {
      // Read the controller again at the gesture boundary. An old render closure
      // cannot dispatch into its previous project while App is still settling scope.
      const current = controller.getSnapshot()
      if (!project_id || !current.connected || current.project_id !== project_id || !Object.hasOwn(current.projects, project_id)) {
        throw new EmployeeAssetAdapterError('request_invalid')
      }
      operation(); setMessage('')
    } catch (error) {
      setMessage(error instanceof EmployeeInstanceModelConfigError ? '请重新读取模型候选并明确选择，再保存；当前修改仍保留。'
        : error instanceof EmployeeInstanceLimitsError ? '请先修正本项目限额，再保存；当前修改仍保留。'
        : error instanceof EmployeeHumanSlotsError ? '请先修正人工职责约定，再保存；当前修改仍保留。'
        : error instanceof EmployeeArtifactContractError ? '请先修正交付物和验收标准，再保存；当前修改仍保留。'
        : error instanceof EmployeeLoopPolicyError ? '请先修正自主执行上限，再保存；当前修改仍保留。'
        : error instanceof EmployeeIOSchemaError ? '请先修正输入输出约定，再保存；当前修改仍保留。'
        : error instanceof EmployeeScopePatchError ? scopeErrorWords(error)
        : error instanceof EmployeeAssetAdapterError || error instanceof EmployeeAssetControllerError ? error.message : '员工操作尚不能核实；保留当前内容。')
    }
  }
  useEffect(() => {
    setSelected(null); setSelectedInstance(null); setOpenAfterRead(null); setOpenInstanceAfterRead(null); setSource('published')
    setTemplateId(''); setTemplateBudget('')
    try {
      controller.selectProject(project_id)
      if (project_id !== null && controller.getSnapshot().connected) {
        const current = controller.getSnapshot().projects[project_id]
        if (current.published.state !== 'ready' && current.published.state !== 'loading') controller.loadPackages()
        if (current.instances.state !== 'ready' && current.instances.state !== 'loading') controller.loadInstances()
      }
      setMessage('')
    } catch (error) {
      setMessage(error instanceof EmployeeAssetAdapterError || error instanceof EmployeeAssetControllerError ? error.message : '员工读取尚不能核实。')
    }
    // No cleanup of the App-owned controller here: unknown writes/editor survive tab unmount.
  }, [controller, project_id, snapshot.connected])
  useEffect(() => {
    if (!openAfterRead || project?.detail.request?.request_id !== openAfterRead || project.detail.state === 'loading') return
    if (project.detail.state === 'ready' && project.detail.request.source === 'draft') {
      try { controller.openEditor(); setMessage('') } catch (error) {
        setMessage(error instanceof EmployeeAssetAdapterError ? error.message : '保留当前编辑，先核对目标与修订。')
      }
    }
    setOpenAfterRead(null)
  }, [controller, openAfterRead, project?.detail])
  useEffect(() => {
    if (!openInstanceAfterRead || project?.detail.request?.request_id !== openInstanceAfterRead || project.detail.state === 'loading') return
    if (project.detail.state === 'ready' && project.detail.request.action === 'employee_instance_detail') {
      run(() => controller.openInstanceEditor())
    }
    setOpenInstanceAfterRead(null)
  }, [controller, openInstanceAfterRead, project?.detail])
  useEffect(() => { setDraftStep('identity') }, [project?.editor?.package_id])
  let packages: ReturnType<typeof projectEmployeePackage>[] = []
  let instances: ReturnType<typeof projectEmployeeInstance>[] = []
  let employees: ReturnType<typeof projectEmployeeProjectRow>[] | null = null
  let mappingError = ''
  try {
    if (view?.state === 'ready' && Array.isArray(view.data?.packages)) packages = view.data.packages.map(item => projectEmployeePackage(item, source))
    if (project?.instances.state === 'ready' && Array.isArray(project.instances.data?.instances) && project_id) {
      instances = project.instances.data.instances.map(item => projectEmployeeInstance(item, project_id))
      employees = project.instances.data.instances.map(item => projectEmployeeProjectRow(item, project_id))
    }
  } catch { mappingError = '员工视图字段尚不能核实；不使用示例或猜测值。' }
  const openPackage = (packageId: string, selectedVersion: string | undefined, edit = false): void => run(() => {
    // Draft has one current file: always read by its real ID, including missing version.
    const requestId = controller.loadPackage(packageId, { source, ...(source === 'published' && selectedVersion ? { version: selectedVersion } : {}) })
    if (edit) setOpenAfterRead(requestId)
  })
  const creationAllowed = snapshot.connected && scopeCurrent && !blocked && project?.drafts.state === 'ready'
    && Array.isArray(project.drafts.data?.allowed_actions) && project.drafts.data.allowed_actions.includes('employee_package_draft_create')
  const templates = project?.drafts.state === 'ready' && Array.isArray(project.drafts.data?.templates)
    ? project.drafts.data.templates.filter(row).filter(item => item.persisted === false
      && typeof item.template_id === 'string' && typeof item.source_revision === 'string') : []
  const selectedTemplate = templates.find(item => item.template_id === templateId)
  const editor = project?.editor
  let editorPatchError = ''
  if (editor?.dirty) {
    try { employeeDraftChanges(editor.fields, editor.baseline, editor.scopes, editor.scope_baseline, editor.artifact, editor.artifact_baseline, editor.human, editor.human_baseline, editor.loop, editor.loop_baseline, editor.ios, editor.io_baseline) }
    catch (error) { editorPatchError = error instanceof EmployeeHumanSlotsError ? '请先修正人工职责约定，再保存；当前修改仍保留。'
      : error instanceof EmployeeArtifactContractError ? '请先修正交付物和验收标准，再保存；当前修改仍保留。'
      : error instanceof EmployeeLoopPolicyError ? '请先修正自主执行上限，再保存；当前修改仍保留。'
      : error instanceof EmployeeIOSchemaError ? '请先修正输入输出约定，再保存；当前修改仍保留。'
      : error instanceof EmployeeScopePatchError ? scopeErrorWords(error) : '内容暂时无法确认，当前修改仍保留。' }
  }
  const detail = project?.detail
  const editableCurrent = snapshot.connected && scopeCurrent && !blocked && editor && detail?.state === 'ready'
    && detail.request?.source === 'draft' && detail.request.package_id === editor.package_id && detail.data?.revision === editor.base_revision
    && Array.isArray(detail.data.allowed_actions) && detail.data.allowed_actions.includes('employee_package_draft_save')
  const currentWrite = snapshot.writes.filter(item => item.request.project_id === project_id).at(-1)
  const detailPackage = detail?.state === 'ready' && detail.request?.action === 'employee_package_detail' && row(detail.data?.package) ? detail.data.package : null
  const detailInstance = detail?.state === 'ready' && detail.request?.action === 'employee_instance_detail' && detail.data ? detail.data : null
  const instanceEditor = project?.instance_editor
  const instanceNameDirty = !!instanceEditor && (instanceEditor.display_name !== instanceEditor.baseline_name || instanceEditor.note !== '')
  const instanceLimitsDirty = !!instanceEditor && !sameEmployeeInstanceLimits(instanceEditor.limits, instanceEditor.limits_baseline)
  const instanceModelDirty = !!instanceEditor && !sameEmployeeInstanceModelConfig(instanceEditor.model.config, instanceEditor.model_baseline)
  let instanceModelCandidates = null as ReturnType<typeof controller.instanceModelCandidates>
  let instanceModelPatchError = ''
  try {
    instanceModelCandidates = scopeCurrent ? controller.instanceModelCandidates() : null
    if (instanceEditor && instanceModelDirty) {
      if (!instanceModelCandidates) instanceModelPatchError = '当前模型候选尚不能核实；请重新读取并明确选择，原修改仍保留。'
      else employeeInstanceModelConfigValue(instanceEditor.model, instanceModelCandidates)
    }
  } catch { instanceModelPatchError = '模型选择来源已变化；请重新明确选择，原修改仍保留。' }
  let instanceLimitsPatchError = ''
  if (instanceEditor && instanceLimitsDirty) {
    try { employeeInstanceLimitsValue(instanceEditor.limits) }
    catch { instanceLimitsPatchError = '请先修正本项目限额，再保存；当前修改仍保留。' }
  }
  const instanceEditableCurrent = snapshot.connected && scopeCurrent && !blocked && instanceEditor && detailInstance
    && detail?.request?.instance_id === instanceEditor.instance_id && detailInstance.revision === instanceEditor.base_revision
    && detailInstance.fingerprint === instanceEditor.base_fingerprint && Array.isArray(detailInstance.allowed_actions)
    && detailInstance.allowed_actions.includes('employee_instance_revise')
  let detailEmployee: ReturnType<typeof projectEmployeeProjectRow> | null = null
  if (detailInstance && project_id) {
    try { detailEmployee = projectEmployeeProjectRow(detailInstance, project_id) }
    catch { mappingError = '实例详情字段尚不能核实；不生成可操作入口。' }
  }

  return <section className="ai-employee-library" aria-label="AI 员工岗位资产" data-testid="ai-employee-asset-host">
    <header className="ai-employee-library__header"><h2>AI 员工</h2>
      <p className="ai-employee-library__muted">岗位草稿先做限额测试，再逐条人工审核与发布；项目实例分别管理名称、说明、限额与模型。发布不等于启用员工执行。</p>
      <p className="ai-employee-library__muted">已保存流程节点可明确选择本项目员工；映射保存与运行准入分别核对。</p>
      <button type="button" disabled={!scopeCurrent || source === 'published'} onClick={() => { setSource('published'); setSelected(null) }}>发布目录</button>
      <button type="button" disabled={!snapshot.connected || !scopeCurrent || source === 'draft'} onClick={() => run(() => {
        setSource('draft'); setSelected(null)
        controller.loadPackages('draft')
      })}>所有者草稿</button>
      <button type="button" disabled={!snapshot.connected || !scopeCurrent || view?.state === 'loading'} onClick={() => run(() => controller.loadPackages(source))}>读取当前列表</button>
    </header>
    {(message || mappingError) && <p role="alert">{message || mappingError}</p>}
    {!project_id && <p role="status">先选择明确项目；不使用默认项目代替。</p>}
    {!snapshot.connected && <p role="status">连接已断开；保留编辑与原请求，不自动重发。</p>}
    {snapshot.writes.filter(item => ['unknown', 'partial', 'pending'].includes(item.receipt.state)).map(item => <section
      className="ai-employee-editor__status" key={item.request.request_id} role="status" data-write-request={item.request.request_id}>
      <p>原项目 {item.request.project_id} · 原岗位 {item.request.instance_id ?? item.request.package_id} · 请求 {item.request.request_id}</p>
      <p>{item.receipt.message || '原保存尚未核实。'}</p>
      <p>{item.request.action === 'employee_binding_resolve_set' || item.request.action === 'employee_instance_revise' && row(item.request.message.changes) && Object.hasOwn(item.request.message.changes, 'model_config') ? '只核实原选择请求；请求未到达或事务未完成时，不补写、不修复。'
        : '“核实并恢复原保存”会再次提交原来的保存，可能继续完成上次没结束的保存，不是只读查看。'}</p>
      <button type="button" disabled={!snapshot.connected || !scopeCurrent || item.receipt.state === 'pending'}
        onClick={() => run(() => controller.recoverWrite(item.request.request_id))}>{item.request.action === 'employee_binding_resolve_set' || item.request.action === 'employee_instance_revise' && row(item.request.message.changes) && Object.hasOwn(item.request.message.changes, 'model_config') ? '只读核实原请求' : '核实并恢复原保存'}</button>
    </section>)}
    {view?.state !== 'ready' ? <p role="status">{readWords(view)}</p> : mappingError ? null : packages.length === 0
      ? <p role="status">{source === 'published' ? '当前可见发布索引视图没有已发布岗位；这不表示没有草稿或任何资产。' : '当前所有者草稿视图没有岗位草稿。'}</p>
      : <AIEmployeeLibrary packages={packages} instances={instances} project={project_id ? { project_id, name: '当前项目', revision: null,
        loaded_revision: null, allowed_actions: [] } : null} selected_package={selected} selected_instance_id={selectedInstance}
        query={query} edit_scope={source === 'draft' ? 'standard_package' : null} connected={snapshot.connected && scopeCurrent} busy={blocked}
        result={currentWrite?.receipt ?? { state: 'idle' }} onQueryChange={setQuery} onSelectPackage={reference => {
          setSelected(reference); openPackage(reference.package_id, reference.package_version)
        }} onSelectInstance={reference => { setSelectedInstance(reference.instance_id); run(() => controller.loadInstance(reference.instance_id)) }}
        onView={reference => openPackage(reference.package_id, reference.package_version)}
        {...(source === 'draft' ? { onEdit: (request: { package: AIEmployeePackageRef }) => openPackage(request.package.package_id, undefined, true) } : {})} />}
    {source === 'draft' && view?.state === 'ready' && Array.isArray(view.data?.packages) && view.data.packages.filter(row).map(item => {
      const manifest = row(item.package) ? item.package : null
      return manifest && !text(manifest.version) ? <button type="button" key={text(manifest.package_id)} disabled={!snapshot.connected || blocked}
        onClick={() => openPackage(text(manifest.package_id), undefined, true)}>补全未设版本草稿：{text(manifest.name) || text(manifest.package_id)}</button> : null
    })}
    {source === 'draft' && <section className="ai-employee-editor" aria-label="新建岗位草稿">
      <h3>新建岗位草稿</h3><p className="ai-employee-editor__muted">先保存为草稿，其他岗位信息可以继续补充。这不代表已经通过测试或可以投入使用。</p>
      <fieldset className="ai-employee-editor__section" disabled={!creationAllowed}>
        <label className="ai-employee-editor__field">创建方式<select aria-label="岗位草稿模板" value={templateId} onChange={event => setTemplateId(event.target.value)}>
          <option value="">空白岗位</option>{templates.map(item => <option key={text(item.template_id)} value={text(item.template_id)}>{text(item.name)} · 内置模板</option>)}
        </select></label>
        {selectedTemplate && <details className="ai-employee-editor__section"><summary>查看模板能力与交付</summary>
          <p>{text(selectedTemplate.responsibility)}</p>
          <p>模板版本 {text(selectedTemplate.version)} · 尚未创建本地草稿</p>
          {(['scope_in', 'scope_out'] as const).map(key => <div key={key}><h4>{key === 'scope_in' ? '可以处理' : '职责边界'}</h4>
            <ul>{Array.isArray(selectedTemplate[key]) && selectedTemplate[key].filter((item): item is string => typeof item === 'string').map((item, index) => <li key={index}>{item}</li>)}</ul></div>)}
          <p>创建时一并保存岗位约定、话术原则、知识约定、技能与任务集。后续可在岗位编辑器调整。</p>
        </details>}
        {(['package_id', 'name', 'responsibility', 'version'] as const).filter(field => !templateId || field === 'package_id' || field === 'name').map(field => <label className="ai-employee-editor__field" key={field}>
          {{ package_id: '岗位包编号', name: '名称', responsibility: '职责', version: '版本' }[field]}
          <input aria-label={`新建${{ package_id: '岗位包编号', name: '名称', responsibility: '职责', version: '版本' }[field]}`} maxLength={field === 'responsibility' ? 4096 : 128} value={newDraft[field]}
            onChange={event => run(() => controller.updateCreation({ ...newDraft, [field]: event.target.value }))} />
        </label>)}
        {templateId && <label className="ai-employee-editor__field">金额上限（元）<input aria-label="模板岗位金额上限" inputMode="decimal" placeholder="请明确填写，0 表示不允许付费调用" value={templateBudget} onChange={event => setTemplateBudget(event.target.value)} /></label>}
        <button type="button" disabled={!newDraft.package_id || !newDraft.name || (templateId ? !selectedTemplate || !/^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?$/.test(templateBudget) : !newDraft.version)}
          onClick={() => run(() => templateId && selectedTemplate
            ? controller.createDraftFromTemplate(templateId, text(selectedTemplate.source_revision), { package_id: newDraft.package_id, name: newDraft.name, max_cost_cny: templateBudget })
            : controller.createDraft({ schema_version: 1, ...newDraft }))}>创建岗位草稿</button>
      </fieldset><button type="button" disabled={!snapshot.connected || !scopeCurrent || blocked}
        onClick={() => run(() => controller.clearCreation())}>清空新建内容</button>
      {!creationAllowed && <p className="ai-employee-editor__muted">请先打开本项目的所有者草稿列表。若有保存结果待确认，请先处理。</p>}
    </section>}
    {detailPackage && <section className="ai-employee-editor__section" aria-label="岗位包真实详情">
      <h3>{text(detailPackage.name) || text(detailPackage.package_id)} · {text(detailPackage.version) || '版本尚未设置'}</h3>
      <p>{text(detailPackage.responsibility) || '职责尚未设置'}</p><p>真实 revision：{text(detail?.data?.revision)} · 状态：{text(detail?.data?.status)}</p>
      {detail?.request?.source === 'draft' && <button type="button" disabled={!snapshot.connected || blocked || !!editor?.dirty}
        onClick={() => run(() => controller.openEditor())}>编辑岗位信息</button>}
    </section>}
    {detail && detail.state !== 'ready' && detail.state !== 'not_loaded' && <p role="status">详情：{readWords(detail)}</p>}
    {editor && <div className="ai-employee-draft" data-testid="employee-draft-desk">
      <div className="ai-employee-draft-head">
        <h3>编辑 {editor.fields.name || editor.package_id}</h3>
        <div className="ai-employee-lifecycle-steps" role="tablist" aria-label="编辑岗位">
          {([['identity', '身份'], ['rules', '职责与规矩'], ['capabilities', '能力'], ['release', '试跑与发布']] as const).map(([id, label]) => (
            <button key={id} type="button" role="tab" data-draft-tab={id} aria-selected={draftStep === id} onClick={() => setDraftStep(id)}>{label}</button>
          ))}
        </div>
      </div>
      <section className="ai-employee-editor" aria-label="岗位草稿编辑">
        <div className="ai-employee-draft-panel ai-employee-draft-panel--split" data-draft-step="identity" hidden={draftStep !== 'identity'}>
          <p className="ai-employee-editor__muted">只保存你改过的内容。岗位包编号 {editor.package_id}。</p>
          {(['name', 'version'] as const).map(field => <label className="ai-employee-editor__field" key={field}>
            {{ name: '名称', version: '版本' }[field]}
            <input aria-label={`编辑${{ name: '名称', version: '版本' }[field]}`} maxLength={512} value={editor.fields[field]}
              disabled={!snapshot.connected || blocked} onChange={event => run(() => controller.updateEditor({ ...editor.fields, [field]: event.target.value }))} />
          </label>)}
        </div>
        <div className="ai-employee-draft-panel" data-draft-step="rules" hidden={draftStep !== 'rules'}>
          <p className="ai-employee-editor__muted">这个岗位做什么、不做什么、谁来把关。</p>
          <label className="ai-employee-editor__field">职责
            <input aria-label="编辑职责" maxLength={32768} value={editor.fields.responsibility}
              disabled={!snapshot.connected || blocked} onChange={event => run(() => controller.updateEditor({ ...editor.fields, responsibility: event.target.value }))} />
          </label>
          {(['scope_in', 'scope_out'] as const).map(field => <ScopedStringList key={field} field={field} value={editor.scopes[field]}
            disabled={!snapshot.connected || !scopeCurrent || blocked} onChange={value => run(() => controller.updateScope(field, value))}
            onReset={() => run(() => controller.resetScope(field))} />)}
          <EmployeeHumanSlotsEditor value={editor.human} disabled={!snapshot.connected || !scopeCurrent || blocked}
            onChange={value => run(() => controller.updateHumanSlots(value))} onReset={() => run(() => controller.resetHumanSlots())} />
        </div>
        <div className="ai-employee-draft-panel" data-draft-step="capabilities" hidden={draftStep !== 'capabilities'}>
          <p className="ai-employee-editor__muted">输入输出、交付物和执行上限。</p>
          <EmployeeIOSchemaEditor value={editor.ios} disabled={!snapshot.connected || !scopeCurrent || blocked}
            onChange={(field, value) => run(() => controller.updateIOSchema(field, value))} onReset={field => run(() => controller.resetIOSchema(field))} />
          <EmployeeArtifactContractEditor value={editor.artifact} disabled={!snapshot.connected || !scopeCurrent || blocked}
            onChange={value => run(() => controller.updateArtifact(value))} onReset={() => run(() => controller.resetArtifact())} />
          <EmployeeLoopPolicyEditor value={editor.loop} disabled={!snapshot.connected || !scopeCurrent || blocked}
            onChange={value => run(() => controller.updateLoopPolicy(value))} onReset={() => run(() => controller.resetLoopPolicy())} />
        </div>
        {editorPatchError && <p role="alert">{editorPatchError}</p>}
        <div className="ai-employee-draft-actions" hidden={draftStep === 'release'}>
          {draftStep !== 'identity' && <button type="button" onClick={() => setDraftStep(draftStep === 'capabilities' ? 'rules' : 'identity')}>上一步</button>}
          <button type="button" className={editor.dirty ? 'ai-employee-editor__primary' : undefined} disabled={!editableCurrent || !editor.dirty || !editor.fields.version || !!editorPatchError} onClick={() => run(() => controller.saveEditor())}>保存修改</button>
          {draftStep !== 'capabilities' && <button type="button" className={editor.dirty ? undefined : 'ai-employee-editor__primary'} onClick={() => setDraftStep(draftStep === 'identity' ? 'rules' : 'capabilities')}>下一步</button>}
          {draftStep === 'capabilities' && <button type="button" className={editor.dirty ? undefined : 'ai-employee-editor__primary'} onClick={() => setDraftStep('release')}>去试跑</button>}
          <button type="button" disabled={!snapshot.connected || detail?.state === 'loading'} onClick={() => run(() => controller.loadPackage(editor.package_id, { source: 'draft' }))}>查看最新草稿</button>
          <button type="button" disabled={!snapshot.connected || blocked} onClick={() => run(() => controller.closeEditor(true))}>放弃修改并关闭</button>
        </div>
        {!editableCurrent && <p className="ai-employee-editor__muted">最新内容或上次保存结果还需要确认。查看草稿不会覆盖未保存的修改，也不能证明上次保存是否完成。</p>}
      </section>
      <div data-draft-step="release" hidden={draftStep !== 'release'}>
        <button type="button" onClick={() => setDraftStep('capabilities')}>返回能力</button>
        {!instanceEditor && <EmployeeLifecyclePanel controller={controller.lifecycle} project_id={project_id}
          candidate={source === 'draft' && scopeCurrent ? lifecycleCandidate : null}
          editable={!!lifecycleCandidate && controller.canUseLifecycleCandidate(lifecycleCandidate)} />}
      </div>
    </div>}
    {!editor && !instanceEditor && <EmployeeLifecyclePanel controller={controller.lifecycle} project_id={project_id}
      candidate={source === 'draft' && scopeCurrent ? lifecycleCandidate : null}
      editable={!!lifecycleCandidate && controller.canUseLifecycleCandidate(lifecycleCandidate)} />}
    {currentWrite && !['pending', 'unknown', 'partial'].includes(currentWrite.receipt.state) && <p role="status">{currentWrite.receipt.message} 请只读刷新相应列表/详情，编辑内容仍被保留。</p>}
    {currentWrite?.receipt.state === 'succeeded' && currentWrite.request.action === 'employee_package_draft_create' && currentWrite.request.package_id && <button type="button" disabled={!snapshot.connected || !scopeCurrent || blocked} onClick={() => run(() => {
      setSource('draft'); controller.loadPackages('draft')
      const requestId = controller.loadPackage(currentWrite.request.package_id!, { source: 'draft' }); setOpenAfterRead(requestId)
    })}>查看并编辑已创建岗位</button>}
    <AIEmployeeProjectPanel project_id={project_id} connected={snapshot.connected && scopeCurrent} employees={employees} configure_label="配置本项目员工"
      read_busy={project?.instances.state === 'loading'} load_error={project?.instances.state === 'error' || project?.instances.state === 'stale' ? readWords(project.instances) : null}
      onReload={() => run(() => controller.loadInstances())}
      onAction={intent => run(() => {
        if (intent.action !== 'configure') throw new EmployeeAssetAdapterError('unsupported_mapping')
        setOpenInstanceAfterRead(controller.loadInstance(intent.instance_id))
      })} />
    {employees?.map(employee => <button type="button" key={employee.instance_id} disabled={!snapshot.connected}
      onClick={() => run(() => controller.loadInstance(employee.instance_id))}>只读查看实例：{employee.instance_id}</button>)}
    {detailEmployee && project_id && <AIEmployeeProjectPanel project_id={project_id} connected={snapshot.connected}
      employees={[detailEmployee]} configure_label="配置本项目员工" />}
    {detailInstance && <button type="button" disabled={!snapshot.connected || !scopeCurrent || blocked || !!instanceEditor?.dirty
      || !Array.isArray(detailInstance.allowed_actions) || !detailInstance.allowed_actions.includes('employee_instance_revise')}
      onClick={() => run(() => controller.openInstanceEditor())}>配置本项目员工</button>}
    <AIEmployeeAdoptionPreview project_id={scopeCurrent ? project_id : null} connected={snapshot.connected && scopeCurrent}
      instance={detailInstance} published={project?.published} target={project?.adoption_target} view={project?.adoption}
      onTargetChange={target => run(() => target ? controller.loadAdoptionTarget(target.package_id, target.package_version, target.expected_fingerprint) : controller.clearAdoptionTarget())}
      onPreview={target => run(() => controller.previewAdoption(target.instance_id, target.package_id, target.package_version, target.expected_fingerprint, target.expected_revision))} />
    <EmployeeInstanceAdoptionPanel controller={controller.instanceAdoption} project_id={scopeCurrent ? project_id : null}
      create={adoptionCandidates.create} adopt={adoptionCandidates.adopt}
      editable={snapshot.connected && scopeCurrent && !blocked && !editor?.dirty && !instanceEditor?.dirty}
      onReadInstance={instanceId => run(() => controller.loadInstance(instanceId))} />
    {instanceEditor && <section className="ai-employee-editor" aria-label="项目实例配置">
      <h3>配置本项目员工</h3><p>实例 {instanceEditor.instance_id} · 原修订 {instanceEditor.base_revision}。包版本、能力、人员与在途运行不变。</p>
      <label className="ai-employee-editor__field">显示名称<input aria-label="实例显示名称" maxLength={256} value={instanceEditor.display_name}
        disabled={!snapshot.connected || !scopeCurrent || blocked} onChange={event => run(() => controller.updateInstanceEditor({ display_name: event.target.value, note: instanceEditor.note }))} /></label>
      <label className="ai-employee-editor__field">修订说明<textarea aria-label="实例修订说明" maxLength={4000} value={instanceEditor.note}
        disabled={!snapshot.connected || !scopeCurrent || blocked} onChange={event => run(() => controller.updateInstanceEditor({ display_name: instanceEditor.display_name, note: event.target.value }))} /></label>
      <button type="button" disabled={!instanceEditableCurrent || !instanceNameDirty || !instanceEditor.display_name.trim()}
        onClick={() => run(() => controller.saveInstanceEditor())}>保存实例名称</button>
      <EmployeeInstanceLimitsEditor value={instanceEditor.limits} disabled={!snapshot.connected || !scopeCurrent || blocked}
        onChange={value => run(() => controller.updateInstanceLimits(value))} onReset={() => run(() => controller.resetInstanceLimits())} />
      {instanceLimitsPatchError && <p role="alert">{instanceLimitsPatchError}</p>}
      <button type="button" disabled={!instanceEditableCurrent || !instanceLimitsDirty || !!instanceLimitsPatchError}
        onClick={() => run(() => controller.saveInstanceLimitsEditor())}>保存本项目限额</button>
      <EmployeeInstanceModelEditor value={instanceEditor.model} candidates={instanceModelCandidates}
        disabled={!instanceEditableCurrent || project?.model_candidates.state === 'loading'}
        onLoad={() => run(() => controller.loadInstanceModelCandidates())}
        onChange={value => run(() => controller.updateInstanceModelConfig(value))}
        onReset={() => run(() => controller.resetInstanceModelConfig())} />
      {project?.model_candidates.state === 'loading' && <p role="status">正在读取本项目模型候选。</p>}
      {project?.model_candidates.state === 'error' && <p role="status">模型候选尚不能核实；不使用示例目录或自动默认值。</p>}
      {instanceModelPatchError && <p role="alert">{instanceModelPatchError}</p>}
      <button type="button" disabled={!instanceEditableCurrent || !instanceModelDirty || !instanceModelCandidates || !!instanceModelPatchError}
        onClick={() => run(() => controller.saveInstanceModelEditor())}>保存本项目模型</button>
      <EmployeeInstanceKnowledgeEditor controller={controller.knowledge} disabled={!instanceEditableCurrent} onError={setMessage} />
      <section className="ai-employee-editor__section" aria-label="当前实例重新测试">
        <h4>用已保存的配置重新测试</h4>
        <p>模型、参数或知识变更后，重新验证这名员工。结果只适用于本项目的当前实例，不会发布或替换岗位包。</p>
        <button type="button" disabled={!instanceEditableCurrent || instanceEditor.dirty || !!editor?.dirty || project?.adoption_target.state === 'loading'}
          onClick={() => run(() => controller.loadInstanceTestPackage())}>读取当前实例的固定岗位版本（不计费）</button>
        <EmployeeLifecyclePanel controller={controller.lifecycle} project_id={project_id} mode="instance"
          candidate={scopeCurrent ? instanceLifecycleCandidate : null}
          editable={!!instanceLifecycleCandidate && controller.canUseLifecycleCandidate(instanceLifecycleCandidate)} />
      </section>
      <p className="ai-employee-editor__muted">名称与限额分别保存，一次只提交所选内容。一次保存后，另一项未保存修改仍保留；请先核对最新修订，不会自动覆盖或换用新修订。</p>
      <button type="button" disabled={!snapshot.connected || !scopeCurrent || detail?.state === 'loading'}
        onClick={() => run(() => controller.loadInstance(instanceEditor.instance_id))}>查看最新实例</button>
      <button type="button" disabled={!snapshot.connected || !scopeCurrent || blocked}
        onClick={() => run(() => controller.closeInstanceEditor(true))}>放弃未保存修改并关闭</button>
      {!instanceEditableCurrent && <p>原修订或上次保存结果仍需核对。新读取不会覆盖这份修改，也不会自动改用新修订。</p>}
      <p>此保存不证明实例已就绪；工具授权、发布依据和实际运行仍需分别核验。</p>
    </section>}
    <AIEmployeeStoredBindingPreview project_id={scopeCurrent ? project_id : null} connected={snapshot.connected && scopeCurrent}
      workflows={workflows} workflow_error={workflow_error} workflows_read_project_id={workflows_read_project_id}
      open_builder_workflow_id={open_builder_workflow_id} view={project?.binding} onSelectionChange={clearBinding}
      onPreview={target => run(() => controller.previewBinding(target.workflow_id, target.node_id, target.expected_workflow_revision, target.binding_slot))}
      selectionSlot={target => {
        const candidateView = project?.binding_candidates
        const raw = candidateView?.state === 'ready' ? candidateView.data : null
        // Exact public projection only after the adapter validated the original ACK.
        const data = raw ? { project_id: raw.project_id, workflow_id: raw.workflow_id, node_id: raw.node_id,
          binding_slot: raw.binding_slot, workflow_revision: raw.workflow_revision, workflow_source: raw.workflow_source,
          workflow_version: raw.workflow_version, binding_revision: raw.binding_revision, candidates: raw.candidates,
          selected_instance_id: raw.selected_instance_id, allowed_actions: raw.allowed_actions } : null
        const bindingWrites = snapshot.writes.filter(item => item.request.action === 'employee_binding_resolve_set')
        const original = bindingWrites.find(item => employeeAssetWriteBlocked(item.receipt, false))
          ?? bindingWrites.filter(item => item.request.project_id === target?.project_id && item.request.workflow_id === target?.workflow_id
            && item.request.node_id === target?.node_id && item.request.workflow_revision === target?.expected_workflow_revision).at(-1)
        return <AIEmployeeBindingSelection project_id={scopeCurrent ? project_id : null} connected={snapshot.connected && scopeCurrent}
          target={target} view={candidateView ? { ...candidateView, data } : undefined}
          selected_instance_id={bindingChoice.instance_id} selected_read_request_id={bindingChoice.read_request_id}
          writesBlocked={blocked} write={original ? { state: original.receipt.state, request_id: original.request.request_id,
            project_id: original.request.project_id, workflow_id: original.request.workflow_id, node_id: original.request.node_id,
            workflow_revision: original.request.workflow_revision,
            verify_available: snapshot.connected && scopeCurrent && ['unknown','partial'].includes(original.receipt.state) } : null}
          onLoad={selectedTarget => run(() => {
            setBindingChoice({ instance_id: null, read_request_id: null })
            controller.loadBindingCandidates(selectedTarget.workflow_id, selectedTarget.node_id, selectedTarget.expected_workflow_revision)
          })} onSelect={(instance_id, read_request_id) => setBindingChoice({ instance_id, read_request_id })}
          onSave={choice => run(() => controller.saveBindingSelection(choice))}
          onRecoverExplicit={requestId => run(() => controller.recoverWrite(requestId))} />
      }} />
  </section>
}


