import { reportProject, reportWorkflows } from '../lib/reportFocus'
import { builderCrumb, type BuilderCrumb } from '../lib/builderCrumb'
import { type ModelPlan } from '../workspace/modelTiers'
import { OrderDrawer, STEP_TEXT } from './OrderDrawer'
import { orderStep } from '../lib/orderDrawer'
import { rowShowsTools, toggled } from '../lib/orderRows'
import { waitingOn } from '../lib/orderWaiting'
import { childrenOf, orderLabel, parentOf } from '../lib/orderLineage'
import type { DashboardBlocker } from './DashboardPage'
import { ORDER_QUEUES, ORDER_QUEUE_LABELS, flowsWithOrders, personWaits as personWaitsOf, queueCounts, queueOf, shownOrders, type OrderQueue } from '../lib/orderQueues'
import type { TrialArtifactState } from './NodeTrialSection'
import type { NodeTrialPin, NodeTrialPinning } from '../lib/nodeTrialPins'
import { serverBlockersFor, type NodeBlocker } from '../workspace/workflowHealth'
import { clearDraft, loadDraft, recoveredDraftState, recoveryOffer, type RecoveryOffer } from '../lib/draftRecovery'
import { dropDraft } from '../workspace/useNodeDraftState'
import type { EditLockApi } from '../workspace/useEditLock'
import { DraftRecoveryBanner } from './DraftRecoveryBanner'
import { OrderLaunchForm } from './OrderLaunchForm'
import { FlowAdmissionDialog, FlowTestDialog, type FlowTrialSummary, type FlowTrialTarget, type PublishedFlowTrialCandidate } from './BuilderActionDialogs'
import { admissionView, flowTestView, refusalWords, workflowRevisionMatches, type AdmissionRefusal } from '../lib/flowAdmission'
import type { FlowVersions } from '../lib/flowVersions'
import type { ModelProvider } from '../workspace/stepModels'
import { BudgetLimitInputs, budgetLimitsFromDraft, type BudgetDraft } from './BudgetLimitInputs'
import { Fragment, Suspense, lazy, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useI18n } from '../i18n'
import { integrityLine } from '../lib/timelineIntegrity'
import { timelineEventName, timelineEventTime } from '../lib/orderEventNames'
import { whoseTurn } from '../lib/orderTurn'
import { dueState, type OrderHandling } from '../lib/orderHandling'
import { importOrReload } from '../lib/staleChunk'
import { classifyPageError, pageErrorMessageKey } from '../lib/pageError'
import type { OrgEmployee } from '../types/visual'
import { composeWorkflowDraft, draftIsStale, nextPatchVersion, proposalIsStale, rosterFor, trialSkillsOnSteps, unadmittedNewSkills } from '../workspace/workflowGraph'
import { onlyPositionsChanged } from '../workspace/graphEdits'
import { flowComponents, libraryDeclarable, type InternalSkill } from '../lib/componentCatalog'
import { appliedProposal, revertDraft, type NodeProposal, type ProposalCandidate, type ProposalRevert } from '../lib/nodeProposal'
import { takeSavedNotice, useNodeDraftState, type NodeDraftState } from '../workspace/useNodeDraftState'
import type { ConnectorParams } from '../workspace/browserConnector'
import type { DesktopConnectorParams } from '../workspace/desktopConnector'
import type { BookContact } from '../lib/contactBook'
import { canvasNodesForSave, ownerCanvasNodesForSave, copyEmployeeBinding, type PortableEmployeeBinding } from '../lib/workflowEmployeeBinding'
import { EmployeeWorkflowTrialPanel } from './EmployeeWorkflowTrialPanel'
import type { EmployeeWorkflowTrialAck, EmployeeWorkflowTrialSend } from '../lib/employeeWorkflowTrial'
import type { CanvasEmployeeDesign } from '../lib/canvasEmployeeDesign'

// React Flow is heavy and imports CSS node cannot load; load the canvas only when a workflow's
// canvas view is opened -- keeps it out of node contract tests and off the initial bundle.
// A tab opened before a new build was published names a canvas file that no longer exists: reload once instead of
// crashing (lib/staleChunk).
const WorkflowCanvas = lazy(() => importOrReload(() => import('./WorkflowCanvas')).then(module => ({ default: module.WorkflowCanvas })))

// A workflow package: one tested business line, invoked as one order.
export interface WorkflowIntakeField {
  id: string
  label: string
  required?: boolean
  multiline?: boolean
}

export interface WorkflowTestRecord {
  file: string
  date: string
  outcome: string
  duration_minutes: number | null
  model_calls: number | null
  cost_cny_estimate: number | null
  notes: string
  failure_reason?: string
  execution_mode?: 'manual' | 'simulated' | 'provider' | string
}

export interface WorkflowNode {
  id: string
  name: string
  type: 'ai_do' | 'human_do' | 'human_review' | 'invoke_workflow' | string
  role: string
  assignee: string
  gate: 'none' | 'human_required' | 'ai_first_human_spot_check' | string
  gate_to: string
  depends_on: string[]
  deliverables: string[]
  /** Which deliverables the step needs, by name. Path-like ones are checked against what upstream promises. */
  inputs?: string[]
  skills?: string[]
  tools?: string[]
  responsibility?: string
  /** Read-only, from the package: what a human gate judges this step by, and where it may write. The builder
   *  shows them and never edits them -- the server drops either from any draft that carries them back. */
  standard?: string[]
  writes?: string[]
  invoke: Record<string, unknown>
  /** Portable package design, never project instance/revision/pins/grants. Null
   *  is an explicit CAS draft unbind; a server manifest omits an absent binding. */
  employee_binding?: PortableEmployeeBinding | null
  /** An AI step's own model; absent or '' = the configured default (workspace/stepModels.ts). */
  model?: string
  /** An AI step's tier -- 轻 / 标准 / 强 (workspace/modelTiers.ts). A step that also names a model runs on
   *  the model; absent or '' = the suggestion, which is 标准 for a work step. */
  tier?: string
  /** The step's browser universal connector (workspace/browserConnector.ts): its parameters, or null for none. */
  connector?: ConnectorParams | DesktopConnectorParams | null
  /** A person's review only (4.7-A07): the upstream step its 打回 sends the work back to; absent or '' = this step. */
  rework_to?: string
  /** Read-only, from the package (4.7 A02): a branch step's 分支条件, the words that choose it at the decision before
   *  it; absent or '' = not a branch. The card shows it (graphEdits.branchRowText); the builder has no editor for it. */
  branch_when?: string
  /** Where a person placed the step on the builder canvas, relative to its lane; empty or null = auto layout. */
  canvas?: { x?: number; y?: number } | null
}

/** A flow id not used yet: flow_1, flow_2, ... (the console refuses one a loaded package already has). */
export function nextFlowId(taken: readonly string[]): string {
  const used = new Set(taken)
  for (let n = 1; ; n++) if (!used.has(`flow_${n}`)) return `flow_${n}`
}

const FLOW_ID = /^[a-z][a-z0-9_-]{1,63}$/

/** ＋ 新建业务流 (4.7 Q01): an empty draft -- no steps, one intake field -- run by a saved team. Nothing runs. */
function BlankFlowForm({ teams, busy, connected, suggestedId, onCreate, onCancel, t }: {
  teams: ReadonlyArray<{ name: string; label: string }>
  busy: boolean
  connected: boolean
  suggestedId: string
  onCreate: (draftId: string, team: string, name: string) => void
  onCancel: () => void
  t: ReturnType<typeof useI18n>['t']
}) {
  const [draftId, setDraftId] = useState(suggestedId)
  const [name, setName] = useState('')
  const [team, setTeam] = useState(teams.length === 1 ? teams[0].name : '')
  const idOk = FLOW_ID.test(draftId.trim())
  return (
    <form className="wf-blank-form" data-testid="blank-flow-form" onSubmit={event => { event.preventDefault(); if (idOk && team && !busy) onCreate(draftId.trim(), team, name.trim()) }}>
      <label>{t('workflows.blank.name')}<input value={name} placeholder={t('workflows.blank.namePlaceholder')} onChange={event => setName(event.target.value)} /></label>
      <label>{t('workflows.blank.id')}<input value={draftId} aria-invalid={!idOk} onChange={event => setDraftId(event.target.value.toLowerCase())} /></label>
      <label>{t('workflows.blank.team')}
        <select value={team} onChange={event => setTeam(event.target.value)}>
          <option value="">{teams.length ? t('workflows.blank.teamPick') : t('workflows.blank.teamNone')}</option>
          {teams.map(item => <option key={item.name} value={item.name}>{item.label}</option>)}
        </select>
      </label>
      <small className="wf-blank-hint">{idOk ? t('workflows.blank.hint') : t('workflows.blank.idRule')}</small>
      <div className="wf-blank-actions">
        <button type="submit" className="workflows-invoke" disabled={!connected || !idOk || !team || busy}>{busy ? t('workflows.blank.creating') : t('workflows.blank.create')}</button>
        <button type="button" className="workflows-cancel" onClick={onCancel}>{t('workflows.blank.cancel')}</button>
      </div>
    </form>
  )
}

/** The flow on the builder canvas and the step being edited ('' when none): what 安安 can be told a message is about. */
export interface BuilderFocus {
  workflowId: string
  workflowName: string
  draftRevision: string
  nodeId: string
  nodeName: string
}

export interface WorkflowSummary {
  id: string
  name: string
  version: string
  /** Exact saved workflow.yaml bytes observed in this row; saves and release mutations must name it. */
  draft_revision?: string
  /** The project this flow was derived for (派生为本项目草稿, policy); absent for a global flow. */
  project_id?: string
  /** Where a derived flow came from: the global flow and the version it was derived at. */
  derived_from?: { id: string; version: string } | null
  status: string
  summary: string
  applies_to: string[]
  org_id: string
  /** The roles of the org this workflow runs on -- what a node's role may name (the save checks the same list). */
  org_roles?: { id: string; name: string }[]
  gates: { station: string; kind: string; who: string; decides: string }[]
  gate_count: number
  stations: string[]
  nodes: WorkflowNode[]
  intake_fields: WorkflowIntakeField[]
  /** The flow's own acceptance criteria as written; an empty list means an order gets one generated at launch. */
  acceptance?: string[]
  latest_test: WorkflowTestRecord | null
  test_count: number
  problems: string[]
  invokable: boolean
  skill_contracts?: Array<{ id: string; version: string; complete: boolean; problems: string[] }>
  /** Whether the package's skills come from the shared internal library (a draft cut into the runtime home has its own). */
  shared_skill_library?: boolean
  /** A runtime-home draft that copies an internal-library skill in when a save names it (policy). */
  library_by_copy?: boolean
  /** Internal-library skills a step of this package can take and a save declares and locks. */
  library_skills?: string[]
  /** Internal-library skills this package cannot lock (incomplete release contract): a save naming one is refused. */
  library_skills_unlockable?: string[]
  /** 4.7-A06: whether the owner opened the desktop channel; closed, a desktop step saves but does not run. */
  desktop_channel_open?: boolean
  /** Ungranted tools that reach outside the workspace: each one stops the run for a person. */
  reaching_tools_without_grant?: string[]
  /** Ungranted tools whose effects this build cannot state at all. */
  tools_with_unknown_effects?: string[]
  skill_test_report?: {
    status: 'passed' | 'failed' | 'unsupported' | string
    skills?: Array<{ skill_id: string; status: string }>
  }
  skill_versions?: Record<string, string>
  release_ready?: boolean
  release_blockers?: string[]
  publication?: {
    published_at?: string
    published_by?: string
    evidence_test?: string
    evidence_manifest_sha256?: string
    evidence_skill_sha256?: Record<string, string>
    rollback_snapshot?: string
    rollback_manifest_sha256?: string
    rollback_workflow_version?: string
  }
  rollback_preview?: {
    available: boolean
    status: string
    snapshot?: string
    expected_sha256?: string
    actual_sha256?: string
    blockers?: string[]
  }
  release_approval?: {
    approval_id?: string
    status?: string
    requested_by?: string
    decided_by?: string
    decision_note?: string
  }
  release_evidence?: {
    version: string
    skill_versions?: Record<string, string>
    manifest_sha256: string
    skill_sha256: Record<string, string>
    test_records: WorkflowTestRecord[]
    approval_records: Array<Record<string, unknown>>
    change_note: string
    rollback_point: string
    candidate_fingerprint?: string
    evidence_file?: string
    evidence_file_sha256?: string
  }
  rollout?: {
    status?: 'canary' | 'healthy' | 'unhealthy' | 'insufficient' | 'rolled_back' | string
    candidate_fingerprint?: string
    canary_percent?: number
    min_observations?: number
    max_failure_rate?: number
    observation_count?: number
    started_by?: string
    started_at?: string
  }
  rollout_evaluation?: {
    rollout_status?: string
    observed_count?: number
    failed_count?: number
    unknown_count?: number
    failure_rate?: number
    blockers?: string[]
  } | null
}

export interface RunBudget {
  budget_id: string
  order_id: string
  customer_id: string
  /** 'open' | 'pending_verification' | 'closed' — whichever stops spending first. */
  state: string
  state_reason: string
  limits: Record<string, number>
  held: Record<string, number>
  consumed: Record<string, number>
  available: Record<string, number | null>
}

export interface SolutionBlueprint {
  blueprint_id: string
  name: string
  created_at: string
  created_by: string
  source_order_id: string
  source_customer_id: string
  workflow_id: string
  note: string
  plan: Record<string, unknown>
}

export interface ReleaseEvidence {
  workflow_id: string
  version: string
  status: string
  published_evidence: {
    manifest_sha256: string
    candidate_fingerprint: string
    evidence_file: string
    test_records: number
    approval_records: number
    change_note: string
    immutable: boolean
  }
  runtime_evidence: {
    rollout: { key: string; status: string; observations: number; outcomes: Record<string, number>; earlier_candidates: Array<{ key: string; observations: number }> }
    acceptance: { counts: Record<string, number>; settled?: number; available: boolean; orders: Array<{ order_id: string; customer_name: string; status: string }> }
    immutable: boolean
  }
}

export interface OrderTimelineEntry {
  at: string
  source: 'order' | 'gate' | 'runtime' | string
  kind: string
  actor: string
  detail: string
  work_item_id?: string
  chain_verified?: boolean
}

export interface OrderTimeline {
  order_id: string
  entries: OrderTimelineEntry[]
  counts: { order: number; gate: number; runtime: number; effect?: number; browser?: number }
  /** Stream by stream: what can be proven, and what is only recorded. */
  integrity?: {
    streams: Array<{ stream: string; level: string; note: string; checked: number; tamper_evident: boolean }>
    broken: string[]
    tamper_evident: string[]
    summary: string
  }
  business_chain_verified: boolean
  runtime_chain_available: boolean
  /** The effect journal is append-only but unchained, like the runtime half. */
  effect_chain_available?: boolean
  run_ids: string[]
}

export interface WorkOrderSummary {
  order_id: string
  workflow_id: string
  status: string
  customer_name: string
  brand_name: string
  customer_id?: string
  environment_id?: string
  source: string
  reviewer_employee_id: string
  review_note: string
  launched_task_id: string
  /** How many orders this order's run started (and theirs) are still going; counted by the server. */
  running_child_orders?: number
  /** The order's own fields as the server keeps them. The invoke node writes the parent contract in here. */
  fields?: Record<string, unknown> | null
  /** Who handles it, by when, for how much; null until someone sets one (`order_handling_update`). */
  handling?: OrderHandling | null
  created_at: string
  updated_at: string
  events_chain_verified?: boolean
  fde_control_plane?: {
    phase: string
    as_is: string
    to_be: string
    release?: { workflow_version?: string; status?: string }
    uat?: { status?: string; criteria?: string[] }
    sla?: Record<string, unknown>
    rollback?: { available?: boolean; plan?: string }
    roi?: Record<string, unknown>
    trace?: Record<string, unknown>
  } | null
  acceptance?: {
    status: string
    criteria: string[]
    criteria_hash: string
    evidence: Array<Record<string, unknown>>
    evidence_hash: string
    workflow_version: string
    decision_note?: string
    round_no?: number
    rework_target?: string
    rework_feedback?: string
    previous_evidence_hash?: string
    runtime_rework?: {
      state: 'pending' | string
      request_id: string
      target_node: string
      attempt_count: number
      last_error?: string
    }
    artifact_snapshot?: {
      verified?: boolean
      files?: Array<{ source_path: string; archive_path: string; size: number; sha256: string }>
      error?: string
    }
    artifact_comparison?: {
      comparison?: 'byte_inventory' | string
      earlier_round?: number
      later_round?: number
      added?: string[]
      removed?: string[]
      changed?: Array<{ source_path: string; before_sha256: string; after_sha256: string; before_size: number; after_size: number }>
      unchanged?: string[]
      error?: string
    }
    history?: Array<Record<string, unknown> & {
      artifact_snapshot?: {
        verified?: boolean
        files?: Array<{ source_path: string; archive_path: string; size: number; sha256: string }>
      }
      artifact_comparison?: {
        comparison?: 'byte_inventory' | string
        earlier_round?: number
        later_round?: number
        added?: string[]
        removed?: string[]
        changed?: Array<{ source_path: string; before_sha256: string; after_sha256: string; before_size: number; after_size: number }>
        unchanged?: string[]
        error?: string
      }
    }>
  } | null
}

export interface WorkflowInvokeState {
  workflowId: string
  status: 'sending' | 'error'
  error?: string
}

export interface WorkflowPreflight {
  /** The node-shaped part of `blockers`, each tied to its step and field. */
  node_blockers?: NodeBlocker[]
  workflow_id: string
  workflow_version: string
  focus_node_id?: string
  status: string
  ready: boolean
  mode: 'simulated' | string
  blockers: string[]
  warnings?: string[]
  missing_fields: string[]
  steps: Array<{ node_id: string; name: string; type: string; status: string; tools: string[]; skills: string[]; gate: string; depends_on?: string[]; inputs?: string[]; deliverables?: string[] }>
  simulated_effects: Array<{ tool: string; mode: string; reason: string }>
  failure_explanation?: { category: string; root_cause: string; blocker_count: number; actions: string[]; basis: string } | null
}

export interface WorkflowNodeTrial {
  spec_id: string
  request_id: string
  project_id: string
  workflow_id: string
  workflow_version: string
  node_id: string
  budget_id: string
  model_call_limit: number
  image_limit?: number
  created_at?: string
  state: 'queued' | 'running' | 'succeeded' | 'failed' | 'validated' | 'outcome_unknown' | string
  artifacts: Array<{ path: string; size: number; sha256?: string; from_upstream?: boolean; changed_here?: boolean }>
  counts_as_evidence: boolean
  pinned_upstream?: boolean
  question?: { step: string; reason: string; context_note?: string; questions: Array<{ id: string; header: string; question: string; options: string[]; allow_freeform: boolean }> }
  pins?: Array<{ node_id: string; spec_id: string; files: Array<{ path: string; size: number; sha256: string }> }>
  released_by_assumption?: string[]
  budget?: { state?: string; consumed?: Record<string, number>; held?: Record<string, number> }
  round?: { outcome?: string; failure_reason?: string; provider_error?: string }
}

/** Everything the node-trial panel needs beyond the trials themselves, as one prop so the page does not grow five. */
export interface NodeTrialExtras {
  pinning: NodeTrialPinning | null
  onPlan: (workflowId: string, nodeId: string) => void
  artifact: TrialArtifactState
  onOpenArtifact: (specId: string, path: string) => void
  onCloseArtifact: () => void
  onAnswer: (workflowId: string, specId: string, reply: string) => void
  onAbandon: (specId: string) => void
}

export type OrderMetrics = {
  order_id: string
  saving: { state: string; value: number | null; bound: string; missing: string[]; rests_on: string[] }
  sla: { state: string; elapsed_hours: number | null; remaining_hours: number | null; missing: string[] }
}

export type WorkflowCompositionRequest = {
  callerId: string
  targetId: string
  seq: number
}

/** 4.7-A16: the builder step a component was opened from, held by App (not a route) so 组件库 can go back to it. */
export interface StepReturn {
  workflowId: string
  nodeId: string
  /** What the way back is labelled with: the step's and the flow's names as the builder showed them. */
  stepName: string
  flowName: string
}

interface WorkflowsPageProps {
  employeeTrial?: { projectId: string; ack: EmployeeWorkflowTrialAck | null; send: EmployeeWorkflowTrialSend;
    onOpenOrder: (orderId: string) => void }

  /** One truthful surface per main entry. `all` remains for embedded/legacy callers. */
  surface?: 'all' | 'builder' | 'orders'
  /** The flow open in the builder, for the top bar; null when the builder shows none. */
  onBuilderFlow?: (crumb: BuilderCrumb | null) => void
  /** Leave the builder for 组件库. Absent on a console that has no library page. */
  onOpenLibrary?: () => void
  /** Open 业务工单 on this queue (from a count elsewhere). A new `seq` asks again, even for the same queue. */
  orderQueueRequest?: { queue: OrderQueue; query?: string; open?: string; flow?: string; seq: number } | null
  /** Bring 搭建台 forward (业务工单 has no canvas of its own): the flow to show is already chosen here. */
  onShowBuilder?: () => void
  /** 搭建台's 安安协助: open 安安 beside the canvas. Its reference to the flow and step comes from the builder focus. */
  onOpenAssistant?: () => void
  /** A step's skill, opened read-only over the builder (the same component detail 组件库's 查看 shows). */
  onViewComponent?: (componentId: string) => void
  /** 4.7-A16: a step's skill, edited in 组件库; `from` is the step it was opened from, for 回到刚才的步骤. */
  onEditComponent?: (componentId: string, from: StepReturn) => void
  /** Back from 组件库 (回到刚才的步骤): open that flow on the builder with that step selected, once per request. */
  stepReturnRequest?: { workflowId: string; nodeId: string; seq: number } | null
  onStepReturnHandled?: (seq: number) => void
  /** The configured default model, when the console knows it: what an AI step runs on unless it names one. */
  defaultModel?: string
  /** The console's model providers (settings_get): which have a key here, and their models -- for a step's own model. */
  modelProviders?: ModelProvider[]
  /** 流程版本面板 in 验收准入: what the console last said about each flow's versions (null: it could not read them). */
  workflowVersions?: Record<string, FlowVersions | null>
  /** Ask for a flow's versions again; absent when this console may not (not the owner), and the panel is not shown. */
  onLoadVersions?: (workflowId: string) => void
  /** A project's name by id, for the projects that adopted a flow. */
  projectNameOf?: (projectId: string) => string
  workflows: WorkflowSummary[] | null
  orders: WorkOrderSummary[] | null
  error?: string | null
  invokeState: WorkflowInvokeState | null
  onRefresh: () => void
  onPublish?: (workflowId: string, expectedRevision: string) => void
  publishingWorkflowId?: string | null
  onRequestRelease?: (workflowId: string, expectedRevision: string) => void
  requestingReleaseWorkflowId?: string | null
  onDecideRelease?: (workflowId: string, expectedRevision: string, decision: 'approved' | 'rejected') => void
  decidingReleaseWorkflowId?: string | null
  onRollback?: (workflowId: string, expectedRevision: string) => void
  rollingBackWorkflowId?: string | null
  onRolloutStart?: (workflowId: string, expectedRevision: string) => void
  rolloutBusyWorkflowId?: string | null
  onRolloutObserve?: (workflowId: string, expectedRevision: string, outcome: 'passed' | 'failed' | 'unknown') => void
  onRolloutEvaluate?: (workflowId: string) => void
  onRolloutFinalize?: (workflowId: string, expectedRevision: string) => void
  onRolloutRollback?: (workflowId: string, expectedRevision: string) => void
  rolloutRollbackBusyWorkflowId?: string | null
  onCreateDraft?: (workflowId: string, draftId: string, name: string, summary: string, changeNote: string) => void
  creatingDraftWorkflowId?: string | null
  onRecordTest?: (workflowId: string, expectedRevision: string, outcome: 'passed' | 'failed' | 'stopped', notes: string, failureReason: string) => void
  onSimulateTest?: (workflowId: string, expectedRevision: string, fields: Record<string, string>) => void
  simulatingTestWorkflowId?: string | null
  recordingTestWorkflowId?: string | null
  onSubmitTesting?: (workflowId: string, expectedRevision: string, changeNote: string) => void
  submittingTestingWorkflowId?: string | null
  /** What the builder is on -- the flow, and the step whose editor is open -- for 安安 to offer as a reference. */
  onBuilderFocus?: (focus: BuilderFocus) => void
  /** ＋ 新建业务流: an empty draft run by a saved team. Absent = no such button. */
  onCreateBlank?: (draftId: string, team: string, name: string) => void
  creatingBlank?: boolean
  /** The saved teams a new flow can run on (org_saved_list): name is the id sent, label what is shown. */
  teams?: Array<{ name: string; label: string }>
  /** expectedRevision: the draft_revision the draft was made against ('' when unknown). */
  employeeDesign?: CanvasEmployeeDesign
  onUpdateNodes?: (workflowId: string, nodes: WorkflowNode[], expectedVersion: string, newVersion: string, changeNote: string, expectedRevision?: string) => void
  /** Bumped each time a node save succeeds (seq increases); the saved draft is then dropped. */
  nodesSaved?: { workflowId: string; seq: number } | null
  /** Node drafts held by App so they outlive the page; absent = the page keeps its own. */
  nodeDraftState?: NodeDraftState
  /** Project center staged one workflow inside another. It opens an unsaved builder draft only. */
  compositionRequest?: WorkflowCompositionRequest | null
  onCompositionHandled?: (seq: number) => void
  /** 调用 from 组件库: open this workflow on the builder with the component first on its shelf (seq changes per 调用). */
  broughtComponent?: { kind: 'skill'; id: string; name: string; workflowId: string; seq: number; trial?: { slug: string; version: string; source?: string } } | null
  onDismissBrought?: () => void
  /** Controls shown under the brought component on the shelf (a skill on trial: its test notes and verdict). */
  broughtExtra?: ReactNode
  /** Skills on trial (调用测试): a draft naming one is not saved until it is admitted to the internal library. */
  trialSkills?: string[]
  /** Components master control suggested from outside the library, per workflow, kept once its proposal was applied. */
  proposalCandidates?: Record<string, ProposalCandidate[]>
  /** A proposal was applied to a draft: its suggestions stay with that workflow's builder shelf. */
  onProposalApplied?: (workflowId: string, candidates: ProposalCandidate[]) => void
  /** What the builder shelf offers for one suggested component (调用测试, or where to find it). */
  candidateActions?: (workflowId: string, candidate: { id: string; source: 'skillhub' | 'catalog'; name: string; can_try?: boolean }) => ReactNode
  /** The internal library's skills, so the canvas knows which ones a save can declare; absent = none offered. */
  internalSkills?: InternalSkill[]
  /** A master-control proposal to preview on the builder canvas; nothing is saved until the person saves. */
  nodeProposal?: NodeProposal | null
  /** The proposal was applied to the draft or discarded: the page no longer previews it. */
  onNodeProposalDone?: () => void
  /** The org's employees, so the canvas 组件库 can offer them as draggable chips (§2.3 万物皆组件). */
  employees?: OrgEmployee[]
  /** The runs paused for a person, as the console already holds them. Without them an order stopped at a human
   *  gate looks exactly like one that is running. Nothing here answers a pause: that stays in the approval centre. */
  blockers?: DashboardBlocker[]
  /** The org those employees belong to; the canvas offers them only on workflows that run on this org. */
  employeesOrgId?: string
  /** The contact book as last read (null = not read): a person's step says whether its person can be reached. */
  contacts?: BookContact[] | null
  /** 去绑定: take the owner to 人员与职责, where a member's channels are bound. Absent = no link. */
  onOpenPeople?: () => void
  /** Owner-only. Records that a person reviewed a drafted skill; absent means this account may look and not vouch. */
  onReviewSkill?: (workflowId: string, expectedRevision: string, skillId: string, note: string) => void
  updatingWorkflowId?: string | null
  preflight?: WorkflowPreflight | null
  onPreflight?: (workflowId: string, fields: Record<string, string>, nodeId?: string) => void
  preflightingWorkflowId?: string | null
  nodeTrials?: WorkflowNodeTrial[]
  onNodeTrial?: (workflowId: string, workflowVersion: string, nodeId: string, pins: NodeTrialPin[], imageLimit?: number) => void
  nodeTrialExtras?: NodeTrialExtras
  /** One tab edits a workflow at a time (this browser only). Absent: every tab may edit, as before. */
  editLock?: EditLockApi
  /** The last save the server refused: which steps and fields it pointed at. Cleared by the next save that passes. */
  refusedSave?: { workflowId: string; blockers: NodeBlocker[] } | null
  /** The last 提交测试 / 申请发布审批 the server refused, shown beside that button until the next action: a toast
   *  alone is gone before the owner looks up. */
  admissionRefusal?: AdmissionRefusal | null
  nodeTrialBusy?: boolean
  /** Whole-flow draft and published-pin trials for this project (Owner). */
  flowTrials?: Array<NonNullable<FlowTrialSummary>>
  publishedFlowTrials?: readonly PublishedFlowTrialCandidate[]
  publishedFlowTrialError?: string | null
  onFlowTrial?: (workflowId: string, workflowVersion: string, action: 'start' | 'continue' | 'new_round' | 'stop', limits: { perNode: number; total: number }, target: FlowTrialTarget) => void
  flowTrialBusy?: boolean
  onInvoke: (workflowId: string, fields: Record<string, string>, budgetLimits?: Record<string, number>) => void
  /** Owner-only: give an order that has no allowance yet its ceilings. Absent = the control is not offered.
   *  Returns whether the request went out; what was typed is kept until it did. */
  onCreateBudget?: (orderId: string, limits: Record<string, number>) => boolean
  /** Owner-only: take an order's open allowance away, with a reason (the way out when a ceiling was reached). */
  onCloseBudget?: (orderId: string, reason: string) => boolean
  /** Owner-only: raise the ceilings of an order's open allowance in place. Returns whether the request went out. */
  onRaiseBudget?: (orderId: string, limits: Record<string, number>) => boolean
  onReviewOrder: (orderId: string, decision: 'approve' | 'reject', note: string) => void
  /** Owner-only. Cancels a whole order: run suspended, turns asked to stop, effects not rolled back. */
  /** Returns whether the cancel was really sent; the reason stays in the box when it was not. */
  onCancelOrder?: (orderId: string, reason: string, cascadeChildren: boolean) => boolean
  /** The order whose 取消整单 is out and waiting for its receipt (lib/requestTracker): the button says so and waits. */
  cancellingOrderId?: string | null
  /** The order whose allowance write (grant / raise / close) is out: its budget buttons wait for the receipt. */
  budgetBusyOrderId?: string | null
  onOpenTask: (taskId: string) => void
  onAcceptanceGet: (orderId: string) => void
  onOrderTimeline?: (orderId: string) => void
  budgets?: RunBudget[]
  /** Derived numbers, each of which may honestly be "not calculated". */
  orderMetrics?: OrderMetrics[]
  /** What each order's model calls were for: work, or judging work. Read from the recorded cost rows. */
  orderUsage?: OrderUsage[]
  /** Only a person can say what happened to a call nobody has an answer for. */
  onResolveBudget?: (reservationId: string, happened: boolean) => void
  pendingReservations?: Array<{ reservation_id: string; budget_id: string; kind: string; amount: number; note: string }>
  releaseEvidence?: ReleaseEvidence | null
  onReleaseEvidence?: (workflowId: string) => void
  blueprints?: SolutionBlueprint[]
  onSaveBlueprint?: (orderId: string, name: string) => void
  onApplyBlueprint?: (blueprintId: string, orderId: string) => void
  orderTimeline?: OrderTimeline | null
  /** What settings the open order was launched under, from its own event. null = it has none. */
  orderModelPlan?: ModelPlan
  orderTimelineError?: string | null
  acceptanceBusyOrderId?: string | null
  onAcceptanceEvidence: (orderId: string, criterion: string, evidence: Record<string, unknown>) => void
  onAcceptanceSubmit: (orderId: string) => void
  /** `closeOrder`: 确认收货并结案 -- the server takes a delivering order through evidence, submission, this decision and the close. */
  onAcceptanceDecide: (orderId: string, decision: 'accepted' | 'rejected' | 'accepted_with_exceptions', note: string, exceptions: string[], options?: { closeOrder?: boolean }) => void
  onAcceptanceRework: (orderId: string, targetNode: string, requestId?: string) => void
  onAcceptanceRoundExport?: (orderId: string, roundNo: number) => void
  onPortfolioExport?: (orderId: string) => void
  /** The order whose acceptance panel starts open (a link into one order's acceptance; the panel is otherwise closed). */
  initialAcceptanceOpenId?: string
  /** The order whose drawer starts open -- for a test. A link into one order goes through `orderQueueRequest.open`,
   *  which also asks for its timeline. */
  initialDrawerOrderId?: string
  /** The server listed only the latest part of the orders (its order list is capped). The page says so, so nobody
   *  takes the list, or the counts above it, for all of them. Absent = the whole list. */
  ordersTruncated?: boolean
  /** 4.7-A08 「查看更早的工单」: ask for the next older page (added below the list). Absent = nothing older to ask for. */
  onLoadOlderOrders?: () => void
  olderOrdersLoading?: boolean
  /** A card the page adds to one order's drawer (the owner's 分发授权单). Absent = none. */
  drawerExtra?: (order: WorkOrderSummary) => ReactNode
  /** order_metrics' `truncated`: the cost / saving / promise figures below cover only the newest orders the store
   *  handed back (its hard cap), so the page says so instead of showing them as everyone's. Absent = all of them. */
  orderMetricsTruncated?: boolean
  /** P5-08: the customer of this cleanly accepted order explicitly authorized redistributing the version it ran. */
  onAuthorizeRedistribution?: (orderId: string) => void
  /** Open a path-backed acceptance artifact in the shared preview drawer. */
  onOpenArtifact?: (path: string) => void
  onUpdateFdeControlPlane?: (orderId: string, patch: Record<string, unknown>) => void
  updatingFdeControlPlaneOrderId?: string | null
  /** 经办: save an order's assignee / due date / amounts. Absent = read only (not the owner's console). */
  onUpdateHandling?: (orderId: string, patch: Record<string, unknown>) => void
  updatingHandlingOrderId?: string | null
  connected?: boolean
  orderScope?: { customer_id: string; environment_id: string }
  onOrderScopeChange?: (scope: { customer_id: string; environment_id: string }) => void
  /** Shared artifact drawer used while reviewing an order or acceptance. */
  artifactsSlot?: ReactNode
  onCustomerPreview?: (orderId: string) => void
  customerPreviewSlot?: ReactNode
  /** 4.7-A11 提一个改进 on a published flow's card; absent = no such control. */
  improvementSlot?: (workflow: WorkflowSummary) => ReactNode
  /** 4.7-A11: open this flow on the builder canvas, once per seq (the draft an improvement just made). */
  openFlowRequest?: { workflowId: string; seq: number } | null
  onOpenFlowHandled?: (seq: number) => void
}

function formatMinutes(value: number | null): string {
  if (value === null || Number.isNaN(value)) return '—'
  return value >= 60 ? `${(value / 60).toFixed(1)} h` : `${Math.round(value)} min`
}

export interface OrderUsage {
  order_id: string
  total: { calls: number; tokens_in: number; tokens_out: number; cost: number }
  judgement: {
    calls: number; tokens_in: number; tokens_out: number; cost: number
    share_of_tokens: number | null; output_share_of_judgement: number | null; share_of_calls: number | null
  }
  by_purpose: Array<{ purpose: string; calls: number; tokens_in: number; tokens_out: number; cost: number }>
  limits: string[]
}

const percent = (value: number | null | undefined) => (value == null ? '—' : `${Math.round(value * 1000) / 10}%`)

type Translate = ReturnType<typeof useI18n>['t']

/** Why an allowance waits for an answer, in the owner's words. The server writes the reasons it knows in English (and
 *  rows written before AN still carry them); a reason that is already a sentence in the owner's words is said as is. */
function budgetHoldReason(reason: string, t: Translate): string {
  const said = reason.trim()
  if (/^held by a (console|process) that stopped/i.test(said)) return t('orders.budget.reason.consoleStopped')
  if (/^model outcome uncertain/i.test(said)) return t('orders.budget.reason.modelUncertain')
  if (/^dispatch outcome not established/i.test(said)) return t('orders.budget.reason.dispatchUnknown')
  const heldFor = /^held for more than (\d+) minutes?$/i.exec(said)
  if (heldFor) return t('orders.budget.reason.heldTooLong', { n: heldFor[1] })
  if (/^outcome unknown$/i.test(said)) return t('orders.budget.reason.outcomeUnknown')
  return said
}

const BUDGET_COUNTED_KINDS = new Set(['image', 'model_call', 'retry', 'cost_cny'])

/** How many of what a held call would have spent: 「1 次模型调用」, never 「1 model_call」. */
function budgetAmountInWords(amount: number, kind: string, t: Translate): string {
  const key = (BUDGET_COUNTED_KINDS.has(kind) ? `orders.budget.count.${kind}` : 'orders.budget.count.other') as Parameters<Translate>[0]
  return t(key, { n: amount })
}

export function WorkflowsPage({ employeeTrial, surface = 'all', onBuilderFlow, onOpenLibrary, orderQueueRequest = null, onShowBuilder, onOpenAssistant, onViewComponent, onEditComponent, stepReturnRequest = null, onStepReturnHandled, defaultModel = '', modelProviders, workflowVersions = {}, onLoadVersions, projectNameOf, workflows: allWorkflows, orders, error, invokeState, onOrderTimeline, budgets, orderMetrics, orderUsage, onResolveBudget, pendingReservations, releaseEvidence, onReleaseEvidence, blueprints, onSaveBlueprint, onApplyBlueprint, orderTimeline, orderTimelineError, orderModelPlan = null, onRefresh, onPublish, publishingWorkflowId, onRequestRelease, requestingReleaseWorkflowId, onDecideRelease, decidingReleaseWorkflowId, onRollback, rollingBackWorkflowId, onRolloutStart, rolloutBusyWorkflowId, onRolloutObserve, onRolloutEvaluate, onRolloutFinalize, onRolloutRollback, rolloutRollbackBusyWorkflowId, onCreateDraft, creatingDraftWorkflowId, onRecordTest, recordingTestWorkflowId, onSimulateTest, simulatingTestWorkflowId, onSubmitTesting, submittingTestingWorkflowId, onUpdateNodes, employeeDesign, onBuilderFocus, onCreateBlank, creatingBlank = false, teams = [], nodesSaved = null, nodeDraftState, compositionRequest = null, onCompositionHandled, broughtComponent = null, onDismissBrought, broughtExtra, trialSkills = [], proposalCandidates = {}, onProposalApplied, candidateActions, internalSkills, nodeProposal = null, onNodeProposalDone, employees = [], employeesOrgId = '', contacts = null, onOpenPeople, blockers = [], onReviewSkill, updatingWorkflowId, preflight, onPreflight, preflightingWorkflowId, nodeTrials = [], onNodeTrial, nodeTrialExtras, editLock, refusedSave = null, admissionRefusal = null, nodeTrialBusy = false, flowTrials = [], publishedFlowTrials = [], publishedFlowTrialError = null, onFlowTrial, flowTrialBusy = false, onInvoke, onReviewOrder, onCancelOrder, onOpenTask, onAcceptanceGet, acceptanceBusyOrderId, onAcceptanceEvidence, onAcceptanceSubmit, onAcceptanceDecide, onAcceptanceRework, onAcceptanceRoundExport, onPortfolioExport, onAuthorizeRedistribution, initialAcceptanceOpenId, initialDrawerOrderId, ordersTruncated = false, onLoadOlderOrders, olderOrdersLoading = false, drawerExtra, orderMetricsTruncated = false, onCreateBudget, onCloseBudget, onRaiseBudget, onOpenArtifact, onUpdateFdeControlPlane, updatingFdeControlPlaneOrderId, onUpdateHandling, updatingHandlingOrderId = null, cancellingOrderId = null, budgetBusyOrderId = null, connected = true, orderScope = { customer_id: '', environment_id: '' }, onOrderScopeChange, artifactsSlot, onCustomerPreview, customerPreviewSlot, improvementSlot, openFlowRequest = null, onOpenFlowHandled }: WorkflowsPageProps) {
  const { t, locale } = useI18n()
  const focusProject = typeof window === 'undefined' ? '' : reportProject(window.location.search)
  const workflows = useMemo(() => allWorkflows ? reportWorkflows(allWorkflows, focusProject) : allWorkflows, [allWorkflows, focusProject])
  const [openId, setOpenId] = useState<string | null>(null)
  const [drafts, setDrafts] = useState<Record<string, Record<string, string>>>({})
  const [launchBudgets, setLaunchBudgets] = useState<Record<string, BudgetDraft>>({})
  const [orderBudget, setOrderBudget] = useState<{ orderId: string; draft: BudgetDraft }>({ orderId: '', draft: {} })
  const [closingBudget, setClosingBudget] = useState<{ orderId: string; reason: string }>({ orderId: '', reason: '' })
  const [raisingBudget, setRaisingBudget] = useState<{ orderId: string; draft: Record<string, string> }>({ orderId: '', draft: {} })
  const [acceptanceOpenId, setAcceptanceOpenId] = useState<string | null>(initialAcceptanceOpenId ?? null)
  const [timelineOpenId, setTimelineOpenId] = useState<string | null>(null)
  const [blueprintNames, setBlueprintNames] = useState<Record<string, string>>({})
  const [blueprintChoice, setBlueprintChoice] = useState<Record<string, string>>({})
  const [evidenceDrafts, setEvidenceDrafts] = useState<Record<string, Record<string, { runId: string; artifactId: string; note: string }>>>({})
  const [acceptanceNotes, setAcceptanceNotes] = useState<Record<string, string>>({})
  const [acceptanceExceptions, setAcceptanceExceptions] = useState<Record<string, string>>({})
  const [acceptanceReworkTargets, setAcceptanceReworkTargets] = useState<Record<string, string>>({})
  const [reviewNotes, setReviewNotes] = useState<Record<string, string>>({})
  const [cancelCascade, setCancelCascade] = useState<Record<string, boolean>>({})
  const [fdeEditorOpenId, setFdeEditorOpenId] = useState<string | null>(null)
  const [fdeDrafts, setFdeDrafts] = useState<Record<string, { asIs: string; toBe: string; uat: string; sla: string; rollback: string; roi: string }>>({})
  const [fdeEditorErrors, setFdeEditorErrors] = useState<Record<string, string>>({})
  // Node drafts, the builder's workflow and the version fields live above the page when App passes them, so a
  // trip to 组件库 and back keeps them (see useNodeDraftState); a page rendered on its own keeps its own.
  const ownDraftState = useNodeDraftState()
  const {
    editingWorkflowId, setEditingWorkflowId, nodeDrafts, setNodeDrafts, builderWorkflowId, setBuilderWorkflowId,
    nodeVersions, setNodeVersions, nodeBaseVersions, setNodeBaseVersions, nodeChangeNotes, setNodeChangeNotes, handledSavedSeq, handledBroughtSeq,
    nodeBaseRevisions, setNodeBaseRevisions,
  } = nodeDraftState ?? ownDraftState
  const builderRef = useRef<HTMLElement>(null)
  const [blankFormOpen, setBlankFormOpen] = useState(false)
  const [builderDraftOpenId, setBuilderDraftOpenId] = useState<string | null>(null)
  const [builderDraftIds, setBuilderDraftIds] = useState<Record<string, string>>({})
  const [builderDraftNames, setBuilderDraftNames] = useState<Record<string, string>>({})
  const [builderDraftSummaries, setBuilderDraftSummaries] = useState<Record<string, string>>({})
  const [builderDraftNotes, setBuilderDraftNotes] = useState<Record<string, string>>({})
  const [testRecordOpen, setTestRecordOpen] = useState<{ workflowId: string; expectedRevision: string } | null>(null)
  // 搭建台's 测试 and 验收准入 dialogs (4.7 A2-2), and what they ask of the canvas (a view, a step on a page).
  const [actionDialog, setActionDialog] = useState<{ kind: 'test' | 'admit'; workflowId: string; expectedRevision: string } | null>(null)
  const [builderRequest, setBuilderRequest] = useState<{ workflowId: string; view: 'canvas' | 'preview' | 'package'; nodeId?: string; tab?: 'inputs' | 'config' | 'output'; seq: number } | null>(null)
  const askBuilder = (workflowId: string, view: 'canvas' | 'preview' | 'package', nodeId?: string, tab?: 'inputs' | 'config' | 'output') =>
    setBuilderRequest(current => ({ workflowId, view, nodeId, tab, seq: (current?.seq ?? 0) + 1 }))
  useEffect(() => {
    if (!actionDialog) return
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setActionDialog(null) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [actionDialog])
  // 验收准入 reads the flow's versions afresh each time it opens (once per opening, whatever the parent re-renders).
  const loadVersionsRef = useRef(onLoadVersions)
  loadVersionsRef.current = onLoadVersions
  useEffect(() => {
    if (actionDialog?.kind === 'admit') loadVersionsRef.current?.(actionDialog.workflowId)
  }, [actionDialog])
  const [testRecordOutcomes, setTestRecordOutcomes] = useState<Record<string, 'passed' | 'failed' | 'stopped'>>({})
  const [testRecordNotes, setTestRecordNotes] = useState<Record<string, string>>({})
  const [testRecordFailures, setTestRecordFailures] = useState<Record<string, string>>({})
  const [refreshing, setRefreshing] = useState(false)
  const [scopeDraft, setScopeDraft] = useState(orderScope)
  const compositionHandledSeq = useRef(0)
  const [compositionNotice, setCompositionNotice] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null)

  useEffect(() => {
    setScopeDraft(orderScope)
  }, [orderScope.customer_id, orderScope.environment_id])

  useEffect(() => {
    setRefreshing(false)
  }, [workflows, orders, error])

  const setField = (workflowId: string, fieldId: string, value: string) => {
    setDrafts(prev => ({ ...prev, [workflowId]: { ...(prev[workflowId] ?? {}), [fieldId]: value } }))
  }

  const beginFdeEdit = (order: WorkOrderSummary) => {
    const plane = order.fde_control_plane
    const stringify = (value: unknown) => value && typeof value === 'object' ? JSON.stringify(value, null, 2) : ''
    setFdeEditorOpenId(order.order_id)
    setFdeEditorErrors(prev => ({ ...prev, [order.order_id]: '' }))
    setFdeDrafts(prev => ({
      ...prev,
      [order.order_id]: {
        asIs: plane?.as_is ?? '',
        toBe: plane?.to_be ?? '',
        uat: stringify(plane?.uat),
        sla: stringify(plane?.sla),
        rollback: stringify(plane?.rollback),
        roi: stringify(plane?.roi),
      },
    }))
  }

  const submitFdeEdit = (orderId: string) => {
    const draft = fdeDrafts[orderId]
    if (!draft || !onUpdateFdeControlPlane) return
    const patch: Record<string, unknown> = {}
    if (draft.asIs.trim()) patch.as_is = draft.asIs.trim()
    if (draft.toBe.trim()) patch.to_be = draft.toBe.trim()
    for (const key of ['uat', 'sla', 'rollback', 'roi'] as const) {
      const raw = draft[key].trim()
      if (!raw) continue
      try {
        const parsed = JSON.parse(raw)
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('object required')
        patch[key] = parsed
      } catch {
        setFdeEditorErrors(prev => ({ ...prev, [orderId]: `${key} must be valid JSON object` }))
        return
      }
    }
    if (Object.keys(patch).length === 0) {
      setFdeEditorErrors(prev => ({ ...prev, [orderId]: 'enter at least one editable field' }))
      return
    }
    setFdeEditorErrors(prev => ({ ...prev, [orderId]: '' }))
    onUpdateFdeControlPlane(orderId, patch)
  }

  const draftFrom = (wf: WorkflowSummary): WorkflowNode[] =>
    wf.nodes.map(node => ({ ...node, depends_on: [...node.depends_on], deliverables: [...node.deliverables], inputs: [...(node.inputs ?? [])], skills: [...(node.skills ?? [])], tools: [...(node.tools ?? [])], invoke: { ...node.invoke }, ...(node.employee_binding ? { employee_binding: copyEmployeeBinding(node.employee_binding) } : {}), canvas: node.canvas ? { ...node.canvas } : node.canvas }))

  // The 编辑节点契约 button: start (or restart) a draft from what is saved.
  const beginNodeEdit = (wf: WorkflowSummary) => {
    setEditingWorkflowId(wf.id)
    setNodeDrafts(prev => ({ ...prev, [wf.id]: draftFrom(wf) }))
    setNodeVersions(prev => ({ ...prev, [wf.id]: wf.version }))
    setNodeBaseVersions(prev => ({ ...prev, [wf.id]: wf.version }))
    setNodeBaseRevisions(prev => ({ ...prev, [wf.id]: wf.draft_revision ?? '' }))
    setNodeChangeNotes(prev => ({ ...prev, [wf.id]: '' }))
  }

  // Canvas edits: open a draft only if there is none. Several patches can land in one event (a multi-node
  // drag, deleting several wires); restarting the draft for each would wipe the ones before it.
  const ensureNodeDraft = (wf: WorkflowSummary) => {
    setEditingWorkflowId(wf.id)
    setNodeDrafts(prev => (prev[wf.id] ? prev : { ...prev, [wf.id]: draftFrom(wf) }))
    setNodeVersions(prev => (prev[wf.id] !== undefined ? prev : { ...prev, [wf.id]: wf.version }))
    setNodeBaseVersions(prev => (prev[wf.id] !== undefined ? prev : { ...prev, [wf.id]: wf.version }))
    setNodeBaseRevisions(prev => (prev[wf.id] !== undefined ? prev : { ...prev, [wf.id]: wf.draft_revision ?? '' }))
    setNodeChangeNotes(prev => (prev[wf.id] !== undefined ? prev : { ...prev, [wf.id]: '' }))
  }

  // Forget a draft and the version fields that belong to it (取消, or after its save succeeded).
  // The draft a refused save was about, by reference: drafts are immutable, so any edit or a discard makes a
  // different value and the refusal stops being shown as if it still described what is on screen.
  const refusedDraft = useRef<{ save: typeof refusedSave; draft: WorkflowNode[] | undefined }>({ save: null, draft: undefined })
  if (refusedDraft.current.save !== refusedSave) {
    refusedDraft.current = { save: refusedSave, draft: refusedSave ? nodeDrafts[refusedSave.workflowId] : undefined }
  }

  // The browser's copy goes in the same step (dropDraft). Left to the persistence effect it would still be there
  // when this page renders next, and the page would offer to recover the draft that was just discarded or saved.
  const dropNodeDraft = (workflowId: string) => dropDraft({ setEditingWorkflowId, setNodeDrafts, setNodeVersions, setNodeBaseVersions, setNodeBaseRevisions, setNodeChangeNotes }, workflowId)

  // 整体撤销 安安's change (4.7 A04): a proposal applied to a draft is remembered with the table it was checked
  // against; once that draft is saved, the builder offers to put that table back -- as a new draft the person saves
  // as a new version. Nothing is rewritten in place: the change and its undoing are both versions on record.
  // The steps themselves are lib/nodeProposal's (appliedProposal, revertDraft), where a test walks them end to end.
  const [appliedProposals, setAppliedProposals] = useState<Record<string, ProposalRevert>>({})
  const [proposalRevert, setProposalRevert] = useState<ProposalRevert | null>(null)
  const forgetApplied = (workflowId: string) => setAppliedProposals(prev => {
    if (!prev[workflowId]) return prev
    const next = { ...prev }
    delete next[workflowId]
    return next
  })

  // 取消: drop the draft, so the canvas and the list show what is saved again.
  const discardNodeDraft = (wf: WorkflowSummary) => { dropNodeDraft(wf.id); forgetApplied(wf.id) }

  // A save went through: the draft is what is saved now. Keeping it would leave a stale base version behind,
  // and the next save from it would be refused as a conflict with the version it just created.
  // Each save notice is acted on once: a page mounted again later sees the same notice and must not drop a draft
  // opened after it.
  useEffect(() => {
    const savedWorkflowId = takeSavedNotice(handledSavedSeq, nodesSaved)
    if (savedWorkflowId) {
      const applied = appliedProposals[savedWorkflowId]
      if (applied) { setProposalRevert(applied); forgetApplied(savedWorkflowId) }
      dropNodeDraft(savedWorkflowId)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodesSaved?.seq])
  const revertProposal = (wf: WorkflowSummary, offer: ProposalRevert) => {
    const undo = revertDraft(offer, wf, locale.startsWith('zh'))
    setProposalRevert(null)
    setEditingWorkflowId(wf.id)
    setNodeDrafts(prev => ({ ...prev, [wf.id]: undo.nodes }))
    setNodeBaseVersions(prev => ({ ...prev, [wf.id]: undo.baseVersion }))
    setNodeBaseRevisions(prev => ({ ...prev, [wf.id]: undo.baseRevision }))
    setNodeVersions(prev => ({ ...prev, [wf.id]: undo.version }))
    setNodeChangeNotes(prev => ({ ...prev, [wf.id]: undo.note }))
  }

  const suggestSemanticVersion = (workflowId: string, liveVersion: string) => {
    setNodeVersions(prev => {
      const current = prev[workflowId] ?? liveVersion
      return current === liveVersion ? { ...prev, [workflowId]: nextPatchVersion(liveVersion) } : prev
    })
  }

  const updateNodeDraft = (workflowId: string, nodeId: string, patch: Partial<WorkflowNode>) => {
    setNodeDrafts(prev => ({
      ...prev,
      [workflowId]: (prev[workflowId] ?? []).map(node => node.id === nodeId ? { ...node, ...patch } : node),
    }))
    if (Object.keys(patch).some(key => key !== 'canvas')) {
      const liveVersion = workflows?.find(workflow => workflow.id === workflowId)?.version ?? ''
      suggestSemanticVersion(workflowId, liveVersion)
    }
  }

  const mayEditNodesHere = (wf: WorkflowSummary) => Boolean(onUpdateNodes) && (wf.status === 'draft' || wf.status === 'testing')
  // Another tab of this browser is editing it: read-only here until that tab is done or this one takes over.
  const editedElsewhere = (wf: WorkflowSummary) => mayEditNodesHere(wf) && editLock?.stateOf(wf.id) === 'other'
  const mayEditNodes = (wf: WorkflowSummary) => mayEditNodesHere(wf) && !editedElsewhere(wf)
  // An unsaved draft this browser kept across a reload, looked up once per workflow per render and only while no
  // draft is open. While a recoverable one is waiting for the person's decision the workflow is not editable: every
  // way of opening a fresh draft (a nudge on the canvas, 编辑节点契约, applying a proposal, a composition) would
  // otherwise be stored over it, and hours of unsaved work would be gone without anyone having decided that.
  const offers = new Map<string, RecoveryOffer>()
  const offerFor = (wf: WorkflowSummary): RecoveryOffer => {
    if (!mayEditNodes(wf) || nodeDrafts[wf.id]) return { kind: 'none' }
    let offer = offers.get(wf.id)
    if (!offer) {
      offer = recoveryOffer(loadDraft(wf.id), wf.version, draftFrom(wf))
      offers.set(wf.id, offer)
    }
    return offer
  }
  const canEditNodes = (wf: WorkflowSummary) => mayEditNodes(wf) && offerFor(wf).kind !== 'recover'

  // The canvas builder is the front stage of this page (policy: the most valuable module now and the
  // FDE's high-frequency deployment screen). It edits the same node draft the list editor below does.
  // A draft with no steps yet (＋ 新建业务流) is built here too: its first step is added on this canvas.
  const buildable = (workflows ?? []).filter(wf => wf.status !== 'retired' && ((wf.nodes?.length ?? 0) > 0 || wf.status === 'draft'))
  const builderWorkflow = buildable.find(wf => wf.id === builderWorkflowId) ?? buildable.find(canEditNodes) ?? buildable[0]
  const [packageHost, setPackageHost] = useState<HTMLDivElement | null>(null)
  const [openCanvasSignal, setOpenCanvasSignal] = useState(0)
  // The top bar says which flow this is. Reported upward as plain facts; the page keeps the state.
  const crumbDraft = builderWorkflow && editingWorkflowId === builderWorkflow.id ? (nodeDrafts[builderWorkflow.id] ?? null) : null
  const crumb = surface !== 'orders' && builderWorkflow ? builderCrumb(builderWorkflow, crumbDraft) : null
  const crumbKey = crumb ? JSON.stringify(crumb) : ''
  useEffect(() => {
    onBuilderFlow?.(crumb)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [crumbKey])
  useEffect(() => () => { onBuilderFlow?.(null) },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [])
  // 业务工单: which queue is shown and what is searched for. A view of the orders already here; nothing is asked anew.
  const [orderQueue, setOrderQueue] = useState<OrderQueue>('all')
  const [orderQuery, setOrderQuery] = useState('')
  const [newOrderOpen, setNewOrderOpen] = useState(false)
  // 新建工单's form, in place on 业务工单 (the package card's form lives inside 搭建台's 工作流包 view, which this page
  // never shows). Closed once the send for it comes back placed; an error keeps it open with the reason.
  const [newOrderFlowId, setNewOrderFlowId] = useState<string | null>(null)
  const newOrderFlow = newOrderFlowId ? (workflows ?? []).find(wf => wf.id === newOrderFlowId && wf.invokable) ?? null : null
  const lastInvoke = useRef(invokeState)
  useEffect(() => {
    const before = lastInvoke.current
    lastInvoke.current = invokeState
    if (before?.status === 'sending' && before.workflowId === newOrderFlowId && invokeState === null) setNewOrderFlowId(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [invokeState])
  const orderQueueSeq = orderQueueRequest?.seq ?? 0
  useEffect(() => {
    if (!orderQueueRequest) return
    setOrderQueue(orderQueueRequest.queue); setOrderQuery(orderQueueRequest.query ?? ''); setOrderFlow(orderQueueRequest.flow ?? '')
    // Sent here to see one order: open it, and bring its row into view behind the drawer, so closing the drawer
    // leaves the person looking at it instead of at the top of a list they have to search again.
    const one = orderQueueRequest.open ?? ''
    if (one) { openOrderDrawer(one); window.setTimeout(() => document.querySelector(`[data-order-row="${CSS.escape(one)}"]`)?.scrollIntoView({ block: 'center' }), 80) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orderQueueSeq])
  // The order read in the drawer. Opening it asks for that order's timeline, as the row's own button does.
  const [drawerOrderId, setDrawerOrderId] = useState<string | null>(initialDrawerOrderId ?? null)
  // The rows whose tools (review, cancel, budget, template...) are unfolded. In the new shell a row is one line until asked.
  const [openRowIds, setOpenRowIds] = useState<readonly string[]>([])
  const [orderFlow, setOrderFlow] = useState('')
  const drawerOrder = drawerOrderId ? (orders ?? []).find(order => order.order_id === drawerOrderId) ?? null : null
  const openOrderDrawer = (orderId: string) => { setDrawerOrderId(orderId); onOrderTimeline?.(orderId) }
  const goToOrderRow = (orderId: string) => {
    setDrawerOrderId(null)
    setOrderQueue('all'); setOrderQuery(''); setOrderFlow('')
    setOpenRowIds(current => (current.includes(orderId) ? current : [...current, orderId]))
    window.setTimeout(() => document.querySelector(`[data-order-row="${CSS.escape(orderId)}"]`)?.scrollIntoView({ block: 'center', behavior: 'smooth' }), 60)
  }
  // Two different things wait on a person inside a running order: a pause the run raised, and an allowance that
  // stopped. Both belong in 等人处理. The set is the one 项目中心 counts with too (lib/orderQueues' personWaits), and
  // the list, the counts and the drawer all read it, so none of them can disagree.
  const personWaits = personWaitsOf(orders, budgets, blockers)
  const employeeName = (id: string) => employees.find(person => person.employee_id === id)?.name || id
  const orderCounts = queueCounts(orders ?? [], personWaits)
  // 逾期 (the prototype's quick filter): past the due date the 经办 card records, and not ended. From that date only --
  // an order with no due date is never called overdue.
  const [overdueOnly, setOverdueOnly] = useState(false)
  const overdueNow = new Date()
  const isOverdue = (order: WorkOrderSummary) => dueState(order.handling?.due_at, overdueNow, true, queueOf(order, personWaits) === 'ended').kind === 'overdue'
  const overdueCount = (orders ?? []).filter(isOverdue).length
  const listedOrders = orders ? shownOrders(orders, orderQueue, orderQuery, personWaits, orderFlow).filter(order => !overdueOnly || isOverdue(order)) : []
  // The flow an order runs on, by the name people call it. The page already holds every flow for 新建工单.
  const flowOf = (id: string) => (workflows ?? []).find(flow => flow.id === id) ?? null
  const openInBuilder = (workflowId: string) => {
    setBuilderWorkflowId(workflowId)
    setOpenCanvasSignal(signal => signal + 1)
    // An instant jump: a smooth scroll waits on animation frames, which a hidden or background tab does not run.
    builderRef.current?.scrollIntoView({ block: 'start' })
  }

  // A proposal from master control opens its workflow on the builder canvas.
  useEffect(() => {
    if (nodeProposal?.package_id) openInBuilder(nodeProposal.package_id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeProposal])
  const proposalMissing = Boolean(nodeProposal && workflows && !buildable.some(wf => wf.id === nodeProposal.package_id))
  // 4.7-A11: the draft 提一个改进 just made opens on the canvas, once per request.
  useEffect(() => {
    if (!openFlowRequest?.workflowId) return
    openInBuilder(openFlowRequest.workflowId)
    onOpenFlowHandled?.(openFlowRequest.seq)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openFlowRequest?.seq])

  // 调用 from 组件库 opens its workflow on the builder, once per 调用 (not again each time the page comes back).
  useEffect(() => {
    const workflowId = takeSavedNotice(handledBroughtSeq, broughtComponent)
    if (workflowId) openInBuilder(workflowId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [broughtComponent?.seq])

  // 回到刚才的步骤 from 组件库 (4.7-A16): that flow on the builder, that step selected with its editor open. Once: App
  // drops the request when told it was handled, so coming back to this page later does not jump there again.
  useEffect(() => {
    if (!stepReturnRequest || !workflows) return
    if (buildable.some(wf => wf.id === stepReturnRequest.workflowId)) {
      openInBuilder(stepReturnRequest.workflowId)
      askBuilder(stepReturnRequest.workflowId, 'canvas', stepReturnRequest.nodeId)
    }
    onStepReturnHandled?.(stepReturnRequest.seq)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stepReturnRequest?.seq, workflows])

  // The internal-library skills a package can take on a step and declare when saved. The package payload says
  // (library_skills_for on the server); the catalog is only a fallback for a payload without it.
  // Trial skills this workflow's draft may not be saved with: a name the saved steps already carry (a placeholder
  // the process factory wrote, say) is not the trial's to block, even if a market skill of that name is on trial.
  const trialSkillsFor = (wf: WorkflowSummary) => {
    const saved = new Set(wf.nodes.flatMap(node => node.skills ?? []))
    return trialSkills.filter(skill => !saved.has(skill))
  }

  // Master control's suggestions still in play for this workflow: named on the draft's (or saved) steps and not yet
  // declared or in the library. A discarded draft or an admitted skill drops out without anyone clearing a list.
  const candidatesFor = (wf: WorkflowSummary): ProposalCandidate[] => {
    const onSteps = new Set((nodeDrafts[wf.id] ?? wf.nodes).flatMap(node => node.skills ?? []))
    const settled = new Set([...(wf.skill_contracts ?? []).map(skill => skill.id), ...librarySkillsFor(wf)])
    return (proposalCandidates[wf.id] ?? []).filter(candidate => onSteps.has(candidate.id) && !settled.has(candidate.id))
  }

  const librarySkillsFor = (wf: WorkflowSummary) => wf.library_skills
    ?? (wf.shared_skill_library === false ? [] : (internalSkills ?? []).filter(skill => libraryDeclarable(skill, wf.status)).map(skill => skill.id))

  // Who may be named on a workflow's nodes: the console org's employees, only when that org is the one the
  // workflow runs on (see rosterFor). The list editor and the canvas offer the same people.
  const rosterOf = (wf: WorkflowSummary) => rosterFor(wf.org_id, employeesOrgId, employees).roster

  // The live workflows as larger components (业务流): the canvas offers them as call steps and refuses a loop.
  const liveFlows = flowComponents(workflows ?? [])

  useEffect(() => {
    if (!compositionRequest || !workflows || compositionRequest.seq <= compositionHandledSeq.current) return
    compositionHandledSeq.current = compositionRequest.seq
    const caller = workflows.find(workflow => workflow.id === compositionRequest.callerId)
    const target = workflows.find(workflow => workflow.id === compositionRequest.targetId)
    if (!caller || !target) {
      setCompositionNotice({ kind: 'error', text: locale.startsWith('zh') ? '组合失败：业务流已经不存在，请刷新项目中心。' : 'Composition failed: a workflow no longer exists. Refresh Project Center.' })
    } else if (!canEditNodes(caller)) {
      setCompositionNotice({ kind: 'error', text: locale.startsWith('zh') ? `${caller.name} 不是草稿或测试中版本，不能直接改。` : `${caller.name} is not a draft or testing version and cannot be edited.` })
    } else {
      const base = nodeDrafts[caller.id] ?? draftFrom(caller)
      const result = composeWorkflowDraft(caller.id, { id: target.id, name: target.name }, base, liveFlows)
      if ('rejection' in result) {
        const reason = result.rejection === 'duplicate'
          ? (locale.startsWith('zh') ? '已经调用了这个业务流。' : 'This workflow is already called.')
          : result.rejection === 'cycle'
            ? (locale.startsWith('zh') ? '这次组合会形成循环调用。' : 'This composition would create a call cycle.')
            : (locale.startsWith('zh') ? '业务流不能调用自己。' : 'A workflow cannot call itself.')
        setCompositionNotice({ kind: 'error', text: reason })
      } else {
        setEditingWorkflowId(caller.id)
        setNodeDrafts(prev => ({ ...prev, [caller.id]: result.nodes }))
        setNodeBaseVersions(prev => ({ ...prev, [caller.id]: prev[caller.id] ?? caller.version }))
        setNodeBaseRevisions(prev => ({ ...prev, [caller.id]: prev[caller.id] ?? caller.draft_revision ?? '' }))
        setNodeVersions(prev => ({ ...prev, [caller.id]: prev[caller.id] ?? nextPatchVersion(caller.version) }))
        setNodeChangeNotes(prev => ({ ...prev, [caller.id]: prev[caller.id]?.trim() ? prev[caller.id] : `${locale.startsWith('zh') ? '组合业务流' : 'Compose workflow'}: ${target.name}` }))
        setBuilderWorkflowId(caller.id)
        setCompositionNotice({ kind: 'ok', text: locale.startsWith('zh') ? `已把「${target.name}」加入「${caller.name}」草稿。请在暂存区补角色，再保存版本。` : `Added “${target.name}” to the “${caller.name}” draft. Assign its role in staging, then save a version.` })
      }
    }
    onCompositionHandled?.(compositionRequest.seq)
    // One request is consumed once; draft state changes must not replay it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [compositionRequest?.seq, workflows])

  // The versioned save of a node draft. The list editor's footer and the expanded canvas render the same
  // controls over the same state, so building full-window never needs a detour back to the card to save.
  const nodeSaveControls = (wf: WorkflowSummary) => {
    const baseVersion = nodeBaseVersions[wf.id] ?? wf.version
    const baseRevision = nodeBaseRevisions[wf.id] ?? ''
    // Saved over at the same version (a layout-only save keeps it) is as stale as a newer version: saving would
    // quietly drop that save. The server refuses it too (expected_revision); this says so before the click.
    const stale = draftIsStale(baseVersion, wf.version)
      || Boolean(nodeDrafts[wf.id] && baseRevision && wf.draft_revision && baseRevision !== wf.draft_revision)
    // A skill on trial may sit on a draft to see how it fits, but it is not saved into a package until admitted.
    // A package on the shared library: any skill name the draft adds that the save would not declare blocks it (a
    // trial skill, a component master control suggested from outside, a typo). A package with its own skills
    // keeps its own names; only a trial skill blocks there.
    const onTrial = wf.shared_skill_library === false
      ? trialSkillsOnSteps(nodeDrafts[wf.id] ?? wf.nodes, wf.skill_contracts?.map(skill => skill.id), librarySkillsFor(wf), [...trialSkillsFor(wf), ...candidatesFor(wf).map(candidate => candidate.id)])
      : unadmittedNewSkills(nodeDrafts[wf.id] ?? wf.nodes, wf.nodes, wf.skill_contracts?.map(skill => skill.id), librarySkillsFor(wf))
    const ownerDesign = employeeDesign?.project_id && employeeDesign.project_id === employeeDesign.workflows_read_project_id
      && (!wf.project_id || wf.project_id === employeeDesign.project_id)
    const saveNodes = ownerDesign
      ? ownerCanvasNodesForSave(nodeDrafts[wf.id] ?? wf.nodes, wf.nodes)
      : canvasNodesForSave(nodeDrafts[wf.id] ?? wf.nodes, wf.nodes)
    return (
      <>
        <input aria-label={t('workflows.nodeVersion')} value={nodeVersions[wf.id] ?? wf.version} placeholder={t('workflows.nodeVersion')} onChange={event => setNodeVersions(prev => ({ ...prev, [wf.id]: event.target.value }))} />
        <input aria-label={t('workflows.nodeChangeNote')} value={nodeChangeNotes[wf.id] ?? ''} placeholder={t('workflows.nodeChangeNote')} onChange={event => setNodeChangeNotes(prev => ({ ...prev, [wf.id]: event.target.value }))} />
        <button type="button" className="workflows-invoke" data-testid="node-draft-save" disabled={!connected || stale || onTrial.length > 0 || !saveNodes.ok || updatingWorkflowId === wf.id || !(nodeVersions[wf.id] ?? wf.version).trim()} onClick={() => { if (saveNodes.ok) onUpdateNodes?.(wf.id, saveNodes.nodes, baseVersion, (nodeVersions[wf.id] ?? wf.version).trim(), (nodeChangeNotes[wf.id] ?? '').trim(), nodeDrafts[wf.id] ? baseRevision : (wf.draft_revision ?? '')) }}>
          {updatingWorkflowId === wf.id ? t('workflows.savingNodes') : t('workflows.saveNodes')}
        </button>
        {nodeDrafts[wf.id] && (
          <button type="button" className="workflows-cancel" data-testid="node-draft-discard" disabled={updatingWorkflowId === wf.id} onClick={() => discardNodeDraft(wf)}>
            {t('workflows.discardDraft')}
          </button>
        )}
        {stale && (
          <span className="workflows-draft-stale" role="alert" data-testid="node-draft-stale">
            {draftIsStale(baseVersion, wf.version)
              ? t('workflows.draftStale', { base: baseVersion, live: wf.version })
              : t('workflows.draftSavedOver', { live: wf.version })}
          </span>
        )}
        {onTrial.length > 0 && (
          <span className="workflows-draft-stale" role="alert" data-testid="node-draft-trial">
            {t('workflows.draftOnTrial', { skills: onTrial.join('、') })}
          </span>
        )}
        {!saveNodes.ok && (
          <span className="workflows-draft-stale" role="alert" data-testid="node-draft-binding-save-blocked">
            {locale.startsWith('zh') ? '员工绑定内容或项目范围尚未核实。修改已保留，请检查绑定配置并刷新当前项目。' : 'Check the employee binding and current project before saving. Your draft is retained.'}
          </span>
        )}
      </>
    )
  }

  // The offer itself. Discarding re-renders (the tick) so the lookup runs again; recovering opens an ordinary draft
  // on the version it was started from.
  const [recoveryTick, setRecoveryTick] = useState(0)
  const recoveryBanner = (wf: WorkflowSummary) => {
    const offer = offerFor(wf)
    return (
      <DraftRecoveryBanner
        key={`${wf.id}:${recoveryTick}`}
        offer={offer} savedVersion={wf.version} zh={locale.startsWith('zh')}
        onDiscard={() => { clearDraft(wf.id); setRecoveryTick(tick => tick + 1) }}
        onRecover={() => {
          const recovered = recoveredDraftState(offer, wf.version)
          if (!recovered) return
          setEditingWorkflowId(wf.id)
          setNodeDrafts(prev => ({ ...prev, [wf.id]: recovered.nodes }))
          setNodeBaseVersions(prev => ({ ...prev, [wf.id]: recovered.baseVersion }))
          setNodeVersions(prev => ({ ...prev, [wf.id]: recovered.newVersion }))
          setNodeChangeNotes(prev => ({ ...prev, [wf.id]: recovered.changeNote }))
        }}
      />
    )
  }

  // 安安协助 / 测试 / 验收准入 on the builder's top bar (撤销 and 保存草稿 are the canvas's own). 安安 opens beside the
  // canvas with this flow -- and the step being edited -- already referenced; it posts nothing by itself.
  const builderActions = (wf: WorkflowSummary) => {
    const zh = locale.startsWith('zh')
    return {
      assist: onOpenAssistant ? (
        <button type="button" className="wf-canvas-expand wf-toolbar-assist" data-testid="builder-assist" onClick={onOpenAssistant}
          title={zh ? '在右侧打开安安，带上这条流程和正在编辑的步骤' : 'Open 安安 beside the canvas, with this flow and the step being edited'}>
          {zh ? '✦ 安安协助' : '✦ Ask 安安'}
        </button>
      ) : undefined,
      test: (
        <button type="button" className="wf-canvas-expand wf-toolbar-test" data-testid="builder-test" onClick={() => { onRefresh(); setActionDialog({ kind: 'test', workflowId: wf.id, expectedRevision: wf.draft_revision ?? '' }) }}>
          {zh ? '测试' : 'Test'}
        </button>
      ),
      admit: (
        <button type="button" className="wf-canvas-expand wf-toolbar-admit" data-testid="builder-admit" onClick={() => setActionDialog({ kind: 'admit', workflowId: wf.id, expectedRevision: wf.draft_revision ?? '' })}>
          {zh ? '验收准入' : 'Admission'}
        </button>
      ),
    }
  }

  const actionDialogFor = (wf: WorkflowSummary) => {
    if (!actionDialog || actionDialog.workflowId !== wf.id) return null
    const zh = locale.startsWith('zh')
    const close = () => setActionDialog(null)
    const draftOpen = Boolean(nodeDrafts[wf.id])
    const dialogRevision = actionDialog.expectedRevision
    const dialogCurrent = workflowRevisionMatches(dialogRevision, wf.draft_revision)
    if (actionDialog.kind === 'test') {
      return (
        <FlowTestDialog
          zh={zh} flowName={wf.name || wf.id} savedVersion={wf.version} draftOpen={draftOpen} connected={connected}
          test={flowTestView(wf.nodes, nodeTrials.filter(trial => trial.workflow_id === wf.id), wf.version)}
          preflight={preflight?.workflow_id === wf.id ? preflight : null}
          preflightBusy={preflightingWorkflowId === wf.id}
          latestTest={wf.latest_test}
          onPreflight={onPreflight ? () => onPreflight(wf.id, drafts[wf.id] ?? {}) : undefined}
          onOpenStep={stepId => { close(); askBuilder(wf.id, 'canvas', stepId, 'output') }}
          onRecordTest={onRecordTest && (wf.status === 'draft' || wf.status === 'testing') ? () => {
            close()
            setTestRecordOpen({ workflowId: wf.id, expectedRevision: dialogRevision })
            askBuilder(wf.id, 'package')
            window.setTimeout(() => document.querySelector(`[data-flow-card="${CSS.escape(wf.id)}"] .workflows-test-record-form`)?.scrollIntoView({ block: 'center' }), 80)
          } : undefined}
          employeeTrial={wf.nodes.some(node => !!node.employee_binding) ? (employeeTrial
            ? <EmployeeWorkflowTrialPanel projectId={employeeTrial.projectId} workflowId={wf.id}
                workflowVersion={wf.version} manifestSha256={wf.draft_revision ?? ''} workflowStatus={wf.status}
                saved={!draftOpen} hasEmployeeBinding intakeFields={wf.intake_fields} connected={connected}
                send={employeeTrial.send} ack={employeeTrial.ack} onOpenOrder={employeeTrial.onOpenOrder} />
            : <p role="status">员工测试单入口尚不可用，请刷新后重试。</p>) : undefined}
          onFlowTrial={onFlowTrial ? (action, limits, target) => onFlowTrial(wf.id, target.workflowVersion, action, limits, target) : undefined}
          flowTrialBusy={flowTrialBusy}
          latestFlowTrial={flowTrials.find(item => item.workflow_id === wf.id && ['accepted', 'running', 'waiting', 'unknown'].includes(item.phase || ''))
            ?? flowTrials.find(item => item.workflow_id === wf.id) ?? null}
          publishedCandidates={publishedFlowTrials.filter(item => item.workflow_id === wf.id)}
          publishedCandidatesError={publishedFlowTrialError}
          onClose={close}
        />
      )
    }
    const busy = [submittingTestingWorkflowId, requestingReleaseWorkflowId, decidingReleaseWorkflowId, publishingWorkflowId].includes(wf.id)
    return (
      <FlowAdmissionDialog
        zh={zh} flowName={wf.name || wf.id} version={wf.version} connected={connected}
        view={admissionView(wf, { draftOpen, observedRevision: dialogRevision })}
        busy={busy}
        versions={onLoadVersions ? (wf.id in workflowVersions ? workflowVersions[wf.id] : 'loading') : undefined}
        projectNameOf={projectNameOf}
        onSubmitTesting={onSubmitTesting ? () => { if (dialogCurrent) onSubmitTesting(wf.id, dialogRevision, `submitted from Builder on ${new Date().toISOString().slice(0, 10)}`) } : undefined}
        onRequestRelease={onRequestRelease ? () => { if (dialogCurrent) onRequestRelease(wf.id, dialogRevision) } : undefined}
        onDecideRelease={onDecideRelease ? decision => { if (dialogCurrent) onDecideRelease(wf.id, dialogRevision, decision) } : undefined}
        onPublish={onPublish ? () => { if (dialogCurrent) onPublish(wf.id, dialogRevision) } : undefined}
        onOpenPackage={() => { close(); askBuilder(wf.id, 'package') }}
        onClose={close}
      />
    )
  }

  const renderCanvas = (wf: WorkflowSummary) => (
    <Suspense fallback={<div className="wf-canvas" />}>
      {editedElsewhere(wf) && (
        <div className="wf-draft-recovery" role="status" data-testid="edit-lock">
          <span>{locale.startsWith('zh')
            ? '这条流程正在这台浏览器的另一个标签页里编辑，这里只读。接管后，那个标签页转为只读，它没保存的草稿会在这里提示恢复。'
            : 'This flow is being edited in another tab of this browser; it is read-only here. Taking over makes that tab read-only, and its unsaved draft is offered here for recovery.'}</span>
          <button type="button" className="workflows-approve" onClick={() => editLock?.takeOver(wf.id)}>{locale.startsWith('zh') ? '由我接管' : 'Take over'}</button>
        </div>
      )}
      {recoveryBanner(wf)}
      {proposalRevert?.workflowId === wf.id && !nodeDrafts[wf.id] && canEditNodes(wf) && (
        <div className="wf-draft-recovery" role="status" data-testid="proposal-revert">
          <span>{locale.startsWith('zh')
            ? `安安的修改已保存为 v${wf.version}${proposalRevert.note ? `（${proposalRevert.note}）` : ''}。要整体撤销，就把 v${proposalRevert.baseVersion} 的步骤表作为新草稿打开，确认后保存成新版本——两次改动都留在记录里。`
            : `安安's change is saved as v${wf.version}${proposalRevert.note ? ` (${proposalRevert.note})` : ''}. To undo all of it, open the steps of v${proposalRevert.baseVersion} as a new draft and save it as a new version -- both changes stay on record.`}</span>
          <button type="button" className="workflows-approve" data-testid="proposal-revert-open" onClick={() => revertProposal(wf, proposalRevert)}>
            {locale.startsWith('zh') ? `回到 v${proposalRevert.baseVersion} 的步骤表` : `Back to v${proposalRevert.baseVersion}'s steps`}
          </button>
          <button type="button" className="workflows-cancel" onClick={() => setProposalRevert(null)}>{locale.startsWith('zh') ? '不用了' : 'No need'}</button>
        </div>
      )}
      <WorkflowCanvas
        key={wf.id}
        employeeDesign={employeeDesign?.project_id && employeeDesign.project_id === employeeDesign.workflows_read_project_id
          && (!wf.project_id || wf.project_id === employeeDesign.project_id) ? employeeDesign : undefined}
        employeeSavedRevision={wf.draft_revision ?? ''}
        onOpenLibrary={onOpenLibrary}
        onFocusStep={onBuilderFocus ? nodeId => {
          const step = (nodeDrafts[wf.id] ?? wf.nodes).find(node => node.id === nodeId)
          onBuilderFocus({
            workflowId: wf.id, workflowName: wf.name || wf.id,
            // A draft's own revision if one is open: the reference then says which saved state it was made against.
            draftRevision: nodeDrafts[wf.id] ? (nodeBaseRevisions[wf.id] ?? '') : (wf.draft_revision ?? ''),
            nodeId: step ? step.id : '', nodeName: step ? (step.name || step.id) : '',
          })
        } : undefined}
        intakeFields={wf.intake_fields}
        nodes={nodeDrafts[wf.id] ?? wf.nodes}
        locale={locale}
        workflowId={wf.id}
        employees={employees}
        employeesOrgId={employeesOrgId}
        contacts={contacts}
        onOpenPeople={onOpenPeople}
        workflowOrgId={wf.org_id}
        orgRoles={wf.org_roles}
        packageSkills={wf.skill_contracts?.map(skill => skill.id)}
        librarySkills={librarySkillsFor(wf)}
        libraryUnlockable={wf.library_skills_unlockable ?? []}
        desktopChannelOpen={wf.desktop_channel_open === true}
        brought={broughtComponent?.workflowId === wf.id ? { kind: broughtComponent.kind, id: broughtComponent.id, name: broughtComponent.name } : null}
        onDismissBrought={onDismissBrought}
        broughtExtra={broughtComponent?.workflowId === wf.id ? broughtExtra : undefined}
        trialSkills={trialSkillsFor(wf)}
        candidates={candidatesFor(wf)}
        candidateActions={candidateActions ? candidate => candidateActions(wf.id, candidate) : undefined}
        flows={liveFlows}
        proposal={nodeProposal?.package_id === wf.id ? {
          nodes: nodeProposal.nodes,
          baseNodes: nodeProposal.base_nodes,
          baseVersion: nodeProposal.base_version,
          newVersion: nodeProposal.new_version,
          changeNote: nodeProposal.change_note,
          liveVersion: wf.version,
          stale: proposalIsStale(nodeProposal, wf),
          // An open draft that is itself out of date cannot take the proposal (its save would be refused). A current
          // one can, once the person has confirmed on the card that its own changes are overwritten -- the card
          // names each of them (proposalConflicts, 4.7 A04).
          blockedReason: !canEditNodes(wf)
            ? t('workflows.proposal.notEditable')
            : nodeDrafts[wf.id] && (draftIsStale(nodeBaseVersions[wf.id] ?? wf.version, wf.version)
              || Boolean(nodeBaseRevisions[wf.id] && wf.draft_revision && nodeBaseRevisions[wf.id] !== wf.draft_revision))
              ? t('workflows.proposal.draftBlocks') : null,
          testing: wf.status === 'testing',
          candidates: nodeProposal.candidates,
        } : null}
        onApplyProposal={() => {
          // The applied table is a fresh draft on exactly the version and table the proposal was checked against;
          // the person reviews it and saves it. 放弃草稿 takes all of it back out at once.
          if (!nodeProposal) return
          setEditingWorkflowId(wf.id)
          setNodeBaseVersions(prev => ({ ...prev, [wf.id]: nodeProposal.base_version }))
          setNodeVersions(prev => ({ ...prev, [wf.id]: nodeProposal.new_version }))
          setNodeChangeNotes(prev => ({ ...prev, [wf.id]: nodeProposal.change_note }))
          // Remembered until this draft is saved (then offered back as 整体撤销) or discarded (then forgotten).
          setAppliedProposals(prev => ({ ...prev, [wf.id]: appliedProposal(wf.id, nodeProposal) }))
          onProposalApplied?.(wf.id, nodeProposal.candidates)
          onNodeProposalDone?.()
        }}
        onDiscardProposal={() => onNodeProposalDone?.()}
        onNodePatch={canEditNodes(wf) ? (nodeId, patch) => { ensureNodeDraft(wf); updateNodeDraft(wf.id, nodeId, patch) } : undefined}
        onNodesReplace={canEditNodes(wf) ? (next) => {
          ensureNodeDraft(wf)
          // 4.7 AQ: a table that differs from the saved one only in where its cards sit (一键对齐, a drag) is a
          // layout-only save: the draft opens so the positions are kept, but no new version is suggested, and the
          // save keeps the version. A version typed in the save bar is never touched here.
          if (!onlyPositionsChanged(wf.nodes, next)) suggestSemanticVersion(wf.id, wf.version)
          setNodeDrafts(prev => ({ ...prev, [wf.id]: next }))
        } : undefined}
        skillNames={internalSkills}
        onNodePreflight={onPreflight ? nodeId => onPreflight(wf.id, drafts[wf.id] ?? {}, nodeId) : undefined}
        nodePreflight={preflight?.workflow_id === wf.id ? preflight : null}
        nodePreflightBusy={preflightingWorkflowId === wf.id}
        nodePreflightBlocked={Boolean(nodeDrafts[wf.id])}
        onNodeTrial={onNodeTrial ? (nodeId, pins, imageLimit) => onNodeTrial(wf.id, wf.version, nodeId, pins, imageLimit) : undefined}
        draftOpen={Boolean(nodeDrafts[wf.id])}
        savedVersion={wf.version}
        serverBlockers={serverBlockersFor({
          workflowId: wf.id, refusedSave, preflight,
          draftUnchanged: nodeDrafts[wf.id] === refusedDraft.current.draft,
          draftOpen: Boolean(nodeDrafts[wf.id]),
        })}
        nodeTrialPinning={nodeTrialExtras?.pinning && nodeTrialExtras.pinning.workflow_id === wf.id ? nodeTrialExtras.pinning : null}
        onNodeTrialPlan={nodeTrialExtras ? nodeId => nodeTrialExtras.onPlan(wf.id, nodeId) : undefined}
        trialArtifact={nodeTrialExtras?.artifact}
        onOpenTrialArtifact={nodeTrialExtras?.onOpenArtifact}
        onCloseTrialArtifact={nodeTrialExtras?.onCloseArtifact}
        onAnswerTrial={nodeTrialExtras ? (specId, reply) => nodeTrialExtras.onAnswer(wf.id, specId, reply) : undefined}
        onAbandonTrial={nodeTrialExtras?.onAbandon}
        nodeTrials={nodeTrials.filter(trial => trial.workflow_id === wf.id)}
        nodeTrialBusy={nodeTrialBusy}
        nodeTrialBlocked={Boolean(nodeDrafts[wf.id])}
        saveBar={canEditNodes(wf) && editingWorkflowId === wf.id ? <div className="wf-canvas-savebar">{nodeSaveControls(wf)}</div> : undefined}
        packageHostRef={surface === 'builder' ? setPackageHost : undefined}
        openCanvasSignal={openCanvasSignal}
        toolbarActions={builderActions(wf)}
        builderRequest={builderRequest?.workflowId === wf.id ? builderRequest : null}
        onViewSkill={onViewComponent}
        onEditSkill={onEditComponent ? (skillId, nodeId) => onEditComponent(skillId, {
          workflowId: wf.id, nodeId,
          stepName: (nodeDrafts[wf.id] ?? wf.nodes).find(node => node.id === nodeId)?.name || nodeId,
          flowName: wf.name || wf.id,
        }) : undefined}
        defaultModel={defaultModel}
        modelProviders={modelProviders}
      />
    </Suspense>
  )

  // The order form, for a flow's package card and for 业务工单's ＋ 新建工单 alike: one form, one set of answers per flow.
  const launchForm = (wf: WorkflowSummary, onCancel: () => void) => (
    <OrderLaunchForm
      fields={wf.intake_fields}
      values={drafts[wf.id] ?? {}}
      onField={(fieldId, value) => setField(wf.id, fieldId, value)}
      budget={launchBudgets[wf.id] ?? {}}
      onBudget={next => setLaunchBudgets(current => ({ ...current, [wf.id]: next }))}
      sending={invokeState?.workflowId === wf.id && invokeState.status === 'sending'}
      error={invokeState?.workflowId === wf.id && invokeState.status === 'error' ? invokeState.error ?? '' : ''}
      connected={connected}
      onCancel={onCancel}
      onSubmit={limits => onInvoke(wf.id, drafts[wf.id] ?? {}, limits)}
    />
  )

  const evidenceFor = (order: WorkOrderSummary, criterion: string) =>
    (order.acceptance?.evidence ?? []).filter(item => String(item.criterion ?? '') === criterion)

  const evidenceDraftFor = (orderId: string, criterion: string) =>
    evidenceDrafts[orderId]?.[criterion] ?? { runId: '', artifactId: '', note: '' }

  const setEvidenceDraft = (orderId: string, criterion: string, patch: Partial<{ runId: string; artifactId: string; note: string }>) => {
    setEvidenceDrafts(prev => ({
      ...prev,
      [orderId]: {
        ...(prev[orderId] ?? {}),
        [criterion]: { ...evidenceDraftFor(orderId, criterion), ...patch },
      },
    }))
  }

  return (
    <div className="workflows-page" data-surface={surface}>
      {!connected && <div className="workflows-offline-state" role="status">{t('workflows.offline')}</div>}
      <div className="workflows-header">
        <div>
          <h2 className="workflows-title">{surface === 'orders' ? t('app.page.orders') : t('workflows.title')}</h2>
          <p className="workflows-subtitle">{surface === 'orders' ? t('orders.pageSubtitle') : t('workflows.subtitle')}</p>
        </div>
        <button
          className="workflows-refresh"
          disabled={refreshing || !connected}
          aria-busy={refreshing}
          onClick={() => {
            if (refreshing) return
            setRefreshing(true)
            onRefresh()
          }}
        >
          {refreshing ? t('workflows.loading') : t('workflows.refresh')}
        </button>
      </div>

      {error && (
        <div className={`workflows-error workflows-error--page workflows-error--${classifyPageError(error)}`} role="alert" data-error-kind={classifyPageError(error)}>
          <span><strong>{t(pageErrorMessageKey(error) as Parameters<typeof t>[0])}</strong><br />{error}</span>
          <button className="workflows-cancel" onClick={onRefresh} disabled={!connected}>{t('workflows.retry')}</button>
        </div>
      )}

      {proposalMissing && nodeProposal && (
        <div className="workflows-error" role="alert" data-testid="node-proposal-missing">
          <span>{t('workflows.proposal.missing', { name: nodeProposal.package_name })}</span>
          <button type="button" className="workflows-cancel" onClick={() => onNodeProposalDone?.()}>{t('workflows.proposal.discard')}</button>
        </div>
      )}

      {compositionNotice && surface !== 'orders' && (
        <div className={`workflows-composition-notice is-${compositionNotice.kind}`} role={compositionNotice.kind === 'error' ? 'alert' : 'status'} data-testid="workflow-composition-notice">
          <span>{compositionNotice.text}</span>
          <button type="button" onClick={() => setCompositionNotice(null)}>{locale.startsWith('zh') ? '知道了' : 'Dismiss'}</button>
        </div>
      )}

      {surface !== 'orders' && builderWorkflow && (
        <section className="wf-builder" ref={builderRef} aria-label={locale.startsWith('zh') ? '画布搭建' : 'Canvas builder'}>
          <div className="wf-builder-head">
            <h3 className="wf-builder-title">{locale.startsWith('zh') ? '画布搭建' : 'Canvas builder'}</h3>
            <div className="wf-builder-picker" role="tablist" aria-label={locale.startsWith('zh') ? '选择工作流' : 'Choose a workflow'}>
              {buildable.map(wf => {
                const statusKey = `workflows.status.${wf.status}` as Parameters<typeof t>[0]
                const selected = wf.id === builderWorkflow.id
                return (
                  <button key={wf.id} type="button" role="tab" aria-selected={selected} className={`wf-builder-tab${selected ? ' is-active' : ''}`} onClick={() => setBuilderWorkflowId(wf.id)}>
                    <span className="wf-builder-tab-name">{wf.name}</span>
                    <span className="wf-builder-tab-meta">v{wf.version} · {t(statusKey) === statusKey ? wf.status : t(statusKey)}{editingWorkflowId === wf.id ? (locale.startsWith('zh') ? ' · 草稿编辑中' : ' · draft open') : ''}</span>
                  </button>
                )
              })}
              {onCreateBlank && (
                <button type="button" className="wf-builder-tab wf-builder-new" data-testid="builder-new-flow" aria-expanded={blankFormOpen}
                  onClick={() => setBlankFormOpen(open => !open)}>{t('workflows.blank.open')}</button>
              )}
            </div>
          </div>
          {onCreateBlank && blankFormOpen && (
            <BlankFlowForm
              teams={teams} busy={creatingBlank} t={t} connected={connected}
              suggestedId={nextFlowId((workflows ?? []).map(wf => wf.id))}
              onCancel={() => setBlankFormOpen(false)}
              onCreate={(draftId, team, name) => { onCreateBlank(draftId, team, name); setBlankFormOpen(false) }}
            />
          )}
          {renderCanvas(builderWorkflow)}
          {actionDialogFor(builderWorkflow)}
        </section>
      )}

      {surface !== 'builder' && drawerOrder && (() => {
        const zh = locale.startsWith('zh')
        const statusKey = `orders.status.${drawerOrder.status}` as Parameters<typeof t>[0]
        return (
          <>
            <div className="order-drawer-scrim" onClick={() => setDrawerOrderId(null)} aria-hidden="true" />
            <OrderDrawer
              zh={zh} order={drawerOrder} flowName={flowOf(drawerOrder.workflow_id)?.name || undefined}
              waiting={waitingOn(drawerOrder, blockers, employeeName)}
              parent={(() => { const link = parentOf(drawerOrder); if (!link) return null; const found = (orders ?? []).find(item => item.order_id === link.orderId); return { ...link, label: orderLabel(found) || link.orderId, known: !!found } })()}
              children={childrenOf(drawerOrder, orders).map(item => ({ orderId: item.order_id, label: orderLabel(item), status: item.status }))}
              onOpenOrder={openOrderDrawer}
              statusLabel={t(statusKey) === statusKey ? drawerOrder.status : t(statusKey)}
              employees={employees}
              handlingBusy={updatingHandlingOrderId === drawerOrder.order_id}
              onUpdateHandling={onUpdateHandling ? patch => onUpdateHandling(drawerOrder.order_id, patch) : undefined}
              extra={drawerExtra ? drawerExtra(drawerOrder) : null}
              acceptanceLabel={drawerOrder.acceptance?.status ? t(`orders.acceptance.${drawerOrder.acceptance.status}` as Parameters<typeof t>[0]) : ''}
              queueLabel={ORDER_QUEUE_LABELS[queueOf(drawerOrder, personWaits)][zh ? 0 : 1]}
              timeline={orderTimeline && orderTimeline.order_id === drawerOrder.order_id ? orderTimeline : null}
              timelineError={orderTimelineError}
              modelPlan={orderModelPlan}
              onClose={() => setDrawerOrderId(null)}
              onOpenTask={onOpenTask}
              onGoToRow={() => goToOrderRow(drawerOrder.order_id)}
              onOpenAcceptance={drawerOrder.acceptance ? () => { setAcceptanceOpenId(drawerOrder.order_id); goToOrderRow(drawerOrder.order_id) } : undefined}
              onOpenFile={connected ? onOpenArtifact : undefined}
            />
          </>
        )
      })()}
      {surface !== 'builder' && <section className="orders-panel">
        <div className="orders-heading-row">
          <h3 className="orders-title">{t('orders.title')}</h3>
          {surface === 'orders' && (
            <div className="orders-new">
              <button type="button" className="shell-btn shell-btn--primary" data-testid="orders-new" aria-expanded={newOrderOpen} disabled={!connected}
                onClick={() => setNewOrderOpen(open => !open)}>{locale.startsWith('zh') ? '＋ 新建工单' : '+ New order'}</button>
              {newOrderOpen && (
                <div className="orders-new-list" data-testid="orders-new-list">
                  <p>{locale.startsWith('zh')
                    ? '选一条可接单的业务流，在这里填写它的开单字段并设定预算上限。开单会真实运行并花费模型调用。'
                    : 'Pick a flow that takes orders, then fill in its fields and the allowance here. Placing an order really runs and spends model calls.'}</p>
                  {(workflows ?? []).filter(wf => wf.invokable).map(wf => (
                    <button key={wf.id} type="button" data-flow={wf.id} onClick={() => { setNewOrderOpen(false); setNewOrderFlowId(wf.id) }}>
                      <strong>{wf.name || wf.id}</strong><small>v{wf.version} · {wf.id}</small>
                    </button>
                  ))}
                  {(workflows ?? []).filter(wf => wf.invokable).length === 0 && (
                    <em>{workflows === null
                      ? (locale.startsWith('zh') ? '正在读取业务流…' : 'Reading the flows…')
                      : (locale.startsWith('zh') ? '现在没有可接单的业务流：业务流要通过测试并可调用后才能开单。' : 'No flow takes orders right now: a flow must pass its tests and be invokable first.')}</em>
                  )}
                </div>
              )}
            </div>
          )}
          <form className="orders-scope-filter" onSubmit={event => { event.preventDefault(); onOrderScopeChange?.({ customer_id: scopeDraft.customer_id.trim(), environment_id: scopeDraft.environment_id.trim() }) }}>
            <input
              aria-label={t('orders.filter.customer')}
              placeholder={t('orders.filter.customer')}
              value={scopeDraft.customer_id}
              disabled={!connected}
              onChange={event => setScopeDraft(prev => ({ ...prev, customer_id: event.target.value }))}
            />
            <input
              aria-label={t('orders.filter.environment')}
              placeholder={t('orders.filter.environment')}
              value={scopeDraft.environment_id}
              disabled={!connected}
              onChange={event => setScopeDraft(prev => ({ ...prev, environment_id: event.target.value }))}
            />
            <button type="submit" className="workflows-cancel" disabled={!connected}>{t('orders.filter.apply')}</button>
            {(scopeDraft.customer_id || scopeDraft.environment_id) && <button type="button" className="workflows-cancel" disabled={!connected} onClick={() => { const empty = { customer_id: '', environment_id: '' }; setScopeDraft(empty); onOrderScopeChange?.(empty) }}>{t('orders.filter.clear')}</button>}
          </form>
        </div>
        {surface === 'orders' && newOrderFlow && (
          <section className="orders-new-form" data-testid="orders-new-form" data-flow={newOrderFlow.id}
            aria-label={locale.startsWith('zh') ? '新建工单' : 'New order'}>
            <header>
              <strong>{locale.startsWith('zh') ? '新建工单' : 'New order'} · {newOrderFlow.name || newOrderFlow.id}</strong>
              <small>v{newOrderFlow.version} · {newOrderFlow.id}</small>
            </header>
            {launchForm(newOrderFlow, () => setNewOrderFlowId(null))}
          </section>
        )}
        {orders !== null && orders.length > 0 && (
          <div className="orders-queues">
            <div className="orders-queue-tabs" role="group" aria-label={locale.startsWith('zh') ? '工单队列' : 'Order queues'}>
              {ORDER_QUEUES.map(queue => (
                <button key={queue} type="button" data-queue={queue} aria-pressed={orderQueue === queue} className={orderQueue === queue ? 'is-active' : ''}
                  onClick={() => setOrderQueue(queue)}>
                  {ORDER_QUEUE_LABELS[queue][locale.startsWith('zh') ? 0 : 1]}<small>{orderCounts[queue]}</small>
                </button>
              ))}
            </div>
            {flowsWithOrders(orders).length > 1 && (
              <select className="orders-flow-filter" data-testid="orders-flow-filter" aria-label={locale.startsWith('zh') ? '按业务流筛选' : 'Filter by flow'}
                value={orderFlow} onChange={event => setOrderFlow(event.target.value)}>
                <option value="">{locale.startsWith('zh') ? '全部业务流' : 'All flows'}</option>
                {flowsWithOrders(orders).map(id => <option key={id} value={id}>{flowOf(id)?.name || id}</option>)}
              </select>
            )}
            <input type="search" className="orders-search" value={orderQuery} onChange={event => setOrderQuery(event.target.value)}
              placeholder={locale.startsWith('zh') ? '搜索工单号、客户、品牌、业务流' : 'Search id, customer, brand, flow'}
              aria-label={locale.startsWith('zh') ? '搜索工单' : 'Search orders'} />
            <button type="button" className={`orders-overdue${overdueOnly ? ' is-active' : ''}`} data-testid="orders-overdue" aria-pressed={overdueOnly}
              disabled={overdueCount === 0 && !overdueOnly} onClick={() => setOverdueOnly(on => !on)}
              title={locale.startsWith('zh') ? '只看过了截止日期、还没结束的工单' : 'Only orders past their due date that have not ended'}>
              {locale.startsWith('zh') ? '逾期' : 'Overdue'}<small>{overdueCount}</small>
            </button>
          </div>
        )}
        {ordersTruncated && orders !== null && orders.length > 0 && (
          <p className="orders-truncated" role="status" data-testid="orders-truncated">
            {locale.startsWith('zh')
              ? `工单比较多，这里只列出了最近的 ${orders.length} 张，更早的没有列出来；上面各栏的数字也只算了这 ${orders.length} 张。`
              : `There are many orders: only the latest ${orders.length} are listed here, and the counts above cover only these.`}
          </p>
        )}
        {orderMetricsTruncated && orders !== null && orders.length > 0 && (
          <p className="orders-truncated" role="status" data-testid="order-metrics-truncated">
            {locale.startsWith('zh')
              ? '「省下」「承诺」两栏的金额只算了最近 500 单，更早的工单没有算进去。'
              : 'The saving and promise figures cover only the latest 500 orders; older ones are not counted.'}
          </p>
        )}
        {orders === null ? (
          <div className="workflows-empty">{t('workflows.loading')}</div>
        ) : orders.length === 0 ? (
          <div className="workflows-empty">{t('orders.empty')}</div>
        ) : listedOrders.length === 0 ? (
          <div className="workflows-empty" role="status" data-testid="orders-none-shown">
            {locale.startsWith('zh') ? `这个队列和搜索下没有工单（共 ${orders.length} 单）。` : `No order in this queue and search (${orders.length} in all).`}
          </div>
        ) : (
          <table className="orders-table">
            <thead>
              <tr>
                <th>{t('orders.col.id')}</th>
                <th>{t('orders.col.customer')}</th>
                <th>{t('orders.col.workflow')}</th>
                <th>{t('orders.col.status')}</th>
                <th>{t('orders.col.turn')}{locale.startsWith('zh') ? ' / 截止' : ' / due'}</th>
                <th>{t('orders.col.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {listedOrders.map(order => {
                const statusKey = `orders.status.${order.status}` as Parameters<typeof t>[0]
                const acceptanceStatus = order.acceptance?.status ?? ''
                const acceptanceOpen = acceptanceOpenId === order.order_id && !!order.acceptance
                const acceptanceBusy = acceptanceBusyOrderId === order.order_id
                const auditState = order.events_chain_verified === true
                  ? 'verified'
                  : order.events_chain_verified === false ? 'invalid' : 'unknown'
                const auditLabel = auditState === 'verified'
                  ? t('orders.auditVerified')
                  : auditState === 'invalid' ? t('orders.auditInvalid') : t('orders.auditUnknown')
                const step = orderStep(order)
                const flow = flowOf(order.workflow_id)
                const waiting = waitingOn(order, blockers, employeeName)
                const rowOpen = rowShowsTools(order.order_id, openRowIds, budgets)
                const folded = rowOpen ? '' : ' is-folded'
                return (
                  <Fragment key={order.order_id}>
                  <tr data-order-row={order.order_id} data-row-open={rowOpen || undefined} className={drawerOrderId === order.order_id ? 'is-in-drawer' : undefined}>
                    <td className="orders-id">
                      <button type="button" className="orders-open-drawer" data-testid="order-open-drawer" title={locale.startsWith('zh') ? '查看这张工单' : 'Open this order'}
                        onClick={() => openOrderDrawer(order.order_id)}>{order.order_id}</button>
                    </td>
                    <td>
                      <span>{order.customer_name}{order.brand_name ? ` · ${order.brand_name}` : ''}</span>
                      {(order.customer_id || order.environment_id) && (
                        <span className="orders-scope-label">
                          {order.customer_id ? `customer:${order.customer_id}` : ''}
                          {order.customer_id && order.environment_id ? ' · ' : ''}
                          {order.environment_id ? `env:${order.environment_id}` : ''}
                        </span>
                      )}
                    </td>
                    <td className="orders-flow">
                      {flow && flow.status !== 'retired' && onShowBuilder
                        ? <button type="button" className="orders-flow-open" data-testid="order-flow-open" data-flow={order.workflow_id}
                            title={locale.startsWith('zh') ? `到搭建台看这条业务流` : 'Open this flow in the builder'}
                            onClick={() => { openInBuilder(order.workflow_id); onShowBuilder() }}>{flow.name || order.workflow_id}</button>
                        : <span>{flow?.name || order.workflow_id}</span>}
                      {flow?.name && <small>{order.workflow_id}</small>}
                    </td>
                    <td>
                      <span className="orders-step" data-person={step.person || personWaits.has(order.order_id) || undefined}>{(STEP_TEXT[step.key] ?? STEP_TEXT.running)[locale.startsWith('zh') ? 0 : 2]}</span>
                      <span className={`orders-status orders-status--${order.status}`}>{t(statusKey) === statusKey ? order.status : t(statusKey)}</span>
                      {waiting && (
                        // Which step stopped and whose turn it is, as the gate itself says, read on from the status
                        // (生产中 · 等人：整理待核查订单号). No owner is invented for an order: when the gate names
                        // nobody, the row says it is waiting and stops there.
                        <span className="orders-waiting" data-testid="order-waiting" data-checkpoint={waiting.checkpointId || undefined}>
                          {locale.startsWith('zh') ? '等人' : 'Waiting'}
                          {waiting.step ? `：${waiting.step}` : ''}
                          {waiting.who ? (locale.startsWith('zh') ? ` · 轮到 ${waiting.who}` : ` · ${waiting.who}`) : ''}
                          {waiting.count > 1 ? (locale.startsWith('zh') ? ` · 共 ${waiting.count} 处` : ` · ${waiting.count} in all`) : ''}
                        </span>
                      )}
                      <span
                        className={`orders-audit-badge is-${auditState}`}
                        title={auditLabel}
                      >
                        {auditLabel}
                      </span>
                      {order.acceptance && (
                        <div className="orders-acceptance">
                          {t('orders.acceptance')}: {t(`orders.acceptance.${order.acceptance.status}` as Parameters<typeof t>[0])}
                        </div>
                      )}
                      {order.fde_control_plane && (
                        <div
                          className="orders-fde-summary"
                          title={[order.fde_control_plane.as_is, order.fde_control_plane.to_be].filter(Boolean).join(' → ')}
                        >
                          {/* In the owner's words: the stage of the delivery, never "FDE validating". An unknown
                              phase shows as it came, rather than a missing-key marker. */}
                          <span>{t('orders.fde.stage')}</span>
                          <span>{(() => {
                            const key = `orders.fde.phase.${order.fde_control_plane.phase}` as Parameters<typeof t>[0]
                            const label = t(key)
                            return label === key ? order.fde_control_plane.phase : label
                          })()}</span>
                          {order.fde_control_plane.release?.workflow_version && <span>v{order.fde_control_plane.release.workflow_version}</span>}
                          {order.fde_control_plane.rollback?.available && <span>↶</span>}
                        </div>
                      )}
                    </td>
                    {(() => {
                      // Whose turn it is, from what the row already holds. Never an invented owner: a name only when
                      // the gate or the order names one, a role otherwise, and the run itself when no person is due.
                      const turn = whoseTurn(step, waiting, order.reviewer_employee_id ? employeeName(order.reviewer_employee_id) : '', locale.startsWith('zh'))
                      return (
                        <td className="orders-turn" data-testid="order-turn" data-turn={turn.kind}>
                          <span>{turn.who}</span>
                          {(() => {
                            // The order's own handling facts, when someone recorded them: who looks after it, and when
                            // it is due. An ended order is never overdue.
                            const zhRow = locale.startsWith('zh')
                            const due = dueState(order.handling?.due_at, new Date(), zhRow, turn.kind === 'none')
                            return <>
                              {order.handling?.assignee_employee_id && <small data-testid="order-assignee">{zhRow ? '负责人' : 'Assignee'} {employeeName(order.handling.assignee_employee_id)}</small>}
                              {due.text && <small className="orders-due" data-testid="order-due" data-due={due.kind}>{due.text}</small>}
                            </>
                          })()}
                          {order.source && <small title={t('orders.col.source')}>{t('orders.col.source')} {order.source}</small>}
                        </td>
                      )
                    })()}
                    <td className="orders-actions">
                      <span className="orders-row-next">
                        <button type="button" className={`shell-btn${step.person ? ' shell-btn--primary' : ''}`} data-testid="order-row-next" onClick={() => openOrderDrawer(order.order_id)}>
                          {step.person ? (locale.startsWith('zh') ? '查看并处理' : 'Open and act') : (locale.startsWith('zh') ? '查看进度' : 'See progress')}
                        </button>
                        <button type="button" className="shell-btn orders-row-toggle" data-testid="order-row-tools" aria-expanded={rowOpen}
                          onClick={() => {
                            // Folding a row takes away what its tools opened: the timeline, the acceptance and the FDE
                            // editor have no close control of their own, so left behind they sit there uncloseable.
                            if (rowOpen) {
                              if (timelineOpenId === order.order_id) setTimelineOpenId(null)
                              if (acceptanceOpenId === order.order_id) setAcceptanceOpenId(null)
                              if (fdeEditorOpenId === order.order_id) setFdeEditorOpenId(null)
                            }
                            setOpenRowIds(current => toggled(current, order.order_id))
                          }}>{locale.startsWith('zh') ? '操作' : 'Tools'}</button>
                      </span>
                      <div className="orders-row-tools">
                      {order.status === 'pending_review' && (
                        <>
                          <textarea
                            className="orders-review-note"
                            rows={2}
                            aria-label={t('orders.reviewNote')}
                            placeholder={t('orders.reviewNote')}
                            value={reviewNotes[order.order_id] ?? ''}
                            disabled={!connected}
                            onChange={event => setReviewNotes(prev => ({ ...prev, [order.order_id]: event.target.value }))}
                          />
                          <button className="workflows-cancel" disabled={!connected} onClick={() => onReviewOrder(order.order_id, 'approve', reviewNotes[order.order_id] ?? '')}>{t('orders.approve')}</button>
                          <button className="workflows-cancel" disabled={!connected || !(reviewNotes[order.order_id] ?? '').trim()} onClick={() => onReviewOrder(order.order_id, 'reject', reviewNotes[order.order_id] ?? '')}>{t('orders.reject')}</button>
                        </>
                      )}
                      {onCancelOrder && ['launched', 'producing', 'delivering'].includes(order.status) && (
                        <span data-testid="order-cancel">
                          <textarea
                            className="orders-review-note"
                            rows={2}
                            aria-label="取消原因"
                            placeholder="为什么取消整单（会写进工单事件）"
                            value={reviewNotes[order.order_id] ?? ''}
                            disabled={!connected}
                            onChange={event => setReviewNotes(prev => ({ ...prev, [order.order_id]: event.target.value }))}
                          />
                          {(order.running_child_orders ?? 0) > 0 && (
                            <label className="orders-cancel-cascade" data-testid="order-cancel-cascade" title={t('orders.cancel.cascadeHint')}>
                              <input
                                type="checkbox"
                                disabled={!connected}
                                // On unless the owner unticks it (policy): cancelling the order that started the
                                // others means the whole piece of business is off. The server still cascades only when asked.
                                checked={cancelCascade[order.order_id] !== false}
                                onChange={event => setCancelCascade(prev => ({ ...prev, [order.order_id]: event.target.checked }))}
                              />
                              <span>{t('orders.cancel.cascade', { count: order.running_child_orders ?? 0 })}</span>
                            </label>
                          )}
                          <button className="workflows-cancel" data-testid="order-cancel-confirm" disabled={!connected || cancellingOrderId === order.order_id || (reviewNotes[order.order_id] ?? '').trim().length < 2}
                            title="停掉调度、请求正在跑的回合停下、关单并记录原因。已经发出去的动作不会撤回。"
                            onClick={() => {
                              const sent = onCancelOrder(order.order_id, (reviewNotes[order.order_id] ?? '').trim(), (order.running_child_orders ?? 0) > 0 && cancelCascade[order.order_id] !== false)
                              if (!sent) return  // not sent: the reason and the choice stay where they are
                              setReviewNotes(prev => ({ ...prev, [order.order_id]: '' }))
                              setCancelCascade(prev => { const next = { ...prev }; delete next[order.order_id]; return next })
                            }}>{cancellingOrderId === order.order_id ? t('request.busy') : '取消整单'}</button>
                        </span>
                      )}
                      {order.launched_task_id && (
                        <button className="workflows-cancel" disabled={!connected} onClick={() => onOpenTask(order.launched_task_id)}>{t('orders.open')}</button>
                      )}
                      {onCustomerPreview && <button className="workflows-cancel" data-testid="customer-preview-entry" disabled={!connected} onClick={() => onCustomerPreview(order.order_id)}>{t('customerPreview.title')}</button>}
                      {onOrderTimeline && (
                        <button
                          className={`workflows-cancel${timelineOpenId === order.order_id ? ' is-active' : ''}`}
                          data-testid="orders-timeline"
                          disabled={!connected}
                          onClick={() => {
                            if (timelineOpenId === order.order_id) { setTimelineOpenId(null); return }
                            setTimelineOpenId(order.order_id)
                            onOrderTimeline(order.order_id)
                          }}
                        >
                          {t('orders.timeline')}
                        </button>
                      )}
                      {order.acceptance && (
                        <button
                          className={`workflows-cancel${acceptanceOpen ? ' is-active' : ''}`}
                          disabled={!connected || acceptanceBusy}
                          onClick={() => {
                            setAcceptanceOpenId(acceptanceOpen ? null : order.order_id)
                            if (!acceptanceOpen) onAcceptanceGet(order.order_id)
                          }}
                        >
                          {t('orders.acceptance')}
                        </button>
                      )}
                      {onUpdateFdeControlPlane && order.fde_control_plane && (
                        <button
                          className={`workflows-cancel${fdeEditorOpenId === order.order_id ? ' is-active' : ''}`}
                          disabled={!connected || updatingFdeControlPlaneOrderId === order.order_id}
                          onClick={() => fdeEditorOpenId === order.order_id ? setFdeEditorOpenId(null) : beginFdeEdit(order)}
                        >
                          {updatingFdeControlPlaneOrderId === order.order_id ? t('orders.fde.saving') : t('orders.fde.edit')}
                        </button>
                      )}
                      </div>
                    </td>
                  </tr>
                  {timelineOpenId === order.order_id && (
                    <tr className="orders-timeline-row">
                      <td colSpan={6}>
                        <div className="orders-timeline-panel" data-order-timeline={order.order_id}>
                          <div className="orders-timeline-head">
                            <strong>{t('orders.timeline')}</strong>
                            {orderTimelineError ? (
                              <span className={`orders-timeline-error orders-timeline-error--${classifyPageError(orderTimelineError)}`} role="alert" data-error-kind={classifyPageError(orderTimelineError)}>
                                {t(pageErrorMessageKey(orderTimelineError) as Parameters<typeof t>[0])}: {orderTimelineError}
                              </span>
                            ) : orderTimeline && orderTimeline.order_id === order.order_id ? (
                              <span className="orders-timeline-counts">
                                {t('orders.timelineCounts', { order: orderTimeline.counts.order, gate: orderTimeline.counts.gate, runtime: orderTimeline.counts.runtime, effect: orderTimeline.counts.effect ?? 0, browser: orderTimeline.counts.browser ?? 0 })}
                                {' · '}
                                {orderTimeline.business_chain_verified ? t('orders.auditVerified') : t('orders.auditInvalid')}
                                {' · '}
                                {t('orders.timelineRuntimeUnchained')}
                                {(() => {
                                  const line = integrityLine(orderTimeline.integrity)
                                  return line ? (
                                    <span data-testid="timeline-integrity">
                                      {' · '}
                                      {t(line.key as Parameters<typeof t>[0], line.params)}
                                    </span>
                                  ) : null
                                })()}
                              </span>
                            ) : (
                              <span className="orders-timeline-counts">{t('orders.timelineLoading')}</span>
                            )}
                          </div>
                          {orderTimeline && orderTimeline.order_id === order.order_id && !orderTimelineError && (
                            orderTimeline.entries.length === 0 ? (
                              <div className="capability-muted">{t('orders.timelineEmpty')}</div>
                            ) : (
                              <ol className="orders-timeline-list">
                                {orderTimeline.entries.map((entry, index) => (
                                  <li key={`${entry.at}-${entry.source}-${index}`} className={`orders-timeline-item orders-timeline-item--${entry.source}`} data-timeline-source={entry.source}>
                                    <span className="orders-timeline-at" title={timelineEventTime(entry.at, true)}>{timelineEventTime(entry.at)}</span>
                                    <span className={`capability-pill capability-pill--${entry.source === 'order' || entry.source === 'gate' ? 'ok' : entry.source === 'effect' ? 'warn' : 'muted'}`}>{t(`orders.timelineSource.${entry.source}` as Parameters<typeof t>[0])}</span>
                                    <span className="orders-timeline-kind" data-kind={entry.kind}>{timelineEventName(entry.kind, locale.startsWith('zh'))}</span>
                                    <span className="orders-timeline-actor">{entry.actor}</span>
                                    <span className="orders-timeline-detail">{entry.detail}</span>
                                  </li>
                                ))}
                              </ol>
                            )
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                  {acceptanceOpen && order.acceptance && (
                    <tr className="orders-acceptance-row">
                      <td colSpan={6}>
                        <div className="orders-acceptance-panel" aria-busy={acceptanceBusy}>
                          <div className="orders-acceptance-head">
                            <div>
                              <strong>{t('orders.acceptance')}</strong>
                              <span className={`orders-status orders-status--${acceptanceStatus}`}>{t(`orders.acceptance.${acceptanceStatus}` as Parameters<typeof t>[0])}</span>
                              <span className="orders-acceptance-round">{t('orders.acceptance.round', { round: order.acceptance.round_no ?? 1 })}</span>
                            </div>
                            <span className="orders-acceptance-hash">{order.acceptance.criteria_hash.slice(0, 12)}</span>
                          </div>
                          <div className="orders-acceptance-criteria">
                            {order.acceptance.criteria.map(criterion => {
                              const evidence = evidenceFor(order, criterion)
                              const draft = evidenceDraftFor(order.order_id, criterion)
                              const canRecord = !!draft.runId.trim() && !!draft.artifactId.trim()
                              return (
                                <div key={criterion} className="orders-acceptance-criterion">
                                  <div className="orders-acceptance-criterion-title">
                                    <span className={evidence.length > 0 ? 'is-met' : ''}>{evidence.length > 0 ? '✓' : '○'}</span>
                                    <span>{criterion}</span>
                                  </div>
                                  {evidence.length > 0 ? (
                                    <div className="orders-acceptance-evidence-list">
                                      {evidence.map(item => (
                                        (() => {
                                          const artifactPath = [item.artifact_path, item.path]
                                            .find(value => typeof value === 'string' && value.trim()) as string | undefined
                                          const evidenceLabel = `${String(item.artifact_id ?? item.artifact_hash ?? item.event_id ?? '')} · ${String(item.run_id ?? '')}`
                                          return (
                                            <span key={String(item.evidence_id ?? `${criterion}-${item.run_id}`)} className="orders-acceptance-evidence">
                                              {evidenceLabel}
                                              {artifactPath && onOpenArtifact && (
                                                <button type="button" className="orders-acceptance-evidence-open" onClick={() => onOpenArtifact(artifactPath)}>
                                                  {t('artifacts.preview')}
                                                </button>
                                              )}
                                            </span>
                                          )
                                        })()
                                      ))}
                                    </div>
                                  ) : (acceptanceStatus === 'criteria_defined' || acceptanceStatus === 'evidence_collecting') ? (
                                    <div className="orders-acceptance-evidence-form">
                                      <input
                                        value={draft.runId}
                                        placeholder={t('orders.acceptance.runId')}
                                        onChange={event => setEvidenceDraft(order.order_id, criterion, { runId: event.target.value })}
                                      />
                                      <input
                                        value={draft.artifactId}
                                        placeholder={t('orders.acceptance.artifactId')}
                                        onChange={event => setEvidenceDraft(order.order_id, criterion, { artifactId: event.target.value })}
                                      />
                                      <input
                                        value={draft.note}
                                        placeholder={t('orders.acceptance.note')}
                                        onChange={event => setEvidenceDraft(order.order_id, criterion, { note: event.target.value })}
                                      />
                                      <button
                                        className="workflows-cancel"
                                        disabled={!canRecord || acceptanceBusy || !connected}
                                        onClick={() => onAcceptanceEvidence(order.order_id, criterion, {
                                          run_id: draft.runId.trim(),
                                          artifact_path: draft.artifactId.trim(),
                                          ...(draft.note.trim() ? { note: draft.note.trim() } : {}),
                                        })}
                                      >
                                        {t('orders.acceptance.recordEvidence')}
                                      </button>
                                    </div>
                                  ) : null}
                                </div>
                              )
                            })}
                          </div>
                          <div className="orders-acceptance-actions">
                            {(acceptanceStatus === 'criteria_defined' || acceptanceStatus === 'evidence_collecting') && (
                              <button
                                className="workflows-invoke"
                                disabled={!connected || acceptanceBusy || order.acceptance.criteria.some(criterion => evidenceFor(order, criterion).length === 0)}
                                onClick={() => onAcceptanceSubmit(order.order_id)}
                              >
                                {t('orders.acceptance.submit')}
                              </button>
                            )}
                            {acceptanceStatus === 'submitted' && (
                              <>
                                <textarea
                                  rows={2}
                                  value={acceptanceNotes[order.order_id] ?? ''}
                                  disabled={!connected || acceptanceBusy}
                                  placeholder={t('orders.acceptance.decisionNote')}
                                  onChange={event => setAcceptanceNotes(prev => ({ ...prev, [order.order_id]: event.target.value }))}
                                />
                                <button className="workflows-invoke" disabled={!connected || acceptanceBusy} onClick={() => onAcceptanceDecide(order.order_id, 'accepted', acceptanceNotes[order.order_id] ?? '', [])}>
                                  {t('orders.acceptance.accept')}
                                </button>
                                <button className="workflows-cancel" disabled={!connected || acceptanceBusy || !(acceptanceNotes[order.order_id] ?? '').trim()} onClick={() => onAcceptanceDecide(order.order_id, 'rejected', acceptanceNotes[order.order_id] ?? '', [])}>
                                  {t('orders.acceptance.reject')}
                                </button>
                                <button
                                  className="workflows-cancel"
                                  disabled={!connected || acceptanceBusy || !(acceptanceExceptions[order.order_id] ?? '').trim()}
                                  onClick={() => onAcceptanceDecide(order.order_id, 'accepted_with_exceptions', acceptanceNotes[order.order_id] ?? '', (acceptanceExceptions[order.order_id] ?? '').split(/[,，\n]/).map(item => item.trim()).filter(Boolean))}
                                >
                                  {t('orders.acceptance.acceptWithExceptions')}
                                </button>
                                <input
                                  className="orders-acceptance-exceptions"
                                  value={acceptanceExceptions[order.order_id] ?? ''}
                                  disabled={!connected || acceptanceBusy}
                                  placeholder={t('orders.acceptance.exceptions')}
                                  onChange={event => setAcceptanceExceptions(prev => ({ ...prev, [order.order_id]: event.target.value }))}
                                />
                              </>
                            )}
                            {order.status === 'delivering' && ['criteria_defined', 'evidence_collecting', 'submitted', 'accepted', 'accepted_with_exceptions'].includes(acceptanceStatus) && (
                              // 确认收货并结案 (B5): the owner's one click for a delivering order. The server records
                              // the receipt as evidence where none was (the last step's deliverable files), submits, passes and
                              // closes -- the same road as the buttons above, walked at once. Asked once more: a close is not undone.
                              // Once the owner passed the acceptance on those buttons the order is still delivering and this is
                              // its only way out: the click then sends the decision the record holds and only closes.
                              <button
                                type="button"
                                className="workflows-invoke"
                                data-testid="acceptance-receive-and-close"
                                disabled={!connected || acceptanceBusy}
                                title={t(acceptanceStatus === 'accepted' || acceptanceStatus === 'accepted_with_exceptions' ? 'orders.acceptance.closeAcceptedHint' : 'orders.acceptance.receiveAndCloseHint')}
                                onClick={() => {
                                  const passedAlready = acceptanceStatus === 'accepted' || acceptanceStatus === 'accepted_with_exceptions'
                                  if (!window.confirm(t(passedAlready ? 'orders.acceptance.closeAcceptedConfirm' : 'orders.acceptance.receiveAndCloseConfirm'))) return
                                  onAcceptanceDecide(order.order_id, acceptanceStatus === 'accepted_with_exceptions' ? 'accepted_with_exceptions' : 'accepted', acceptanceNotes[order.order_id] ?? '', [], { closeOrder: true })
                                }}
                              >
                                {t('orders.acceptance.receiveAndClose')}
                              </button>
                            )}
                            {acceptanceStatus === 'rejected' && (
                              <div className="orders-acceptance-rework">
                                <p>{order.acceptance.decision_note || order.acceptance.rework_feedback || t('orders.acceptance.reworkHint')}</p>
                                <input
                                  value={acceptanceReworkTargets[order.order_id] ?? ''}
                                  disabled={!connected || acceptanceBusy}
                                  placeholder={t('orders.acceptance.reworkTarget')}
                                  onChange={event => setAcceptanceReworkTargets(prev => ({ ...prev, [order.order_id]: event.target.value }))}
                                />
                                <button
                                  className="workflows-invoke"
                                  disabled={!connected || acceptanceBusy || !(acceptanceReworkTargets[order.order_id] ?? '').trim()}
                                  onClick={() => onAcceptanceRework(order.order_id, (acceptanceReworkTargets[order.order_id] ?? '').trim())}
                                >
                                  {t('orders.acceptance.startRework')}
                                </button>
                              </div>
                            )}
                            {order.acceptance.runtime_rework?.state === 'pending' && (
                              <div className="orders-acceptance-rework" data-testid="acceptance-runtime-recovery">
                                <p>
                                  {t('orders.acceptance.runtimePending', {
                                    node: order.acceptance.runtime_rework.target_node,
                                    count: order.acceptance.runtime_rework.attempt_count,
                                  })}
                                </p>
                                {order.acceptance.runtime_rework.last_error && (
                                  <p className="workflows-error">{order.acceptance.runtime_rework.last_error}</p>
                                )}
                                <button
                                  className="workflows-invoke"
                                  disabled={!connected || acceptanceBusy}
                                  onClick={() => onAcceptanceRework(
                                    order.order_id,
                                    order.acceptance?.runtime_rework?.target_node ?? '',
                                    order.acceptance?.runtime_rework?.request_id,
                                  )}
                                >
                                  {t('orders.acceptance.resumeRework')}
                                </button>
                              </div>
                            )}
                            {onAuthorizeRedistribution && order.acceptance.status === 'accepted' && (
                              // Owner only, and only a clean acceptance can carry a redistribution grant (the server refuses
                              // the rest). It does not hang on the verified archive: the grant binds the accepted version,
                              // and an acceptance whose evidence was never a file has no archive at all.
                              <div className="orders-acceptance-history-files">
                                <button
                                  type="button"
                                  className="orders-acceptance-evidence-open"
                                  data-testid="acceptance-redistribution-authorize"
                                  disabled={!connected}
                                  onClick={() => onAuthorizeRedistribution(order.order_id)}
                                >
                                  {t('orders.acceptance.authorizeRedistribution')}
                                </button>
                              </div>
                            )}
                            {order.acceptance.artifact_snapshot?.verified && (
                              <div className="orders-acceptance-history-files" data-testid="acceptance-final-files">
                                <span>{t('orders.acceptance.finalSnapshotVerified', { count: order.acceptance.artifact_snapshot.files?.length ?? 0 })}</span>
                                <button
                                  type="button"
                                  className="orders-acceptance-evidence-open"
                                  disabled={!connected || !onAcceptanceRoundExport}
                                  onClick={() => onAcceptanceRoundExport?.(
                                    order.order_id,
                                    Number(order.acceptance?.round_no ?? 1),
                                  )}
                                >
                                  {t('orders.acceptance.exportRound')}
                                </button>
                                {(order.acceptance.status === 'accepted' || order.acceptance.status === 'accepted_with_exceptions') && (
                                  <button
                                    type="button"
                                    className="orders-acceptance-evidence-open"
                                    data-testid="acceptance-portfolio-export"
                                    disabled={!connected || !onPortfolioExport}
                                    onClick={() => onPortfolioExport?.(order.order_id)}
                                  >
                                    {t('orders.acceptance.exportPortfolio')}
                                  </button>
                                )}
                                {order.acceptance.artifact_snapshot.files?.map(file => (
                                  <button
                                    key={file.archive_path}
                                    type="button"
                                    className="orders-acceptance-evidence-open"
                                    disabled={!connected || !onOpenArtifact}
                                    onClick={() => onOpenArtifact?.(file.archive_path)}
                                    title={file.sha256}
                                  >
                                    {file.source_path}
                                  </button>
                                ))}
                              </div>
                            )}
                            {order.acceptance.artifact_comparison && !order.acceptance.artifact_comparison.error && (
                              <div className="settings-note" data-testid="acceptance-final-comparison">
                                {t('orders.acceptance.byteComparison', {
                                  earlier: order.acceptance.artifact_comparison.earlier_round ?? '?',
                                  later: order.acceptance.artifact_comparison.later_round ?? '?',
                                  added: order.acceptance.artifact_comparison.added?.length ?? 0,
                                  removed: order.acceptance.artifact_comparison.removed?.length ?? 0,
                                  changed: order.acceptance.artifact_comparison.changed?.length ?? 0,
                                  unchanged: order.acceptance.artifact_comparison.unchanged?.length ?? 0,
                                })}
                              </div>
                            )}
                            {(order.acceptance.history?.length ?? 0) > 0 && (
                              <details className="orders-acceptance-history">
                                <summary>{t('orders.acceptance.history', { count: order.acceptance.history?.length ?? 0 })}</summary>
                                <ol>
                                  {order.acceptance.history?.map((item, index) => (
                                    <li key={`${String(item.round_no ?? index + 1)}-${String(item.evidence_hash ?? '')}`}>
                                      {t('orders.acceptance.round', { round: Number(item.round_no ?? index + 1) })}
                                      {' · '}{t(`orders.acceptance.${String(item.status ?? '')}` as Parameters<typeof t>[0])}
                                      {' · '}{Array.isArray(item.evidence) ? item.evidence.length : 0} {t('orders.acceptance.evidenceCount')}
                                      {/* What the customer said about that round stays with it: once the next round starts, the
                                          rework form that showed it is gone, and this is the only place left to read it. */}
                                      {String(item.decision_note ?? '').trim() && (
                                        <div className="orders-acceptance-history-note" data-testid="acceptance-history-note">
                                          {t('orders.acceptance.decisionNote')}：{String(item.decision_note)}
                                        </div>
                                      )}
                                      {item.artifact_snapshot?.verified && (
                                        <div className="orders-acceptance-history-files" data-testid="acceptance-history-files">
                                          <span>{t('orders.acceptance.snapshotVerified', { count: item.artifact_snapshot.files?.length ?? 0 })}</span>
                                          <button
                                            type="button"
                                            className="orders-acceptance-evidence-open"
                                            disabled={!connected || !onAcceptanceRoundExport}
                                            onClick={() => onAcceptanceRoundExport?.(
                                              order.order_id,
                                              Number(item.round_no ?? index + 1),
                                            )}
                                          >
                                            {t('orders.acceptance.exportRound')}
                                          </button>
                                          {item.artifact_snapshot.files?.map(file => (
                                            <button
                                              key={file.archive_path}
                                              type="button"
                                              className="orders-acceptance-evidence-open"
                                              disabled={!connected || !onOpenArtifact}
                                              onClick={() => onOpenArtifact?.(file.archive_path)}
                                              title={file.sha256}
                                            >
                                              {file.source_path}
                                            </button>
                                          ))}
                                        </div>
                                      )}
                                      {item.artifact_comparison && !item.artifact_comparison.error && (
                                        <div className="settings-note" data-testid="acceptance-round-comparison">
                                          {t('orders.acceptance.byteComparison', {
                                            earlier: item.artifact_comparison.earlier_round ?? '?',
                                            later: item.artifact_comparison.later_round ?? '?',
                                            added: item.artifact_comparison.added?.length ?? 0,
                                            removed: item.artifact_comparison.removed?.length ?? 0,
                                            changed: item.artifact_comparison.changed?.length ?? 0,
                                            unchanged: item.artifact_comparison.unchanged?.length ?? 0,
                                          })}
                                        </div>
                                      )}
                                      {item.artifact_comparison?.error && (
                                        <div className="workflows-error">{item.artifact_comparison.error}</div>
                                      )}
                                    </li>
                                  ))}
                                </ol>
                              </details>
                            )}
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                  {(orderMetrics ?? []).filter(m => m.order_id === order.order_id).map(metric => (
                    <tr className={`orders-budget-row${folded}`} key={`metrics-${metric.order_id}`}>
                      <td colSpan={6}>
                        <div className="orders-budget" data-testid="order-metrics">
                          <span className="orders-budget-kind" data-testid="order-saving">
                            {t('orders.metrics.saving')}{' '}
                            {metric.saving.state === 'calculated'
                              ? `${metric.saving.bound === 'at_most' ? t('orders.metrics.atMost') : ''}¥${(Number(metric.saving.value ?? 0) / 100).toFixed(2)}`
                              : `${t('orders.metrics.notCalculated')} · ${t('orders.metrics.missing')} ${metric.saving.missing.join('、')}`}
                          </span>
                          {metric.saving.rests_on.length > 0 && (
                            <span className="settings-note" data-testid="order-saving-rests-on">
                              {t('orders.metrics.restsOn', { names: metric.saving.rests_on.join('、') })}
                            </span>
                          )}
                          {(orderUsage ?? []).filter(usage => usage.order_id === order.order_id && usage.total.calls > 0).map(usage => (
                            <span className="orders-budget-kind" data-testid="order-usage-by-purpose" key={`usage-${usage.order_id}`}
                              title={[...usage.by_purpose.map(row => `${t(`orders.usage.purpose.${row.purpose}` as Parameters<typeof t>[0])}: ${row.calls} × · ${row.tokens_in} + ${row.tokens_out}`), ...usage.limits].join('\n')}>
                              {t('orders.usage.line', {
                                calls: usage.total.calls, tokens: usage.total.tokens_in + usage.total.tokens_out,
                                judgementCalls: usage.judgement.calls, share: percent(usage.judgement.share_of_tokens),
                                outputShare: percent(usage.judgement.output_share_of_judgement),
                              })}
                            </span>
                          ))}
                          <span className="orders-budget-kind" data-testid="order-sla">
                            {t('orders.metrics.sla')}{' '}
                            {metric.sla.state === 'not_calculated'
                              ? `${t('orders.metrics.notCalculated')} · ${t('orders.metrics.missing')} ${metric.sla.missing.join('、')}`
                              : t(`orders.metrics.sla.${metric.sla.state}` as Parameters<typeof t>[0], {
                                  hours: String(metric.sla.remaining_hours ?? metric.sla.elapsed_hours ?? 0),
                                })}
                          </span>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {/* A closed allowance is history, not a ceiling: the order is offered a new one. */}
                  {onCreateBudget && budgets != null && !(budgets ?? []).some(b => b.order_id === order.order_id && b.state !== 'closed')
                    && !['closed', 'rejected'].includes(order.status) && (
                    <tr className={`orders-budget-row${folded}`}>
                      <td colSpan={6}>
                        <div className="orders-budget" data-testid="order-budget-grant" data-order-id={order.order_id}>
                          <span className="settings-note">{t('orders.budget.unlimited')}</span>
                          {orderBudget.orderId !== order.order_id ? (
                            <button type="button" className="workflows-cancel" data-testid="order-budget-open" disabled={!connected}
                              onClick={() => setOrderBudget({ orderId: order.order_id, draft: {} })}>
                              {t('orders.budget.grant')}
                            </button>
                          ) : (() => {
                            const allowance = budgetLimitsFromDraft(orderBudget.draft)
                            const empty = Object.keys(allowance.limits).length === 0
                            return <>
                              <BudgetLimitInputs draft={orderBudget.draft} onChange={draft => setOrderBudget({ orderId: order.order_id, draft })} />
                              <span className="settings-note">{t(order.launched_task_id ? 'orders.budget.grantRunningHint' : 'orders.budget.grantHint')}</span>
                              <button type="button" className="workflows-invoke" data-testid="order-budget-confirm"
                                disabled={!connected || budgetBusyOrderId === order.order_id || empty || allowance.invalid.length > 0}
                                onClick={() => { if (onCreateBudget(order.order_id, allowance.limits)) setOrderBudget({ orderId: '', draft: {} }) }}>
                                {budgetBusyOrderId === order.order_id ? t('request.busy') : t('orders.budget.grantConfirm')}
                              </button>
                              <button type="button" className="workflows-cancel" onClick={() => setOrderBudget({ orderId: '', draft: {} })}>{t('workflows.cancel')}</button>
                            </>
                          })()}
                        </div>
                      </td>
                    </tr>
                  )}
                  {(budgets ?? []).some(b => b.order_id === order.order_id) && (
                    <tr className={`orders-budget-row${folded}`}>
                      <td colSpan={6}>
                        {(budgets ?? []).filter(b => b.order_id === order.order_id).map(budget => (
                          <div className="orders-budget" key={budget.budget_id} data-testid="order-budget" data-order-id={order.order_id}>
                            <span className={`capability-pill capability-pill--${budget.state === 'open' ? 'ok' : budget.state === 'closed' ? 'muted' : 'warn'}`}>
                              {t(`orders.budget.${budget.state}` as Parameters<typeof t>[0])}
                            </span>
                            {Object.keys(budget.limits).length === 0 && <span className="settings-note">{t('orders.budget.unlimited')}</span>}
                            {/* Every order is held from its launch, without a ceiling unless the owner sets one (model-routing
                                phase 0): ceilings are set on that same allowance, in place, for the whole run at once. */}
                            {onCreateBudget && budget.state === 'open' && Object.keys(budget.limits).length === 0 && (orderBudget.orderId !== order.order_id ? (
                              <button type="button" className="workflows-cancel" data-testid="order-budget-set-ceiling-open" disabled={!connected}
                                onClick={() => setOrderBudget({ orderId: order.order_id, draft: {} })}>
                                {t('orders.budget.setCeiling')}
                              </button>
                            ) : (() => {
                              const allowance = budgetLimitsFromDraft(orderBudget.draft)
                              const empty = Object.keys(allowance.limits).length === 0
                              return (
                                <span className="orders-budget-resolve" data-testid="order-budget-set-ceiling">
                                  <BudgetLimitInputs draft={orderBudget.draft} onChange={draft => setOrderBudget({ orderId: order.order_id, draft })} />
                                  <button type="button" className="workflows-invoke" data-testid="order-budget-set-ceiling-confirm"
                                    disabled={!connected || budgetBusyOrderId === order.order_id || empty || allowance.invalid.length > 0}
                                    onClick={() => { if (onCreateBudget(order.order_id, allowance.limits)) setOrderBudget({ orderId: '', draft: {} }) }}>
                                    {budgetBusyOrderId === order.order_id ? t('request.busy') : t('orders.budget.setCeilingConfirm')}
                                  </button>
                                  <button type="button" className="workflows-cancel" onClick={() => setOrderBudget({ orderId: '', draft: {} })}>{t('workflows.cancel')}</button>
                                  <span className="settings-note">{t('orders.budget.setCeilingHint')}</span>
                                </span>
                              )
                            })())}
                            {Object.entries(budget.limits).map(([kind, limit]) => (
                              <span className="orders-budget-kind" key={kind}>
                                {t(`orders.budget.kind.${kind}` as Parameters<typeof t>[0])} {budget.consumed[kind] ?? 0}
                                {(budget.held[kind] ?? 0) > 0 && ` (+${budget.held[kind]} ${t('orders.budget.held')})`} / {limit}
                              </span>
                            ))}
                            {onRaiseBudget && budget.state === 'open' && Object.keys(budget.limits).some(kind => kind !== 'cost_cny') && (raisingBudget.orderId !== order.order_id ? (
                              <button type="button" className="workflows-cancel" data-testid="order-budget-raise-open" disabled={!connected}
                                onClick={() => setRaisingBudget({ orderId: order.order_id, draft: {} })}>
                                {t('orders.budget.raise')}
                              </button>
                            ) : (() => {
                              // Only up, and only ceilings that exist: the server refuses anything else, and so does this form.
                              const raised: Record<string, number> = {}
                              let invalid = false
                              for (const [kind, text] of Object.entries(raisingBudget.draft)) {
                                if (!text.trim()) continue
                                const value = Number(text)
                                if (!Number.isInteger(value) || value <= (budget.limits[kind] ?? 0)) invalid = true
                                else raised[kind] = value
                              }
                              return (
                                <span className="orders-budget-resolve" data-testid="order-budget-raise">
                                  {Object.entries(budget.limits).filter(([kind]) => kind !== 'cost_cny').map(([kind, limit]) => (
                                    <label key={kind} className="orders-budget-kind">
                                      {t(`orders.budget.kind.${kind}` as Parameters<typeof t>[0])} {limit} →{' '}
                                      <input
                                        className="capability-refresh-btn" inputMode="numeric" style={{ width: '5em' }}
                                        data-testid={`order-budget-raise-${kind}`}
                                        value={raisingBudget.draft[kind] ?? ''}
                                        onChange={event => setRaisingBudget({ orderId: order.order_id, draft: { ...raisingBudget.draft, [kind]: event.target.value } })}
                                      />
                                    </label>
                                  ))}
                                  <button type="button" className="workflows-invoke" data-testid="order-budget-raise-confirm"
                                    disabled={!connected || budgetBusyOrderId === order.order_id || invalid || Object.keys(raised).length === 0}
                                    onClick={() => { if (onRaiseBudget(order.order_id, raised)) setRaisingBudget({ orderId: '', draft: {} }) }}>
                                    {budgetBusyOrderId === order.order_id ? t('request.busy') : t('orders.budget.raiseConfirm')}
                                  </button>
                                  <button type="button" className="workflows-cancel" onClick={() => setRaisingBudget({ orderId: '', draft: {} })}>{t('workflows.cancel')}</button>
                                  <span className="settings-note">{t('orders.budget.raiseHint')}</span>
                                </span>
                              )
                            })())}
                            {/* A running order is never left unheld: "close" lifts its ceilings in place (the server does the same),
                                so there is nothing to offer when it has none. An order that is not running closes as before. */}
                            {(() => {
                              const running = Boolean(order.launched_task_id) && !['closed', 'rejected'].includes(order.status)
                              if (!onCloseBudget || budget.state === 'closed' || (running && Object.keys(budget.limits).length === 0)) return null
                              return closingBudget.orderId !== order.order_id ? (
                                <button type="button" className="workflows-cancel" data-testid="order-budget-close-open" disabled={!connected}
                                  onClick={() => setClosingBudget({ orderId: order.order_id, reason: '' })}>
                                  {t(running ? 'orders.budget.lift' : 'orders.budget.close')}
                                </button>
                              ) : (
                                <span className="orders-budget-resolve" data-testid="order-budget-close">
                                  <input
                                    className="capability-refresh-btn"
                                    placeholder={t(running ? 'orders.budget.liftWhy' : 'orders.budget.closeWhy')}
                                    value={closingBudget.reason}
                                    onChange={event => setClosingBudget({ orderId: order.order_id, reason: event.target.value })}
                                  />
                                  <button type="button" className="workflows-cancel" data-testid="order-budget-close-confirm"
                                    disabled={!connected || budgetBusyOrderId === order.order_id || closingBudget.reason.trim().length < 2}
                                    onClick={() => { if (onCloseBudget(order.order_id, closingBudget.reason.trim())) setClosingBudget({ orderId: '', reason: '' }) }}>
                                    {budgetBusyOrderId === order.order_id ? t('request.busy') : t(running ? 'orders.budget.liftConfirm' : 'orders.budget.closeConfirm')}
                                  </button>
                                  <button type="button" className="workflows-cancel" onClick={() => setClosingBudget({ orderId: '', reason: '' })}>{t('workflows.cancel')}</button>
                                  <span className="settings-note">{t(running ? 'orders.budget.liftHint' : 'orders.budget.closeHint')}</span>
                                </span>
                              )
                            })()}
                            {budget.state === 'pending_verification' && (
                              <span className="orders-budget-pending">
                                <span className="workflows-error">{t('orders.budget.pendingHint')}{budget.state_reason ? ` · ${budgetHoldReason(budget.state_reason, t)}` : ''}</span>
                                {onResolveBudget && (pendingReservations ?? []).filter(r => r.budget_id === budget.budget_id).map(reservation => (
                                  <span key={reservation.reservation_id} className="orders-budget-resolve">
                                    <button
                                      type="button"
                                      className="workflows-cancel"
                                      data-testid="budget-resolve-happened"
                                      disabled={!connected}
                                      onClick={() => onResolveBudget(reservation.reservation_id, true)}
                                    >
                                      {t('orders.budget.happened', { what: budgetAmountInWords(reservation.amount, reservation.kind, t) })}
                                    </button>
                                    <button
                                      type="button"
                                      className="workflows-cancel"
                                      data-testid="budget-resolve-not-happened"
                                      disabled={!connected}
                                      onClick={() => onResolveBudget(reservation.reservation_id, false)}
                                    >
                                      {t('orders.budget.notHappened')}
                                    </button>
                                  </span>
                                ))}
                              </span>
                            )}
                          </div>
                        ))}
                      </td>
                    </tr>
                  )}
                  {(onSaveBlueprint || onApplyBlueprint) && order.fde_control_plane && (
                    <tr className={`orders-blueprint-row${folded}`}>
                      <td colSpan={6}>
                      <div className="orders-blueprint-bar">
                        <span className="settings-note">{t('orders.blueprintHint')}</span>
                        {onSaveBlueprint && (
                          <span className="orders-blueprint-save">
                            <input
                              aria-label={t('orders.blueprintSave')}
                              placeholder={t('orders.blueprintNamePlaceholder')}
                              value={blueprintNames[order.order_id] ?? ''}
                              onChange={event => setBlueprintNames(prev => ({ ...prev, [order.order_id]: event.target.value }))}
                            />
                            <button
                              type="button"
                              className="workflows-cancel"
                              data-testid="blueprint-save"
                              disabled={!connected || !(blueprintNames[order.order_id] ?? '').trim()}
                              onClick={() => { onSaveBlueprint(order.order_id, (blueprintNames[order.order_id] ?? '').trim()); setBlueprintNames(prev => ({ ...prev, [order.order_id]: '' })) }}
                            >
                              {t('orders.blueprintSave')}
                            </button>
                          </span>
                        )}
                        {onApplyBlueprint && (blueprints ?? []).filter(b => b.source_order_id !== order.order_id).length > 0 && (
                          <span className="orders-blueprint-apply">
                            <select
                              aria-label={t('orders.blueprintApply')}
                              data-testid="blueprint-choice"
                              value={blueprintChoice[order.order_id] ?? ''}
                              onChange={event => setBlueprintChoice(prev => ({ ...prev, [order.order_id]: event.target.value }))}
                            >
                              <option value="">{t('orders.blueprintPick')}</option>
                              {(blueprints ?? []).filter(b => b.source_order_id !== order.order_id).map(b => (
                                <option key={b.blueprint_id} value={b.blueprint_id}>
                                  {b.name}{b.source_customer_id ? ` · ${b.source_customer_id}` : ''}
                                </option>
                              ))}
                            </select>
                            <button
                              type="button"
                              className="workflows-cancel"
                              data-testid="blueprint-apply"
                              disabled={!connected || !(blueprintChoice[order.order_id] ?? '')}
                              onClick={() => onApplyBlueprint(blueprintChoice[order.order_id] ?? '', order.order_id)}
                            >
                              {t('orders.blueprintApply')}
                            </button>
                          </span>
                        )}
                      </div>
                      </td>
                    </tr>
                  )}
                  {fdeEditorOpenId === order.order_id && order.fde_control_plane && onUpdateFdeControlPlane && (
                    <tr className="orders-fde-editor-row">
                      <td colSpan={6}>
                        <div className="workflows-builder-form orders-fde-editor" role="form" aria-label={t('orders.fde.edit')}>
                          <strong>{t('orders.fde.title')}</strong>
                          <textarea aria-label={t('orders.fde.asIs')} rows={2} value={fdeDrafts[order.order_id]?.asIs ?? ''} placeholder={t('orders.fde.asIs')} onChange={event => setFdeDrafts(prev => ({ ...prev, [order.order_id]: { ...(prev[order.order_id] ?? { asIs: '', toBe: '', uat: '', sla: '', rollback: '', roi: '' }), asIs: event.target.value } }))} />
                          <textarea aria-label={t('orders.fde.toBe')} rows={2} value={fdeDrafts[order.order_id]?.toBe ?? ''} placeholder={t('orders.fde.toBe')} onChange={event => setFdeDrafts(prev => ({ ...prev, [order.order_id]: { ...(prev[order.order_id] ?? { asIs: '', toBe: '', uat: '', sla: '', rollback: '', roi: '' }), toBe: event.target.value } }))} />
                          {(['uat', 'sla', 'rollback', 'roi'] as const).map(key => (
                            <textarea key={key} aria-label={t(`orders.fde.${key}` as Parameters<typeof t>[0])} rows={2} value={fdeDrafts[order.order_id]?.[key] ?? ''} placeholder={t(`orders.fde.${key}` as Parameters<typeof t>[0])} onChange={event => setFdeDrafts(prev => ({ ...prev, [order.order_id]: { ...(prev[order.order_id] ?? { asIs: '', toBe: '', uat: '', sla: '', rollback: '', roi: '' }), [key]: event.target.value } }))} />
                          ))}
                          {fdeEditorErrors[order.order_id] && <span className="workflows-error">{fdeEditorErrors[order.order_id]}</span>}
                          <div className="workflows-form-actions">
                            <button type="button" className="workflows-cancel" disabled={updatingFdeControlPlaneOrderId === order.order_id} onClick={() => setFdeEditorOpenId(null)}>{t('workflows.cancel')}</button>
                            <button type="button" className="workflows-invoke" disabled={!connected || updatingFdeControlPlaneOrderId === order.order_id} onClick={() => submitFdeEdit(order.order_id)}>{t('orders.fde.save')}</button>
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        )}
        {/* 4.7-A08: the next older page, added below; the rows already here stay where they are. */}
        {ordersTruncated && onLoadOlderOrders && orders !== null && orders.length > 0 && (
          <p className="orders-truncated" data-testid="orders-load-older">
            <button type="button" className="shell-btn" disabled={olderOrdersLoading || !connected} onClick={onLoadOlderOrders}>
              {olderOrdersLoading
                ? (locale.startsWith('zh') ? '正在读取更早的工单…' : 'Reading older orders…')
                : (locale.startsWith('zh') ? '查看更早的工单' : 'Show older orders')}
            </button>
          </p>
        )}
      </section>}

      {surface !== 'builder' && customerPreviewSlot}
      {surface !== 'builder' && artifactsSlot && <div className="workflows-artifacts" aria-label={t('artifacts.title')}>{artifactsSlot}</div>}

      {surface !== 'orders' && (() => {
        const catalog = <div data-testid="workflow-package-catalog">
        <h3 className="orders-title">{t('workflows.packagesTitle')}</h3>
      {workflows === null ? (
        <div className="workflows-empty">{t('workflows.loading')}</div>
      ) : workflows.length === 0 ? (
        <div className="workflows-empty">{t('workflows.empty')}</div>
      ) : (
        <div className="workflows-grid">
          {(surface === 'builder' && builderWorkflow ? workflows.filter(wf => wf.id === builderWorkflow.id) : workflows).map(wf => {
            const open = openId === wf.id
            const statusKey = `workflows.status.${wf.status}` as Parameters<typeof t>[0]
            const statusLabel = t(statusKey) === statusKey ? wf.status : t(statusKey)
            return (
              <section key={wf.id} data-flow-card={wf.id} className={`workflows-card workflows-card--${wf.status}`}>
                <header className="workflows-card-head">
                  <div>
                    <h3 className="workflows-card-name">{wf.name}</h3>
                    <span className="workflows-card-version">v{wf.version || '0'}</span>
                  </div>
                  <span className={`workflows-badge workflows-badge--${wf.status}`}>{statusLabel}</span>
                </header>
                <p className="workflows-card-summary">{wf.summary}</p>
                {/* A flow that wrote no acceptance criteria is not stopped from saving or placing orders: the launch
                    generates one criterion from the last step's deliverables (B5). Said here, once. */}
                {Array.isArray(wf.acceptance) && wf.acceptance.length === 0 && wf.status !== 'retired' && (
                  <p className="workflows-note" data-testid="workflow-no-acceptance">{t('workflows.noAcceptanceHint')}</p>
                )}
                {wf.applies_to.length > 0 && (
                  <div className="workflows-chips">
                    {wf.applies_to.map(item => <span key={item} className="workflows-chip">{item}</span>)}
                  </div>
                )}
                <dl className="workflows-facts">
                  <div><dt>{t('workflows.fact.stations')}</dt><dd>{wf.stations.length}</dd></div>
                  <div><dt>{t('workflows.fact.gates')}</dt><dd>{wf.gate_count}</dd></div>
                  {wf.skill_contracts && wf.skill_contracts.length > 0 && (
                    <div>
                      <dt>{t('workflows.fact.skills')}</dt>
                      <dd className={wf.skill_contracts.every(skill => skill.complete) ? '' : 'workflows-fact-warn'}>
                        {wf.skill_contracts.filter(skill => skill.complete).length}/{wf.skill_contracts.length}
                      </dd>
                    </div>
                  )}
                  {onReviewSkill && (wf.status === 'draft' || wf.status === 'testing') && (wf.skill_contracts ?? []).some(skill => skill.problems.some(p => p.includes('draft'))) && (
                    <div data-testid="skill-drafts">
                      <dt>草稿技能</dt>
                      <dd>
                        {(wf.skill_contracts ?? []).filter(skill => skill.problems.some(p => p.includes('draft'))).map(skill => (
                          <span key={skill.id} data-testid="skill-draft-row">
                            {skill.id} · {skill.version}
                            <button type="button" className="workflows-invoke" data-testid="skill-review"
                              title="你看过这份技能、认可它的写法，才点。会把你的名字和时间写进技能文件，去掉草稿标记；发布就绪检查才会放行。"
                              disabled={!connected || !wf.draft_revision?.trim()}
                              onClick={() => onReviewSkill(wf.id, wf.draft_revision ?? '', skill.id, '')}>我看过了，认可</button>
                          </span>
                        ))}
                      </dd>
                    </div>
                  )}
                  {wf.skill_versions && Object.keys(wf.skill_versions).length > 0 && (
                    <div>
                      <dt>{t('workflows.fact.skillLock')}</dt>
                      <dd title={Object.entries(wf.skill_versions).map(([id, version]) => `${id}: ${version}`).join(' · ')}>
                        {Object.values(wf.skill_versions).every(Boolean) ? '✓' : '⚠'} {Object.keys(wf.skill_versions).length}
                      </dd>
                    </div>
                  )}
                  {((wf.reaching_tools_without_grant?.length ?? 0) > 0 || (wf.tools_with_unknown_effects?.length ?? 0) > 0) && (
                    <div>
                      <dt>{t('workflows.fact.toolGrants')}</dt>
                      <dd
                        className="workflows-fact-warn"
                        data-testid="workflows-tool-grant-gap"
                        title={[...(wf.reaching_tools_without_grant ?? []), ...(wf.tools_with_unknown_effects ?? [])].join(' · ')}
                      >
                        {t('workflows.fact.toolGrantsAsk', { n: (wf.reaching_tools_without_grant?.length ?? 0) + (wf.tools_with_unknown_effects?.length ?? 0) })}
                      </dd>
                    </div>
                  )}
                  {wf.skill_test_report && (
                    <div>
                      <dt>{t('workflows.fact.skillTests')}</dt>
                      <dd className={wf.skill_test_report.status === 'passed' ? '' : 'workflows-fact-warn'}>
                        {wf.skill_test_report.status === 'passed' ? '✓' : '⚠'} {wf.skill_test_report.status}
                      </dd>
                    </div>
                  )}
                  <div>
                    <dt>{t('workflows.fact.lastRun')}</dt>
                    <dd className={wf.latest_test && wf.latest_test.outcome !== 'passed' ? 'workflows-fact-warn' : ''} data-last-run-outcome={wf.latest_test?.outcome ?? ''}>
                      {wf.latest_test
                        ? `${wf.latest_test.outcome === 'passed' ? '✓' : '✗'} ${wf.latest_test.outcome} · ${wf.latest_test.date} · ${formatMinutes(wf.latest_test.duration_minutes)} · ${wf.latest_test.cost_cny_estimate !== null ? `≈¥${wf.latest_test.cost_cny_estimate.toFixed(2)}` : '—'}${wf.latest_test.execution_mode ? ` · ${t(`workflows.testMode.${wf.latest_test.execution_mode}` as Parameters<typeof t>[0]) === `workflows.testMode.${wf.latest_test.execution_mode}` ? wf.latest_test.execution_mode : t(`workflows.testMode.${wf.latest_test.execution_mode}` as Parameters<typeof t>[0])}` : ''}`
                        : t('workflows.fact.noRun')}
                    </dd>
                  </div>
                </dl>
                {wf.skill_contracts && wf.skill_contracts.length > 0 && (
                  <ul className="workflows-skill-details" aria-label={t('workflows.skillDetails')}>
                    {wf.skill_contracts.map(skill => {
                      const problems = Array.isArray(skill.problems) ? skill.problems.filter(Boolean) : []
                      return (
                        <li key={skill.id} className={skill.complete ? 'is-complete' : 'is-blocked'}>
                          <span className="workflows-skill-status" aria-hidden="true">{skill.complete ? '✓' : '!'}</span>
                          <span className="workflows-skill-name">{skill.id}</span>
                          <span className="workflows-skill-version">v{skill.version || '—'}</span>
                          {problems.length > 0 && <span className="workflows-skill-problems" title={problems.join('；')}>{problems.join('；')}</span>}
                        </li>
                      )
                    })}
                  </ul>
                )}
                {wf.nodes && wf.nodes.length > 0 ? (
                  <>
                    {wf.status !== 'retired' && (
                      <div className="workflows-node-view">
                        {/* the canvas lives at the top of the page (the builder's front stage); this opens it on this workflow */}
                        <button type="button" onClick={() => openInBuilder(wf.id)}>{locale.startsWith('zh') ? '打开无限画布' : 'Open infinite canvas'}</button>
                      </div>
                    )}
                  <ol className="workflows-nodes">
                    {wf.nodes.map(node => {
                      const typeKey = `workflows.node.${node.type}` as Parameters<typeof t>[0]
                      const gateKey = `workflows.gate.${node.gate}` as Parameters<typeof t>[0]
                      const who = node.assignee || node.role || (node.invoke?.workflow ? String(node.invoke.workflow) : '')
                      return (
                        <li key={node.id} className={`workflows-node workflows-node--${node.type}`} title={node.deliverables.join(' · ')}>
                          <span className="workflows-node-name">{node.name}</span>
                          <span className="workflows-node-type">{t(typeKey) === typeKey ? node.type : t(typeKey)}</span>
                          {who && <span className="workflows-node-who">{who}</span>}
                          {node.gate !== 'none' && (
                            <span className="workflows-node-gate">✋ {t(gateKey) === gateKey ? node.gate : t(gateKey)}{node.gate_to ? ` · ${node.gate_to}` : ''}</span>
                          )}
                        </li>
                      )
                    })}
                  </ol>
                  </>
                ) : wf.stations.length > 0 && (
                  <ol className="workflows-stations">
                    {wf.stations.map(station => <li key={station}>{station}</li>)}
                  </ol>
                )}
                {onUpdateNodes && (wf.status === 'draft' || wf.status === 'testing') && (
                  editingWorkflowId !== wf.id ? (
                    <button type="button" className="workflows-cancel" disabled={!connected} onClick={() => beginNodeEdit(wf)}>
                      {t('workflows.editNodes')}
                    </button>
                  ) : (
                    <div className="workflows-node-editor" aria-label={t('workflows.editNodes')}>
                      <div className="workflows-node-editor-head"><strong>{t('workflows.editNodes')}</strong><button type="button" className="workflows-cancel" onClick={() => discardNodeDraft(wf)} disabled={updatingWorkflowId === wf.id}>{t('workflows.cancel')}</button></div>
                      {(nodeDrafts[wf.id] ?? wf.nodes).map(node => (
                        <div className="workflows-node-editor-row" key={node.id}>
                          <strong>{node.name || node.id}</strong>
                          {(wf.org_roles ?? []).length > 0 ? (
                            <select aria-label={`${node.id} ${t('workflows.nodeRole')}`} value={node.role} onChange={event => updateNodeDraft(wf.id, node.id, { role: event.target.value, canvas: null })}>
                              <option value="">{t('workflows.nodeRole')}</option>
                              {node.role && !(wf.org_roles ?? []).some(role => role.id === node.role) && (
                                <option value={node.role}>{node.role}{locale.startsWith('zh') ? '（不在组织里）' : ' (not in the org)'}</option>
                              )}
                              {(wf.org_roles ?? []).map(role => <option key={role.id} value={role.id}>{role.name && role.name !== role.id ? `${role.name} · ${role.id}` : role.id}</option>)}
                            </select>
                          ) : (
                            <input aria-label={`${node.id} ${t('workflows.nodeRole')}`} value={node.role} placeholder={t('workflows.nodeRole')} onChange={event => updateNodeDraft(wf.id, node.id, { role: event.target.value, canvas: null })} />
                          )}
                          {/* the same choices as the canvas: owner, or someone on the roster of the org this workflow runs on */}
                          <select aria-label={`${node.id} ${t('workflows.nodeAssignee')}`} value={node.assignee} onChange={event => updateNodeDraft(wf.id, node.id, { assignee: event.target.value })}>
                            <option value="">{locale.startsWith('zh') ? '（未指派）' : '(unassigned)'}</option>
                            <option value="owner">owner</option>
                            {node.assignee && node.assignee !== 'owner' && !rosterOf(wf).some(emp => emp.employee_id === node.assignee) && (
                              <option value={node.assignee}>{node.assignee}</option>
                            )}
                            {rosterOf(wf).map(emp => <option key={emp.employee_id} value={emp.employee_id}>{emp.name || emp.employee_id}</option>)}
                          </select>
                          <select aria-label={`${node.id} ${t('workflows.nodeGate')}`} value={node.gate} onChange={event => updateNodeDraft(wf.id, node.id, { gate: event.target.value })}>
                            <option value="none">{t('workflows.gate.none')}</option>
                            <option value="human_required">{t('workflows.gate.human_required')}</option>
                            <option value="ai_first_human_spot_check">{t('workflows.gate.ai_first_human_spot_check')}</option>
                          </select>
                          <input aria-label={`${node.id} ${t('workflows.nodeDeliverables')}`} value={node.deliverables.join(', ')} placeholder={t('workflows.nodeDeliverables')} onChange={event => updateNodeDraft(wf.id, node.id, { deliverables: event.target.value.split(/[,，]/).map(item => item.trim()).filter(Boolean) })} />
                          <input aria-label={`${node.id} ${t('workflows.nodeSkills')}`} value={(node.skills ?? []).join(', ')} placeholder={t('workflows.nodeSkills')} onChange={event => updateNodeDraft(wf.id, node.id, { skills: event.target.value.split(/[,，]/).map(item => item.trim()).filter(Boolean) })} />
                          <input aria-label={`${node.id} ${t('workflows.nodeTools')}`} value={(node.tools ?? []).join(', ')} placeholder={t('workflows.nodeTools')} onChange={event => updateNodeDraft(wf.id, node.id, { tools: event.target.value.split(/[,，]/).map(item => item.trim()).filter(Boolean) })} />
                        </div>
                      ))}
                      <div className="workflows-node-editor-footer">
                        {nodeSaveControls(wf)}
                      </div>
                    </div>
                  )
                )}
                {wf.gates.length > 0 && (
                  <ul className="workflows-gates">
                    {wf.gates.map(gate => (
                      <li key={gate.station} title={gate.decides}>
                        ✋ {gate.station} · {gate.who}
                      </li>
                    ))}
                  </ul>
                )}
                {wf.problems.length > 0 && (
                  <ul className="workflows-problems">
                    {wf.problems.map(problem => <li key={problem}>{problem}</li>)}
                  </ul>
                )}
                {!wf.draft_revision?.trim() && (wf.status === 'draft' || wf.status === 'testing' || wf.status === 'published') && (
                  <p className="workflows-error" role="status" data-testid="workflow-revision-missing">
                    {locale.startsWith('zh') ? '缺少已读取版本，请刷新工作流后再试。' : 'The observed revision is missing. Refresh this workflow before retrying.'}
                  </p>
                )}
                {wf.status === 'testing' && (
                  <div className={`workflows-release-readiness${wf.release_ready ? ' is-ready' : ''}`}>
                    {wf.release_ready ? t('workflows.release.ready') : t('workflows.release.blocked')}
                    {!wf.release_ready && wf.release_blockers && wf.release_blockers.length > 0 && (
                      <span title={wf.release_blockers.join('；')}> · {wf.release_blockers[0]}</span>
                    )}
                    {wf.release_ready && onRequestRelease && wf.release_approval?.status !== 'approved' && wf.release_approval?.status !== 'pending' && (
                      <button
                        type="button"
                        className="workflows-publish"
                        disabled={!connected || !wf.draft_revision?.trim() || requestingReleaseWorkflowId === wf.id}
                        onClick={() => onRequestRelease(wf.id, wf.draft_revision ?? '')}
                      >
                        {requestingReleaseWorkflowId === wf.id ? t('workflows.requestingRelease') : t('workflows.requestRelease')}
                      </button>
                    )}
                    {admissionRefusal?.workflowId === wf.id && admissionRefusal.action === 'request_release' && (
                      <span className="workflows-error" role="alert" data-testid="release-request-refused">
                        {' · '}{locale.startsWith('zh') ? '申请没通过：' : 'Refused: '}{refusalWords(admissionRefusal.error)[locale.startsWith('zh') ? 0 : 1]}
                      </span>
                    )}
                    {wf.release_approval?.status === 'pending' && (
                      <span className="workflows-release-approval-pending"> · {t('workflows.releaseApprovalPending')}</span>
                    )}
                    {wf.release_ready && wf.release_approval?.status === 'approved' && onPublish && (
                      <button
                        type="button"
                        className="workflows-publish"
                        disabled={!connected || !wf.draft_revision?.trim() || publishingWorkflowId === wf.id}
                        onClick={() => onPublish(wf.id, wf.draft_revision ?? '')}
                      >
                        {publishingWorkflowId === wf.id ? t('request.busy') : t('workflows.publish')}
                      </button>
                    )}
                    {!onRequestRelease && wf.release_ready && onPublish && (
                      <button
                        type="button"
                        className="workflows-publish"
                        disabled={!connected || !wf.draft_revision?.trim() || publishingWorkflowId === wf.id}
                        onClick={() => onPublish(wf.id, wf.draft_revision ?? '')}
                      >
                        {publishingWorkflowId === wf.id ? t('request.busy') : t('workflows.publish')}
                      </button>
                    )}
                  </div>
                )}
                {wf.status === 'testing' && wf.release_approval?.status === 'pending' && onDecideRelease && (
                  <div className="workflows-release-approval-actions">
                    <button type="button" className="workflows-invoke" disabled={!connected || !wf.draft_revision?.trim() || decidingReleaseWorkflowId === wf.id} onClick={() => onDecideRelease(wf.id, wf.draft_revision ?? '', 'approved')}>{t('workflows.approveRelease')}</button>
                    <button type="button" className="workflows-cancel" disabled={!connected || !wf.draft_revision?.trim() || decidingReleaseWorkflowId === wf.id} onClick={() => onDecideRelease(wf.id, wf.draft_revision ?? '', 'rejected')}>{t('workflows.rejectRelease')}</button>
                  </div>
                )}
                {onReleaseEvidence && (
                  <div className="workflows-runtime-evidence">
                    <button
                      type="button"
                      className="workflows-cancel"
                      data-testid="workflows-runtime-evidence"
                      onClick={() => onReleaseEvidence(wf.id)}
                    >
                      {t('workflows.runtimeEvidence')}
                    </button>
                    {releaseEvidence && releaseEvidence.workflow_id === wf.id && (
                      <div className="workflows-runtime-evidence-body" role="status">
                        <div>
                          <strong>{t('workflows.runtimeEvidence.published')}</strong>
                          <span className="capability-pill capability-pill--ok">{t('workflows.runtimeEvidence.sealed')}</span>
                          <span>{releaseEvidence.published_evidence.test_records} {t('workflows.evidenceTests')} · {releaseEvidence.published_evidence.approval_records} {t('workflows.runtimeEvidence.approvals')}</span>
                        </div>
                        <div>
                          <strong>{t('workflows.runtimeEvidence.after')}</strong>
                          <span className="capability-pill capability-pill--muted">{t('workflows.runtimeEvidence.unsealed')}</span>
                          <span>
                            {t('workflows.runtimeEvidence.canary', { n: releaseEvidence.runtime_evidence.rollout.observations })}
                            {releaseEvidence.runtime_evidence.rollout.earlier_candidates.length > 0 && (
                              <span title={releaseEvidence.runtime_evidence.rollout.earlier_candidates.map(c => `${c.key}: ${c.observations}`).join(' · ')}>
                                {' '}· {t('workflows.runtimeEvidence.earlier', { n: releaseEvidence.runtime_evidence.rollout.earlier_candidates.length })}
                              </span>
                            )}
                          </span>
                          <span>
                            {releaseEvidence.runtime_evidence.acceptance.available
                              ? Object.entries(releaseEvidence.runtime_evidence.acceptance.counts).map(([status, count]) => `${status} ${count}`).join(' · ') || t('workflows.runtimeEvidence.noOrders')
                              : t('workflows.runtimeEvidence.unavailable')}
                          </span>
                        </div>
                      </div>
                    )}
                  </div>
                )}
                {wf.release_evidence && (
                  <div className="workflows-release-evidence" title={wf.release_evidence.manifest_sha256}>
                    {t('workflows.evidence')} · {wf.release_evidence.test_records.length} {t('workflows.evidenceTests')} · {wf.release_evidence.manifest_sha256 ? `${wf.release_evidence.manifest_sha256.slice(0, 12)}…` : '—'}
                    {wf.release_evidence.candidate_fingerprint && (
                      <span title={wf.release_evidence.candidate_fingerprint}> · {t('workflows.candidateFingerprint')} {wf.release_evidence.candidate_fingerprint.slice(0, 12)}…</span>
                    )}
                    {wf.release_evidence.evidence_file && (
                      <span title={wf.release_evidence.evidence_file}> · {t('workflows.evidenceFile')}</span>
                    )}
                    {wf.publication?.evidence_manifest_sha256 && (
                      <span> · {t('workflows.publishedEvidence')} {wf.publication.evidence_manifest_sha256.slice(0, 12)}…</span>
                    )}
                    {wf.publication?.rollback_snapshot && (
                      <span title={wf.publication.rollback_snapshot}> · {t('workflows.rollbackPoint')}</span>
                    )}
                  </div>
                )}
                {wf.status === 'published' && improvementSlot && (
                  <div className="workflows-release-readiness" data-testid="workflow-improvement">{improvementSlot(wf)}</div>
                )}
                {wf.status === 'published' && wf.publication?.rollback_snapshot && onRollback && (
                  <div className="workflows-release-readiness">
                    <span className={wf.rollback_preview?.available === false ? 'workflows-fact-warn' : ''}>
                      {wf.rollback_preview?.available === false
                        ? `${t('workflows.rollbackUnavailable')} · ${wf.rollback_preview.status}`
                        : t('workflows.rollbackAvailable')}
                    </span>
                    <button
                      type="button"
                      className="workflows-cancel"
                      disabled={!connected || !wf.draft_revision?.trim() || rollingBackWorkflowId === wf.id}
                      onClick={() => onRollback(wf.id, wf.draft_revision ?? '')}
                    >
                      {rollingBackWorkflowId === wf.id ? t('workflows.rollingBack') : t('workflows.rollback')}
                    </button>
                  </div>
                )}
                {wf.status === 'published' && onRolloutStart && (
                  <div className="workflows-release-readiness">
                    <span>
                      灰度：{wf.rollout?.status ?? '未启动'}
                      {wf.rollout_evaluation?.observed_count !== undefined && ` · ${wf.rollout_evaluation.observed_count} 次观察`}
                      {wf.rollout_evaluation?.blockers?.[0] && ` · ${wf.rollout_evaluation.blockers[0]}`}
                    </span>
                    {(!wf.rollout?.status || wf.rollout.status === 'rolled_back') && (
                      <button type="button" className="workflows-invoke" disabled={!connected || !wf.draft_revision?.trim() || rolloutBusyWorkflowId === wf.id} onClick={() => onRolloutStart(wf.id, wf.draft_revision ?? '')}>
                        {rolloutBusyWorkflowId === wf.id ? '启动中…' : '启动灰度'}
                      </button>
                    )}
                    {wf.rollout?.status === 'canary' && onRolloutEvaluate && (
                      <>
                        <button type="button" className="workflows-invoke" disabled={!connected} onClick={() => onRolloutEvaluate(wf.id)}>评估</button>
                        {onRolloutObserve && <>
                          <button type="button" className="workflows-invoke" disabled={!connected || !wf.draft_revision?.trim() || rolloutBusyWorkflowId === wf.id} onClick={() => onRolloutObserve(wf.id, wf.draft_revision ?? '', 'passed')}>记为通过</button>
                          <button type="button" className="workflows-cancel" disabled={!connected || !wf.draft_revision?.trim() || rolloutBusyWorkflowId === wf.id} onClick={() => onRolloutObserve(wf.id, wf.draft_revision ?? '', 'failed')}>记为失败</button>
                        </>}
                        {wf.rollout_evaluation?.rollout_status === 'healthy' && onRolloutFinalize && (
                          <button type="button" className="workflows-publish" disabled={!connected || !wf.draft_revision?.trim()} onClick={() => onRolloutFinalize(wf.id, wf.draft_revision ?? '')}>确认完成灰度</button>
                        )}
                      </>
                    )}
                    {wf.rollout_evaluation?.rollout_status === 'unhealthy' && onRolloutRollback && (
                      <button type="button" className="workflows-cancel" disabled={!connected || !wf.draft_revision?.trim() || rolloutRollbackBusyWorkflowId === wf.id} onClick={() => onRolloutRollback(wf.id, wf.draft_revision ?? '')}>
                        {rolloutRollbackBusyWorkflowId === wf.id ? '回滚中…' : '按门槛回滚'}
                      </button>
                    )}
                  </div>
                )}

                {onPreflight && (
                  <div className="workflows-preflight-actions">
                    <button
                      type="button"
                      className="workflows-cancel"
                      disabled={!connected || preflightingWorkflowId === wf.id}
                      onClick={() => onPreflight(wf.id, drafts[wf.id] ?? {})}
                    >
                      {preflightingWorkflowId === wf.id ? t('workflows.preflightRunning') : t('workflows.preflight')}
                    </button>
                    {onSimulateTest && (wf.status === 'draft' || wf.status === 'testing') && (
                      <button
                        type="button"
                        className="workflows-cancel"
                        disabled={!connected || !wf.draft_revision?.trim() || simulatingTestWorkflowId === wf.id}
                        onClick={() => onSimulateTest(wf.id, wf.draft_revision ?? '', drafts[wf.id] ?? {})}
                        title={t('workflows.simulateTestHint')}
                      >
                        {simulatingTestWorkflowId === wf.id ? t('workflows.simulatingTest') : t('workflows.simulateTest')}
                      </button>
                    )}
                    {preflight?.workflow_id === wf.id && (
                      <span className={`workflows-preflight-status${preflight.ready ? ' is-ready' : ' is-blocked'}`}>
                        {preflight.ready ? t('workflows.preflightReady') : t('workflows.preflightBlocked')}
                      </span>
                    )}
                  </div>
                )}
                {preflight?.workflow_id === wf.id && (
                  <div className="workflows-preflight-panel" role="status">
                    <div><strong>{t('workflows.preflightTitle')}</strong> · {t('workflows.simulatedOnly')}</div>
                    {preflight.blockers.length > 0 && (
                      <ul className="workflows-problems">{preflight.blockers.map(item => <li key={item}>{item}</li>)}</ul>
                    )}
                    {preflight.warnings && preflight.warnings.length > 0 && (
                      <ul className="workflows-preflight-warnings">{preflight.warnings.map(item => <li key={item}>{item}</li>)}</ul>
                    )}
                    {preflight.failure_explanation && (
                      <div className="workflows-preflight-explanation">
                        <strong>{t('workflows.failureExplanation')}</strong>
                        <span>{preflight.failure_explanation.root_cause}</span>
                        {preflight.failure_explanation.actions.map(action => <span key={action}> · {action}</span>)}
                      </div>
                    )}
                    <div className="workflows-preflight-meta">{preflight.steps.length} {t('workflows.preflightSteps')} · {preflight.simulated_effects.length} {t('workflows.preflightEffects')}</div>
                  </div>
                )}
                {onCreateDraft && (
                  <div className="workflows-builder-actions">
                    {builderDraftOpenId !== wf.id ? (
                      <button
                        type="button"
                        className="workflows-cancel"
                        disabled={!connected || creatingDraftWorkflowId === wf.id}
                        onClick={() => {
                          setBuilderDraftOpenId(wf.id)
                          setBuilderDraftIds(prev => ({ ...prev, [wf.id]: wf.id + '-draft' }))
                          setBuilderDraftNames(prev => ({ ...prev, [wf.id]: wf.name + ' · 草稿' }))
                        }}
                      >
                        {creatingDraftWorkflowId === wf.id ? t('workflows.creatingDraft') : t('workflows.createDraft')}
                      </button>
                    ) : (
                      <div className="workflows-builder-form" role="form" aria-label={t('workflows.createDraft')}>
                        <strong>{t('workflows.builderDraftTitle')}</strong>
                        <input
                          aria-label={t('workflows.draftId')}
                          value={builderDraftIds[wf.id] ?? ''}
                          placeholder={t('workflows.draftId')}
                          onChange={event => setBuilderDraftIds(prev => ({ ...prev, [wf.id]: event.target.value }))}
                        />
                        <input
                          aria-label={t('workflows.draftName')}
                          value={builderDraftNames[wf.id] ?? ''}
                          placeholder={t('workflows.draftName')}
                          onChange={event => setBuilderDraftNames(prev => ({ ...prev, [wf.id]: event.target.value }))}
                        />
                        <input
                          aria-label={t('workflows.draftSummary')}
                          value={builderDraftSummaries[wf.id] ?? ''}
                          placeholder={t('workflows.draftSummary')}
                          onChange={event => setBuilderDraftSummaries(prev => ({ ...prev, [wf.id]: event.target.value }))}
                        />
                        <input
                          aria-label={t('workflows.nodeChangeNote')}
                          value={builderDraftNotes[wf.id] ?? ''}
                          placeholder={t('workflows.nodeChangeNote')}
                          onChange={event => setBuilderDraftNotes(prev => ({ ...prev, [wf.id]: event.target.value }))}
                        />
                        <div className="workflows-form-actions">
                          <button type="button" className="workflows-cancel" disabled={creatingDraftWorkflowId === wf.id} onClick={() => setBuilderDraftOpenId(null)}>{t('workflows.cancel')}</button>
                          <button
                            type="button"
                            className="workflows-invoke"
                            disabled={!connected || creatingDraftWorkflowId === wf.id || !(builderDraftIds[wf.id] ?? '').trim()}
                            onClick={() => onCreateDraft(
                              wf.id,
                              (builderDraftIds[wf.id] ?? '').trim(),
                              (builderDraftNames[wf.id] ?? '').trim(),
                              (builderDraftSummaries[wf.id] ?? '').trim(),
                              (builderDraftNotes[wf.id] ?? '').trim(),
                            )}
                          >
                            {creatingDraftWorkflowId === wf.id ? t('workflows.creatingDraft') : t('workflows.createDraft')}
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                )}
                {onRecordTest && (wf.status === 'draft' || wf.status === 'testing') && (
                  <div className="workflows-test-record-actions">
                    {testRecordOpen?.workflowId !== wf.id ? (
                      <button
                        type="button"
                        className="workflows-cancel"
                        disabled={!connected || recordingTestWorkflowId === wf.id}
                        onClick={() => setTestRecordOpen({ workflowId: wf.id, expectedRevision: wf.draft_revision ?? '' })}
                      >
                        {recordingTestWorkflowId === wf.id ? t('workflows.recordingTest') : t('workflows.recordTest')}
                      </button>
                    ) : (
                      <div className="workflows-test-record-form" role="form" aria-label={t('workflows.recordTest')}>
                        <strong>{t('workflows.testRecordTitle')}</strong>
                        {!workflowRevisionMatches(testRecordOpen.expectedRevision, wf.draft_revision) && (
                          <p className="workflows-error" role="status">{locale.startsWith('zh') ? '工作流版本已变化或缺失，请刷新并重新打开记录表单。' : 'The workflow revision changed or is missing. Refresh it and reopen this form.'}</p>
                        )}
                        <select
                          aria-label={t('workflows.testOutcome')}
                          value={testRecordOutcomes[wf.id] ?? 'passed'}
                          onChange={event => setTestRecordOutcomes(prev => ({ ...prev, [wf.id]: event.target.value as 'passed' | 'failed' | 'stopped' }))}
                        >
                          <option value="passed">{t('workflows.testPassed')}</option>
                          <option value="failed">{t('workflows.testFailed')}</option>
                          <option value="stopped">{t('workflows.testStopped')}</option>
                        </select>
                        <textarea
                          rows={2}
                          aria-label={t('workflows.testNotes')}
                          value={testRecordNotes[wf.id] ?? ''}
                          placeholder={t('workflows.testNotes')}
                          onChange={event => setTestRecordNotes(prev => ({ ...prev, [wf.id]: event.target.value }))}
                        />
                        {(testRecordOutcomes[wf.id] ?? 'passed') === 'failed' && (
                          <textarea
                            rows={2}
                            aria-label={t('workflows.failureReason')}
                            value={testRecordFailures[wf.id] ?? ''}
                            placeholder={t('workflows.failureReason')}
                            onChange={event => setTestRecordFailures(prev => ({ ...prev, [wf.id]: event.target.value }))}
                          />
                        )}
                        <div className="workflows-form-actions">
                          <button type="button" className="workflows-cancel" disabled={recordingTestWorkflowId === wf.id} onClick={() => setTestRecordOpen(null)}>{t('workflows.cancel')}</button>
                          <button
                            type="button"
                            className="workflows-invoke"
                            disabled={
                              !connected ||
                              !workflowRevisionMatches(testRecordOpen.expectedRevision, wf.draft_revision) ||
                              recordingTestWorkflowId === wf.id ||
                              ((testRecordOutcomes[wf.id] ?? 'passed') === 'failed' && !(testRecordFailures[wf.id] ?? '').trim() && !(testRecordNotes[wf.id] ?? '').trim())
                            }
                            onClick={() => { if (workflowRevisionMatches(testRecordOpen.expectedRevision, wf.draft_revision)) onRecordTest(
                              wf.id,
                              testRecordOpen.expectedRevision,
                              testRecordOutcomes[wf.id] ?? 'passed',
                              (testRecordNotes[wf.id] ?? '').trim(),
                              (testRecordFailures[wf.id] ?? '').trim(),
                            ) }}
                          >
                            {recordingTestWorkflowId === wf.id ? t('workflows.recordingTest') : t('workflows.saveTestRecord')}
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                )}
                {onSubmitTesting && wf.status === 'draft' && wf.latest_test?.outcome === 'passed' && (
                  <div className="workflows-builder-actions">
                    <button
                      type="button"
                      className="workflows-invoke"
                      disabled={!connected || !wf.draft_revision?.trim() || submittingTestingWorkflowId === wf.id || wf.latest_test.execution_mode === 'simulated'}
                      title={wf.latest_test.execution_mode === 'simulated' ? t('workflows.simulatedEvidenceBlocksPromotion') : ''}
                      onClick={() => onSubmitTesting(wf.id, wf.draft_revision ?? '', `submitted from Builder on ${new Date().toISOString().slice(0, 10)}`)}
                    >
                      {submittingTestingWorkflowId === wf.id ? t('workflows.submittingTesting') : t('workflows.submitTesting')}
                    </button>
                    {wf.latest_test.execution_mode === 'simulated' && <span className="workflows-note">{t('workflows.simulatedEvidenceBlocksPromotion')}</span>}
                    {admissionRefusal?.workflowId === wf.id && admissionRefusal.action === 'submit_testing' && (
                      <span className="workflows-error" role="alert" data-testid="testing-submission-refused">
                        {locale.startsWith('zh') ? '提交没通过：' : 'Refused: '}{refusalWords(admissionRefusal.error)[locale.startsWith('zh') ? 0 : 1]}
                      </span>
                    )}
                  </div>
                )}

                {!open ? (
                  <button
                    className="workflows-invoke"
                    disabled={!connected || !wf.invokable}
                    onClick={() => setOpenId(wf.id)}
                    title={wf.invokable ? '' : t('workflows.notInvokable')}
                  >
                    {t('workflows.placeOrder')}
                  </button>
                ) : launchForm(wf, () => setOpenId(null))}
              </section>
            )
          })}
        </div>
      )}
        </div>
        return surface === 'builder' ? (packageHost ? createPortal(catalog, packageHost) : null) : catalog
      })()}
    </div>
  )
}
