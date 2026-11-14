/** Pure host/leaf adapter for usable AE01 asset actions, including narrow instance rename.
 * No socket, React state, clock, UUID, retries, sample assets or permissions live here.
 * Server authentication/authorization remains mandatory; UI grants are not authority.
 */
import type { AIEmployeeEditorDraft, AIEmployeeEditorRequest } from '../components/AIEmployeeEditor'
import type { AIEmployeeInstanceView, AIEmployeePackageView } from '../components/aiEmployees/AIEmployeeLibrary'
import type { AIEmployeeProjectView, EmployeeOperationReceipt } from '../components/aiEmployees/AIEmployeeProjectPanel'
import type { AIEmployeeProjection } from '../components/aiEmployees/AIEmployeeProjectionBoundary'
import { validateEmployeeInstanceLimits } from './aiEmployeeInstanceLimits'
import {
  employeeInstanceModelCandidates, employeeInstanceModelConfigState, sameEmployeeInstanceModelConfig,
  type EmployeeInstanceModelFields,
} from './aiEmployeeInstanceModelConfig'

export const EMPLOYEE_ASSET_ACTIONS = [
  'employee_package_list', 'employee_package_detail', 'employee_package_draft_create',
  'employee_package_draft_save', 'employee_instance_list', 'employee_instance_detail',
  'employee_instance_revise',
  'employee_binding_preview',
  'employee_binding_candidates', 'employee_binding_resolve_set',
  'employee_instance_adoption_preview',
  'employee_instance_model_candidates',
] as const
export type EmployeeAssetAction = typeof EMPLOYEE_ASSET_ACTIONS[number]
export type PackageSource = 'published' | 'draft'
type JSONValue = null | boolean | number | string | JSONValue[] | { [key: string]: JSONValue }
type JSONObject = { [key: string]: JSONValue }
type Row = Record<string, unknown>
const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/
const PACKAGE = /^[a-z0-9][a-z0-9_-]{2,63}$/
const INSTANCE = /^ei-[a-f0-9]{16}$/
const SHA = /^[a-f0-9]{64}$/
const MANAGED = new Set(['package_fingerprint', 'draft_revision', 'status', 'created_at', 'created_by',
  'published_at', 'published_by', 'retired_at', 'retired_by', 'retired_reason', 'release_approval',
  'updated_at', 'updated_by', 'latest_test', 'evaluation_results', 'evaluation_records', 'evaluations',
  'test_results', 'test_passed', 'pending_review'])

export class EmployeeAssetAdapterError extends Error {
  readonly code: 'request_invalid' | 'unsupported_mapping' | 'grant_missing' | 'receipt_invalid'
  constructor(code: EmployeeAssetAdapterError['code']) {
    super(code === 'unsupported_mapping' ? '这些编辑字段尚无无损的服务端映射。'
      : code === 'grant_missing' ? '此目标尚无服务端操作许可。'
        : code === 'receipt_invalid' ? '员工回执尚不能核实。' : '员工请求字段无效。')
    this.name = 'EmployeeAssetAdapterError'
    this.code = code
  }
}
const fail = (code: EmployeeAssetAdapterError['code'] = 'request_invalid'): never => { throw new EmployeeAssetAdapterError(code) }
const row = (value: unknown): value is Row => value !== null && typeof value === 'object' && !Array.isArray(value)
const valid = (value: unknown, pattern: RegExp): value is string => typeof value === 'string' && pattern.test(value)
// Explicit full-match guard for the new selection contract.
const bindingValid = (value: unknown, pattern: RegExp): value is string => typeof value === 'string' && pattern.exec(value)?.[0] === value
const version = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 128
  && value.trim() === value && !/[\u0000-\u001f\u007f]/.test(value)
const strings = (value: unknown): string[] => Array.isArray(value) && value.every(v => typeof v === 'string') ? [...value] : []
const text = (value: unknown): string => typeof value === 'string' ? value : ''
const positiveRevision = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0

/** Reject oversized/cyclic/non-JSON input before serializing; preserve JSON keys without prototype setters. */
function jsonCopy(value: unknown): JSONValue {
  let nodes = 0
  let characters = 0
  const ancestors = new Set<object>()
  const charge = (value: string): void => {
    characters += value.length
    if (characters > 2 * 1024 * 1024) fail()
  }
  function copy(item: unknown, depth: number): JSONValue {
    if (++nodes > 50_000 || depth > 64) return fail()
    if (typeof item === 'string') { charge(item); return item }
    if (item === null || typeof item === 'boolean') return item
    if (typeof item === 'number') return Number.isFinite(item) ? item : fail()
    if (!row(item) && !Array.isArray(item)) return fail()
    const object = item as object
    if (ancestors.has(object)) return fail()
    const prototype = Object.getPrototypeOf(object)
    if (!Array.isArray(item) && prototype !== Object.prototype && prototype !== null) return fail()
    ancestors.add(object)
    let result: JSONValue
    if (Array.isArray(item)) {
      if (item.length > 50_000) return fail()
      if (Object.keys(item).length !== item.length) return fail()
      result = []
      for (let index = 0; index < item.length; index++) {
        const descriptor = Object.getOwnPropertyDescriptor(item, String(index))
        if (!descriptor || !('value' in descriptor)) return fail()
        result.push(copy(descriptor.value, depth + 1))
      }
    } else {
      const keys = Object.keys(item)
      if (keys.length > 50_000) return fail()
      const output: JSONObject = {}
      for (const key of keys) {
        charge(key)
        const descriptor = Object.getOwnPropertyDescriptor(item, key)
        if (!descriptor || !('value' in descriptor)) return fail()
        Object.defineProperty(output, key, { value: copy(descriptor.value, depth + 1), enumerable: true, writable: true, configurable: true })
      }
      result = output
    }
    ancestors.delete(object)
    return result
  }
  const result = copy(value, 0)
  if (new TextEncoder().encode(JSON.stringify(result)).byteLength > 2 * 1024 * 1024) return fail()
  return result
}
function objectCopy(value: unknown): JSONObject {
  const copied = jsonCopy(value)
  return row(copied) ? copied as JSONObject : fail()
}
function freezeJSON(value: JSONValue): JSONValue {
  if (value !== null && typeof value === 'object') {
    for (const child of Object.values(value)) freezeJSON(child)
    Object.freeze(value)
  }
  return value
}

export interface MutationGrant { project_id: string; allowed_actions: readonly string[] }
export interface DraftGrant extends MutationGrant {
  source: 'draft'; package_id: string; revision: string; status: 'draft' | 'testing'
}
export interface InstanceGrant extends MutationGrant {
  instance_id: string; revision: number; fingerprint: string
}
/** Opened fixed-package scope, never a backend authorization substitute. */
export interface InstanceModelCandidatesGrant extends InstanceGrant {
  package_id: string; package_version: string; package_fingerprint: string
}
export interface EmployeeInstanceModelRequestScope {
  readonly instance_revision: number; readonly revision_fingerprint: string
  readonly package_id: string; readonly package_version: string; readonly package_fingerprint: string
}
export interface BindingSelectionGrant extends MutationGrant {
  workflow_id: string; node_id: string; workflow_revision: string; binding_slot: 'executor'
  binding_revision: number; instance_id: string; instance_revision: number; revision_fingerprint: string
}
export interface AdoptionComparisonGrant extends InstanceGrant {
  package_id: string; package_version: string; package_fingerprint: string
}
export interface EmployeeAdoptionTarget { package_id: string; package_version: string; expected_fingerprint: string }
export const EMPLOYEE_ADOPTION_SECTIONS = ['capability_scope', 'methods', 'tools', 'models', 'knowledge_protocol', 'budget', 'verification_scope'] as const
export type EmployeeAdoptionSection = typeof EMPLOYEE_ADOPTION_SECTIONS[number]
const ADOPTION_GAPS = new Set(['adoption_write_unavailable', 'evaluation_source_unverified', 'publication_index_unverified', 'package_retired', 'private_configuration_incompatible'])
const ADOPTION_KEYS: Record<EmployeeAdoptionSection, Record<string, 'text' | 'object' | 'strings' | 'objects'>> = {
  capability_scope: { scope_in: 'strings', scope_out: 'strings' },
  methods: { responsibility: 'text', input_schema: 'object', output_schema: 'object', artifact_contract: 'object', checklist: 'objects',
    skills: 'objects', prompt_refs: 'strings', template_refs: 'objects', human_slots: 'objects' },
  tools: { capability_requirements: 'objects' }, models: { model_profile: 'object' }, knowledge_protocol: { knowledge_protocol: 'object' },
  budget: { loop_policy: 'object', evaluation_budget: 'object' },
  verification_scope: { evaluation_profile: 'object', declared_verified_models: 'strings' },
}
type Common = { project_id: string; request_id: string }
export type EmployeeAssetRequestInput = Common & (
  | { action: 'employee_package_list'; source?: PackageSource }
  | { action: 'employee_package_detail'; source?: PackageSource; package_id: string; version?: string }
  | { action: 'employee_package_draft_create'; manifest: unknown; grant: MutationGrant; template_id?: string; expected_template_revision?: string }
  | { action: 'employee_package_draft_save'; package_id: string; expected_revision: string; changes: unknown; grant: DraftGrant }
  | { action: 'employee_instance_list' }
  | { action: 'employee_instance_detail'; instance_id: string }
  | { action: 'employee_instance_model_candidates'; instance_id: string; expected_revision: number;
      expected_fingerprint: string; grant: InstanceModelCandidatesGrant }
  | { action: 'employee_instance_adoption_preview'; instance_id: string; expected_revision: number;
      package_id: string; package_version: string; expected_fingerprint: string; grant: AdoptionComparisonGrant }
  | { action: 'employee_binding_preview'; workflow_id: string; node_id: string;
      expected_workflow_revision: string; binding_slot?: 'executor' }
  | { action: 'employee_binding_candidates'; workflow_id: string; node_id: string;
      expected_workflow_revision: string; binding_slot?: 'executor' }
  | { action: 'employee_binding_resolve_set'; workflow_id: string; node_id: string; binding_slot: 'executor';
      expected_workflow_revision: string; instance_id: string; expected_binding_revision: number;
      expected_instance_revision: number; expected_instance_fingerprint: string; grant: BindingSelectionGrant }
  | { action: 'employee_instance_revise'; instance_id: string; expected_revision: number;
      changes: unknown; note?: string; grant: InstanceGrant; expected_instance_fingerprint?: string;
      expected_model_catalogue_revision?: string; check_only?: boolean }
)
export interface EmployeeAssetRequest {
  readonly action: EmployeeAssetAction
  readonly project_id: string
  readonly request_id: string
  readonly source: PackageSource | 'project' | 'workflow'
  readonly package_id: string | null
  readonly instance_id: string | null
  readonly version: string | null
  readonly workflow_id: string | null
  readonly node_id: string | null
  readonly workflow_revision: string | null
  readonly binding_slot: 'executor' | null
  readonly adoption_revision: number | null
  readonly adoption_instance_fingerprint: string | null
  readonly target_fingerprint: string | null
  readonly from_version: string | null
  readonly from_fingerprint: string | null
  readonly key: string
  readonly write: boolean
  readonly message: Readonly<JSONObject>
  /** Present only for a model-candidate read; old requests gain no new runtime member. */
  readonly model_scope?: EmployeeInstanceModelRequestScope
}

function checkGrant(input: Common, action: string, grant: MutationGrant): void {
  if (!grant || grant.project_id !== input.project_id || !Array.isArray(grant.allowed_actions)
    || !grant.allowed_actions.includes(action)) fail('grant_missing')
}

/** The host supplies a fresh opaque ID even for reads. No implicit active/default project. */
export function buildEmployeeAssetRequest(input: EmployeeAssetRequestInput): EmployeeAssetRequest {
  const modelKind = instanceModelInputKind(input)
  if (modelKind !== null) return buildInstanceModelRequest(input, modelKind)
  if (!valid(input.project_id, ID) || !valid(input.request_id, ID)
    || !(EMPLOYEE_ASSET_ACTIONS as readonly string[]).includes(input.action)) return fail()
  const action = input.action
  if (action === 'employee_binding_candidates' || action === 'employee_binding_resolve_set') {
    if (!bindingValid(input.project_id, ID) || !bindingValid(input.request_id, ID)
      || !bindingValid(input.workflow_id, ID) || !bindingValid(input.node_id, ID)
      || !bindingValid(input.expected_workflow_revision, SHA)) return fail()
    if (input.action === 'employee_binding_resolve_set'
      && (!bindingValid(input.instance_id, INSTANCE) || !bindingValid(input.expected_instance_fingerprint, SHA))) return fail()
  }
  const write = action === 'employee_package_draft_create' || action === 'employee_package_draft_save' || action === 'employee_instance_revise'
    || action === 'employee_binding_resolve_set'
  let source: EmployeeAssetRequest['source'] = action.startsWith('employee_instance_') ? 'project' : 'published'
  let packageId: string | null = null
  let instanceId: string | null = null
  let selectedVersion: string | null = null
  let workflowId: string | null = null
  let nodeId: string | null = null
  let workflowRevision: string | null = null
  let bindingSlot: 'executor' | null = null
  let adoptionRevision: number | null = null, targetFingerprint: string | null = null, adoptionInstanceFingerprint: string | null = null
  let fromVersion: string | null = null, fromFingerprint: string | null = null
  const message: JSONObject = { type: action, project_id: input.project_id, request_id: input.request_id }
  switch (input.action) {
    case 'employee_instance_model_candidates': return fail() // A non-data action cannot enter the new contract.
    case 'employee_package_list':
    case 'employee_package_detail':
      source = input.source ?? 'published'
      if (source !== 'draft' && source !== 'published') return fail()
      if (source === 'draft') message.source = 'draft'
      if (input.action === 'employee_package_detail') {
        if (!valid(input.package_id, PACKAGE)) return fail()
        packageId = input.package_id
        message.package_id = packageId
        if (input.version !== undefined) {
          if (!version(input.version)) return fail()
          selectedVersion = input.version
          message.version = selectedVersion
        }
      }
      break
    case 'employee_package_draft_create': {
      source = 'draft'
      checkGrant(input, action, input.grant)
      const manifest = objectCopy(input.manifest)
      if (!valid(manifest.package_id, PACKAGE) || Object.keys(manifest).some(key => MANAGED.has(key))) return fail()
      packageId = manifest.package_id
      message.manifest = manifest
      if (input.template_id !== undefined || input.expected_template_revision !== undefined) {
        if (!valid(input.template_id, PACKAGE) || !valid(input.expected_template_revision, SHA)
          || Object.keys(manifest).sort().join(',') !== 'max_cost_cny,name,package_id'
          || typeof manifest.name !== 'string' || !manifest.name.trim() || manifest.name.length > 128
          || typeof manifest.max_cost_cny !== 'string' || !/^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?$/.test(manifest.max_cost_cny)) return fail()
        message.template_id = input.template_id
        message.expected_template_revision = input.expected_template_revision
      }
      break
    }
    case 'employee_package_draft_save': {
      source = 'draft'
      checkGrant(input, action, input.grant)
      if (!valid(input.package_id, PACKAGE) || !valid(input.expected_revision, SHA)
        || input.grant.source !== 'draft' || input.grant.package_id !== input.package_id
        || input.grant.revision !== input.expected_revision || !['draft', 'testing'].includes(input.grant.status)) return fail('grant_missing')
      const changes = objectCopy(input.changes)
      if (Object.keys(changes).some(key => MANAGED.has(key) || key === 'package_id' || key === 'schema_version')) return fail()
      packageId = input.package_id
      message.package_id = packageId
      message.expected_revision = input.expected_revision
      message.changes = changes
      break
    }
    case 'employee_instance_list': break
    case 'employee_instance_detail':
      if (!valid(input.instance_id, INSTANCE)) return fail()
      instanceId = input.instance_id
      message.instance_id = instanceId
      break
    case 'employee_instance_adoption_preview':
      checkGrant(input, action, input.grant)
      if (!valid(input.instance_id, INSTANCE) || !positiveRevision(input.expected_revision)
        || !valid(input.package_id, PACKAGE) || !version(input.package_version) || !valid(input.expected_fingerprint, SHA)
        || input.grant.instance_id !== input.instance_id || input.grant.revision !== input.expected_revision
        || !valid(input.grant.fingerprint, SHA) || input.grant.package_id !== input.package_id
        || !version(input.grant.package_version) || !valid(input.grant.package_fingerprint, SHA)
        || Object.keys(input).some(key => !['action', 'project_id', 'request_id', 'instance_id', 'expected_revision', 'package_id', 'package_version', 'expected_fingerprint', 'grant'].includes(key))) return fail()
      instanceId = input.instance_id; packageId = input.package_id; selectedVersion = input.package_version
      adoptionRevision = input.expected_revision; targetFingerprint = input.expected_fingerprint
      adoptionInstanceFingerprint = input.grant.fingerprint
      fromVersion = input.grant.package_version; fromFingerprint = input.grant.package_fingerprint
      message.instance_id = instanceId; message.expected_revision = adoptionRevision; message.package_id = packageId
      message.package_version = selectedVersion; message.expected_fingerprint = targetFingerprint
      break
    case 'employee_binding_preview':
    case 'employee_binding_candidates':
      if (!valid(input.workflow_id, ID) || !valid(input.node_id, ID) || !valid(input.expected_workflow_revision, SHA)
        || (input.binding_slot !== undefined && input.binding_slot !== 'executor')
        || Object.keys(input).some(key => !['action', 'project_id', 'request_id', 'workflow_id', 'node_id', 'expected_workflow_revision', 'binding_slot'].includes(key))) return fail()
      source = 'workflow'
      workflowId = input.workflow_id; nodeId = input.node_id; workflowRevision = input.expected_workflow_revision
      bindingSlot = input.binding_slot ?? 'executor'
      message.workflow_id = workflowId; message.node_id = nodeId
      message.expected_workflow_revision = workflowRevision; message.binding_slot = bindingSlot
      break
    case 'employee_binding_resolve_set': {
      checkGrant(input, action, input.grant)
      if (!valid(input.workflow_id, ID) || !valid(input.node_id, ID) || !valid(input.expected_workflow_revision, SHA)
        || input.binding_slot !== 'executor' || !valid(input.instance_id, INSTANCE)
        || !Number.isSafeInteger(input.expected_binding_revision) || input.expected_binding_revision < 0
        || input.expected_binding_revision >= Number.MAX_SAFE_INTEGER || !positiveRevision(input.expected_instance_revision)
        || !valid(input.expected_instance_fingerprint, SHA)
        || input.grant.workflow_id !== input.workflow_id || input.grant.node_id !== input.node_id
        || input.grant.workflow_revision !== input.expected_workflow_revision || input.grant.binding_slot !== input.binding_slot
        || input.grant.binding_revision !== input.expected_binding_revision || input.grant.instance_id !== input.instance_id
        || input.grant.instance_revision !== input.expected_instance_revision || input.grant.revision_fingerprint !== input.expected_instance_fingerprint
        || Object.keys(input).some(key => !['action','project_id','request_id','workflow_id','node_id','binding_slot',
          'expected_workflow_revision','instance_id','expected_binding_revision','expected_instance_revision','expected_instance_fingerprint','grant'].includes(key))) return fail()
      source = 'workflow'; workflowId = input.workflow_id; nodeId = input.node_id
      workflowRevision = input.expected_workflow_revision; bindingSlot = input.binding_slot; instanceId = input.instance_id
      Object.assign(message, { workflow_id: workflowId, node_id: nodeId, binding_slot: bindingSlot,
        expected_workflow_revision: workflowRevision, instance_id: instanceId,
        expected_binding_revision: input.expected_binding_revision, expected_instance_revision: input.expected_instance_revision,
        expected_instance_fingerprint: input.expected_instance_fingerprint })
      break
    }
    case 'employee_instance_revise': {
      checkGrant(input, action, input.grant)
      if (!valid(input.instance_id, INSTANCE) || !positiveRevision(input.expected_revision)
        || input.expected_revision >= Number.MAX_SAFE_INTEGER || input.grant.instance_id !== input.instance_id
        || input.grant.revision !== input.expected_revision || !valid(input.grant.fingerprint, SHA)) return fail('grant_missing')
      const originalChanges = input.changes
      let changes: JSONObject
      try { changes = objectCopy(originalChanges) } catch (error) {
        let limitsInput = false
        try { limitsInput = Object.getOwnPropertyDescriptor(originalChanges as object, 'limits') !== undefined } catch { /* No inferred new branch. */ }
        if (limitsInput) return fail()
        throw error  // Preserve the original name-only copying behavior.
      }
      const copiedLimits = Object.hasOwn(changes, 'limits')
      let limitsDescriptor: PropertyDescriptor | undefined
      try { limitsDescriptor = Object.getOwnPropertyDescriptor(originalChanges as object, 'limits') } catch {
        if (copiedLimits) return fail()
      }
      if (copiedLimits || limitsDescriptor !== undefined) {
        if (Object.keys(changes).some(key => key !== 'limits')) return fail()
        try {
          // The shared JSON budget is checked first. Validate original own
          // properties too: JSON copying must not silently omit hidden extras.
          const keys = Reflect.ownKeys(originalChanges as object)
          if (keys.length !== 1 || keys[0] !== 'limits' || !limitsDescriptor || !Object.hasOwn(limitsDescriptor, 'value')) return fail()
          if (copiedLimits) validateEmployeeInstanceLimits(changes.limits)
          changes.limits = { ...validateEmployeeInstanceLimits(limitsDescriptor.value) }
        } catch { return fail() }
      } else if (Object.keys(changes).length !== 1 || !Object.hasOwn(changes, 'display_name')
        || typeof changes.display_name !== 'string' || !changes.display_name.trim() || changes.display_name.length > 256
        || /[\u0000-\u001f\u007f]/.test(changes.display_name)) return fail()
      const note = input.note ?? ''
      if (typeof note !== 'string' || note.length > 4000) return fail()
      instanceId = input.instance_id
      message.instance_id = instanceId
      message.expected_revision = input.expected_revision
      message.changes = changes
      message.note = note
      break
    }
  }
  return Object.freeze({ action, project_id: input.project_id, request_id: input.request_id, source,
    package_id: packageId, instance_id: instanceId, version: selectedVersion, write,
    workflow_id: workflowId, node_id: nodeId, workflow_revision: workflowRevision, binding_slot: bindingSlot,
    adoption_revision: adoptionRevision, adoption_instance_fingerprint: adoptionInstanceFingerprint,
    target_fingerprint: targetFingerprint, from_version: fromVersion, from_fingerprint: fromFingerprint,
    key: JSON.stringify([action, input.project_id, source, packageId, instanceId, selectedVersion, workflowId, nodeId, workflowRevision, bindingSlot,
      adoptionRevision, adoptionInstanceFingerprint, targetFingerprint, fromVersion, fromFingerprint,
      ...(action === 'employee_binding_resolve_set' ? [message.expected_binding_revision, message.expected_instance_revision, message.expected_instance_fingerprint] : [])]),
    message: freezeJSON(message) as Readonly<JSONObject> })
}

export interface ActiveEmployeeRead { project_id: string; key: string; request_id: string }
export interface EmployeeAssetReceipt extends EmployeeOperationReceipt {
  state: 'succeeded' | 'failed' | 'unknown' | 'partial'
  request_id: string
  action: EmployeeAssetAction
  project_id: string
  revision: string | number | null
  effects_produced: boolean | null
  preserve_draft: true
  requires_read: boolean
  blocks_write: boolean
  /** Present only for a validated, current read. Never replace a draft from a write ack. */
  data: Row | null
  projection: AIEmployeeProjection | null
}
export type EmployeeReceiptDecision = { kind: 'ignored'; reason: 'uncorrelated' | 'stale_read' }
  | { kind: 'matched'; receipt: EmployeeAssetReceipt }

const activeMatches = (request: EmployeeAssetRequest, active: ActiveEmployeeRead | undefined): boolean => !!active
  && active.project_id === request.project_id && active.key === request.key && active.request_id === request.request_id
function readShape(request: EmployeeAssetRequest, payload: Row): boolean {
  if (request.action === 'employee_instance_adoption_preview') return adoptionShape(request, payload)
  if (request.action === 'employee_binding_candidates') return bindingCandidatesShape(request, payload)
  if (request.action === 'employee_binding_preview') return payload.workflow_id === request.workflow_id
    && payload.node_id === request.node_id && payload.binding_slot === request.binding_slot
    && payload.workflow_revision === request.workflow_revision
    && ['distribution', 'runtime'].includes(text(payload.workflow_source))
    && (payload.workflow_version === null || typeof payload.workflow_version === 'string')
    && ['stored_mapping', 'single_candidate'].includes(text(payload.resolution_basis))
    && ['human_choice', 'single_candidate'].includes(text(payload.source))
    && typeof payload.binding_revision === 'number' && Number.isSafeInteger(payload.binding_revision) && payload.binding_revision >= 0
    && payload.revision === payload.binding_revision && valid(payload.instance_id, INSTANCE)
    && positiveRevision(payload.instance_revision) && valid(payload.revision_fingerprint, SHA)
    && payload.preview_scope === 'binding_resolution' && payload.effects_produced === false
    && typeof payload.ready === 'boolean' && Array.isArray(payload.gaps) && payload.gaps.length <= 500
    && payload.gaps.every(gap => typeof gap === 'string' && gap.length <= 16384)
    && row(payload.verified_scope) && row(payload.effective_limits)
  if (request.action === 'employee_package_list') return valid(payload.revision, SHA)
    && Array.isArray(payload.packages) && payload.packages.length <= 500
    && payload.packages.every(item => packageShape(item, request.source))
  if (request.action === 'employee_package_detail') return payload.package_id === request.package_id
    && packageShape(payload, request.source) && row(payload.package)
    && payload.package.package_id === request.package_id
    && (request.version === null || payload.package.version === request.version)
    && (request.target_fingerprint === null || payload.fingerprint === request.target_fingerprint)
    && (request.source !== 'published' || request.version !== null || payload.status === 'published')
  if (request.action === 'employee_instance_list') return valid(payload.revision, SHA)
    && Array.isArray(payload.instances) && payload.instances.length <= 500
    && payload.instances.every(item => instanceShape(item, request.project_id))
  return request.action === 'employee_instance_detail' && payload.instance_id === request.instance_id
    && instanceShape(payload, request.project_id) && row(payload.instance) && payload.instance.instance_id === request.instance_id
}
function bindingCandidatesShape(request: EmployeeAssetRequest, payload: Row): boolean {
  const allowed = new Set(['ok','action','project_id','request_id','revision','result','effects_produced','problems','allowed_actions',
    'workflow_id','node_id','binding_slot','workflow_revision','workflow_source','workflow_version','binding_revision',
    'selection_scope','candidates','selected_instance_id'])
  const fields = ['instance_id','display_name','package_id','package_version','package_fingerprint','instance_revision',
    'revision_fingerprint','intake_state','execution_state','ready','gaps'].sort().join(',')
  return Object.keys(payload).every(key => allowed.has(key)) && payload.workflow_id === request.workflow_id && payload.node_id === request.node_id
    && payload.binding_slot === request.binding_slot && payload.workflow_revision === request.workflow_revision
    && ['distribution','runtime'].includes(text(payload.workflow_source))
    && (payload.workflow_version === null || version(payload.workflow_version))
    && payload.selection_scope === 'saved_node_selection' && payload.effects_produced === false
    && typeof payload.binding_revision === 'number' && Number.isSafeInteger(payload.binding_revision) && payload.binding_revision >= 0
    && payload.revision === payload.binding_revision
    && (payload.selected_instance_id === null || bindingValid(payload.selected_instance_id, INSTANCE))
    && Array.isArray(payload.problems) && payload.problems.length === 0
    && Array.isArray(payload.candidates) && payload.candidates.length <= 500
    && (payload.selected_instance_id === null || payload.candidates.some(item => row(item) && item.instance_id === payload.selected_instance_id))
    && new Set(payload.candidates.filter(row).map(item => item.instance_id)).size === payload.candidates.length
    && payload.candidates.every(item => row(item) && Object.keys(item).sort().join(',') === fields
      && bindingValid(item.instance_id, INSTANCE) && typeof item.display_name === 'string'
      && item.display_name.length <= 256 && !/[\u0000-\u001f\u007f]/.test(item.display_name)
      && bindingValid(item.package_id, PACKAGE) && version(item.package_version) && bindingValid(item.package_fingerprint, SHA)
      && positiveRevision(item.instance_revision) && bindingValid(item.revision_fingerprint, SHA)
      && ['open','paused'].includes(text(item.intake_state)) && ['active','paused','disabled'].includes(text(item.execution_state))
      && typeof item.ready === 'boolean' && Array.isArray(item.gaps) && item.gaps.length <= 500
      && (!item.ready || (item.gaps.length === 0 && item.intake_state === 'open' && item.execution_state === 'active'))
      && new Set(item.gaps).size === item.gaps.length
      && item.gaps.every(gap => bindingValid(gap, /^[a-z][a-z0-9_]{0,127}$/)))
    && Array.isArray(payload.allowed_actions)
    && payload.allowed_actions.length <= 2 && new Set(payload.allowed_actions).size === payload.allowed_actions.length
    && payload.allowed_actions.every(action => action === 'employee_binding_candidates' || action === 'employee_binding_resolve_set')
}
function adoptionProjection(section: EmployeeAdoptionSection, value: unknown): boolean {
  if (!row(value)) return false
  const allowed = ADOPTION_KEYS[section]
  return Object.entries(value).every(([key, item]) => Object.hasOwn(allowed, key) && (
    allowed[key] === 'text' ? typeof item === 'string' : allowed[key] === 'object' ? row(item)
      : allowed[key] === 'strings' ? Array.isArray(item) && item.every(value => typeof value === 'string')
        : Array.isArray(item) && item.every(row)))
}
function adoptionShape(request: EmployeeAssetRequest, payload: Row): boolean {
  const allowed = new Set(['ok', 'action', 'project_id', 'request_id', 'instance_id', 'package_id', 'revision', 'fingerprint', 'result',
    'effects_produced', 'problems', 'allowed_actions', 'preview_scope', 'from_version', 'from_fingerprint', 'to_version', 'to_fingerprint',
    'target_status', 'is_current_verified', 'new_adoption_allowed', 'gaps', 'differences', 'verification_scope_kind', 'private_configuration_policy'])
  return Object.keys(payload).every(key => allowed.has(key)) && payload.instance_id === request.instance_id
    && payload.package_id === request.package_id && payload.revision === request.adoption_revision
    && payload.fingerprint === request.adoption_instance_fingerprint
    && payload.from_version === request.from_version && payload.from_fingerprint === request.from_fingerprint
    && payload.to_version === request.version && payload.to_fingerprint === request.target_fingerprint
    && payload.preview_scope === 'adoption_comparison' && payload.verification_scope_kind === 'declared_package_profile_not_trial_evidence'
    && payload.private_configuration_policy === 'preserved_not_projected' && typeof payload.new_adoption_allowed === 'boolean'
    && (payload.new_adoption_allowed === false || payload.target_status === 'published' && payload.is_current_verified === true)
    && ['published', 'retired'].includes(text(payload.target_status)) && typeof payload.is_current_verified === 'boolean'
    && Array.isArray(payload.gaps) && payload.gaps.length <= ADOPTION_GAPS.size && new Set(payload.gaps).size === payload.gaps.length
    && payload.gaps.every(gap => typeof gap === 'string' && ADOPTION_GAPS.has(gap))
    && payload.gaps.includes('evaluation_source_unverified')
    && payload.gaps.includes('publication_index_unverified') === (payload.is_current_verified === false)
    && payload.gaps.includes('package_retired') === (payload.target_status === 'retired')
    && Array.isArray(payload.allowed_actions) && payload.allowed_actions.every(action => action === 'employee_instance_detail' || action === 'employee_instance_adoption_preview')
    && Array.isArray(payload.differences) && payload.differences.length === EMPLOYEE_ADOPTION_SECTIONS.length
    && payload.differences.every((item, index) => row(item) && Object.keys(item).sort().join(',') === 'after,before,changed,section'
      && item.section === EMPLOYEE_ADOPTION_SECTIONS[index] && typeof item.changed === 'boolean'
      && adoptionProjection(EMPLOYEE_ADOPTION_SECTIONS[index], item.before) && adoptionProjection(EMPLOYEE_ADOPTION_SECTIONS[index], item.after))
}
/** Only an explicit observed published manifest is a candidate, never a latest fallback or an implicit grant. */
export function projectEmployeeAdoptionTargets(value: unknown, packageId: string): readonly EmployeeAdoptionTarget[] {
  if (!valid(packageId, PACKAGE) || !row(value) || !valid(value.revision, SHA) || !Array.isArray(value.packages) || value.packages.length > 500) return []
  const result: EmployeeAdoptionTarget[] = []
  for (const item of value.packages) {
    if (!packageShape(item, 'published') || item.status !== 'published' || !row(item.package)
      || item.package.status !== 'published' || item.package.package_id !== packageId) continue
    result.push(Object.freeze({ package_id: packageId, package_version: item.package.version as string, expected_fingerprint: item.fingerprint as string }))
  }
  // Duplicate public identity is ambiguous, not a reason to take the first row.
  return result.length === 1 ? Object.freeze(result) : []
}
function packageShape(value: unknown, source: EmployeeAssetRequest['source']): value is Row {
  if (!row(value) || !row(value.package) || !valid(value.package.package_id, PACKAGE)
    || !valid(value.revision, SHA) || !valid(value.fingerprint, SHA)
    || value.package.package_fingerprint !== value.fingerprint || value.effects_produced !== false) return false
  if (source === 'draft') return ['draft', 'testing'].includes(text(value.status))
    && value.package.status === value.status
  const effectiveStatus = row(value.publication) ? value.publication.status : value.status
  return ['published', 'retired'].includes(text(effectiveStatus)) && value.status === effectiveStatus
    && ['published', 'retired'].includes(text(value.package.status)) && value.package.package_fingerprint === value.fingerprint
    && version(value.package.version)
}
function instanceShape(value: unknown, projectId: string): value is Row {
  return row(value) && row(value.instance) && value.instance.project_id === projectId
    && valid(value.instance.instance_id, INSTANCE) && positiveRevision(value.revision)
    && value.instance.instance_revision === value.revision && valid(value.fingerprint, SHA)
    && value.instance.revision_fingerprint === value.fingerprint && value.effects_produced === false
}
function resultReceipt(request: EmployeeAssetRequest, state: EmployeeAssetReceipt['state'], revision: string | number | null,
  effects: boolean | null, data: Row | null = null): EmployeeAssetReceipt {
  return { state, action: request.action, project_id: request.project_id, request_id: request.request_id,
    revision, effects_produced: effects, preserve_draft: true,
    requires_read: request.write, blocks_write: state === 'unknown' || state === 'partial', data,
    message: state === 'succeeded' ? '服务端已确认完成。' : state === 'failed'
      ? request.action === 'employee_binding_preview' || request.action === 'employee_binding_candidates' ? '本次绑定读取尚不能核实；请核对权限与保存版本后重新读取。'
        : request.action === 'employee_instance_adoption_preview' ? '采用差异尚不能核实；请重新读取当前实例与明确目标版本。此请求不会采用或运行。' : '请求未完成；保留草稿并核对。'
      : '结果未完全确认；保留原请求与草稿，只读核对，不重发写入。',
    projection: data ? { connected: true, state: 'ready', source: 'server', requested_project_id: request.project_id,
      observed_project_id: request.project_id, requested_key: request.key, observed_key: request.key,
      requested_token: request.request_id, observed_token: request.request_id, revision: String(revision) } : null }
}

/** Exact request correlation comes before any generic requestTracker receipt settlement.
 * Null project only permits a correlated error to settle, never a read projection.
 * Late write receipts settle the ORIGINAL request, even after navigating to another view.
 */
export function acceptEmployeeAssetReceipt(request: EmployeeAssetRequest, envelope: unknown,
  active?: ActiveEmployeeRead): EmployeeReceiptDecision {
  if (request.action === 'employee_instance_model_candidates' || isInstanceModelWrite(request)) {
    return acceptInstanceModelReceipt(request, envelope, active)
  }
  if (!row(envelope) || envelope.type !== 'ack' || !row(envelope.payload)) return { kind: 'ignored', reason: 'uncorrelated' }
  const raw = envelope.payload
  if (raw.request_id !== request.request_id || raw.action !== request.action
    || (raw.project_id !== request.project_id && !(raw.project_id === null && raw.ok === false))) {
    return { kind: 'ignored', reason: 'uncorrelated' }
  }
  if (!request.write && !activeMatches(request, active)) return { kind: 'ignored', reason: 'stale_read' }
  // An unscoped refusal during recovery is not evidence about the original
  // write's durable outcome. It must not clear an existing unknown write latch.
  if (request.write && raw.project_id === null) return { kind: 'matched', receipt: resultReceipt(request, 'unknown', null, null) }
  const invalid = (): EmployeeReceiptDecision => ({ kind: 'matched', receipt: resultReceipt(request,
    request.write ? 'unknown' : 'failed', null, request.write ? null : false) })
  let payload: Row
  try { payload = objectCopy(raw) } catch { return invalid() }
  if (['employee_binding_preview','employee_binding_candidates','employee_binding_resolve_set'].includes(request.action)) {
    // A refusal may have no saved-source receipt. Any supplied scope must still
    // name THIS frozen read; it cannot settle a different workflow generation.
    const scope = { workflow_id: request.workflow_id, node_id: request.node_id,
      workflow_revision: request.workflow_revision, binding_slot: request.binding_slot }
    if (Object.entries(scope).some(([key, value]) => Object.hasOwn(payload, key) && payload[key] !== value)) {
      return { kind: 'ignored', reason: 'uncorrelated' }
    }
  }
  if (request.action === 'employee_instance_adoption_preview') {
    const scope = { instance_id: request.instance_id, package_id: request.package_id, to_version: request.version, to_fingerprint: request.target_fingerprint }
    if (Object.entries(scope).some(([key, value]) => Object.hasOwn(payload, key) && payload[key] !== value)) return { kind: 'ignored', reason: 'uncorrelated' }
  }
  if (typeof payload.ok !== 'boolean' || !Array.isArray(payload.allowed_actions) || !Array.isArray(payload.problems)) return invalid()
  const actualRevision = valid(payload.revision, SHA) || positiveRevision(payload.revision)
    || (['employee_binding_preview','employee_binding_candidates'].includes(request.action) && typeof payload.revision === 'number'
      && Number.isSafeInteger(payload.revision) && payload.revision >= 0) ? payload.revision : null
  const effects = typeof payload.effects_produced === 'boolean' ? payload.effects_produced : null
  if (payload.ok === true) {
    if (payload.project_id !== request.project_id || payload.result !== 'success' || actualRevision === null) return invalid()
    if (request.write) {
      if (typeof payload.effects_produced !== 'boolean') return invalid()
      if (request.action === 'employee_instance_revise') {
        const expected = request.message.expected_revision
        const limitsOnly = row(request.message.changes) && Object.hasOwn(request.message.changes, 'limits')
        const retest = payload.retest_scope
        if (payload.instance_id !== request.instance_id || !positiveRevision(actualRevision)
          || typeof expected !== 'number' || actualRevision !== expected + 1
          || !valid(payload.fingerprint, SHA) || !Array.isArray(retest)
          || (limitsOnly && effects !== true)
          || (limitsOnly ? !(retest.length === 0 || retest.length === 1 && typeof retest[0] === 'string' && retest[0] === 'limits')
            : retest.length !== 0)) return invalid()
      } else if (request.action === 'employee_binding_resolve_set') {
        const expected = request.message.expected_binding_revision
        if (payload.instance_id !== request.instance_id || payload.workflow_id !== request.workflow_id
          || payload.node_id !== request.node_id || payload.binding_slot !== request.binding_slot
          || payload.workflow_revision !== request.workflow_revision || payload.source !== 'human_choice'
          || payload.selection_scope !== 'saved_node_selection' || typeof expected !== 'number'
          || !positiveRevision(actualRevision) || actualRevision !== expected + 1 || payload.binding_revision !== actualRevision
          || payload.instance_revision !== request.message.expected_instance_revision
          || payload.revision_fingerprint !== request.message.expected_instance_fingerprint
          || !bindingValid(payload.revision_fingerprint, SHA)
          || effects !== true) return invalid()
      } else if (payload.package_id !== request.package_id || !valid(actualRevision, SHA)) return invalid()
      return { kind: 'matched', receipt: resultReceipt(request, 'succeeded', actualRevision, effects) }
    }
    if (effects !== false || !readShape(request, payload)) return invalid()
    return { kind: 'matched', receipt: resultReceipt(request, 'succeeded', actualRevision, false, freezeJSON(payload as JSONObject) as Row) }
  }
  if (!request.write) {
    const receipt = resultReceipt(request, 'failed', actualRevision, false)
    if (request.action === 'employee_instance_adoption_preview') {
      const messages: Record<string, string> = {
        revision_conflict: '实例已有新修订；请重新读取实例后再比较，不使用旧差异。',
        fingerprint_mismatch: '目标包指纹已不一致；请重新读取发布目录与明确版本。',
        instance_not_found: '当前项目实例尚不能核实；不会读取其他项目的实例。',
        owner_required: '当前账户没有这份采用差异的读取许可。',
        version_not_available: '目标固定包版本不可用；不会改用最新版。',
      }
      if (typeof payload.error === 'string' && Object.hasOwn(messages, payload.error)) receipt.message = messages[payload.error]
    }
    if (request.action === 'employee_binding_preview' || request.action === 'employee_binding_candidates') {
      // Only stable codes select our fixed copy. Never echo error/message/path.
      const messages: Record<string, string> = {
        workflow_revision_conflict: '流程已保存了新版本，请刷新流程清单后重新选择。',
        binding_unresolved: '这个保存节点没有可解析的项目员工；此入口不会创建或替你绑定。',
        multiple_instances_choice_required: '有多个候选，需要明确选择；此只读入口不会代选。',
        owner_required: '当前账户无权读取这份员工绑定，请核对账户权限。',
        candidate_read_limit: '候选员工资料超过本次读取范围，未返回不完整清单。请联系管理员处理读取范围。',
        candidate_projection_limit: '候选员工清单超过本次显示范围，未截短为部分结果。请联系管理员处理范围。',
      }
      if (typeof payload.error === 'string' && Object.hasOwn(messages, payload.error)) receipt.message = messages[payload.error]
    }
    return { kind: 'matched', receipt }
  }
  if (payload.result === 'outcome_unknown' || payload.result === 'partial' || effects !== false) {
    return { kind: 'matched', receipt: resultReceipt(request, effects === true || payload.result === 'partial' ? 'partial' : 'unknown', actualRevision, effects) }
  }
  if (!['failed', 'unsupported'].includes(text(payload.result))) return invalid()
  const receipt = resultReceipt(request, 'failed', actualRevision, false)
  if (request.action === 'employee_instance_revise' && payload.error === 'revision_conflict') {
    receipt.message = '实例已有新修订；保留修改后查看最新实例，不覆盖旧版本。'
  } else if (request.action === 'employee_instance_revise' && payload.error === 'not_allowed_in_status') {
    receipt.message = '本项目当前不允许修订实例；保留修改并核对项目状态。'
  }
  if (request.action === 'employee_binding_resolve_set') {
    const words: Record<string,string> = {
      binding_revision_conflict: '其他窗口已保存了新选择；请读取候选并重新明确选择，不覆盖对方。',
      revision_conflict: '所选员工或映射已有新修订；请重新读取并选择，不替你换用新修订。',
      workflow_revision_conflict: '流程保存版本已变化；请刷新流程清单后重新选择。',
      not_allowed_in_status: '当前项目不允许保存员工选择；保留原请求并核对项目状态。',
    }
    if (typeof payload.error === 'string' && Object.hasOwn(words, payload.error)) receipt.message = words[payload.error]
  }
  return { kind: 'matched', receipt }
}

/** timeout, disconnect and generic wsClient send-failed are NOT proof that a write had no effect. */
export function employeeAssetResponseLost(request: EmployeeAssetRequest): EmployeeAssetReceipt {
  return resultReceipt(request, request.write ? 'unknown' : 'failed', null, request.write ? null : false)
}
export function employeeAssetWriteBlocked(writeReceipt: EmployeeOperationReceipt | null, busy: boolean): boolean {
  return busy || (!!writeReceipt && ['pending', 'received', 'unknown', 'partial'].includes(writeReceipt.state))
}

/** Public presentation only. Declarations are not genuine evaluated model/test/cost facts. */
export function projectEmployeePackage(value: unknown, source: PackageSource): AIEmployeePackageView {
  if (!packageShape(value, source) || !row(value.package)) return fail('receipt_invalid')
  const manifest = value.package
  const selectedVersion = text(manifest.version)
  const actions = strings(value.allowed_actions)
  const skills = Array.isArray(manifest.skills) ? manifest.skills.filter(row).map(skill => ({
    id: text(skill.skill_name), version: text(skill.version), name: text(skill.skill_name),
  })) : []
  const tools = Array.isArray(manifest.capability_requirements) ? manifest.capability_requirements.filter(row).map(tool => ({
    id: text(tool.tool), version: '', name: text(tool.tool), // no package capability implementation version exists
  })) : []
  return { package_id: text(manifest.package_id), package_version: selectedVersion, name: text(manifest.name),
    responsibility: text(manifest.responsibility),
    inputs: row(manifest.input_schema) ? [JSON.stringify(manifest.input_schema)] : [],
    outputs: row(manifest.output_schema) ? [JSON.stringify(manifest.output_schema)] : [],
    applicable: strings(manifest.scope_in), excluded: strings(manifest.scope_out), skills, tools, models: [],
    source: { label: source === 'draft' ? '服务端岗位草稿' : '服务端发布索引视图',
      ...(row(manifest.provenance) && typeof manifest.provenance.license === 'string' ? { license: manifest.provenance.license } : {}) },
    package_status: value.status === 'testing' ? 'candidate' : value.status as AIEmployeePackageView['package_status'],
    verification: { state: 'unknown', scope: '此读取未提供可独立核实的评测事实。', formats: [], environments: [], mode: 'unknown',
      unchecked: ['真实评测事实', '模型组合与样本集匹配', '审批与当前发布锚定'] },
    cost: { state: 'unknown' }, revision: text(value.revision), loaded_revision: text(value.revision),
    allowed_actions: selectedVersion ? [
      ...(actions.includes('employee_package_detail') ? ['view' as const] : []),
      ...(source === 'draft' && actions.includes('employee_package_draft_save') ? ['edit' as const] : []),
    ] : [] }
}

/** Snapshot revisions are numbers on the wire; decimal strings are B's display-only representation. */
export function projectEmployeeInstance(value: unknown, projectId: string): AIEmployeeInstanceView {
  if (!instanceShape(value, projectId) || !row(value.instance)) return fail('receipt_invalid')
  const instance = value.instance
  if (!valid(instance.package_id, PACKAGE) || !version(instance.package_version)) return fail('receipt_invalid')
  const revision = String(value.revision)
  return { project_id: projectId, instance_id: text(instance.instance_id), instance_revision: revision,
    package_id: instance.package_id, package_version: instance.package_version, name: text(instance.display_name),
    purpose: '', owner_label: '人员展示名称未提供', loaded_revision: revision,
    server_ready: typeof value.ready === 'boolean' ? value.ready : null,
    readiness_revision: typeof value.ready === 'boolean' ? revision : null, gaps: strings(value.gaps),
    adoption_status: 'unknown', knowledge_status: '连接依据未提供', connection_status: '连接依据未提供', allowed_actions: [] }
}
export function projectEmployeeProjectRow(value: unknown, projectId: string): AIEmployeeProjectView {
  const display = projectEmployeeInstance(value, projectId)
  if (!row(value) || !row(value.instance)) return fail('receipt_invalid')
  const instance = value.instance
  return { project_id: projectId, package_id: display.package_id, package_name: '',
    ...(display.name ? { instance_name: display.name } : {}),
    package_version: display.package_version, instance_id: display.instance_id, purpose: '', owner_name: null,
    revision: display.instance_revision, current_revision: display.instance_revision, adoption_state: 'unknown',
    intake_state: instance.intake_state === 'open' || instance.intake_state === 'paused' ? instance.intake_state : 'unknown',
    execution_state: instance.execution_state === 'active' || instance.execution_state === 'paused' ? instance.execution_state
      : instance.execution_state === 'disabled' ? 'stopped' : 'unknown',
    ready: value.ready === true ? 'ready' : value.ready === false ? 'not_ready' : 'unknown', readiness_source: null,
    observed_at: null, gaps: Array.isArray(value.gaps) ? strings(value.gaps) : null,
    knowledge_connections: null, software_connections: null, validation: null, quality: null,
    allowed_actions: strings(value.allowed_actions).includes('employee_instance_revise') ? ['configure'] : [],
    busy: false, receipt: { state: 'idle' } }
}

/** Compare untouched editor fields rather than silently dropping unsupported changes.
 * This is an exact three-field patch, NOT a manifest conversion. Preserve the raw manifest in host state.
 */
export function editorDraftChanges(baseline: AIEmployeeEditorDraft, request: AIEmployeeEditorRequest,
  loaded: DraftGrant): JSONObject {
  if (request.action !== 'save' || request.scope !== 'standard_package' || !request.draft
    || request.target.package_id !== loaded.package_id || request.target.instance_id || request.target.node_id
    || (request.target.project_id && request.target.project_id !== loaded.project_id)
    || request.expected_revision !== loaded.revision || request.version !== request.draft.version.candidate_version) return fail('unsupported_mapping')
  const original = objectCopy(baseline)
  const candidate = objectCopy(request.draft)
  const mask = (draft: JSONObject): void => {
    if (!row(draft.delivery) || !row(draft.version)) return fail('unsupported_mapping')
    draft.name = null
    ;(draft.delivery as JSONObject).responsibility = null
    ;(draft.version as JSONObject).candidate_version = null
  }
  mask(original); mask(candidate)
  // Property order is irrelevant; this comparison intentionally does not merge or edit nested bindings.
  function sorted(value: JSONValue): JSONValue {
    if (Array.isArray(value)) return value.map(sorted)
    if (row(value)) return Object.fromEntries(Object.keys(value).sort().map(key => [key, sorted(value[key] as JSONValue)])) as JSONObject
    return value
  }
  if (JSON.stringify(sorted(original)) !== JSON.stringify(sorted(candidate))) return fail('unsupported_mapping')
  if (typeof request.draft.name !== 'string' || typeof request.draft.delivery.responsibility !== 'string'
    || !version(request.draft.version.candidate_version)) return fail()
  const changes: JSONObject = {}
  if (baseline.name !== request.draft.name) changes.name = request.draft.name
  if (baseline.delivery.responsibility !== request.draft.delivery.responsibility) changes.responsibility = request.draft.delivery.responsibility
  if (baseline.version.candidate_version !== request.draft.version.candidate_version) changes.version = request.draft.version.candidate_version
  return changes
}

type InstanceModelInputKind = 'candidates' | 'revise'
const INSTANCE_MODEL_GRANT_KEYS = ['project_id', 'allowed_actions', 'instance_id', 'revision', 'fingerprint'] as const
const INSTANCE_MODEL_PACKAGE_KEYS = ['package_id', 'package_version', 'package_fingerprint'] as const
const INSTANCE_MODEL_COMMON_ACK = ['ok', 'action', 'project_id', 'request_id', 'revision', 'fingerprint',
  'result', 'effects_produced', 'problems', 'allowed_actions'] as const
const INSTANCE_MODEL_READ_ACK = [...INSTANCE_MODEL_COMMON_ACK, 'selection_scope', 'catalogue_source',
  'projection_version', 'model_catalogue_revision', 'instance_id', 'instance_revision', 'revision_fingerprint',
  'package_id', 'package_version', 'package_fingerprint', 'candidates'] as const
const INSTANCE_MODEL_WRITE_ACK = [...INSTANCE_MODEL_COMMON_ACK, 'instance_id', 'model_catalogue_revision',
  'model_config', 'retest_scope'] as const
const INSTANCE_MODEL_ERROR_ACK = [...INSTANCE_MODEL_READ_ACK, 'model_config', 'retest_scope',
  'error', 'message', 'path', 'details'] as const
const INSTANCE_MODEL_MESSAGES: Readonly<Record<string, string>> = Object.freeze({
  model_catalogue_unavailable: '原模型目录暂不可核实；保留配置，不改用默认模型。',
  model_catalogue_changed: '模型目录已变化；保留本地选择，重新读取后再明确选择。',
  model_unknown: '原目录未识别这个模型；不会推断供应方或改用其他模型。',
  model_retired: '这个模型已退役，不能作为新选择。',
  model_overseas_off: '这个模型当前不允许新选择。',
  model_not_routable: '这个模型没有可核实的配置路由。',
  model_needs_params: '这个模型需要另行配置参数，本入口不能解除限制。',
  model_needs_trial: '这个模型需要独立试验，本入口不能代替评测。',
  candidate_projection_limit: '模型候选超过显示限额，未截短返回。',
  candidate_read_limit: '模型候选超过本次读取范围，未返回部分清单。',
  revision_conflict: '实例已有新修订；保留选择后重新读取，不覆盖旧版本。',
  fingerprint_mismatch: '实例或固定包指纹已变化；保留选择并核对。',
  instance_not_found: '本项目实例尚不能核实，不读取其他项目的实例。',
  owner_required: '当前账户没有本项目模型配置许可。',
  not_allowed_in_status: '当前项目状态不允许模型配置；保留选择。',
})
function instanceModelOwn(value: unknown, key: PropertyKey): PropertyDescriptor | undefined {
  return value !== null && typeof value === 'object' ? Object.getOwnPropertyDescriptor(value, key) : undefined
}
/** Detect only an own new branch; old name/limits request bodies remain unchanged. */
function instanceModelInputKind(input: unknown): InstanceModelInputKind | null {
  let known = false
  try {
    const action = instanceModelOwn(input, 'action')
    if (!action || !Object.hasOwn(action, 'value')) return null
    if (action.value === 'employee_instance_model_candidates') return 'candidates'
    if (action.value !== 'employee_instance_revise') return null
    for (const key of ['expected_instance_fingerprint', 'expected_model_catalogue_revision', 'check_only']) {
      if (instanceModelOwn(input, key)) return 'revise'
    }
    const changes = instanceModelOwn(input, 'changes')
    if (!changes || !Object.hasOwn(changes, 'value')) return null
    known = instanceModelOwn(changes.value, 'model_config') !== undefined
    return known ? 'revise' : null
  } catch { if (known) return fail(); return null }
}
function instanceModelRecord(value: unknown, allowed: readonly string[], required: readonly string[] = allowed): Row {
  if (!row(value)) return fail()
  const prototype = Object.getPrototypeOf(value)
  if (prototype !== Object.prototype && prototype !== null) return fail()
  const keys = Reflect.ownKeys(value), length = keys.length
  if (length > allowed.length || length < required.length) return fail()
  const result: Row = Object.create(null)
  for (let index = 0; index < length; index += 1) {
    const key = keys[index], descriptor = typeof key === 'string' ? Object.getOwnPropertyDescriptor(value, key) : undefined
    if (typeof key !== 'string' || !allowed.includes(key) || !descriptor || !Object.hasOwn(descriptor, 'value')) return fail()
    result[key] = descriptor.value
    if (Reflect.ownKeys(value).length !== length) return fail()
  }
  for (let index = 0; index < required.length; index += 1) if (!Object.hasOwn(result, required[index])) return fail()
  const final = Reflect.ownKeys(value)
  if (final.length !== length) return fail()
  for (let index = 0; index < length; index += 1) if (!final.includes(keys[index])) return fail()
  return result
}
function instanceModelList(value: unknown, maximum: number): readonly unknown[] {
  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) return fail()
  const lengthDescriptor = Object.getOwnPropertyDescriptor(value, 'length')
  const length: unknown = lengthDescriptor?.value
  if (typeof length !== 'number' || !Number.isSafeInteger(length) || length < 0 || length > maximum
    || Reflect.ownKeys(value).length !== length + 1) return fail()
  const result: unknown[] = []
  for (let index = 0; index < length; index += 1) {
    const descriptor = Object.getOwnPropertyDescriptor(value, String(index))
    if (!descriptor || !Object.hasOwn(descriptor, 'value')) return fail()
    result.push(descriptor.value)
    if (Object.getOwnPropertyDescriptor(value, 'length')?.value !== length || Reflect.ownKeys(value).length !== length + 1) return fail()
  }
  if (Object.getOwnPropertyDescriptor(value, 'length')?.value !== length) return fail()
  return Object.freeze(result)
}
function instanceModelGuard<T>(operation: () => T): T {
  let code: EmployeeAssetAdapterError['code'] = 'request_invalid'
  try { return operation() }
  catch (error) {
    try {
      if (error instanceof EmployeeAssetAdapterError && Object.getOwnPropertyDescriptor(error, 'code')?.value === 'grant_missing') code = 'grant_missing'
    } catch { /* No source diagnostics or cause are exposed. */ }
  }
  return fail(code)
}
function instanceModelActions(value: unknown, only?: readonly string[]): readonly string[] {
  const values = instanceModelList(value, only ? only.length : EMPLOYEE_ASSET_ACTIONS.length)
  const result: string[] = [], seen = new Set<string>()
  for (let index = 0; index < values.length; index += 1) {
    const item = values[index]
    if (typeof item !== 'string' || !(EMPLOYEE_ASSET_ACTIONS as readonly string[]).includes(item)
      || only && !only.includes(item) || seen.has(item)) return fail()
    seen.add(item); result.push(item)
  }
  return Object.freeze(result)
}
function instanceModelWhole(value: unknown): EmployeeInstanceModelFields {
  const config = employeeInstanceModelConfigState(value)
  if (!Object.hasOwn(config.fields, 'provider') || !Object.hasOwn(config.fields, 'model')) return fail()
  return config.fields
}
function buildInstanceModelRequest(input: EmployeeAssetRequestInput, kind: InstanceModelInputKind): EmployeeAssetRequest {
  return instanceModelGuard(() => {
    const common = ['action', 'project_id', 'request_id', 'instance_id', 'expected_revision', 'grant']
    const fields = kind === 'candidates' ? [...common, 'expected_fingerprint']
      : [...common, 'changes', 'expected_instance_fingerprint', 'expected_model_catalogue_revision', 'note', 'check_only']
    const required = kind === 'candidates' ? fields
      : [...common, 'changes', 'expected_instance_fingerprint', 'expected_model_catalogue_revision']
    const current = instanceModelRecord(input, fields, required)
    const action = kind === 'candidates' ? 'employee_instance_model_candidates' : 'employee_instance_revise'
    if (current.action !== action || !bindingValid(current.project_id, ID) || !bindingValid(current.request_id, ID)
      || !bindingValid(current.instance_id, INSTANCE) || !positiveRevision(current.expected_revision)
      || kind === 'revise' && current.expected_revision >= Number.MAX_SAFE_INTEGER) return fail()
    const grant = instanceModelRecord(current.grant, [...INSTANCE_MODEL_GRANT_KEYS, ...INSTANCE_MODEL_PACKAGE_KEYS],
      kind === 'candidates' ? [...INSTANCE_MODEL_GRANT_KEYS, ...INSTANCE_MODEL_PACKAGE_KEYS] : INSTANCE_MODEL_GRANT_KEYS)
    const allowed = instanceModelActions(grant.allowed_actions)
    const expectedFingerprint = kind === 'candidates' ? current.expected_fingerprint : current.expected_instance_fingerprint
    if (grant.project_id !== current.project_id || grant.instance_id !== current.instance_id
      || grant.revision !== current.expected_revision || !bindingValid(expectedFingerprint, SHA)
      || grant.fingerprint !== expectedFingerprint || !allowed.includes(action)) return fail('grant_missing')
    const hasPackage = INSTANCE_MODEL_PACKAGE_KEYS.some(key => Object.hasOwn(grant, key))
    if (hasPackage && (!bindingValid(grant.package_id, PACKAGE) || !version(grant.package_version)
      || !bindingValid(grant.package_fingerprint, SHA))) return fail()
    const message: JSONObject = { type: action, project_id: current.project_id, request_id: current.request_id,
      instance_id: current.instance_id, expected_revision: current.expected_revision }
    let scope: EmployeeInstanceModelRequestScope | undefined
    if (kind === 'candidates') {
      if (!bindingValid(grant.package_id, PACKAGE) || !version(grant.package_version) || !bindingValid(grant.package_fingerprint, SHA)) return fail()
      message.expected_fingerprint = expectedFingerprint
      scope = Object.freeze({ instance_revision: current.expected_revision, revision_fingerprint: expectedFingerprint,
        package_id: grant.package_id, package_version: grant.package_version, package_fingerprint: grant.package_fingerprint })
    } else {
      const change = instanceModelRecord(current.changes, ['model_config'])
      const model = instanceModelWhole(change.model_config)
      if (!bindingValid(current.expected_model_catalogue_revision, SHA)) return fail()
      if (Object.hasOwn(current, 'check_only') && typeof current.check_only !== 'boolean') return fail()
      const note = Object.hasOwn(current, 'note') ? current.note : ''
      if (typeof note !== 'string' || note.length > 4000) return fail()
      message.changes = { model_config: { ...model } }
      message.expected_instance_fingerprint = expectedFingerprint
      message.expected_model_catalogue_revision = current.expected_model_catalogue_revision
      message.note = note
      if (Object.hasOwn(current, 'check_only')) message.check_only = current.check_only as boolean
    }
    // The fully known request, including note/digests, is subject to the original 2MiB budget.
    const frozen = freezeJSON(objectCopy(message)) as Readonly<JSONObject>
    const key = JSON.stringify([action, current.project_id, current.instance_id, current.expected_revision, expectedFingerprint,
      ...(scope ? [scope.package_id, scope.package_version, scope.package_fingerprint]
        : [message.expected_model_catalogue_revision, message.changes])])
    return Object.freeze({ action, project_id: current.project_id, request_id: current.request_id,
      source: 'project' as const, package_id: scope?.package_id ?? null, instance_id: current.instance_id,
      version: scope?.package_version ?? null, workflow_id: null, node_id: null, workflow_revision: null,
      binding_slot: null, adoption_revision: null, adoption_instance_fingerprint: null, target_fingerprint: null,
      from_version: null, from_fingerprint: null, key, write: kind === 'revise', message: frozen,
      ...(scope ? { model_scope: scope } : {}) })
  })
}
function isInstanceModelWrite(request: EmployeeAssetRequest): boolean {
  if (request.action !== 'employee_instance_revise') return false
  const changes = instanceModelOwn(request.message, 'changes')
  return !!changes && Object.hasOwn(changes, 'value') && instanceModelOwn(changes.value, 'model_config') !== undefined
}
/** Explicit read-only recovery of the SAME original model request; no new grant/catalogue/read. */
export function buildEmployeeInstanceModelRecoveryRequest(request: EmployeeAssetRequest): EmployeeAssetRequest {
  return instanceModelGuard(() => {
    if (!request.write || !isInstanceModelWrite(request)) return fail()
    const message = instanceModelRecord(request.message, ['type', 'project_id', 'request_id', 'instance_id', 'expected_revision',
      'expected_instance_fingerprint', 'expected_model_catalogue_revision', 'changes', 'note', 'check_only'],
    ['type', 'project_id', 'request_id', 'instance_id', 'expected_revision', 'expected_instance_fingerprint',
      'expected_model_catalogue_revision', 'changes', 'note'])
    if (message.type !== request.action || message.project_id !== request.project_id || message.request_id !== request.request_id
      || message.instance_id !== request.instance_id || !bindingValid(message.project_id, ID) || !bindingValid(message.request_id, ID)
      || !bindingValid(message.instance_id, INSTANCE) || !positiveRevision(message.expected_revision)
      || message.expected_revision >= Number.MAX_SAFE_INTEGER || !bindingValid(message.expected_instance_fingerprint, SHA)
      || !bindingValid(message.expected_model_catalogue_revision, SHA) || typeof message.note !== 'string' || message.note.length > 4000
      || Object.hasOwn(message, 'check_only') && typeof message.check_only !== 'boolean') return fail()
    const changes = instanceModelRecord(message.changes, ['model_config'])
    message.changes = { model_config: { ...instanceModelWhole(changes.model_config) } }
    message.check_only = true
    return Object.freeze({ ...request, message: freezeJSON(objectCopy(message)) as Readonly<JSONObject> })
  })
}
function acceptInstanceModelReceipt(request: EmployeeAssetRequest, envelope: unknown, active?: ActiveEmployeeRead): EmployeeReceiptDecision {
  const invalid = (): EmployeeReceiptDecision => ({ kind: 'matched', receipt: resultReceipt(request,
    request.write ? 'unknown' : 'failed', null, request.write ? null : false) })
  let raw: Row, originalPayload: unknown
  try {
    // Correlation precedes full shape/diagnostic inspection, as on every original action.
    const type = instanceModelOwn(envelope, 'type'), payload = instanceModelOwn(envelope, 'payload')
    if (!type || !payload || !Object.hasOwn(type, 'value') || !Object.hasOwn(payload, 'value') || type.value !== 'ack') return { kind: 'ignored', reason: 'uncorrelated' }
    originalPayload = payload.value
    const headers: Row = Object.create(null)
    for (const key of ['request_id', 'action', 'project_id', 'ok']) {
      const descriptor = instanceModelOwn(originalPayload, key)
      if (!descriptor || !Object.hasOwn(descriptor, 'value')) return { kind: 'ignored', reason: 'uncorrelated' }
      headers[key] = descriptor.value
    }
    raw = headers
  } catch { return { kind: 'ignored', reason: 'uncorrelated' } }
  if (raw.request_id !== request.request_id || raw.action !== request.action
    || raw.project_id !== request.project_id && !(raw.project_id === null && raw.ok === false)) return { kind: 'ignored', reason: 'uncorrelated' }
  if (!request.write && !activeMatches(request, active)) return { kind: 'ignored', reason: 'stale_read' }
  if (request.write && raw.project_id === null) return invalid()
  try {
    instanceModelRecord(envelope, ['type', 'payload'])
    raw = instanceModelRecord(originalPayload, INSTANCE_MODEL_ERROR_ACK, ['ok', 'action', 'project_id', 'request_id'])
    if (raw.request_id !== request.request_id || raw.action !== request.action
      || raw.project_id !== request.project_id || typeof raw.ok !== 'boolean') return invalid()
    const revision = positiveRevision(raw.revision) ? raw.revision : null
    const effects = typeof raw.effects_produced === 'boolean' ? raw.effects_produced : null
    if (typeof raw.ok !== 'boolean' || typeof raw.result !== 'string') return invalid()
    const actions = instanceModelActions(raw.allowed_actions,
      request.write ? undefined : ['employee_instance_model_candidates', 'employee_instance_revise'])
    if (raw.ok) {
      if (raw.project_id !== request.project_id || raw.result !== 'success' || revision === null
        || !bindingValid(raw.fingerprint, SHA) || instanceModelList(raw.problems, 0).length !== 0) return invalid()
      const checked = instanceModelRecord(raw, request.write ? INSTANCE_MODEL_WRITE_ACK : INSTANCE_MODEL_READ_ACK)
      if (request.write) {
        const expected = request.message.expected_revision
        const retest = instanceModelList(checked.retest_scope, 1)
        if (checked.instance_id !== request.instance_id || effects !== true || typeof expected !== 'number'
          || revision !== expected + 1 || checked.model_catalogue_revision !== request.message.expected_model_catalogue_revision
          || !bindingValid(checked.model_catalogue_revision, SHA)
          || retest.length === 1 && retest[0] !== 'model_config') return invalid()
        const changes = instanceModelRecord(request.message.changes, ['model_config'])
        const original = instanceModelWhole(changes.model_config), echoed = instanceModelWhole(checked.model_config)
        if (!sameEmployeeInstanceModelConfig(employeeInstanceModelConfigState(original), employeeInstanceModelConfigState(echoed))) return invalid()
        return { kind: 'matched', receipt: resultReceipt(request, 'succeeded', revision, true) }
      }
      const scope = request.model_scope
      if (!scope || effects !== false || checked.selection_scope !== 'instance_model_configuration'
        || checked.catalogue_source !== 'static_model_catalogue' || checked.instance_id !== request.instance_id
        || revision !== scope.instance_revision || checked.instance_revision !== scope.instance_revision
        || checked.fingerprint !== scope.revision_fingerprint || checked.revision_fingerprint !== scope.revision_fingerprint
        || checked.package_id !== scope.package_id || checked.package_version !== scope.package_version
        || checked.package_fingerprint !== scope.package_fingerprint) return invalid()
      const catalogue = employeeInstanceModelCandidates({ request_id: request.request_id, projection_version: checked.projection_version,
        model_catalogue_revision: checked.model_catalogue_revision, candidates: checked.candidates })
      const projected: Row = Object.freeze({ ...checked, allowed_actions: actions, problems: Object.freeze([]),
        candidates: catalogue.candidates })
      return { kind: 'matched', receipt: resultReceipt(request, 'succeeded', revision, false, projected) }
    }
    // check_only refusals cannot disprove an earlier durable write. Unknown is never reclassified from false effects.
    if (request.write && (request.message.check_only === true || raw.result === 'outcome_unknown' || raw.result === 'partial' || effects !== false)) {
      return { kind: 'matched', receipt: resultReceipt(request,
        effects === true || raw.result === 'partial' ? 'partial' : 'unknown', revision, effects) }
    }
    if (!['failed', 'unsupported'].includes(raw.result) || effects !== false) return invalid()
    instanceModelList(raw.problems, 500)
    const receipt = resultReceipt(request, 'failed', revision, false)
    if (typeof raw.error === 'string' && Object.hasOwn(INSTANCE_MODEL_MESSAGES, raw.error)) receipt.message = INSTANCE_MODEL_MESSAGES[raw.error]
    return { kind: 'matched', receipt }
  } catch { return invalid() }
}


