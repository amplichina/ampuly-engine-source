/** App-owned, bounded employee asset state. No sockets, runtime engine or polling.
 * Keep this instance above the tab: unmounting a Host must not discard unknown writes.
 */
import {
  EmployeeAssetAdapterError, acceptEmployeeAssetReceipt, buildEmployeeAssetRequest,
  employeeAssetResponseLost, employeeAssetWriteBlocked,
  projectEmployeeAdoptionTargets,
  buildEmployeeInstanceModelRecoveryRequest,
} from './aiEmployeeAssetAdapter'
import type { DraftGrant, InstanceGrant, EmployeeAssetReceipt, EmployeeAssetRequest, EmployeeAssetRequestInput, PackageSource } from './aiEmployeeAssetAdapter'
import { EmployeeLifecycleController, lifecycleCandidateKey } from './aiEmployeeLifecycleController'
import { browserLifecyclePendingJournal } from './aiEmployeeLifecycleJournal'
import { EmployeeInstanceKnowledgeController, type EmployeeKnowledgeScope } from './employeeInstanceKnowledgeController'
import { EmployeeInstanceAdoptionController, instanceAdoptionCandidates, instanceAdoptionKey } from './aiEmployeeInstanceAdoptionController'
import type { InstanceAdoptionCandidate } from './aiEmployeeInstanceAdoptionController'
import type { LifecycleCandidate } from './aiEmployeeLifecycleController'
import type { EmployeeOperationReceipt } from '../components/aiEmployees/AIEmployeeProjectPanel'
import { assertEmployeeEditorBudget, employeeDraftChanges, employeeScopeList, readEmployeeScopes, sameEmployeeScopes, validateEmployeeDraftChanges } from './aiEmployeeScopePatch'
import type { EmployeeScopeField, EmployeeScopeList, EmployeeScopes } from './aiEmployeeScopePatch'
import { employeeArtifactState, readEmployeeArtifact, sameEmployeeArtifact } from './aiEmployeeArtifactContract'
import type { EmployeeArtifactState } from './aiEmployeeArtifactContract'
import { employeeHumanSlotsState, readEmployeeHumanSlots, sameEmployeeHumanSlots } from './aiEmployeeHumanSlots'
import type { EmployeeHumanSlotsState } from './aiEmployeeHumanSlots'
import { employeeLoopPolicyState, readEmployeeLoopPolicy, sameEmployeeLoopPolicy } from './aiEmployeeLoopPolicy'
import type { EmployeeLoopPolicyState } from './aiEmployeeLoopPolicy'
import { copyEmployeeInstanceLimitsState, employeeInstanceLimitsValue, readEmployeeInstanceLimits, sameEmployeeInstanceLimits } from './aiEmployeeInstanceLimits'
import type { EmployeeInstanceLimitsState, EmployeeInstanceLimitsValue } from './aiEmployeeInstanceLimits'
import { copyEmployeeIOSchemas, readEmployeeIOSchemas, sameEmployeeIOSchemas } from './aiEmployeeIOSchema'
import type { EmployeeIOSchemaField, EmployeeIOSchemaState, EmployeeIOSchemas } from './aiEmployeeIOSchema'
import { copyEmployeeInstanceModelEdit, employeeInstanceModelCandidates, employeeInstanceModelConfigChanges,
  employeeInstanceModelConfigValue, employeeInstanceModelEdit, readEmployeeInstanceModelConfig,
  sameEmployeeInstanceModelConfig } from './aiEmployeeInstanceModelConfig'
import type { EmployeeInstanceModelConfigState, EmployeeInstanceModelEdit, EmployeeInstanceModelSnapshot } from './aiEmployeeInstanceModelConfig'

type Row = Record<string, unknown>
type SendResult = 'sent' | 'queued' | 'queue-full' | 'send-failed'
type ReadSlot = 'published' | 'drafts' | 'instances' | 'detail' | 'binding' | 'binding_candidates' | 'adoption_target' | 'adoption' | 'model_candidates'
export interface EmployeeAssetReadView {
  readonly state: 'not_loaded' | 'loading' | 'ready' | 'error' | 'stale'
  readonly request: EmployeeAssetRequest | null
  readonly data: Row | null
  readonly receipt: EmployeeAssetReceipt | null
}
export interface EmployeeAssetEditFields { name: string; responsibility: string; version: string }
export interface EmployeeAssetCreationFields extends EmployeeAssetEditFields { package_id: string }
export interface EmployeeAssetEditForm {
  readonly package_id: string
  readonly base_revision: string
  readonly fields: Readonly<EmployeeAssetEditFields>
  readonly baseline: Readonly<EmployeeAssetEditFields>
  readonly scopes: EmployeeScopes
  readonly scope_baseline: EmployeeScopes
  readonly artifact: EmployeeArtifactState
  readonly artifact_baseline: EmployeeArtifactState
  readonly human: EmployeeHumanSlotsState
  readonly human_baseline: EmployeeHumanSlotsState
  readonly loop: EmployeeLoopPolicyState
  readonly loop_baseline: EmployeeLoopPolicyState
  readonly ios: EmployeeIOSchemas
  readonly io_baseline: EmployeeIOSchemas
  readonly dirty: boolean
}
export interface EmployeeAssetProjectState {
  readonly published: EmployeeAssetReadView
  readonly drafts: EmployeeAssetReadView
  readonly instances: EmployeeAssetReadView
  readonly detail: EmployeeAssetReadView
  readonly binding: EmployeeAssetReadView
  readonly binding_candidates: EmployeeAssetReadView
  readonly adoption_target: EmployeeAssetReadView
  readonly adoption: EmployeeAssetReadView
  readonly model_candidates: EmployeeAssetReadView
  readonly editor: EmployeeAssetEditForm | null
  readonly creation: Readonly<EmployeeAssetCreationFields>
  readonly instance_editor: EmployeeInstanceEditForm | null
}
export interface EmployeeInstanceEditForm {
  readonly instance_id: string
  readonly base_revision: number
  readonly base_fingerprint: string
  readonly display_name: string
  readonly baseline_name: string
  readonly note: string
  readonly limits: EmployeeInstanceLimitsState
  readonly limits_baseline: EmployeeInstanceLimitsState
  readonly model: EmployeeInstanceModelEdit
  readonly model_baseline: EmployeeInstanceModelConfigState
  readonly dirty: boolean
}
export interface EmployeeAssetWriteView {
  readonly request: EmployeeAssetRequest
  readonly receipt: EmployeeOperationReceipt | EmployeeAssetReceipt
}
export interface EmployeeAssetSnapshot {
  readonly connected: boolean
  readonly project_id: string | null
  readonly projects: Readonly<Record<string, EmployeeAssetProjectState>>
  readonly writes: readonly EmployeeAssetWriteView[]
}
export interface EmployeeAssetControllerOptions {
  send: (message: Readonly<Record<string, unknown>>) => SendResult
  nextRequestId: () => string
  /** Supply both timer functions or neither. Each explicit request gets one 30-second deadline. */
  setTimer?: (callback: () => void, delay: number) => unknown
  clearTimer?: (handle: unknown) => void
}
interface Pending { request: EmployeeAssetRequest; slot: ReadSlot | null; pending: boolean; recovery: boolean; lastUncertain?: EmployeeAssetReceipt; timer?: unknown }
const MAX_PROJECTS = 8
const MAX_REQUESTS = 64
const MAX_WRITES = 8
const projectMap = (value: Readonly<Record<string, EmployeeAssetProjectState>> = {}): Readonly<Record<string, EmployeeAssetProjectState>> =>
  Object.freeze(Object.assign(Object.create(null) as Record<string, EmployeeAssetProjectState>, value))
const emptyView = (): EmployeeAssetReadView => Object.freeze({ state: 'not_loaded', request: null, data: null, receipt: null })
const emptyCreation = (): Readonly<EmployeeAssetCreationFields> => Object.freeze({ package_id: '', name: '', responsibility: '', version: '' })
const emptyProject = (): EmployeeAssetProjectState => Object.freeze({ published: emptyView(), drafts: emptyView(), instances: emptyView(), detail: emptyView(), binding: emptyView(),
  binding_candidates: emptyView(), adoption_target: emptyView(), adoption: emptyView(), model_candidates: emptyView(), editor: null, creation: emptyCreation(), instance_editor: null })
const modelRequest = (request: EmployeeAssetRequest): boolean => request.action === 'employee_instance_revise'
  && row(request.message.changes) && Object.keys(request.message.changes).length === 1 && Object.hasOwn(request.message.changes, 'model_config')
const instanceEditorDirty = (editor: EmployeeInstanceEditForm, model = editor.model, limits = editor.limits): boolean =>
  editor.display_name !== editor.baseline_name || editor.note !== ''
    || !sameEmployeeInstanceLimits(editor.limits_baseline, limits)
    || !sameEmployeeInstanceModelConfig(editor.model_baseline, model.config)
const creationDirty = (project: EmployeeAssetProjectState): boolean => Object.values(project.creation).some(value => value !== '')
const row = (value: unknown): value is Row => value !== null && typeof value === 'object' && !Array.isArray(value)
const string = (value: unknown): string => typeof value === 'string' ? value : ''
const sameFields = (a: EmployeeAssetEditFields, b: EmployeeAssetEditFields): boolean => a.name === b.name && a.responsibility === b.responsibility && a.version === b.version
const editorDirty = (editor: EmployeeAssetEditForm, fields = editor.fields, scopes = editor.scopes, artifact = editor.artifact, human = editor.human, loop = editor.loop, ios = editor.ios): boolean =>
  !sameFields(editor.baseline, fields) || !sameEmployeeScopes(editor.scope_baseline, scopes)
    || !sameEmployeeArtifact(editor.artifact_baseline, artifact) || !sameEmployeeHumanSlots(editor.human_baseline, human)
    || !sameEmployeeLoopPolicy(editor.loop_baseline, loop) || !sameEmployeeIOSchemas(editor.io_baseline, ios)
const freezeReceipt = (receipt: EmployeeAssetReceipt): EmployeeAssetReceipt => {
  if (receipt.projection) Object.freeze(receipt.projection)
  return Object.freeze(receipt)
}
export class EmployeeAssetControllerError extends Error {
  readonly code = 'protected_view_capacity'
  constructor() { super('员工视图暂存已满；先处理未保存编辑或待确认保存，再打开其他项目。'); this.name = 'EmployeeAssetControllerError' }
}

export class EmployeeAssetController {
  readonly knowledge: EmployeeInstanceKnowledgeController
  private readonly unsubscribeKnowledge: () => void
  readonly lifecycle: EmployeeLifecycleController
  private readonly unsubscribeLifecycle: () => void
  readonly instanceAdoption: EmployeeInstanceAdoptionController
  private readonly unsubscribeInstanceAdoption: () => void
  private snapshot: EmployeeAssetSnapshot = Object.freeze({ connected: false, project_id: null, projects: projectMap(), writes: Object.freeze([]) })
  private readonly listeners = new Set<() => void>()
  private readonly requests = new Map<string, Pending>()
  private readonly projectAccess = new Map<string, number>()
  private accessSequence = 0
  private readonly setTimer: (callback: () => void, delay: number) => unknown
  private readonly clearTimer: (handle: unknown) => void
  private disposed = false
  constructor(private readonly options: EmployeeAssetControllerOptions) {
    if ((options.setTimer === undefined) !== (options.clearTimer === undefined)) throw new EmployeeAssetAdapterError('request_invalid')
    this.setTimer = options.setTimer ?? ((callback, delay) => globalThis.setTimeout(callback, delay))
    this.clearTimer = options.clearTimer ?? (handle => globalThis.clearTimeout(handle as ReturnType<typeof globalThis.setTimeout>))
    this.lifecycle = new EmployeeLifecycleController({ send: options.send, nextRequestId: options.nextRequestId,
      requireHistory: true, journal: browserLifecyclePendingJournal(),
      setTimer: this.setTimer, clearTimer: this.clearTimer, projectId: () => this.snapshot.project_id,
      canWrite: candidate => this.canUseLifecycleCandidate(candidate) })
    this.unsubscribeLifecycle = this.lifecycle.subscribe(() => this.publish(this.snapshot))
    this.instanceAdoption = new EmployeeInstanceAdoptionController({ send: options.send, nextRequestId: options.nextRequestId,
      setTimer: this.setTimer, clearTimer: this.clearTimer, projectId: () => this.snapshot.project_id,
      canWrite: candidate => this.canUseInstanceAdoption(candidate),
      confirmed: candidate => this.invalidateAfterInstanceAdoption(candidate.project_id) })
    this.unsubscribeInstanceAdoption = this.instanceAdoption.subscribe(() => this.publish(this.snapshot))
    this.knowledge = new EmployeeInstanceKnowledgeController({ send: options.send, nextRequestId: options.nextRequestId,
      setTimer: this.setTimer, clearTimer: this.clearTimer, current: () => this.knowledgeScope(),
      confirmed: scope => this.invalidateAfterInstanceAdoption(scope.project_id) })
    this.unsubscribeKnowledge = this.knowledge.subscribe(() => this.publish(this.snapshot))
  }
  getSnapshot = (): EmployeeAssetSnapshot => this.snapshot
  private knowledgeScope(): EmployeeKnowledgeScope | null {
    const projectId = this.snapshot.project_id, project = projectId ? this.snapshot.projects[projectId] : undefined
    const editor = project?.instance_editor, detail = project?.detail
    if (this.disposed || !this.snapshot.connected || !projectId || !editor || editor.dirty || project?.editor?.dirty
      || detail?.state !== 'ready' || detail.request?.action !== 'employee_instance_detail'
      || detail.request.instance_id !== editor.instance_id || detail.data?.revision !== editor.base_revision
      || detail.data?.fingerprint !== editor.base_fingerprint
      || !Array.isArray(detail.data.allowed_actions) || !detail.data.allowed_actions.includes('employee_instance_knowledge_candidates')
      || this.lifecycle.hasBlockingWrite() || this.instanceAdoption.hasBlockingWrite()
      || this.snapshot.writes.some(item => employeeAssetWriteBlocked(item.receipt, false))) return null
    return { project_id: projectId, instance_id: editor.instance_id, revision: editor.base_revision,
      fingerprint: editor.base_fingerprint, allowed: true }
  }
  instanceAdoptionCandidates = (): ReturnType<typeof instanceAdoptionCandidates> => instanceAdoptionCandidates(this.snapshot)
  canUseInstanceAdoption = (candidate: Readonly<InstanceAdoptionCandidate>): boolean => {
    const current = this.instanceAdoptionCandidates()[candidate.kind], p = this.snapshot.projects[candidate.project_id]
    return !this.disposed && this.snapshot.connected && !!current && instanceAdoptionKey(current) === instanceAdoptionKey(candidate)
      && !p?.editor?.dirty && !p?.instance_editor?.dirty && !this.lifecycle.hasBlockingWrite()
      && !this.knowledge.hasBlockingWrite()
      && !this.snapshot.writes.some(item => employeeAssetWriteBlocked(item.receipt, false))
  }
  private invalidateAfterInstanceAdoption(projectId: string): void {
    const project = this.snapshot.projects[projectId]; if (!project) return
    const stale = (view: EmployeeAssetReadView): EmployeeAssetReadView => view.state === 'ready' ? Object.freeze({ ...view, state: 'stale' }) : view
    this.updateProject(projectId, { instances: stale(project.instances), detail: stale(project.detail), adoption_target: emptyView(), adoption: emptyView() })
  }
  lifecycleCandidate = (): LifecycleCandidate | null => {
    const id = this.snapshot.project_id, project = id ? this.snapshot.projects[id] : undefined, detail = project?.detail
    if (!id || detail?.state !== 'ready' || detail.request?.source !== 'draft' || !detail.data || !row(detail.data.package)) return null
    const packageId = string(detail.data.package.package_id), revision = string(detail.data.revision), fingerprint = string(detail.data.fingerprint)
    if (detail.request.package_id !== packageId || !/^[a-z0-9][a-z0-9_-]{2,63}$/.test(packageId)
      || !/^[a-f0-9]{64}$/.test(revision) || !/^[a-f0-9]{64}$/.test(fingerprint)) return null
    return { project_id: id, package_id: packageId, revision, fingerprint }
  }
  instanceLifecycleCandidate = (): LifecycleCandidate | null => {
    const id = this.snapshot.project_id, project = id ? this.snapshot.projects[id] : undefined
    const editor = project?.instance_editor, detail = project?.detail, fixed = project?.adoption_target
    if (!id || !editor || detail?.state !== 'ready' || detail.request?.action !== 'employee_instance_detail'
      || detail.request.instance_id !== editor.instance_id || detail.data?.revision !== editor.base_revision
      || detail.data?.fingerprint !== editor.base_fingerprint || !row(detail.data.instance)
      || fixed?.state !== 'ready' || fixed.request?.source !== 'published' || !fixed.data || !row(fixed.data.package)) return null
    const instance = detail.data.instance, pkg = fixed.data.package
    if (instance.execution_state !== 'active' || pkg.package_id !== instance.package_id || pkg.version !== instance.package_version
      || fixed.data.fingerprint !== instance.package_fingerprint || fixed.request.target_fingerprint !== instance.package_fingerprint
      || fixed.request.version !== instance.package_version || !/^[a-f0-9]{64}$/.test(string(fixed.data.revision))) return null
    return { project_id: id, package_id: string(instance.package_id), revision: string(fixed.data.revision), fingerprint: string(instance.package_fingerprint),
      instance_selection: { instance_id: editor.instance_id, expected_revision: editor.base_revision, expected_fingerprint: editor.base_fingerprint } }
  }
  /** Reuse the published-detail read slot, without treating this as a version adoption. */
  loadInstanceTestPackage(): string {
    const projectId = this.projectId(), project = this.snapshot.projects[projectId], editor = project?.instance_editor, detail = project?.detail
    if (!editor || editor.dirty || project.editor?.dirty || detail?.state !== 'ready' || detail.request?.action !== 'employee_instance_detail'
      || detail.request.instance_id !== editor.instance_id || detail.data?.revision !== editor.base_revision
      || detail.data?.fingerprint !== editor.base_fingerprint || !row(detail.data.instance)) throw new EmployeeAssetAdapterError('grant_missing')
    const instance = detail.data.instance
    const base = buildEmployeeAssetRequest({ ...this.nextInput('employee_package_detail'), action: 'employee_package_detail',
      source: 'published', package_id: string(instance.package_id), version: string(instance.package_version) })
    const fingerprint = string(instance.package_fingerprint)
    if (!/^[a-f0-9]{64}$/.test(fingerprint)) throw new EmployeeAssetAdapterError('grant_missing')
    this.clearAdoptionTarget()
    return this.start(Object.freeze({ ...base, target_fingerprint: fingerprint, key: JSON.stringify([base.key, fingerprint]) }), 'adoption_target')
  }
  canUseLifecycleCandidate = (candidate: Readonly<LifecycleCandidate>): boolean => {
    const current = candidate.instance_selection ? this.instanceLifecycleCandidate() : this.lifecycleCandidate(), project = current ? this.snapshot.projects[current.project_id] : undefined
    return !this.disposed && this.snapshot.connected && !!current && lifecycleCandidateKey(current) === lifecycleCandidateKey(candidate)
      && !project?.editor?.dirty && !project?.instance_editor?.dirty
      && !this.instanceAdoption.hasBlockingWrite()
      && !this.knowledge.hasBlockingWrite()
      && !this.snapshot.writes.some(item => employeeAssetWriteBlocked(item.receipt, false))
  }
  subscribe = (listener: () => void): (() => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  private publish(next: EmployeeAssetSnapshot): void {
    this.snapshot = Object.freeze({ ...next, projects: projectMap(next.projects), writes: Object.freeze([...next.writes]) })
    for (const listener of [...this.listeners]) listener()
  }
  private checkUsable(): void { if (this.disposed) throw new EmployeeAssetAdapterError('request_invalid') }
  private projectId(): string {
    this.checkUsable()
    const id = this.snapshot.project_id
    if (!this.snapshot.connected || !id) throw new EmployeeAssetAdapterError('request_invalid')
    this.projectAccess.set(id, ++this.accessSequence)
    return id
  }
  private updateProject(projectId: string, change: Partial<EmployeeAssetProjectState>): void {
    this.publish({ ...this.snapshot, projects: { ...this.snapshot.projects,
      [projectId]: Object.freeze({ ...(this.snapshot.projects[projectId] ?? emptyProject()), ...change }) } })
  }
  selectProject(projectId: string | null): void {
    this.checkUsable()
    if (projectId !== null && !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(projectId)) throw new EmployeeAssetAdapterError('request_invalid')
    if (projectId !== null) this.projectAccess.set(projectId, ++this.accessSequence)
    if (projectId === this.snapshot.project_id) return
    this.clearBindingPreview()
    this.clearBindingCandidates()
    this.clearAdoptionTarget()
    this.clearInstanceModelCandidates()
    if (projectId !== null && !this.snapshot.projects[projectId] && Object.keys(this.snapshot.projects).length >= MAX_PROJECTS) {
      const protectedProject = (id: string): boolean => !!this.snapshot.projects[id]?.editor?.dirty || !!this.snapshot.projects[id]?.instance_editor?.dirty || creationDirty(this.snapshot.projects[id])
        || this.lifecycle.hasProtectedProject(id)
        || this.instanceAdoption.hasProtectedProject(id)
        || [...this.requests.values()].some(item => item.request.project_id === id && item.pending)
        || this.snapshot.writes.some(item => item.request.project_id === id && employeeAssetWriteBlocked(item.receipt, false))
      const victim = Object.keys(this.snapshot.projects).filter(id => !protectedProject(id))
        .sort((a, b) => (this.projectAccess.get(a) ?? 0) - (this.projectAccess.get(b) ?? 0))[0]
      if (!victim) { this.projectAccess.delete(projectId); throw new EmployeeAssetControllerError() }
      this.forgetProject(victim)
    }
    this.publish({ ...this.snapshot, project_id: projectId, projects: projectId === null || this.snapshot.projects[projectId]
      ? this.snapshot.projects : { ...this.snapshot.projects, [projectId]: emptyProject() } })
  }
  /** Only clean, settled project views may be forgotten; never unknown or unsaved work. */
  forgetProject(projectId: string): void {
    this.checkUsable()
    if (!this.snapshot.projects[projectId]) return
    if (this.lifecycle.hasProtectedProject(projectId)) throw new EmployeeAssetControllerError()
    if (this.instanceAdoption.hasProtectedProject(projectId)) throw new EmployeeAssetControllerError()
    if (this.knowledge.hasProtectedProject(projectId)) throw new EmployeeAssetControllerError()
    if (this.snapshot.projects[projectId].editor?.dirty || this.snapshot.projects[projectId].instance_editor?.dirty || creationDirty(this.snapshot.projects[projectId])
      || [...this.requests.values()].some(item => item.request.project_id === projectId && item.pending)
      || this.snapshot.writes.some(item => item.request.project_id === projectId && employeeAssetWriteBlocked(item.receipt, false))) {
      throw new EmployeeAssetControllerError()
    }
    const projects = { ...this.snapshot.projects }; delete projects[projectId]
    for (const [id, item] of this.requests) if (item.request.project_id === projectId) { this.stopTimer(item); this.requests.delete(id) }
    this.projectAccess.delete(projectId)
    this.lifecycle.forgetProject(projectId)
    this.instanceAdoption.forgetProject(projectId)
    this.publish({ ...this.snapshot, projects, project_id: this.snapshot.project_id === projectId ? null : this.snapshot.project_id,
      writes: this.snapshot.writes.filter(item => item.request.project_id !== projectId) })
  }
  connectionChanged(connected: boolean): void {
    this.checkUsable()
    if (connected === this.snapshot.connected) return
    this.publish({ ...this.snapshot, connected })
    this.lifecycle.connectionChanged(connected)
    this.instanceAdoption.connectionChanged(connected)
    this.knowledge.connectionChanged(connected)
    if (!connected) {
      for (const [id, pending] of this.requests) if (pending.pending) this.markTimeout(id)
      const projects: Record<string, EmployeeAssetProjectState> = {}
      for (const [id, project] of Object.entries(this.snapshot.projects)) {
        const stale = (view: EmployeeAssetReadView): EmployeeAssetReadView => view.state === 'ready' ? Object.freeze({ ...view, state: 'stale' }) : view
        projects[id] = Object.freeze({ ...project, published: stale(project.published), drafts: stale(project.drafts), instances: stale(project.instances), detail: stale(project.detail), binding: stale(project.binding),
          binding_candidates: emptyView(), adoption_target: emptyView(), adoption: emptyView(), model_candidates: emptyView() })
      }
      this.publish({ ...this.snapshot, projects })
    }
  }
  private stopTimer(pending: Pending): void {
    if (pending.timer !== undefined) { this.clearTimer(pending.timer); delete pending.timer }
  }
  private trim(): void {
    for (const [id, pending] of this.requests) {
      if (this.requests.size < MAX_REQUESTS) break
      const write = this.snapshot.writes.find(item => item.request.request_id === id)
      if (!pending.pending && (!write || !employeeAssetWriteBlocked(write.receipt, false))) this.requests.delete(id)
    }
    if (this.requests.size >= MAX_REQUESTS) throw new EmployeeAssetAdapterError('request_invalid')
  }
  private nextInput(action: EmployeeAssetRequestInput['action']): { project_id: string; request_id: string; action: typeof action } {
    return { project_id: this.projectId(), request_id: this.options.nextRequestId(), action }
  }
  private sendPending(pending: Pending): void {
    pending.pending = true
    pending.timer = this.setTimer(() => this.markTimeout(pending.request.request_id), 30_000)
    let result: SendResult
    // Binding verification must never become an initial write when its original
    // request did not reach the server. The flag is metadata, not a new request.
    const wire = pending.recovery && modelRequest(pending.request)
      ? buildEmployeeInstanceModelRecoveryRequest(pending.request).message
      : pending.recovery && pending.request.action === 'employee_binding_resolve_set'
        ? Object.freeze({ ...pending.request.message, check_only: true }) : pending.request.message
    try { result = this.options.send(wire) } catch { result = 'send-failed' }
    // Employee reads are not currently replayable in wsClient. A queued response is
    // also treated conservatively; no controller queue or automatic retry exists.
    if (result !== 'sent') this.markTimeout(pending.request.request_id)
  }
  private start(request: EmployeeAssetRequest, slot: ReadSlot | null): string {
    if (slot === null && this.knowledge.hasBlockingWrite()) throw new EmployeeAssetAdapterError('grant_missing')
    this.trim()
    if (this.requests.has(request.request_id)) throw new EmployeeAssetAdapterError('request_invalid')
    if (slot) {
      const previous = this.snapshot.projects[request.project_id]?.[slot]
      if (previous?.request) {
        const previousPending = this.requests.get(previous.request.request_id)
        if (previousPending) { this.stopTimer(previousPending); previousPending.pending = false }
      }
      this.updateProject(request.project_id, { [slot]: Object.freeze({ state: 'loading', request,
        data: slot === 'adoption' || slot === 'adoption_target' || slot === 'binding_candidates' || slot === 'model_candidates' ? null : previous?.data ?? null, receipt: null }) })
    } else {
      const writes = this.snapshot.writes.filter(item => employeeAssetWriteBlocked(item.receipt, false))
      const settled = this.snapshot.writes.filter(item => !employeeAssetWriteBlocked(item.receipt, false))
      this.publish({ ...this.snapshot, writes: [...settled.slice(-(MAX_WRITES - 1)), ...writes,
        Object.freeze({ request, receipt: Object.freeze({ state: 'pending', request_id: request.request_id, message: '正在保存，等待服务端回执。' }) })] })
    }
    const pending: Pending = { request, slot, pending: true, recovery: false }
    this.requests.set(request.request_id, pending)
    this.sendPending(pending)
    return request.request_id
  }
  loadPackages(source: PackageSource = 'published'): string {
    if (source === 'published') this.clearAdoptionTarget()
    const request = buildEmployeeAssetRequest({ ...this.nextInput('employee_package_list'), action: 'employee_package_list', source })
    return this.start(request, source === 'draft' ? 'drafts' : 'published')
  }
  loadPackage(packageId: string, options: { source?: PackageSource; version?: string } = {}): string {
    this.clearAdoptionTarget()
    const request = buildEmployeeAssetRequest({ ...this.nextInput('employee_package_detail'), action: 'employee_package_detail', package_id: packageId, ...options })
    return this.start(request, 'detail')
  }
  loadInstances(): string {
    this.clearAdoptionPreview()
    const request = buildEmployeeAssetRequest({ ...this.nextInput('employee_instance_list'), action: 'employee_instance_list' })
    return this.start(request, 'instances')
  }
  loadInstance(instanceId: string): string {
    this.clearAdoptionTarget()
    this.clearInstanceModelCandidates()
    const request = buildEmployeeAssetRequest({ ...this.nextInput('employee_instance_detail'), action: 'employee_instance_detail', instance_id: instanceId })
    return this.start(request, 'detail')
  }
  /** Saved-node read only. The host supplies the successful workflow-list SHA. */
  previewBinding(workflowId: string, nodeId: string, expectedWorkflowRevision: string, bindingSlot: 'executor' = 'executor'): string {
    const request = buildEmployeeAssetRequest({ ...this.nextInput('employee_binding_preview'), action: 'employee_binding_preview',
      workflow_id: workflowId, node_id: nodeId, expected_workflow_revision: expectedWorkflowRevision, binding_slot: bindingSlot })
    this.clearBindingPreview()
    return this.start(request, 'binding')
  }
  /** Cancel only this read generation; never details, drafts or uncertain writes. */
  clearBindingPreview(): void {
    this.checkUsable()
    const projectId = this.snapshot.project_id, view = projectId ? this.snapshot.projects[projectId]?.binding : undefined
    if (!projectId || !view || view.state === 'not_loaded') return
    if (view.request) {
      const pending = this.requests.get(view.request.request_id)
      if (pending) { this.stopTimer(pending); pending.pending = false }
    }
    this.updateProject(projectId, { binding: emptyView() })
  }
  /** One explicit saved-node read. Never infer candidates from the instance list. */
  loadBindingCandidates(workflowId: string, nodeId: string, expectedWorkflowRevision: string): string {
    const request = buildEmployeeAssetRequest({ ...this.nextInput('employee_binding_candidates'), action: 'employee_binding_candidates',
      workflow_id: workflowId, node_id: nodeId, expected_workflow_revision: expectedWorkflowRevision, binding_slot: 'executor' })
    this.clearBindingCandidates()
    return this.start(request, 'binding_candidates')
  }
  clearBindingCandidates(): void {
    this.checkUsable()
    const projectId = this.snapshot.project_id, view = projectId ? this.snapshot.projects[projectId]?.binding_candidates : undefined
    if (!projectId || !view || view.state === 'not_loaded') return
    if (view.request) {
      const pending = this.requests.get(view.request.request_id)
      if (pending) { this.stopTimer(pending); pending.pending = false }
    }
    this.updateProject(projectId, { binding_candidates: emptyView() })
  }
  /** Selection is bound to the exact successful read token and observed revisions. */
  saveBindingSelection(choice: unknown): string {
    this.checkWrite()
    const projectId = this.projectId(), view = this.snapshot.projects[projectId]?.binding_candidates
    if (!row(choice) || choice.project_id !== projectId || view?.state !== 'ready'
      || view.request?.action !== 'employee_binding_candidates' || !view.data
      || choice.read_request_id !== view.request.request_id || choice.workflow_id !== view.request.workflow_id
      || choice.node_id !== view.request.node_id || choice.binding_slot !== 'executor'
      || choice.workflow_revision !== view.request.workflow_revision
      || choice.binding_revision !== view.data.binding_revision || choice.workflow_source !== view.data.workflow_source
      || choice.workflow_version !== view.data.workflow_version || !Array.isArray(view.data.candidates)
      || !Array.isArray(view.data.allowed_actions)) throw new EmployeeAssetAdapterError('grant_missing')
    const candidates = view.data.candidates.filter(row).filter(item => item.instance_id === choice.instance_id)
    if (candidates.length !== 1) throw new EmployeeAssetAdapterError('grant_missing')
    const candidate = candidates[0]
    for (const key of ['instance_revision','revision_fingerprint','package_id','package_version','package_fingerprint']) {
      if (choice[key] !== candidate[key]) throw new EmployeeAssetAdapterError('grant_missing')
    }
    const request = buildEmployeeAssetRequest({ ...this.nextInput('employee_binding_resolve_set'), action: 'employee_binding_resolve_set',
      workflow_id: view.request.workflow_id!, node_id: view.request.node_id!, binding_slot: 'executor',
      expected_workflow_revision: view.request.workflow_revision!, instance_id: candidate.instance_id as string,
      expected_binding_revision: view.data.binding_revision as number, expected_instance_revision: candidate.instance_revision as number,
      expected_instance_fingerprint: candidate.revision_fingerprint as string,
      grant: { project_id: projectId, workflow_id: view.request.workflow_id!, node_id: view.request.node_id!,
        binding_slot: 'executor', workflow_revision: view.request.workflow_revision!, binding_revision: view.data.binding_revision as number,
        instance_id: candidate.instance_id as string, instance_revision: candidate.instance_revision as number,
        revision_fingerprint: candidate.revision_fingerprint as string,
        allowed_actions: view.data.allowed_actions.filter((value): value is string => typeof value === 'string') } })
    return this.start(request, null)
  }
  private clearAdoptionSlot(slot: 'adoption' | 'adoption_target'): void {
    this.checkUsable()
    const projectId = this.snapshot.project_id, view = projectId ? this.snapshot.projects[projectId]?.[slot] : undefined
    if (!projectId || !view || view.state === 'not_loaded') return
    if (view.request) {
      const pending = this.requests.get(view.request.request_id)
      if (pending) { this.stopTimer(pending); pending.pending = false }
    }
    this.updateProject(projectId, { [slot]: emptyView() })
  }
  clearAdoptionPreview(): void { this.clearAdoptionSlot('adoption') }
  clearAdoptionTarget(): void { this.clearAdoptionPreview(); this.clearAdoptionSlot('adoption_target') }
  /** Explicit observed published identity only; never current/latest guessing. */
  loadAdoptionTarget(packageId: string, packageVersion: string, fingerprint: string): string {
    const projectId = this.projectId(), project = this.snapshot.projects[projectId], detail = project?.detail
    if (detail?.state !== 'ready' || detail.request?.action !== 'employee_instance_detail'
      || !detail.data || !row(detail.data.instance) || detail.data.instance.package_id !== packageId || project.published.state !== 'ready'
      || !projectEmployeeAdoptionTargets(project.published.data, packageId).some(target => target.package_version === packageVersion && target.expected_fingerprint === fingerprint)) {
      throw new EmployeeAssetAdapterError('grant_missing')
    }
    const base = buildEmployeeAssetRequest({ ...this.nextInput('employee_package_detail'), action: 'employee_package_detail',
      source: 'published', package_id: packageId, version: packageVersion })
    // This is local correlation only: the existing package-detail wire has no
    // fingerprint argument. Do not silently rebase a selection to another hash.
    const request: EmployeeAssetRequest = Object.freeze({ ...base, target_fingerprint: fingerprint,
      key: JSON.stringify([base.key, fingerprint]) })
    this.clearAdoptionTarget()
    return this.start(request, 'adoption_target')
  }
  /** The click's displayed revision must match current detail; no silent rebasing. */
  previewAdoption(instanceId: string, packageId: string, packageVersion: string, fingerprint: string, expectedRevision: number): string {
    const projectId = this.projectId(), project = this.snapshot.projects[projectId], detail = project?.detail, target = project?.adoption_target
    if (detail?.state !== 'ready' || detail.request?.action !== 'employee_instance_detail' || detail.request.instance_id !== instanceId
      || !detail.data || detail.data.revision !== expectedRevision || !row(detail.data.instance) || detail.data.instance.package_id !== packageId
      || typeof detail.data.fingerprint !== 'string' || target?.state !== 'ready' || target.request?.source !== 'published'
      || target.request.package_id !== packageId || target.request.version !== packageVersion || target.request.target_fingerprint !== fingerprint
      || !target.data || !row(target.data.package)
      || target.data.status !== 'published' || target.data.package.status !== 'published' || target.data.package.version !== packageVersion
      || target.data.fingerprint !== fingerprint || project.published.state !== 'ready'
      || !projectEmployeeAdoptionTargets(project.published.data, packageId).some(item => item.package_version === packageVersion && item.expected_fingerprint === fingerprint)) {
      throw new EmployeeAssetAdapterError('grant_missing')
    }
    const grant = this.instanceGrant(instanceId, expectedRevision, detail.data.fingerprint)
    const request = buildEmployeeAssetRequest({ ...this.nextInput('employee_instance_adoption_preview'), action: 'employee_instance_adoption_preview',
      instance_id: instanceId, expected_revision: expectedRevision, package_id: packageId, package_version: packageVersion, expected_fingerprint: fingerprint,
      grant: { ...grant, package_id: packageId, package_version: string(detail.data.instance.package_version), package_fingerprint: string(detail.data.instance.package_fingerprint) } })
    this.clearAdoptionPreview()
    return this.start(request, 'adoption')
  }
  private checkWrite(): void {
    this.projectId()
    if (this.lifecycle.hasBlockingWrite()) throw new EmployeeAssetAdapterError('grant_missing')
    if (this.instanceAdoption.hasBlockingWrite()) throw new EmployeeAssetAdapterError('grant_missing')
    if (this.snapshot.writes.some(item => employeeAssetWriteBlocked(item.receipt, false))) throw new EmployeeAssetAdapterError('grant_missing')
  }
  createDraft(manifest: unknown): string {
    this.checkWrite()
    const projectId = this.projectId()
    const view = this.snapshot.projects[projectId]?.drafts
    if (view?.state !== 'ready' || !view.data || !Array.isArray(view.data.allowed_actions)) throw new EmployeeAssetAdapterError('grant_missing')
    const request = buildEmployeeAssetRequest({ ...this.nextInput('employee_package_draft_create'), action: 'employee_package_draft_create', manifest,
      grant: { project_id: projectId, allowed_actions: view.data.allowed_actions.filter((value): value is string => typeof value === 'string') } })
    return this.start(request, null)
  }
  createDraftFromTemplate(templateId: string, sourceRevision: string, manifest: { package_id: string; name: string; max_cost_cny: string }): string {
    this.checkWrite()
    const projectId = this.projectId(), view = this.snapshot.projects[projectId]?.drafts
    if (view?.state !== 'ready' || !view.data || !Array.isArray(view.data.allowed_actions)
      || !Array.isArray(view.data.templates) || !view.data.templates.some(item => row(item)
        && item.template_id === templateId && item.source_revision === sourceRevision && item.persisted === false)) throw new EmployeeAssetAdapterError('grant_missing')
    const request = buildEmployeeAssetRequest({ ...this.nextInput('employee_package_draft_create'), action: 'employee_package_draft_create',
      manifest, template_id: templateId, expected_template_revision: sourceRevision,
      grant: { project_id: projectId, allowed_actions: view.data.allowed_actions.filter((value): value is string => typeof value === 'string') } })
    return this.start(request, null)
  }
  private draftGrant(packageId: string, expectedRevision: string): DraftGrant {
    const projectId = this.projectId()
    const view = this.snapshot.projects[projectId]?.detail
    if (view?.state !== 'ready' || view.request?.source !== 'draft' || view.request.package_id !== packageId
      || !view.data || view.data.revision !== expectedRevision || !Array.isArray(view.data.allowed_actions)
      || (view.data.status !== 'draft' && view.data.status !== 'testing')) throw new EmployeeAssetAdapterError('grant_missing')
    return { project_id: projectId, package_id: packageId, source: 'draft', status: view.data.status, revision: expectedRevision,
      allowed_actions: view.data.allowed_actions.filter((value): value is string => typeof value === 'string') }
  }
  saveDraft(packageId: string, changes: unknown, expectedRevision: string): string {
    this.checkWrite()
    const supportedChanges = validateEmployeeDraftChanges(changes)
    const request = buildEmployeeAssetRequest({ ...this.nextInput('employee_package_draft_save'), action: 'employee_package_draft_save',
      package_id: packageId, changes: supportedChanges, expected_revision: expectedRevision, grant: this.draftGrant(packageId, expectedRevision) })
    return this.start(request, null)
  }
  private instanceGrant(instanceId: string, expectedRevision: number, expectedFingerprint: string): InstanceGrant {
    const projectId = this.projectId()
    const view = this.snapshot.projects[projectId]?.detail
    if (view?.state !== 'ready' || view.request?.action !== 'employee_instance_detail'
      || view.request.instance_id !== instanceId || !view.data || view.data.revision !== expectedRevision
      || view.data.fingerprint !== expectedFingerprint || !Array.isArray(view.data.allowed_actions)) throw new EmployeeAssetAdapterError('grant_missing')
    return { project_id: projectId, instance_id: instanceId, revision: expectedRevision, fingerprint: expectedFingerprint,
      allowed_actions: view.data.allowed_actions.filter((value): value is string => typeof value === 'string') }
  }
  reviseInstance(instanceId: string, displayName: string, expectedRevision: number, expectedFingerprint: string, note = ''): string {
    this.checkWrite()
    const request = buildEmployeeAssetRequest({ ...this.nextInput('employee_instance_revise'), action: 'employee_instance_revise',
      instance_id: instanceId, changes: { display_name: displayName }, expected_revision: expectedRevision, note,
      grant: this.instanceGrant(instanceId, expectedRevision, expectedFingerprint) })
    return this.start(request, null)
  }
  /** Whole project override only; the server rechecks the original fixed package. */
  reviseInstanceLimits(instanceId: string, limits: EmployeeInstanceLimitsValue, expectedRevision: number, expectedFingerprint: string, note = ''): string {
    this.checkWrite()
    const request = buildEmployeeAssetRequest({ ...this.nextInput('employee_instance_revise'), action: 'employee_instance_revise',
      instance_id: instanceId, changes: { limits }, expected_revision: expectedRevision, note,
      grant: this.instanceGrant(instanceId, expectedRevision, expectedFingerprint) })
    return this.start(request, null)
  }
  openInstanceEditor(): void {
    this.checkWrite()
    const projectId = this.projectId(), project = this.snapshot.projects[projectId], view = project?.detail
    if (view?.state !== 'ready' || view.request?.action !== 'employee_instance_detail'
      || !view.data || !row(view.data.instance) || typeof view.data.revision !== 'number'
      || typeof view.data.fingerprint !== 'string' || typeof view.data.instance.display_name !== 'string') throw new EmployeeAssetAdapterError('grant_missing')
    if (project.instance_editor?.dirty) throw new EmployeeAssetAdapterError('grant_missing')
    const instanceId = view.request.instance_id
    if (!instanceId) throw new EmployeeAssetAdapterError('grant_missing')
    this.instanceGrant(instanceId, view.data.revision, view.data.fingerprint)
    if (!view.data.allowed_actions || !(view.data.allowed_actions as unknown[]).includes('employee_instance_revise')) throw new EmployeeAssetAdapterError('grant_missing')
    const limits = copyEmployeeInstanceLimitsState(readEmployeeInstanceLimits(view.data.instance))
    const modelBaseline = readEmployeeInstanceModelConfig(view.data.instance)
    this.clearInstanceModelCandidates()
    this.updateProject(projectId, { instance_editor: Object.freeze({ instance_id: instanceId, base_revision: view.data.revision,
      base_fingerprint: view.data.fingerprint, display_name: view.data.instance.display_name,
      baseline_name: view.data.instance.display_name, note: '', limits, limits_baseline: limits,
      model: employeeInstanceModelEdit(modelBaseline), model_baseline: modelBaseline, dirty: false }) })
  }
  updateInstanceEditor(fields: { display_name: string; note: string }): void {
    const projectId = this.projectId(), editor = this.snapshot.projects[projectId]?.instance_editor
    if (!editor || typeof fields.display_name !== 'string' || fields.display_name.length > 256
      || typeof fields.note !== 'string' || fields.note.length > 4000) throw new EmployeeAssetAdapterError('request_invalid')
    this.updateProject(projectId, { instance_editor: Object.freeze({ ...editor, display_name: fields.display_name, note: fields.note,
      dirty: instanceEditorDirty({ ...editor, display_name: fields.display_name, note: fields.note }) }) })
  }
  saveInstanceEditor(): string {
    const editor = this.snapshot.projects[this.projectId()]?.instance_editor
    if (!editor?.dirty || (editor.display_name === editor.baseline_name && editor.note === '')) throw new EmployeeAssetAdapterError('request_invalid')
    return this.reviseInstance(editor.instance_id, editor.display_name, editor.base_revision, editor.base_fingerprint, editor.note)
  }
  /** Keep bounded wrong text local; only an explicit present replacement is accepted. */
  updateInstanceLimits(value: EmployeeInstanceLimitsState): void {
    const projectId = this.projectId(), editor = this.snapshot.projects[projectId]?.instance_editor
    if (!editor) throw new EmployeeAssetAdapterError('request_invalid')
    const limits = copyEmployeeInstanceLimitsState(value)
    if (limits.kind !== 'present') throw new EmployeeAssetAdapterError('request_invalid')
    this.updateProject(projectId, { instance_editor: Object.freeze({ ...editor, limits,
      dirty: instanceEditorDirty(editor, editor.model, limits) }) })
  }
  /** Restore the opened three-state value, never a new read or write acknowledgement. */
  resetInstanceLimits(): void {
    const projectId = this.projectId(), editor = this.snapshot.projects[projectId]?.instance_editor
    if (!editor) throw new EmployeeAssetAdapterError('request_invalid')
    const limits = copyEmployeeInstanceLimitsState(editor.limits_baseline)
    this.updateProject(projectId, { instance_editor: Object.freeze({ ...editor, limits,
      dirty: instanceEditorDirty(editor, editor.model, limits) }) })
  }
  saveInstanceLimitsEditor(): string {
    const editor = this.snapshot.projects[this.projectId()]?.instance_editor
    if (!editor || sameEmployeeInstanceLimits(editor.limits_baseline, editor.limits)) throw new EmployeeAssetAdapterError('request_invalid')
    const limits = employeeInstanceLimitsValue(editor.limits)
    return this.reviseInstanceLimits(editor.instance_id, limits, editor.base_revision, editor.base_fingerprint, editor.note)
  }
  /** Explicit finite read from the opened original instance, not defaults or runtime. */
  loadInstanceModelCandidates(): string {
    const projectId = this.projectId(), project = this.snapshot.projects[projectId], editor = project?.instance_editor
    const detail = project?.detail
    if (!editor || detail?.state !== 'ready' || !row(detail.data?.instance)) throw new EmployeeAssetAdapterError('grant_missing')
    const grant = this.instanceGrant(editor.instance_id, editor.base_revision, editor.base_fingerprint)
    const instance = detail.data.instance
    const request = buildEmployeeAssetRequest({ ...this.nextInput('employee_instance_model_candidates'),
      action: 'employee_instance_model_candidates', instance_id: editor.instance_id, expected_revision: editor.base_revision,
      expected_fingerprint: editor.base_fingerprint, grant: { ...grant, package_id: string(instance.package_id),
        package_version: string(instance.package_version), package_fingerprint: string(instance.package_fingerprint) } })
    this.clearInstanceModelCandidates()
    return this.start(request, 'model_candidates')
  }
  clearInstanceModelCandidates(): void {
    this.checkUsable()
    const projectId = this.snapshot.project_id, view = projectId ? this.snapshot.projects[projectId]?.model_candidates : undefined
    if (!projectId || !view || view.state === 'not_loaded') return
    if (view.request) {
      const pending = this.requests.get(view.request.request_id)
      if (pending) { this.stopTimer(pending); pending.pending = false }
    }
    this.updateProject(projectId, { model_candidates: emptyView() })
  }
  /** Current correlated read only. Never rebase a dirty choice to another request. */
  instanceModelCandidates(): EmployeeInstanceModelSnapshot | null {
    const projectId = this.snapshot.project_id, project = projectId ? this.snapshot.projects[projectId] : undefined
    const editor = project?.instance_editor, view = project?.model_candidates, detail = project?.detail
    if (!this.snapshot.connected || !editor || view?.state !== 'ready' || !view.data || !view.request
      || view.request.action !== 'employee_instance_model_candidates' || view.request.instance_id !== editor.instance_id
      || detail?.state !== 'ready' || detail.request?.action !== 'employee_instance_detail'
      || detail.request.instance_id !== editor.instance_id || detail.data?.revision !== editor.base_revision
      || detail.data.fingerprint !== editor.base_fingerprint || !row(detail.data.instance)
      || view.data.instance_id !== editor.instance_id || view.data.instance_revision !== editor.base_revision
      || view.data.revision_fingerprint !== editor.base_fingerprint
      || view.data.package_id !== detail.data.instance.package_id || view.data.package_version !== detail.data.instance.package_version
      || view.data.package_fingerprint !== detail.data.instance.package_fingerprint) return null
    return employeeInstanceModelCandidates({ request_id: view.request.request_id, projection_version: view.data.projection_version,
      model_catalogue_revision: view.data.model_catalogue_revision, candidates: view.data.candidates })
  }
  updateInstanceModelConfig(value: EmployeeInstanceModelEdit): void {
    const projectId = this.projectId(), editor = this.snapshot.projects[projectId]?.instance_editor
    if (!editor) throw new EmployeeAssetAdapterError('request_invalid')
    const model = copyEmployeeInstanceModelEdit(value), directory = this.instanceModelCandidates()
    // A changed setting needs the current related candidate; unchanged legacy state stays untouched.
    employeeInstanceModelConfigChanges(model, editor.model_baseline, directory)
    this.updateProject(projectId, { instance_editor: Object.freeze({ ...editor, model, dirty: instanceEditorDirty(editor, model) }) })
  }
  resetInstanceModelConfig(): void {
    const projectId = this.projectId(), editor = this.snapshot.projects[projectId]?.instance_editor
    if (!editor) throw new EmployeeAssetAdapterError('request_invalid')
    const model = employeeInstanceModelEdit(editor.model_baseline)
    this.updateProject(projectId, { instance_editor: Object.freeze({ ...editor, model, dirty: instanceEditorDirty(editor, model) }) })
  }
  saveInstanceModelEditor(): string {
    this.checkWrite()
    const editor = this.snapshot.projects[this.projectId()]?.instance_editor, directory = this.instanceModelCandidates()
    if (!editor || !directory || sameEmployeeInstanceModelConfig(editor.model.config, editor.model_baseline)) throw new EmployeeAssetAdapterError('grant_missing')
    const model_config = employeeInstanceModelConfigValue(editor.model, directory)
    const request = buildEmployeeAssetRequest({ ...this.nextInput('employee_instance_revise'), action: 'employee_instance_revise',
      instance_id: editor.instance_id, changes: { model_config }, expected_revision: editor.base_revision,
      expected_instance_fingerprint: editor.base_fingerprint, expected_model_catalogue_revision: directory.model_catalogue_revision,
      note: editor.note, grant: this.instanceGrant(editor.instance_id, editor.base_revision, editor.base_fingerprint) })
    return this.start(request, null)
  }
  closeInstanceEditor(discardLocalChanges = false): void {
    const projectId = this.projectId()
    if (this.snapshot.projects[projectId]?.instance_editor?.dirty && !discardLocalChanges) throw new EmployeeAssetAdapterError('grant_missing')
    this.clearInstanceModelCandidates()
    this.updateProject(projectId, { instance_editor: null })
  }
  /** Explicit original-request check. Binding is lookup-only; legacy draft recovery may finish its transaction. */
  recoverWrite(requestId: string): void {
    this.projectId()
    const pending = this.requests.get(requestId)
    const write = this.snapshot.writes.find(item => item.request.request_id === requestId)
    if (!pending || !write || !pending.request.write || pending.pending
      || !['unknown', 'partial'].includes(write.receipt.state)) throw new EmployeeAssetAdapterError('request_invalid')
    pending.recovery = true
    if ('effects_produced' in write.receipt) pending.lastUncertain = write.receipt as EmployeeAssetReceipt
    this.publish({ ...this.snapshot, writes: this.snapshot.writes.map(item => item === write
      ? Object.freeze({ ...item, receipt: Object.freeze({ ...write.receipt, state: 'pending',
        message: pending.request.action === 'employee_binding_resolve_set' || modelRequest(pending.request) ? '正在只读核实原选择请求；未完成保存不会被补写。'
          : '正在核实并恢复原保存；此操作可能完成原事务。' }) }) : item) })
    this.sendPending(pending)
  }
  /** A malformed external ack can never interrupt the shared WS message handler. */
  receive(payloadOrAck: unknown): boolean {
    if (this.knowledge.receive(payloadOrAck)) return true
    if (this.instanceAdoption.receive(payloadOrAck)) return true
    if (this.lifecycle.receive(payloadOrAck)) return true
    try { return this.receiveChecked(payloadOrAck) } catch { return false }
  }
  private receiveChecked(payloadOrAck: unknown): boolean {
    if (!row(payloadOrAck)) return false
    const payload = payloadOrAck.type === 'ack' && row(payloadOrAck.payload) ? payloadOrAck.payload : payloadOrAck
    if (typeof payload.request_id !== 'string') return false
    const pending = this.requests.get(payload.request_id)
    if (!pending) return false
    if (['binding', 'binding_candidates', 'adoption', 'adoption_target', 'model_candidates'].includes(pending.slot ?? '') && this.snapshot.project_id !== pending.request.project_id) return false
    if (pending.slot === 'model_candidates') {
      const project = this.snapshot.projects[pending.request.project_id], editor = project?.instance_editor, detail = project?.detail
      const observed = pending.request.model_scope
      if (!this.snapshot.connected || !editor || !observed || editor.instance_id !== pending.request.instance_id
        || editor.base_revision !== observed.instance_revision || editor.base_fingerprint !== observed.revision_fingerprint
        || detail?.state !== 'ready' || detail.request?.action !== 'employee_instance_detail'
        || detail.request.instance_id !== editor.instance_id || detail.data?.revision !== editor.base_revision
        || detail.data.fingerprint !== editor.base_fingerprint || !row(detail.data.instance)
        || detail.data.instance.package_id !== observed.package_id || detail.data.instance.package_version !== observed.package_version
        || detail.data.instance.package_fingerprint !== observed.package_fingerprint) return false
    }
    if (!Array.isArray(payload.allowed_actions) || !payload.allowed_actions.every(value => typeof value === 'string')) return false
    if (!pending.pending) {
      const write = this.snapshot.writes.find(item => item.request.request_id === pending.request.request_id)
      // An expired/superseded read is dead. An unknown write can still receive
      // its original delayed ack; a terminal write cannot be changed by duplicates.
      if (!pending.request.write || !write || !['unknown', 'partial'].includes(write.receipt.state)) return false
    }
    const view = pending.slot ? this.snapshot.projects[pending.request.project_id]?.[pending.slot] : null
    const current = view?.request ? { project_id: view.request.project_id, key: view.request.key, request_id: view.request.request_id } : undefined
    // Keep the immutable original ledger request, but validate a lookup reply
    // against the check-only envelope that was actually sent. A refusal cannot
    // turn an earlier unknown write into proof of no effects.
    const receiptRequest = pending.recovery && modelRequest(pending.request)
      ? buildEmployeeInstanceModelRecoveryRequest(pending.request) : pending.request
    const decision = acceptEmployeeAssetReceipt(receiptRequest, { type: 'ack', payload }, current)
    if (decision.kind === 'ignored') return false
    this.stopTimer(pending); pending.pending = false
    let receipt = freezeReceipt(decision.receipt)
    if (pending.slot) {
      if (view?.request?.request_id !== pending.request.request_id) return false
      this.updateProject(pending.request.project_id, { [pending.slot]: Object.freeze({ state: receipt.state === 'succeeded' ? 'ready' : 'error',
        request: pending.request, data: pending.slot === 'adoption' || pending.slot === 'adoption_target' || pending.slot === 'binding_candidates' || pending.slot === 'model_candidates'
          ? receipt.data : receipt.data ?? view.data, receipt }) })
    } else {
      // A refusal during explicit replay does not prove the prior uncertain
      // transaction never persisted. Only a complete positive same-ID ack clears it.
      if (pending.recovery && receipt.state === 'failed') receipt = pending.lastUncertain ?? employeeAssetResponseLost(pending.request)
      if (receipt.state !== 'succeeded' && pending.lastUncertain?.effects_produced === true) {
        receipt = freezeReceipt({ ...receipt, state: 'partial', effects_produced: true, blocks_write: true })
      }
      this.publish({ ...this.snapshot, writes: this.snapshot.writes.map(item => item.request.request_id === pending.request.request_id
        ? Object.freeze({ request: item.request, receipt: Object.freeze(receipt) }) : item) })
      this.invalidateWriteReads(pending.request)
    }
    return true
  }
  markTimeout(requestId: string): void {
    const pending = this.requests.get(requestId)
    if (!pending?.pending) return
    this.stopTimer(pending); pending.pending = false
    const lost = employeeAssetResponseLost(pending.request)
    const receipt = freezeReceipt(pending.lastUncertain?.effects_produced === true
      ? { ...lost, state: 'partial' as const, effects_produced: true } : lost)
    if (pending.slot) {
      const view = this.snapshot.projects[pending.request.project_id]?.[pending.slot]
      if (view?.request?.request_id !== requestId) return
      this.updateProject(pending.request.project_id, { [pending.slot]: Object.freeze({ ...view,
        ...(pending.slot === 'adoption' || pending.slot === 'adoption_target' || pending.slot === 'binding_candidates' || pending.slot === 'model_candidates'
          ? { state: 'error' as const, data: null } : { state: view.data ? 'stale' as const : 'error' as const }), receipt }) })
    } else {
      this.publish({ ...this.snapshot, writes: this.snapshot.writes.map(item => item.request.request_id === requestId
        ? Object.freeze({ request: item.request, receipt: Object.freeze(receipt) }) : item) })
      this.invalidateWriteReads(pending.request)
    }
  }
  private invalidateDraftReads(packageId: string | null): void {
    const projects: Record<string, EmployeeAssetProjectState> = {}
    for (const [id, project] of Object.entries(this.snapshot.projects)) {
      const stale = (view: EmployeeAssetReadView): EmployeeAssetReadView => view.state === 'ready' ? Object.freeze({ ...view, state: 'stale' }) : view
      projects[id] = Object.freeze({ ...project, drafts: stale(project.drafts), detail: project.detail.request?.source === 'draft'
        && project.detail.request.package_id === packageId ? stale(project.detail) : project.detail })
    }
    this.publish({ ...this.snapshot, projects })
  }
  private invalidateWriteReads(request: EmployeeAssetRequest): void {
    if (request.action === 'employee_binding_resolve_set') {
      const project = this.snapshot.projects[request.project_id]
      if (project) this.updateProject(request.project_id, { binding: emptyView(), binding_candidates: emptyView() })
      // Expire both pending read generations too: a late pre-save read cannot
      // restore a stale table or grant after this write has settled/timed out.
      for (const pending of this.requests.values()) if (pending.request.project_id === request.project_id
        && (pending.slot === 'binding' || pending.slot === 'binding_candidates')) {
        this.stopTimer(pending); pending.pending = false
      }
      return
    }
    if (request.action !== 'employee_instance_revise') { this.invalidateDraftReads(request.package_id); return }
    const project = this.snapshot.projects[request.project_id]
    if (!project) return
    if (this.snapshot.project_id === request.project_id) this.clearAdoptionTarget()
    for (const pending of this.requests.values()) if (pending.request.project_id === request.project_id && pending.slot === 'model_candidates') {
      this.stopTimer(pending); pending.pending = false
    }
    const stale = (view: EmployeeAssetReadView): EmployeeAssetReadView => view.state === 'ready' ? Object.freeze({ ...view, state: 'stale' }) : view
    this.updateProject(request.project_id, { adoption_target: emptyView(), adoption: emptyView(), model_candidates: emptyView(), instances: stale(project.instances), detail: project.detail.request?.action === 'employee_instance_detail'
      && project.detail.request.instance_id === request.instance_id ? stale(project.detail) : project.detail })
  }
  /** App cleanup/StrictMode-safe: end waits, retain facts and allow later reconnect/load. */
  closeTimers(): void { this.knowledge.closeTimers(); this.instanceAdoption.closeTimers(); this.lifecycle.closeTimers(); for (const [id, pending] of this.requests) { if (pending.pending) this.markTimeout(id); else this.stopTimer(pending) } }
  dispose(): void { this.closeTimers(); this.unsubscribeKnowledge(); this.knowledge.dispose(); this.unsubscribeInstanceAdoption(); this.instanceAdoption.dispose(); this.unsubscribeLifecycle(); this.lifecycle.dispose(); this.listeners.clear(); this.disposed = true }

  openEditor(): void {
    const projectId = this.projectId()
    const project = this.snapshot.projects[projectId]
    const view = project?.detail
    if (view?.state !== 'ready' || view.request?.source !== 'draft' || !view.data || !row(view.data.package)) throw new EmployeeAssetAdapterError('grant_missing')
    const packageId = string(view.data.package.package_id)
    this.draftGrant(packageId, string(view.data.revision))
    if (project.editor?.dirty) throw new EmployeeAssetAdapterError('grant_missing')
    const fields = Object.freeze({ name: string(view.data.package.name), responsibility: string(view.data.package.responsibility), version: string(view.data.package.version) })
    const scopes = readEmployeeScopes(view.data.package)
    const artifact = readEmployeeArtifact(view.data.package)
    const human = readEmployeeHumanSlots(view.data.package)
    const loop = readEmployeeLoopPolicy(view.data.package)
    const ios = readEmployeeIOSchemas(view.data.package)
    assertEmployeeEditorBudget(fields, scopes, artifact, human, loop, ios)
    this.updateProject(projectId, { editor: Object.freeze({ package_id: packageId, base_revision: string(view.data.revision), fields, baseline: fields,
      scopes, scope_baseline: scopes, artifact, artifact_baseline: artifact, human, human_baseline: human,
      loop, loop_baseline: loop, ios, io_baseline: ios, dirty: false }) })
  }
  updateEditor(fields: EmployeeAssetEditFields): void {
    const projectId = this.projectId()
    const editor = this.snapshot.projects[projectId]?.editor
    if (!editor || Object.keys(fields).sort().join(',') !== 'name,responsibility,version') throw new EmployeeAssetAdapterError('request_invalid')
    const copied = Object.freeze({ ...fields })
    assertEmployeeEditorBudget(copied, editor.scopes, editor.artifact, editor.human, editor.loop, editor.ios)
    this.updateProject(projectId, { editor: Object.freeze({ ...editor, fields: copied,
      dirty: editorDirty(editor, copied) }) })
  }
  /** Project-bound controlled rows. Missing is never implicitly replaced with []. */
  updateScope(field: EmployeeScopeField, value: EmployeeScopeList): void {
    const projectId = this.projectId()
    const editor = this.snapshot.projects[projectId]?.editor
    if (!editor || (field !== 'scope_in' && field !== 'scope_out') || value?.kind !== 'present') throw new EmployeeAssetAdapterError('request_invalid')
    const copied = employeeScopeList(value.items)
    const scopes = Object.freeze({ ...editor.scopes, [field]: copied })
    assertEmployeeEditorBudget(editor.fields, scopes, editor.artifact, editor.human, editor.loop, editor.ios)
    this.updateProject(projectId, { editor: Object.freeze({ ...editor, scopes,
      dirty: editorDirty(editor, editor.fields, scopes) }) })
  }
  /** Reset is local only, restoring exact presence/order/text from the read baseline. */
  resetScope(field: EmployeeScopeField): void {
    const projectId = this.projectId()
    const editor = this.snapshot.projects[projectId]?.editor
    if (!editor || (field !== 'scope_in' && field !== 'scope_out')) throw new EmployeeAssetAdapterError('request_invalid')
    const scopes = Object.freeze({ ...editor.scopes, [field]: editor.scope_baseline[field] })
    assertEmployeeEditorBudget(editor.fields, scopes, editor.artifact, editor.human, editor.loop, editor.ios)
    this.updateProject(projectId, { editor: Object.freeze({ ...editor, scopes,
      dirty: editorDirty(editor, editor.fields, scopes) }) })
  }
  /** Explicit local replacement only. Missing/invalid originals are not coerced. */
  updateArtifact(value: EmployeeArtifactState): void {
    const projectId = this.projectId(), editor = this.snapshot.projects[projectId]?.editor
    if (!editor || value?.kind !== 'present') throw new EmployeeAssetAdapterError('request_invalid')
    const artifact = employeeArtifactState(value.deliverables, value.acceptance_criteria)
    assertEmployeeEditorBudget(editor.fields, editor.scopes, artifact, editor.human, editor.loop, editor.ios)
    this.updateProject(projectId, { editor: Object.freeze({ ...editor, artifact,
      dirty: editorDirty(editor, editor.fields, editor.scopes, artifact) }) })
  }
  /** Restore the OPENED baseline's exact three-state value, not a newer read. */
  resetArtifact(): void {
    const projectId = this.projectId(), editor = this.snapshot.projects[projectId]?.editor
    if (!editor) throw new EmployeeAssetAdapterError('request_invalid')
    const artifact = editor.artifact_baseline
    assertEmployeeEditorBudget(editor.fields, editor.scopes, artifact, editor.human, editor.loop, editor.ios)
    this.updateProject(projectId, { editor: Object.freeze({ ...editor, artifact,
      dirty: editorDirty(editor, editor.fields, editor.scopes, artifact) }) })
  }
  /** Explicit local replacement of template duties, never a personnel assignment. */
  updateHumanSlots(value: EmployeeHumanSlotsState): void {
    const projectId = this.projectId(), editor = this.snapshot.projects[projectId]?.editor
    if (!editor || value?.kind !== 'present') throw new EmployeeAssetAdapterError('request_invalid')
    const human = employeeHumanSlotsState(value.items)
    assertEmployeeEditorBudget(editor.fields, editor.scopes, editor.artifact, human, editor.loop, editor.ios)
    this.updateProject(projectId, { editor: Object.freeze({ ...editor, human,
      dirty: editorDirty(editor, editor.fields, editor.scopes, editor.artifact, human) }) })
  }
  /** Restore the OPENED human slots three-state baseline, not a newer read. */
  resetHumanSlots(): void {
    const projectId = this.projectId(), editor = this.snapshot.projects[projectId]?.editor
    if (!editor) throw new EmployeeAssetAdapterError('request_invalid')
    const human = editor.human_baseline
    assertEmployeeEditorBudget(editor.fields, editor.scopes, editor.artifact, human, editor.loop, editor.ios)
    this.updateProject(projectId, { editor: Object.freeze({ ...editor, human,
      dirty: editorDirty(editor, editor.fields, editor.scopes, editor.artifact, human) }) })
  }
  /** Bounded local input text; incomplete draft limits are not runtime grants. */
  updateLoopPolicy(value: EmployeeLoopPolicyState): void {
    const projectId = this.projectId(), editor = this.snapshot.projects[projectId]?.editor
    if (!editor || value?.kind !== 'present') throw new EmployeeAssetAdapterError('request_invalid')
    const loop = employeeLoopPolicyState(value.fields)
    assertEmployeeEditorBudget(editor.fields, editor.scopes, editor.artifact, editor.human, loop, editor.ios)
    this.updateProject(projectId, { editor: Object.freeze({ ...editor, loop,
      dirty: editorDirty(editor, editor.fields, editor.scopes, editor.artifact, editor.human, loop) }) })
  }
  /** Restore exact opened presence and text, never a newer read or save ACK. */
  resetLoopPolicy(): void {
    const projectId = this.projectId(), editor = this.snapshot.projects[projectId]?.editor
    if (!editor) throw new EmployeeAssetAdapterError('request_invalid')
    const loop = editor.loop_baseline
    assertEmployeeEditorBudget(editor.fields, editor.scopes, editor.artifact, editor.human, loop, editor.ios)
    this.updateProject(projectId, { editor: Object.freeze({ ...editor, loop,
      dirty: editorDirty(editor, editor.fields, editor.scopes, editor.artifact, editor.human, loop) }) })
  }
  /** Preserve bounded invalid JSON locally for repair; missing is not a deletion intent. */
  updateIOSchema(field: EmployeeIOSchemaField, value: EmployeeIOSchemaState): void {
    const projectId = this.projectId(), editor = this.snapshot.projects[projectId]?.editor
    if (!editor || (field !== 'input_schema' && field !== 'output_schema')) throw new EmployeeAssetAdapterError('request_invalid')
    const ios = copyEmployeeIOSchemas({ ...editor.ios, [field]: value })
    if (ios[field].kind === 'missing') throw new EmployeeAssetAdapterError('request_invalid')
    assertEmployeeEditorBudget(editor.fields, editor.scopes, editor.artifact, editor.human, editor.loop, ios)
    this.updateProject(projectId, { editor: Object.freeze({ ...editor, ios,
      dirty: editorDirty(editor, editor.fields, editor.scopes, editor.artifact, editor.human, editor.loop, ios) }) })
  }
  /** Restore exactly the OPENED field's presence/text, never a newer read or ACK. */
  resetIOSchema(field: EmployeeIOSchemaField): void {
    const projectId = this.projectId(), editor = this.snapshot.projects[projectId]?.editor
    if (!editor || (field !== 'input_schema' && field !== 'output_schema')) throw new EmployeeAssetAdapterError('request_invalid')
    const ios = Object.freeze({ ...editor.ios, [field]: editor.io_baseline[field] })
    assertEmployeeEditorBudget(editor.fields, editor.scopes, editor.artifact, editor.human, editor.loop, ios)
    this.updateProject(projectId, { editor: Object.freeze({ ...editor, ios,
      dirty: editorDirty(editor, editor.fields, editor.scopes, editor.artifact, editor.human, editor.loop, ios) }) })
  }
  saveEditor(): string {
    const editor = this.snapshot.projects[this.projectId()]?.editor
    if (!editor || !editor.dirty) throw new EmployeeAssetAdapterError('request_invalid')
    const changes = employeeDraftChanges(editor.fields, editor.baseline, editor.scopes, editor.scope_baseline,
      editor.artifact, editor.artifact_baseline, editor.human, editor.human_baseline, editor.loop, editor.loop_baseline,
      editor.ios, editor.io_baseline)
    return this.saveDraft(editor.package_id, changes, editor.base_revision)
  }
  /** Explicitly reload a clean form or discard local changes; never automatic on an ack/read. */
  closeEditor(discardLocalChanges = false): void {
    const projectId = this.projectId()
    if (this.snapshot.projects[projectId]?.editor?.dirty && !discardLocalChanges) throw new EmployeeAssetAdapterError('grant_missing')
    this.updateProject(projectId, { editor: null })
  }
  /** Unsaved create input is project-bound, App-owned and protected from LRU eviction. */
  updateCreation(fields: EmployeeAssetCreationFields): void {
    const projectId = this.projectId()
    if (Object.keys(fields).sort().join(',') !== 'name,package_id,responsibility,version'
      || Object.values(fields).some(value => typeof value !== 'string' || value.length > 4096)) throw new EmployeeAssetAdapterError('request_invalid')
    this.updateProject(projectId, { creation: Object.freeze({ ...fields }) })
  }
  clearCreation(): void { this.updateProject(this.projectId(), { creation: emptyCreation() }) }
}


