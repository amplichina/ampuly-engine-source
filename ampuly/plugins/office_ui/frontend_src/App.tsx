import { loadQuietCompletion, saveQuietCompletion, shouldNotifyBackgroundCompletion } from './lib/quietPreferences'
import { quoteForConversation, type ArtifactQuoteDraft } from './lib/artifactReference'
import { useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'react'
import { classifyPageError } from './lib/pageError'
import { withSavedWorkflow } from './lib/savedWorkflow'
import { authTokenFailurePolicy } from './authSessionPolicy'
import { acceptsInterruptReceipt } from './lib/projectNavigation'
import { CurrentProject, REISSUE_ON_SETTLE, projectIdOf, viewGenerationOf, type ReissueRequest } from './lib/currentProject'
import { VisualSocketClient } from './lib/wsClient'
import { EMPLOYEE_WORKFLOW_TRIAL_ACTIONS, type EmployeeWorkflowTrialAck } from './lib/employeeWorkflowTrial'
import { canReconcileBuilderTrial, matchesBuilderTrialReceipt, newRequestId, type BuilderTrialRequest } from './lib/builderTrialState'
import { flowTrialTargetKey, matchingFlowTrial, readPublishedFlowCandidates, recordedFlowTrialTarget, selectableFlowTrialTarget } from './lib/flowTrialSelection'
import type { FlowTrialSummary, PublishedFlowTrialCandidate } from './components/BuilderActionDialogs'
import { isOwnerConsole } from './lib/ownerConsole'
import { acceptsCustomerPreview, parseCustomerPreviewView, type CustomerPreviewRequest } from './lib/customerPreviewState'
import { CustomerPreviewPanel, type CustomerPreviewView } from './components/CustomerPreviewPanel'
import type { AgentInfo, OrgCreateMemberInput, OrgSavedCreatePayload, OrgInfoPayload, ReorgProposalInfo, SavedOrgSummary, SocketStatus, VisualSnapshot } from './types/visual'
import { useBoardStore, type BoardStoreState } from './kanban/BoardStore'
import { WorkspacePage, type WorkspaceView } from './workspace/WorkspacePage'
import { useNodeDraftState } from './workspace/useNodeDraftState'
import { useChatStore, type ChatStoreState } from './chat/ChatStore'
import { useSessionStore, type SessionStoreState } from './stores/SessionStore'
import { useProjectStore, type ProjectStoreState } from './stores/ProjectStore'
import { ExecutionPanel } from './kanban/ExecutionPanel'
import { JiuwenTeamActivityPanel } from './kanban/JiuwenTeamActivityPanel'
import { ProjectSelector, type RestorePreflightReport } from './components/ProjectSelector'
import { normalizeLifecycleTrail, type ProjectLifecycleTrail, type RestoreBackupState } from './components/ProjectLifecyclePanel'
import { mergeOrderList } from './lib/acceptanceMerge'
import type { GovernancePolicyView } from './components/GovernancePolicyPanel'
import type { BrowserCandidateRow } from './components/BrowserSessionsPanel'
import { browserControlContext, browserPushScope, browserFrameMatchesSession, type BrowserControlContext } from './workspace/browserAccountState'
import { browserAccountInspectRequest, browserInspectionSessionSignature, browserInspectionMatchesSession, browserInspectionReplyMatches, parseBrowserAccountInspection, type BrowserAccountInspectionView } from './workspace/browserAccountInspection'
import { OrgTab } from './org/OrgTab'
import { DashboardPage, EMPTY_READ, type PanoramaReadState, type WorkflowAdoption } from './components/DashboardPage'
import { PeopleContacts, type ContactBindState } from './components/PeopleContacts'
import { bindRequest, contactsFrom, type BookContact } from './lib/contactBook'
import { LoginScreen, type AuthAccount, type AuthState } from './components/LoginScreen'
import { applyProjectCostObservation, type ProjectCostObservation } from './lib/projectCostObservation'
import { EmployeeWorkbench, type Materials, type MyTask, type ReturnedWork } from './components/EmployeeWorkbench'
import { CustomerPage } from './components/CustomerPage'
import { isCustomerAccount, parseCustomerOrders, type CustomerOrder } from './lib/customerPage'
import { AccountsSection } from './components/AccountsSection'
import type { TrialArtifactState } from './components/NodeTrialSection'
import type { NodeTrialPinning } from './lib/nodeTrialPins'
import type { NodeBlocker, RefusedSave } from './workspace/workflowHealth'
import { useDraftPersistence } from './workspace/useDraftPersistence'
import { useEditLock } from './workspace/useEditLock'
import { dropDraft } from './workspace/useNodeDraftState'
import { nextPatchVersion } from './workspace/workflowGraph'
import { WorkflowsPage, nextFlowId, type BuilderFocus, type StepReturn, type WorkflowNode, type OrderTimeline, type SolutionBlueprint, type WorkflowCompositionRequest, type WorkflowInvokeState, type WorkflowNodeTrial, type WorkflowPreflight, type WorkflowSummary, type WorkOrderSummary } from './components/WorkflowsPage'
import { OrdersInboxPage } from './components/OrdersInboxPage'
import { AnswerPath, receiptOf, useAnswering } from './lib/answerPath'
import { RequestTracker, anyBusy, busyIdOf, keyOf, latestOf, stateOf, stillOut, useTracked, type SendDisposition } from './lib/requestTracker'
import { listWaitedOut } from './lib/approvalsLoad'
import type { GateDecision } from './components/GatePanel'
import { MasterControlPage, type MasterSendExtras, type MasterToolActivity } from './components/MasterControlPage'
import { MasterThreadColumn } from './components/MasterThreadColumn'
import {
  arrivalOpen, conversationOpening, groupThreads, isProjectMasterChannel, latestThread, loadColumnCollapsed, masterChannelId, mergeThreadList,
  newConversation, openPlan, openThread, resolveOpen, saveColumnCollapsed, sendPlan, threadFrom, touchThread, turnBelongsTo, upsertThread, viewerStorage,
  type MasterOpen, type MasterThread, type ThreadsByProject,
} from './lib/masterThreads'
import {
  IDLE_BUILD_CONTROL, beginBuildInterrupt, beginBuildSteer, buildActivityFrom, buildAnswerMark, controlFor, dropSettledActivity,
  keepBuildActivity, settledBuildJobs, steerReceiptWords, type BuildActivities, type BuildAnswerMarks, type BuildControl,
} from './lib/masterBuild'
import { attachmentCancelKey, cancelRefusedNote, sourcePreviewFromAck, type SourcePreviewState } from './lib/attachmentsRead'
import { ExecutionWindow, ExecutionInspector } from './components/ExecutionWindow'
import type { BrowserSessionRow } from './components/BrowserSessionsPanel'
import { DesktopSessionControls, type DesktopSessionStatus } from './components/DesktopSessionControls'
import { ExecutionTerminalView } from './components/ExecutionTerminalView'
import { ExecutionBrowserWorkspace } from './components/ExecutionBrowserWorkspace'
import { ExecutionSubSessionsPanel } from './components/ExecutionSubSessionsPanel'
import { ExecutionObservatoryPanel } from './components/ExecutionObservatoryPanel'
import { getWorkItemChildSessions } from './lib/workItemSessions'
import { CapabilityCenterPage, type CapabilityGroup } from './components/CapabilityCenterPage'
import { ComponentLibraryPage, type DistributionInboxRecord, type ImprovementProposal, type SkillImprovement } from './components/ComponentLibraryPage'
import { AIEmployeeAssetHost } from './components/aiEmployees/AIEmployeeAssetHost'
import { AIEmployeeReworkInspectionHost } from './components/aiEmployees/AIEmployeeReworkInspectionHost'
import { reworkInspectionBinding } from './lib/aiEmployeeReworkInspectionScope'
import { EmployeeAssetController } from './lib/aiEmployeeAssetController'
import { EMPLOYEE_ASSET_ACTIONS } from './lib/aiEmployeeAssetAdapter'
import { employeeAssetProjectId } from './lib/aiEmployeeAssetScope'
import type { ComponentCatalog } from './lib/componentCatalog'
import { componentRefusal, type ComponentCandidate, type ComponentDetail, type ComponentVersions } from './lib/componentDetail'
import type { WorkbenchTab } from './components/ComponentWorkbench'
import { ComponentDetailPanel, type ComponentDetailView } from './components/ComponentDetailPanel'
import { ImprovementStarter } from './components/ImprovementStarter'
import type { ImprovementProposalActionHandlers } from './components/ImprovementProposalActions'
import { editorOfProposal, flowImprovementTargets, type ImprovementEditor, type ImprovementStart, type ImprovementTarget } from './lib/improvementProposal'
import type { NodeProposal, ProposalCandidate } from './lib/nodeProposal'
import { MARKET_TOP_LIMIT, hasListingMismatch, marketRefusalWords, trialForListing, trialKey, trialsNotAdmitted, type ListingMismatch, type MarketCategory, type MarketRefusal, type TrialSkill } from './lib/skillMarket'
import { TrialShelf, type MarketBrowseState, type TrialBusy } from './components/SkillMarketPanel'
import { SettingsModal, type GateStatus, type SettingsData, type SettingsTestResult, type SettingsNotifyResult } from './components/SettingsModal'
import { type ModelPlan } from './workspace/modelTiers'
import type { GateDetail } from './components/GatePanel'
import type { OrderMetrics as OrderMetricsRow, OrderUsage, ReleaseEvidence, RunBudget } from './components/WorkflowsPage'
import type { CapabilityCenterPageProps } from './components/CapabilityCenterPage'
type ChannelCheck = NonNullable<CapabilityCenterPageProps['channelCheck']>
type CredentialReference = NonNullable<CapabilityCenterPageProps['credentialReferences']>[number]
type Deployment = NonNullable<CapabilityCenterPageProps['deployments']>[number]
type Rollout = NonNullable<CapabilityCenterPageProps['rollouts']>[number]
type ConnectorSwitch = NonNullable<CapabilityCenterPageProps['connectorSwitches']>[number]
type ConnectorCandidate = NonNullable<CapabilityCenterPageProps['connectorCandidates']>[number]
type ConnectorRegistration = NonNullable<CapabilityCenterPageProps['connectorRegistrations']>[number]
type BrowserSession = NonNullable<CapabilityCenterPageProps['browserSessions']>[number]
type PendingTurn = NonNullable<CapabilityCenterPageProps['pendingTurns']>[number]
type TurnLedger = NonNullable<CapabilityCenterPageProps['turnLedger']>
import { ArtifactsPanel, type ArtifactIndex, type ArtifactPreview } from './components/ArtifactsPanel'
import { DataBrowser } from './components/DataBrowser'
import { ProjectFolderPanel } from './components/ProjectFolderPanel'
import { RecyclePanel } from './components/RecyclePanel'
import { CustomerMemoryPanel } from './components/CustomerMemoryPanel'
import { customerMemoryReducer, EMPTY_CUSTOMER_MEMORY } from './lib/customerMemory'
import { dataReducer, errorLabel, usageReducer, EMPTY_DATA, type BackupList, type CleanupPreview, type DataAck, type DataLocations, type NotRestored, type RemovalPlan, type UsageEntry } from './lib/dataBrowser'
import { folderReducer, recycleReducer, EMPTY_FOLDER_STATE, EMPTY_RECYCLE_STATE, type FolderAck } from './lib/folderAdmin'
import type { DashboardBlocker, DutyLogEntry, DutyLogState } from './components/DashboardPage'
import { mapCollabSyncPayload, mapBackendMessage, mapBackendChannel, mapBackendSession, mergeSessionDetailHasMore } from './lib/collabSync'
import { SessionDetailRefreshGate, SessionMessageLedger, storeAlreadyShows } from './lib/sessionDetailRefresh'
import { CollabSyncCoalescer } from './lib/collabSyncCoalescer'
import { normalizeOrgInfoPayload } from './lib/runtimeOrg'
import { companyRuntimeControlPatchForBoardStatus } from './lib/sessionRuntime'
import { getExecutionTurnId } from './lib/workItemRuntimeIds'
import { normalizeSessionCompanyProfile, normalizeSessionExecMode } from './lib/sessionIdentity'
import { extractSessionRecruitmentByRole, sessionChannelId } from './lib/sessionRecruitment'
import { resolveCanonicalTurnId, terminalAssistantTurnId } from './lib/turnIdentity'
import { compileProjectIdPolicy, type ProjectIdPolicy } from './lib/projectIdPolicy'
import { loadStoredTheme, saveStoredTheme, type ThemeName } from './lib/theme'
import type { AgentAnimStatus, EmployeeAssignment, KanbanPhase, KanbanTask, NativeApprovalLevel, RoleAggregatedStatus, RoleWorkItemSummary, Session, TaskPreferredAgent } from './types/kanban'
import { refusalInWords, translateFor, useI18n } from './i18n'
import { EventTimelineStore } from './devtools/EventTimelineStore'
import { ExecutionTraceStore } from './observatory/ExecutionTraceStore'
import { ObservatoryHistoryReader } from './observatory/historyReader'
import { ExecutionObservatoryHost } from './observatory/ExecutionObservatoryHost'
import { DevToolsOverlay } from './devtools/DevToolsOverlay'
import { useExternalTeamActivityStore, type ExternalTeamActivityRecord } from './stores/ExternalTeamActivityStore'
import { isJiuwenOpaqueTeam } from './lib/externalTeamActivity'
import { loadLastActivePage, saveLastActivePage, type RestorableAppPage } from './lib/appPageState'
import { PAGE_LABEL_KEYS } from './lib/shellNav'
import { isV2SurfaceEnabled } from './lib/v2SurfaceFlags'
import { pendingBadgeServerObservation, stalePendingBadge, type PendingBadgeView } from './lib/pendingApprovalBadgeLifecycle'
import { pageFromSearch, retiredPageLanding, searchForPage, searchForSettledProject, searchForThread, threadFromSearch, type RetiredLanding } from './lib/shellRoute'
import { askedKey, quietListsFor, restoredPageAsk } from './lib/restoredPageLists'
import { sameCrumb, type BuilderCrumb } from './lib/builderCrumb'
import type { FlowVersions } from './lib/flowVersions'
import { NO_ORDER_PAGING, ORDER_QUEUES, foldOrderPage, openOrderCount, type OrderPaging, type OrderQueue } from './lib/orderQueues'
import { DistributionAuthorizationCard, distributionReplyFrom, type DistributionAuthorizationReply } from './components/DistributionAuthorizationCard'
import { refusalWords } from './lib/flowAdmission'
import { ordersTarget } from './lib/orderSpread'
import { loadStartPage, resolveStartPage } from './lib/startPage'
import { attentionItems } from './lib/attention'
import { overviewFrom, type ProjectOverview } from './lib/projectOverview'
import { connectionsFrom, migrationWhy, needsMigration, profileFrom, type ConnectionLane, type ProjectProfile } from './lib/projectPanorama'
import { browserForOrder, executionEntry } from './lib/executionEntry'
import { recentWork } from './lib/recentWork'
import { TODAY_FETCH_LIMIT, todayCountsFrom, todayPartial, todayUnsupported, type TodayGroup } from './lib/masterToday'
import { ShellRail } from './components/shell/ShellRail'
import { ShellTopbar } from './components/shell/ShellTopbar'
import { PreferencesPage } from './components/shell/PreferencesPage'
import { ConfigPage, type ConfigSection } from './components/shell/ConfigPage'
import { WecomChannelCard } from './components/WecomChannelCard'
import { CustomerServiceCard } from './components/CustomerServiceCard'
import { CRMAckBus, CRMProjectRecords } from './components/crm/CRMEntry'
import { EmployeeHandoffHost } from './components/EmployeeHandoffCard'
import { EMPTY_WECOM_CHANNEL, flowChoices, wecomChannelAck, wecomSent, wecomStateFor, type WecomChannelState } from './lib/wecomChannel'
import { EMPTY_CUSTOMER_SERVICE, customerServiceAck, customerServiceSent, type CustomerServiceState } from './lib/customerService'
import { EMPTY_EMPLOYEE_HANDOFFS, employeeHandoffsAck, employeeHandoffsSent, type EmployeeHandoffState } from './lib/employeeHandoff'
import { OPEN_DESKTOP_CHANNEL_EVENT, setDesktopChannelOpener } from './components/DesktopConnectorFieldset'

const MAX_LOG_ITEMS = 80
const TASK_MODE_LOW_VALUE_RUNTIME_EVENTS = new Set([
  'message_start',
  'message_stop',
  'tool_call_delta',
  'status_snapshot',
  'context_usage',
  'cost_update',
  'task_ledger_updated',
  'prompt_prefix_state',
  'prompt_prefix_cache_fingerprint',
  'prefetch_started',
  'prefetch_completed',
  'prefetch_consumed',
  'durable_memory_extracted',
  'durable_memory_extraction_failed',
  'session_memory_updated',
  'session_memory_update_failed',
  'tool_batch_started',
  'tool_batch_completed',
  'permission_predicted',
  'turn_started',
  'turn_completed',
])
const SESSION_DETAIL_REFRESH_LOW_VALUE_RUNTIME_EVENTS = new Set([
  'member_inbox_updated',
])

type AppPage = RestorableAppPage

/** Surface tabs are real execution windows; legacy record links open a separate inspector. */
type ExecutionTab = 'record' | 'steps' | 'graph' | 'browser' | 'desktop' | 'terminal' | 'files' | 'subsessions' | 'observatory'

type ConsoleDeepLink = {
  /** What the address asked for: nothing, one card, or one of the four retired page keys (S3-7). */
  kind: 'none' | 'checkpoint' | 'retired'
  page: AppPage
  projectId: string
  checkpointId: string
  /** Where a retired key lands (lib/shellRoute): 业务工单 on 等你处理, 总控 with 执行区 open over a run when one is
   *  named, or 配置 on a section. */
  landing?: RetiredLanding
}

function readConsoleDeepLink(): ConsoleDeepLink {
  const fallback: ConsoleDeepLink = { kind: 'none', page: 'master', projectId: '', checkpointId: '' }
  try {
    const query = new URLSearchParams(window.location.search || '')
    const checkpointId = String(query.get('checkpoint_id') || query.get('checkpoint') || '').trim()
    const projectId = String(query.get('project_id') || '').trim()
    // A card named in the address (Feishu's audit button: ?page=approvals&checkpoint_id=…&project_id=…) lands in
    // 业务工单 (policy: 总控 lists no cards any more): the order it lives on is opened there once
    // checkpoint_locate says which; a card no order can be found for leaves 业务工单 on 等你处理.
    if (checkpointId) {
      return { kind: 'checkpoint', page: 'orders', projectId, checkpointId }
    }
    // A retired page key (S3-7): ?page=approvals → 业务工单 on 等你处理 (policy; it was 总控's rows until
    // then); ?page=workspace → 总控, with 执行区 open over the run the address names; ?page=capabilities / ?page=org →
    // 配置 on their sections. An old address never 404s and never shows an empty page.
    const landing = retiredPageLanding(window.location.search || '')
    if (landing) {
      return { kind: 'retired', page: landing.page, projectId, checkpointId: '', landing }
    }
  } catch {
    // A malformed URL must never prevent the console shell from loading.
  }
  return fallback
}

// This browser holds the token; the server validates its current-workspace durable session.
const AUTH_TOKEN_KEY = 'ampuly_auth_token'
const LEGACY_AUTH_TOKEN_KEY = 'opc_auth_token'
function readAuthToken(): string {
  try { return localStorage.getItem(AUTH_TOKEN_KEY) ?? localStorage.getItem(LEGACY_AUTH_TOKEN_KEY) ?? '' } catch { return '' }
}
// The browser sends this cookie with <img src> and download links, which
// cannot carry an Authorization header; the server only accepts it for a
// live login token.
function syncSessionCookie(token: string): void {
  void fetch('/api/auth/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
    credentials: 'same-origin',
  }).catch(() => { /* attachments fall back to 401 */ })
}

function writeAuthToken(token: string): void {
  try {
    if (token) localStorage.setItem(AUTH_TOKEN_KEY, token)
    else localStorage.removeItem(AUTH_TOKEN_KEY)
  } catch { /* storage unavailable */ }
  syncSessionCookie(token)
}

// The project this browser was last in. After a console restart the server opens on its own project (default), and the
// owner had to find the way back every time (B5). A project_id in the URL still wins.
const LAST_PROJECT_KEY = 'ampuly.lastProjectId'
// 员工页 / 客户页 (S3-6B): how often the one list of a person's page is asked for again while connected (no refresh
// button, no pushes for these accounts).
const PERSON_PAGE_REFRESH_MS = 45_000
function readLastProjectId(): string {
  try { return String(localStorage.getItem(LAST_PROJECT_KEY) ?? '').trim() } catch { return '' }
}
function writeLastProjectId(projectId: string): void {
  try {
    if (projectId) localStorage.setItem(LAST_PROJECT_KEY, projectId)
  } catch { /* storage unavailable: the console opens on the server's project, as before */ }
}
function readUrlProjectId(): string {
  try { return String(new URLSearchParams(window.location.search || '').get('project_id') || '').trim() } catch { return '' }
}
/** Where the console opens: the URL's project, else this browser's last one -- each only while it still exists. '' = the server's. */
function startProjectId(listed: readonly { id: string }[]): string {
  const ids = new Set(listed.map(project => String(project?.id ?? '').trim()))
  return [readUrlProjectId(), readLastProjectId()].find(id => id !== '' && ids.has(id)) ?? ''
}
type AppExecMode = 'task' | 'company' | 'org'

function defaultWsUrl(): string {
  const wsProto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${wsProto}://${window.location.hostname}:${window.location.port || '8765'}/ws`
}

function normalizeCompanyProfile(value?: string): 'corporate' | 'custom' {
  return normalizeSessionCompanyProfile(value)
}

function normalizeExecMode(value?: string): AppExecMode {
  return normalizeSessionExecMode(value)
}

function companyProfileForExecMode(mode: AppExecMode, _profile?: string): 'corporate' | 'custom' | undefined {
  if (mode === 'task') return undefined
  if (mode === 'org') return 'custom'
  return 'corporate'
}

function orgIdForExecMode(mode: AppExecMode, orgId?: string | null): string | undefined {
  if (mode !== 'org') return undefined
  const normalized = String(orgId ?? '').trim()
  return normalized || undefined
}

function normalizeTaskPreferredAgent(value?: string): TaskPreferredAgent {
  const normalized = String(value ?? '').trim().toLowerCase().replace('-', '_')
  if (
    normalized === 'codex'
    || normalized === 'claude_code'
    || normalized === 'cursor'
    || normalized === 'opencode'
    || normalized === 'jiuwen'
    || normalized === 'jiuwenswarm'
  ) {
    return normalized
  }
  return 'native'
}

function mapAgentPayload(raw: Record<string, unknown>, previous?: AgentInfo): AgentInfo {
  const runtimeStatus = (
    typeof raw.runtime_status === 'string'
      ? raw.runtime_status
      : typeof raw.status === 'string'
        ? raw.status
        : previous?.runtime_status ?? previous?.status ?? 'idle'
  ) as AgentAnimStatus | string
  const appearance = raw.appearance && typeof raw.appearance === 'object'
    ? raw.appearance as AgentInfo['appearance']
    : (previous?.appearance ?? { palette: 0, hue_shift: 0, seat_zone: 'work_area' })
  const specialties = Array.isArray(raw.specialties)
    ? raw.specialties.filter((item): item is string => typeof item === 'string')
    : (previous?.specialties ?? [])
  const agentId = typeof raw.agent_id === 'string' ? raw.agent_id : (previous?.agent_id ?? '')

  return {
    agent_id: agentId,
    name: typeof raw.name === 'string' && raw.name
      ? raw.name
      : typeof raw.role_name === 'string' && raw.role_name
        ? raw.role_name
        : (previous?.name ?? agentId),
    description: typeof raw.description === 'string' ? raw.description : (previous?.description ?? ''),
    specialties,
    status: runtimeStatus,
    office_id: typeof raw.office_id === 'string' ? raw.office_id : previous?.office_id,
    appearance,
    employee_id: typeof raw.employee_id === 'string' ? raw.employee_id : previous?.employee_id,
    ampuly_role_id: typeof raw.ampuly_role_id === 'string' ? raw.ampuly_role_id : previous?.ampuly_role_id,
    runtime_status: runtimeStatus as AgentAnimStatus,
    current_tool: typeof raw.current_tool === 'string'
      ? raw.current_tool
      : raw.current_tool == null
        ? undefined
        : previous?.current_tool,
    current_task_id: typeof raw.current_task_id === 'string'
      ? raw.current_task_id
      : raw.current_task_id == null
        ? undefined
        : previous?.current_task_id,
  }
}

function mapAgentListPayload(rawAgents: unknown[], previous: AgentInfo[] = []): AgentInfo[] {
  const prevById = new Map(previous.map((agent) => [agent.agent_id, agent]))
  return rawAgents
    .filter((raw): raw is Record<string, unknown> => !!raw && typeof raw === 'object')
    .map((raw) => {
      const agentId = typeof raw.agent_id === 'string' ? raw.agent_id : ''
      return mapAgentPayload(raw, prevById.get(agentId))
    })
    .filter((agent) => !!agent.agent_id)
}

function mapEmployeeAssignmentPayload(raw: unknown): EmployeeAssignment | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const value = raw as Record<string, unknown>
  return {
    name: typeof value.name === 'string' ? value.name : undefined,
    employeeId: typeof value.employee_id === 'string'
      ? value.employee_id
      : typeof value.employeeId === 'string'
        ? value.employeeId
        : undefined,
    category: typeof value.category === 'string' ? value.category : undefined,
    experienceScore: typeof value.experience_score === 'number'
      ? value.experience_score
      : typeof value.experienceScore === 'number'
        ? value.experienceScore
        : undefined,
  }
}

function hasOwnPayloadField(raw: Record<string, unknown>, field: string): boolean {
  return Object.prototype.hasOwnProperty.call(raw, field)
}

function runtimeStatusClearsDisplayTool(status: unknown): boolean {
  const normalized = String(status ?? '').trim().toLowerCase()
  return normalized === 'idle'
    || normalized === 'done'
    || normalized === 'failed'
    || normalized === 'cancelled'
}

function workItemIdentityPatchFromPayload(raw: Record<string, unknown>): Partial<KanbanTask> {
  const patch: Partial<KanbanTask> = {}
  const executionMode = typeof raw.execution_mode === 'string' ? raw.execution_mode : ''
  const isTaskModeRuntime = executionMode === 'task_mode' || raw.work_item_projection_id === 'task_mode_execution'
  if (isTaskModeRuntime) {
    const employeeAssignment = mapEmployeeAssignmentPayload(raw.employee_assignment ?? raw.employeeAssignment)
    if (employeeAssignment) patch.employeeAssignment = employeeAssignment
    if (typeof raw.selected_execution_agent === 'string') patch.selectedExecutionAgent = normalizeTaskPreferredAgent(raw.selected_execution_agent)
    else if (typeof raw.selectedExecutionAgent === 'string') patch.selectedExecutionAgent = normalizeTaskPreferredAgent(raw.selectedExecutionAgent)
    return patch
  }
  if (typeof raw.work_item_projection_id === 'string') {
    patch.workItemProjectionId = raw.work_item_projection_id
  } else if (typeof raw.workItemProjectionId === 'string') {
    patch.workItemProjectionId = raw.workItemProjectionId
  }
  if (typeof raw.work_item_turn_type === 'string') patch.workItemTurnType = raw.work_item_turn_type
  else if (typeof raw.workItemTurnType === 'string') patch.workItemTurnType = raw.workItemTurnType

  if (typeof raw.work_item_role_id === 'string') patch.workItemRoleId = raw.work_item_role_id
  else if (typeof raw.workItemRoleId === 'string') patch.workItemRoleId = raw.workItemRoleId

  if (typeof raw.work_item_role_name === 'string') patch.workItemRoleName = raw.work_item_role_name
  else if (typeof raw.workItemRoleName === 'string') patch.workItemRoleName = raw.workItemRoleName

  const employeeAssignment = mapEmployeeAssignmentPayload(raw.employee_assignment ?? raw.employeeAssignment)
  if (employeeAssignment) patch.employeeAssignment = employeeAssignment
  if (typeof raw.selected_execution_agent === 'string') patch.selectedExecutionAgent = normalizeTaskPreferredAgent(raw.selected_execution_agent)
  else if (typeof raw.selectedExecutionAgent === 'string') patch.selectedExecutionAgent = normalizeTaskPreferredAgent(raw.selectedExecutionAgent)
  return patch
}

function sessionRuntimePatchFromPayload(raw: Record<string, unknown>): Partial<import('./types/kanban').Session> {
  const patch: Partial<import('./types/kanban').Session> = {}
  if (raw.latest_notification && typeof raw.latest_notification === 'object') {
    patch.latestNotification = raw.latest_notification as import('./types/kanban').WorkerNotification
  }
  if (typeof raw.runtime_session_id === 'string') patch.runtimeSessionId = raw.runtime_session_id
  if (typeof raw.resume_cursor === 'number') patch.resumeCursor = raw.resume_cursor
  if (Array.isArray(raw.active_subagents)) patch.activeSubagents = raw.active_subagents as Array<Record<string, unknown>>
  if (Array.isArray(raw.permission_requests)) patch.permissionRequests = raw.permission_requests as Array<Record<string, unknown>>
  if (typeof raw.worktree_path === 'string') patch.worktreePath = raw.worktree_path
  if (typeof raw.current_tool === 'string') patch.currentTool = raw.current_tool
  // displayTool is the sticky "last command" label; an empty string between
  // tools must NOT clear it (that causes the header tool-pill to flicker once
  // per tool call). Only write a real, non-empty label here.
  if (typeof raw.display_tool === 'string' && raw.display_tool.trim()) patch.displayTool = raw.display_tool
  if (typeof raw.tool_elapsed_ms === 'number') patch.toolElapsedMs = raw.tool_elapsed_ms
  if (typeof raw.last_tool_summary === 'string') patch.lastToolSummary = raw.last_tool_summary
  if (typeof raw.context_tokens === 'number') patch.contextTokens = raw.context_tokens
  // Ignore a non-positive window: an intra-turn 0 would wipe the last known
  // window and hide the context ring until the next tool call (flicker).
  if (typeof raw.context_window === 'number' && raw.context_window > 0) patch.contextWindow = raw.context_window
  if (typeof raw.context_remaining_pct === 'number') patch.contextRemainingPct = raw.context_remaining_pct
  if (typeof raw.input_tokens === 'number') patch.inputTokens = raw.input_tokens
  else if (typeof raw.input_tokens_total === 'number') patch.inputTokens = raw.input_tokens_total
  else if (typeof raw.tokens_in === 'number') patch.inputTokens = raw.tokens_in
  if (typeof raw.output_tokens === 'number') patch.outputTokens = raw.output_tokens
  else if (typeof raw.output_tokens_total === 'number') patch.outputTokens = raw.output_tokens_total
  else if (typeof raw.tokens_out === 'number') patch.outputTokens = raw.tokens_out
  if (typeof raw.total_tokens === 'number') patch.totalTokens = raw.total_tokens
  else if (typeof raw.tokens_total === 'number') patch.totalTokens = raw.tokens_total
  if (typeof raw.turn_cost_usd === 'number') patch.turnCostUsd = raw.turn_cost_usd
  if (typeof raw.session_cost_usd === 'number') patch.sessionCostUsd = raw.session_cost_usd
  if (typeof raw.image_count === 'number') patch.imageCount = raw.image_count
  if (typeof raw.image_cost_cny === 'number') patch.imageCostCny = raw.image_cost_cny
  if (typeof raw.pending_permission_count === 'number') patch.pendingPermissionCount = raw.pending_permission_count
  if (typeof raw.drain_mode === 'string') patch.drainMode = raw.drain_mode
  if (typeof raw.resident_status === 'string') patch.residentStatus = raw.resident_status
  if (typeof raw.actionable_inbox_count === 'number') patch.actionableInboxCount = raw.actionable_inbox_count
  if (typeof raw.protocol_backlog_count === 'number') patch.protocolBacklogCount = raw.protocol_backlog_count
  if (typeof raw.notification_backlog_count === 'number') patch.notificationBacklogCount = raw.notification_backlog_count
  return patch
}

function kanbanRuntimePatchFromPayload(raw: Record<string, unknown>): Partial<KanbanTask> {
  const patch: Partial<KanbanTask> = {}
  if (raw.latest_notification && typeof raw.latest_notification === 'object') {
    patch.latestNotification = raw.latest_notification as import('./types/kanban').WorkerNotification
  }
  if (typeof raw.current_tool === 'string') patch.currentTool = raw.current_tool
  // Sticky display label — see sessionRuntimePatchFromPayload above.
  if (typeof raw.display_tool === 'string' && raw.display_tool.trim()) patch.displayTool = raw.display_tool
  if (typeof raw.tool_elapsed_ms === 'number') patch.toolElapsedMs = raw.tool_elapsed_ms
  if (typeof raw.last_tool_summary === 'string') patch.lastToolSummary = raw.last_tool_summary
  if (typeof raw.context_tokens === 'number') patch.contextTokens = raw.context_tokens
  if (typeof raw.context_window === 'number' && raw.context_window > 0) patch.contextWindow = raw.context_window
  if (typeof raw.context_remaining_pct === 'number') patch.contextRemainingPct = raw.context_remaining_pct
  if (typeof raw.input_tokens === 'number') patch.inputTokens = raw.input_tokens
  else if (typeof raw.input_tokens_total === 'number') patch.inputTokens = raw.input_tokens_total
  else if (typeof raw.tokens_in === 'number') patch.inputTokens = raw.tokens_in
  if (typeof raw.output_tokens === 'number') patch.outputTokens = raw.output_tokens
  else if (typeof raw.output_tokens_total === 'number') patch.outputTokens = raw.output_tokens_total
  else if (typeof raw.tokens_out === 'number') patch.outputTokens = raw.tokens_out
  if (typeof raw.total_tokens === 'number') patch.totalTokens = raw.total_tokens
  else if (typeof raw.tokens_total === 'number') patch.totalTokens = raw.tokens_total
  if (typeof raw.turn_cost_usd === 'number') patch.turnCostUsd = raw.turn_cost_usd
  if (typeof raw.session_cost_usd === 'number') patch.sessionCostUsd = raw.session_cost_usd
  if (typeof raw.image_count === 'number') patch.imageCount = raw.image_count
  if (typeof raw.image_cost_cny === 'number') patch.imageCostCny = raw.image_cost_cny
  if (typeof raw.pending_permission_count === 'number') patch.pendingPermissionCount = raw.pending_permission_count
  if (typeof raw.drain_mode === 'string') patch.drainMode = raw.drain_mode
  if (typeof raw.resident_status === 'string') patch.residentStatus = raw.resident_status
  if (typeof raw.actionable_inbox_count === 'number') patch.actionableInboxCount = raw.actionable_inbox_count
  if (typeof raw.protocol_backlog_count === 'number') patch.protocolBacklogCount = raw.protocol_backlog_count
  if (typeof raw.notification_backlog_count === 'number') patch.notificationBacklogCount = raw.notification_backlog_count
  return patch
}

function shouldRefreshLiveSession(taskId: string, sessionStore: SessionStoreState | null): boolean {
  if (!sessionStore || !taskId) return false
  if (sessionStore.activeSessionId === taskId) return true
  const active = sessionStore.activeSession
  if (!active) return false
  if (active.taskId === taskId || active.parentSessionId === taskId || active.sessionId === taskId) {
    return true
  }

  const target = sessionStore.sessions.find((session) => session.taskId === taskId)
  if (!target) return false

  const activeKeys = new Set(
    [String(active.taskId ?? '').trim(), String(active.sessionId ?? '').trim()].filter(Boolean),
  )
  const targetParent = String(target.parentSessionId ?? '').trim()
  if (targetParent && activeKeys.has(targetParent)) {
    return true
  }

  const activeParent = String(active.parentSessionId ?? '').trim()
  if (!activeParent) return false
  return String(target.taskId ?? '').trim() === activeParent || String(target.sessionId ?? '').trim() === activeParent
}

function legacyPhaseFromSessionStatus(status: string): KanbanPhase {
  if (status === 'done' || status === 'delivered') return 'approved'
  if (status === 'failed') return 'failed'
  if (status === 'cancelled') return 'cancelled'
  if (status === 'pending') return 'queued'
  if (status === 'awaiting_manager_review' || status === 'awaiting_review') {
    return 'awaiting_manager_review'
  }
  if (status === 'awaiting_human') return 'awaiting_human'
  return 'running'
}

function legacyColumnForPhase(phase: KanbanPhase): string {
  if (phase === 'approved' || phase === 'failed' || phase === 'cancelled') return 'done'
  if (phase === 'awaiting_manager_review' || phase === 'awaiting_human') return 'in-review'
  if (phase === 'queued' || phase === 'ready' || phase === 'ready_for_rework' || phase === 'waiting_dependencies') return 'todo'
  return 'in-progress'
}

function legacyAggregatedStatus(status: string): RoleAggregatedStatus {
  if (status === 'done' || status === 'delivered') return 'done'
  if (status === 'failed' || status === 'cancelled') return 'failed'
  if (status === 'pending') return 'pending'
  if (status === 'awaiting_manager_review' || status === 'awaiting_review' || status === 'awaiting_human') return 'waiting'
  return 'active'
}

function legacyRuntimeStatus(status: string | undefined): AgentAnimStatus {
  if (status === 'reflecting' || status === 'tool_active' || status === 'idle') return status
  return 'idle'
}

function roleSummaryFromLegacySession(session: Session): RoleWorkItemSummary {
  const executionTurnId = getExecutionTurnId(session) || session.taskId
  const roleId = session.workItemRoleId || session.assigneeIds[0] || session.taskId
  const roleName = session.workItemRoleName || roleId.replace(/[_-]/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
  const phase = legacyPhaseFromSessionStatus(session.status)
  return {
    roleKey: roleId,
    roleId,
    roleName,
    runtimeStatus: legacyRuntimeStatus(session.agentStatus),
    aggregatedStatus: legacyAggregatedStatus(session.status),
    workItems: [
      {
        workItemId: session.workItemProjectionId || executionTurnId,
        workItemProjectionId: session.workItemProjectionId,
        phase,
        kanbanColumn: legacyColumnForPhase(phase),
        title: session.title || roleName,
        kind: session.workItemTurnType,
        executorRoleId: roleId,
        executorRoleName: roleName,
        createdAt: session.createdAt,
        updatedAt: session.updatedAt,
        executionTurnId,
        progressLog: session.progressLog,
        activitySections: session.progressLog.length > 0
          ? [{
              kind: 'activity',
              title: 'Runtime activity',
              roleName,
              runtimeTaskId: executionTurnId,
              entries: session.progressLog,
            }]
          : [],
      },
    ],
  }
}

/** Thin wrapper so the execution-panel lookup is a normal component, not a JSX IIFE. */
function MaybeExecutionPanel({ taskId, projectId, sessions, agents, getExternalTeamActivity, onLoadOlder, onSelectInvocation, runtimeOnly, onOpenRuntimeActivity, onClose }: {
  taskId: string | null
  projectId: string
  sessions: Session[]
  agents: AgentInfo[]
  getExternalTeamActivity: (projectId: string, taskId: string, invocationId?: string) => ExternalTeamActivityRecord | undefined
  onLoadOlder: (taskId: string, record: ExternalTeamActivityRecord) => void
  onSelectInvocation: (taskId: string, invocationId: string) => void
  runtimeOnly: boolean
  onOpenRuntimeActivity: () => void
  onClose: () => void
}) {
  if (!taskId) return null

  for (const session of sessions) {
    const payload = session.roleWorkItems
    if (!payload) continue
    for (const role of Object.values(payload)) {
      const row = role.workItems.find(workItem => workItem.executionTurnId === taskId)
      if (!row) continue
      if (isJiuwenOpaqueTeam(row) && !runtimeOnly) {
        const record = getExternalTeamActivity(projectId, taskId)
        return (
          <JiuwenTeamActivityPanel
            key={`${projectId}:${taskId}`}
            title={`${row.executorRoleName || row.executorRoleId || role.roleName || role.roleId} · JiuwenSwarm Team`}
            coveredRoleIds={row.coveredRoleIds}
            record={record}
            fallbackSummary={row.externalTeamSummary}
            onLoadOlder={record?.hasMore ? () => onLoadOlder(taskId, record) : undefined}
            onSelectInvocation={invocationId => onSelectInvocation(taskId, invocationId)}
            onOpenRuntimeActivity={onOpenRuntimeActivity}
            onClose={onClose}
          />
        )
      }
      return (
        <ExecutionPanel
          role={role}
          focusedWorkItemId={row.workItemId}
          focusedExecutionTurnId={row.executionTurnId}
          agents={agents}
          onClose={onClose}
        />
      )
    }
  }

  const focused = sessions.find(x => x.taskId === taskId || getExecutionTurnId(x) === taskId)
  if (!focused) return null
  if (isJiuwenOpaqueTeam(focused) && !runtimeOnly) {
    const runtimeTaskId = getExecutionTurnId(focused) || focused.taskId
    const record = getExternalTeamActivity(projectId, runtimeTaskId)
    return (
      <JiuwenTeamActivityPanel
        key={`${projectId}:${runtimeTaskId}`}
        title={`${focused.workItemRoleName || focused.title || 'Task'} · JiuwenSwarm Team`}
        coveredRoleIds={focused.coveredRoleIds}
        record={record}
        fallbackSummary={focused.externalTeamSummary}
        onLoadOlder={record?.hasMore ? () => onLoadOlder(runtimeTaskId, record) : undefined}
        onSelectInvocation={invocationId => onSelectInvocation(runtimeTaskId, invocationId)}
        onOpenRuntimeActivity={onOpenRuntimeActivity}
        onClose={onClose}
      />
    )
  }
  const role = roleSummaryFromLegacySession(focused)
  return (
    <ExecutionPanel
      role={role}
      focusedExecutionTurnId={taskId}
      agents={agents}
      onClose={onClose}
    />
  )
}

export default function App() {
  const { locale, t, translateMaybe } = useI18n()
  // The socket handlers are created once; a reply they word for a person reads the language chosen now, not then.
  const localeRef = useRef(locale)
  localeRef.current = locale
  const clientRef = useRef<VisualSocketClient | null>(null)
  const [crmAcks] = useState(() => new CRMAckBus())
  // One asset controller survives page/project switches. It uses this existing
  // socket; unknown saves keep their original request rather than auto-retrying.
  const [employeeAssets] = useState(() => new EmployeeAssetController({
    send: message => clientRef.current?.send(message) ?? 'send-failed',
    nextRequestId: () => `ae-${globalThis.crypto.randomUUID()}`,
  }))
  useEffect(() => () => {
    employeeAssets.connectionChanged(false)
    employeeAssets.closeTimers()
  }, [employeeAssets])
  const [eventTimeline] = useState(() => new EventTimelineStore(MAX_LOG_ITEMS))
  const [observatoryTrace] = useState(() => new ExecutionTraceStore(600))
  const [observatoryHistory] = useState(() => new ObservatoryHistoryReader(message => clientRef.current?.send(message, { queue: false }) ?? 'send-failed'))
  const [observatoryOpenSequence, setObservatoryOpenSequence] = useState(0)
  useEffect(() => () => observatoryHistory.cancel(), [observatoryHistory])

  const initialUrl = defaultWsUrl()

  const [wsUrl, setWsUrl] = useState(initialUrl)
  const [wsUrlInput, setWsUrlInput] = useState(initialUrl)
  const [status, setStatus] = useState<SocketStatus>('disconnected')
  const [statusDetail, setStatusDetail] = useState('')
  const [snapshot, setSnapshot] = useState<VisualSnapshot | null>(null)
  const [projectIdPolicy, setProjectIdPolicy] = useState<ProjectIdPolicy | null>(null)
  const [theme, setTheme] = useState<ThemeName>(loadStoredTheme)
  const deepLinkRef = useRef<ConsoleDeepLink>(readConsoleDeepLink())
  const [activePage, setActivePage] = useState<AppPage>(() => (
    deepLinkRef.current.kind !== 'none'
      ? deepLinkRef.current.page
      // An address that names a page wins; otherwise this browser's 启动默认页面 (设置 → 通用), whose default is
      // the page it was last on.
      : pageFromSearch(window.location.search) ?? resolveStartPage(loadStartPage(window.localStorage), loadLastActivePage(window.localStorage, 'master'))
  ))
  // Which flow the builder shows, as the builder page reports it; the top bar names it.
  const [builderFlow, setBuilderFlow] = useState<BuilderCrumb | null>(null)
  // 安安's side panel: open or not, for this visit. On 总控 itself there is nothing to open beside.
  const [assistantOpen, setAssistantOpen] = useState(false)
  // One browser/application surface, sharing the side column with 安安. Records never mount a workbench inside it.
  const [executionPanelOpen, setExecutionPanelOpen] = useState(false)
  const [executionTab, setExecutionTab] = useState<'browser' | 'desktop'>('browser')
  const [executionDetailTab, setExecutionDetailTab] = useState<ExecutionTab | null>(null)
  // A count on 项目中心 opens 业务工单 on the queue it counted. ?page=approvals (retired) opens it on 等你处理 from the
  // first render (policy), so the inbox never stands on 全部 first.
  const [orderQueueRequest, setOrderQueueRequest] = useState<{ queue: OrderQueue; state?: string; query: string; open: string; flow: string; seq: number } | null>(() => {
    const group = deepLinkRef.current.landing?.group
    return group ? { queue: 'all', state: group, query: '', open: '', flow: '', seq: 1 } : null
  })
  const builderFlowRef = useRef<BuilderCrumb | null>(null)
  const reportBuilderFlow = useCallback((next: BuilderCrumb | null) => {
    builderFlowRef.current = next
    setBuilderFlow(current => (sameCrumb(current, next) ? current : next))
  }, [])
  // The socket handlers are created once; they read the page the owner is on from here.
  const activePageRef = useRef(activePage)
  activePageRef.current = activePage
  // Some owner pages read lists that only their menu click used to ask for. A reload restores the page without that
  // click and a project switch drops every answer in flight, so the same lists are asked for again once a project
  // has settled (answers that arrive earlier are discarded by the project filter).
  const requestOwnerPageListsRef = useRef<(projectId: string) => void>(() => undefined)
  // Whether this socket is already known to be signed in as the owner (the auth state is re-sent on many events).
  const ownerSignedInRef = useRef(false)
  // Which project and page the lists were last asked for, by a click or by the quiet re-ask below.
  const restoredPageKeyRef = useRef('')
  requestOwnerPageListsRef.current = (projectId: string) => {
    const client = clientRef.current
    if (!client) return
    const page = activePageRef.current
    if (page === 'config') {
      // 配置 · 服务与连接 holds 控制面's panels (S3-7): the owner's two lists that are not in the quiet set.
      client.governancePolicyList(projectId)
      client.browserCandidateControl(projectId, 'list')
    } else if (page === 'components') {
      client.workflowDistributionInbox(projectId)
    }
    // A page restored without a click (a reload, an address that names it) or after a project switch still needs its
    // lists: once per project and page, quietly. See lib/restoredPageLists for why it is not the click road.
    const restored = restoredPageAsk(restoredPageKeyRef.current, projectId, page)
    restoredPageKeyRef.current = restored.key
    if (!restored.ask) return
    // Every caller of this hook has already established that this console is the owner's -- one of them from the auth
    // event itself, before the auth ref is updated. Asking the ref here again would read the old state, ask for the
    // non-owner lists only, and remember the page as asked: the owner's lists then sat at "loading" for good.
    for (const name of quietListsFor(page, true)) {
      if (name === 'orderList') refreshOrderListRef.current()
      else client[name](projectId)
    }
  }
  useEffect(() => { saveLastActivePage(window.localStorage, activePage) }, [activePage])
  // 总控对话栏 (policy, lib/masterThreads): the conversation 总控 shows. On load it is the one the
  // address names (thread=, so a refresh stays in it), else a new one; from the rail it is always a new one.
  const [masterOpen, setMasterOpen] = useState<MasterOpen>(() => arrivalOpen(threadFromSearch(window.location.search)))
  const masterOpenRef = useRef(masterOpen)
  masterOpenRef.current = masterOpen
  // The address names the page, so a reload or a copied link comes back to it -- and, on 总控, the conversation. Not
  // the project yet: its initial deep link is consumed below, then a separate effect writes only a settled project.
  useEffect(() => {
    try {
      const paged = searchForPage(window.location.search, activePage)
      const next = activePage === 'master' ? searchForThread(paged, masterOpen.threadId) : paged
      if (next !== window.location.search) window.history.replaceState(window.history.state, '', `${window.location.pathname}${next}${window.location.hash}`)
    } catch {
      // An embedding that forbids history changes still gets a working console.
    }
  }, [activePage, masterOpen.threadId])
  // ...and when the address changes without the document reloading -- the back and forward buttons -- the console
  // follows it. Without this the address and the screen drift apart: the page stays put and the effect above quietly
  // writes the old page back into the address, so back appears to do nothing. It goes through the one click road, so
  // the page arrives with its lists asked for, exactly as a click on the rail would.
  const openPageRef = useRef<(page: AppPage) => void>(() => undefined)
  useEffect(() => {
    const onPop = () => {
      const page = pageFromSearch(window.location.search)
      if (page && page !== activePageRef.current) openPageRef.current(page)
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])
  // 执行区's side window, for the code that opens it over a run without the render's openExecution: the socket handler
  // (created once) for a workflow placed from the builder, and a ?task_id= deep link. ("Continue in a new chat" left
  // with the free sessions, policy R1 option B.)
  const openExecutionRef = useRef<(taskId?: string, tab?: ExecutionTab) => void>(() => undefined)
  const [deepLinkOpened, setDeepLinkOpened] = useState(false)
  const [swarmAgents, setSwarmAgents] = useState<AgentInfo[]>([])
  const [showDevTools, setShowDevTools] = useState(false)
  const [globalExecMode, setGlobalExecMode] = useState<AppExecMode>('task')
  const [globalCompanyProfile, setGlobalCompanyProfile] = useState<'corporate' | 'custom'>('corporate')
  const [globalTaskPreferredAgent, setGlobalTaskPreferredAgent] = useState<TaskPreferredAgent>('native')
  const [globalNativeApprovalDefault, setGlobalNativeApprovalDefault] = useState<NativeApprovalLevel>('auto')
  const [orgInfoData, setOrgInfoData] = useState<OrgInfoPayload | null>(null)
  const [orgInfoError, setOrgInfoError] = useState<string | null>(null)
  const [capabilityLoading, setCapabilityLoading] = useState(false)
  const [capabilityReprobing, setCapabilityReprobing] = useState(false)
  const [orderTimeline, setOrderTimeline] = useState<OrderTimeline | null>(null)
  // What settings one order was launched under, read from its own event. null = it has none.
  const [orderModelPlan, setOrderModelPlan] = useState<ModelPlan>(null)
  const [updatingHandlingOrderId, setUpdatingHandlingOrderId] = useState<string | null>(null)
  // The top bar's project chip leads to 项目中心's all-projects level: each click asks for it again.
  const [projectsHomeSeq, setProjectsHomeSeq] = useState(0)
  // 数据与文件 P4: 已经有过的客户，用来给「绑定客户」那个自由文本框做输入提示。
  const [knownCustomers, setKnownCustomers] = useState<string[]>([])
  // 项目中心's all-projects level: where each project's orders stand. null = not read (or refused).
  const [projectOverview, setProjectOverview] = useState<ProjectOverview | null>(null)
  // 项目中心 (S3-2B): the open project's business boundary and its four connection lanes (S3-2A reads), and what
  // the page knows about each read (came / store behind / refused). Emptied on a project switch (lib/currentProject).
  const [projectProfile, setProjectProfile] = useState<ProjectProfile | null>(null)
  const [projectProfileState, setProjectProfileState] = useState<PanoramaReadState>(EMPTY_READ)
  const [projectConnections, setProjectConnections] = useState<ConnectionLane[] | null>(null)
  const [projectConnectionsState, setProjectConnectionsState] = useState<PanoramaReadState>(EMPTY_READ)
  const [orderTimelineError, setOrderTimelineError] = useState<string | null>(null)
  const [blueprints, setBlueprints] = useState<SolutionBlueprint[]>([])
  const [releaseEvidence, setReleaseEvidence] = useState<ReleaseEvidence | null>(null)
  const [channelCheck, setChannelCheck] = useState<ChannelCheck | null>(null)
  // null = never loaded for this project: the page must not read that as "this order has no allowance".
  const [runBudgets, setRunBudgets] = useState<RunBudget[] | null>(null)
  // The calls whose outcome nobody knows. The page has had the buttons to settle one all along and was never sent the list.
  const [pendingReservations, setPendingReservations] = useState<Array<{ reservation_id: string; budget_id: string; kind: string; amount: number; note: string }>>([])
  const [credentialReferences, setCredentialReferences] = useState<CredentialReference[]>([])
  const [deployments, setDeployments] = useState<Deployment[]>([])
  const [orderMetrics, setOrderMetrics] = useState<OrderMetricsRow[]>([])
  const [orderUsage, setOrderUsage] = useState<OrderUsage[]>([])
  const [rollouts, setRollouts] = useState<Rollout[]>([])
  const [rolloutsHasMore, setRolloutsHasMore] = useState(false)
  const [connectorSwitches, setConnectorSwitches] = useState<ConnectorSwitch[]>([])
  // 搭建台's 「去打开」 beside a desktop step's closed-channel note (4.7-A06): 配置 → 渠道连接, landed on the desktop
  // channel's switch. The section asked for is the one 配置 opens on this time only; any later visit opens as usual.
  // A retired deep link (?page=capabilities / ?page=org, S3-7) names its section the same way, from the first render.
  const [configEntry, setConfigEntry] = useState<ConfigSection | undefined>(() => deepLinkRef.current.landing?.section)
  const [desktopChannelFocus, setDesktopChannelFocus] = useState(false)
  const openDesktopChannelRef = useRef<() => void>(() => undefined)
  useEffect(() => {
    const onOpen = () => openDesktopChannelRef.current()
    window.addEventListener(OPEN_DESKTOP_CHANNEL_EVENT, onOpen)
    return () => window.removeEventListener(OPEN_DESKTOP_CHANNEL_EVENT, onOpen)
  }, [])
  useEffect(() => {
    if (activePage !== 'config') { setConfigEntry(undefined); setDesktopChannelFocus(false) }
  }, [activePage])
  const [connectorCandidates, setConnectorCandidates] = useState<ConnectorCandidate[]>([])
  const [connectorRegistrations, setConnectorRegistrations] = useState<ConnectorRegistration[]>([])
  const [connectorCandidateError, setConnectorCandidateError] = useState<string | null>(null)
  const [componentCatalog, setComponentCatalog] = useState<ComponentCatalog | null>(null)
  const [componentCatalogLoading, setComponentCatalogLoading] = useState(false)
  const [componentCatalogError, setComponentCatalogError] = useState<string | null>(null)
  // 组件 · 查看: the one internal skill whose read-only detail is open (4.7 Q03).
  const [componentDetail, setComponentDetail] = useState<ComponentDetailView | null>(null)
  // 组件 · 编辑 / 测试 (4.7 A2-4): the same detail opened on its 契约 or 测试 page, with the skill's version chain.
  const [componentWork, setComponentWork] = useState<{ id: string; tab: WorkbenchTab } | null>(null)
  const componentWorkRef = useRef(componentWork)
  componentWorkRef.current = componentWork
  // refusal (4.7-A03): the last thing refused on one candidate, kept on its card until something on it succeeds.
  const [componentVersionsState, setComponentVersionsState] = useState<{ id: string; versions: ComponentVersions | null; loading: boolean; error: string | null; busy: string; refusal?: { candidateId: string; message: string } | null }>({ id: '', versions: null, loading: false, error: null, busy: '' })
  // 4.7-A16 回到刚才的步骤: the builder step a skill was taken from to 组件库 to be edited (returnTo -- App state, not a
  // route), and the one-off request that takes the builder back to it. Leaving 组件库 any other way forgets it.
  const [stepReturn, setStepReturn] = useState<StepReturn | null>(null)
  const [stepReturnRequest, setStepReturnRequest] = useState<{ workflowId: string; nodeId: string; seq: number } | null>(null)
  const stepReturnSeqRef = useRef(0)
  useEffect(() => { if (activePage !== 'components') setStepReturn(null) }, [activePage])
  // What the builder is on, for 安安 to offer as the thing a message is about (4.7 Q04 引用).
  const [builderFocus, setBuilderFocus] = useState<BuilderFocus | null>(null)
  // ＋ 新建业务流: a blank draft is being created (4.7 Q01).
  const [creatingBlank, setCreatingBlank] = useState(false)
  // 在搭建台打开 a 安安 flow draft: the reply whose draft is being opened (4.7 Q04-B).
  const [flowDraftOpening, setFlowDraftOpening] = useState('')
  const [componentDraftAck, setComponentDraftAck] = useState<Record<string, unknown> | null>(null)
  // 项目采用业务流 for the project on screen (null while it loads).
  const [projectAdoptions, setProjectAdoptions] = useState<WorkflowAdoption[] | null>(null)
  // 流程版本面板 (验收准入): per flow, what the console said about its versions; null when it could not read them.
  const [flowVersionRecords, setFlowVersionRecords] = useState<Record<string, FlowVersions | null>>({})
  const [skillImprovements, setSkillImprovements] = useState<SkillImprovement[] | null>(null)
  const [distributionInbox, setDistributionInbox] = useState<DistributionInboxRecord[] | null>(null)
  const [governancePolicy, setGovernancePolicy] = useState<GovernancePolicyView | null>(null)
  const [governanceError, setGovernanceError] = useState<string | null>(null)
  const [browserCandidates, setBrowserCandidates] = useState<BrowserCandidateRow[] | null>(null)
  const [browserCandidateNote, setBrowserCandidateNote] = useState<string | null>(null)
  const [browserSealedDemo, setBrowserSealedDemo] = useState<{ demo_id: string; steps: number } | null>(null)
  const [restorePreflight, setRestorePreflight] = useState<RestorePreflightReport | null>(null)
  const [restorePreflightError, setRestorePreflightError] = useState<string | null>(null)
  const [lastBackupPath, setLastBackupPath] = useState('')
  const [restoreBackupState, setRestoreBackupState] = useState<RestoreBackupState>({ phase: 'idle' })
  const restoreBackupTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // The trail that is open belongs to the project it was asked for, which need not be the one that is open.
  const [lifecycleProjectId, setLifecycleProjectId] = useState<string | null>(null)
  const lifecycleProjectIdRef = useRef<string | null>(null)
  const [lifecycleTrail, setLifecycleTrail] = useState<ProjectLifecycleTrail | null>(null)
  const [lifecycleError, setLifecycleError] = useState<string | null>(null)
  const [improvementProposals, setImprovementProposals] = useState<ImprovementProposal[] | null>(null)
  // 4.7-A11 提一个改进: the last start (which detail asked, the candidate it made or the refusal), the proposal being
  // acted on, its last refusal, and the draft flow to open on the canvas.
  const [improvementStart, setImprovementStart] = useState<{ key: string; busy: boolean; result: ImprovementStart | null; refusal: string | null }>({ key: '', busy: false, result: null, refusal: null })
  const [improvementBusy, setImprovementBusy] = useState('')
  const [improvementRefusal, setImprovementRefusal] = useState<{ proposalId: string; message: string } | null>(null)
  const [openFlowRequest, setOpenFlowRequest] = useState<{ workflowId: string; seq: number } | null>(null)
  const [skillImprovementBusy, setSkillImprovementBusy] = useState('')
  const [skillImprovementError, setSkillImprovementError] = useState<string | null>(null)
  const [marketCategories, setMarketCategories] = useState<MarketCategory[]>([])
  const [marketBrowse, setMarketBrowse] = useState<MarketBrowseState>({ skills: [], total: 0, keyword: '', category: '', sort: 'stars', loading: false, error: null })
  const [skillTrials, setSkillTrials] = useState<TrialSkill[]>([])
  const [marketBusy, setMarketBusy] = useState<Record<string, TrialBusy>>({})
  // Kept as the reason and what the server said; the panel words it when shown, so it follows a language switch.
  const [skillTrialError, setSkillTrialError] = useState<MarketRefusal | null>(null)
  const [trialFileView, setTrialFileView] = useState<{ key: string; path: string; text: string; truncated: boolean; binary: boolean } | null>(null)
  // An admission refused for want of the owner's yes to a SkillHub package that differs from its list, or for a yes to
  // other differences than the trial's now (changed): that card asks again, with the current differences.
  const [trialMismatchAsk, setTrialMismatchAsk] = useState<{ key: string; seq: number; changed?: boolean } | null>(null)
  const trialMismatchAskSeqRef = useRef(0)
  const trialsSeenRef = useRef(false)
  const [browserSessions, setBrowserSessions] = useState<BrowserSession[]>([])
  const browserSessionsRef = useRef<BrowserSession[]>([])
  browserSessionsRef.current = browserSessions
  const browserViewRequestRef = useRef<ReturnType<typeof browserPushScope>>(null)
  const browserHandoffNoticesRef = useRef(new Map<string, NonNullable<ReturnType<typeof browserPushScope>>>())
  const [browserSessionError, setBrowserSessionError] = useState<string | null>(null)
  const [browserAccountInspection, setBrowserAccountInspection] = useState<BrowserAccountInspectionView | null>(null)
  const browserAccountInspectionRef = useRef<BrowserAccountInspectionView | null>(null)
  const [desktopSessionStatus, setDesktopSessionStatus] = useState<DesktopSessionStatus | null>(null)
  const [desktopSessionError, setDesktopSessionError] = useState<string | null>(null)
  const [desktopSessionBusy, setDesktopSessionBusy] = useState(false)
  const [browserLimits, setBrowserLimits] = useState<NonNullable<CapabilityCenterPageProps['browserLimits']> | null>(null)
  const [browserCombination, setBrowserCombination] = useState<NonNullable<CapabilityCenterPageProps['browserCombination']> | null>(null)
  const [browserTrail, setBrowserTrail] = useState<NonNullable<CapabilityCenterPageProps['browserTrail']> | null>(null)
  const [browserLookBackNote, setBrowserLookBackNote] = useState<string | null>(null)
  const [browserAuditExportNote, setBrowserAuditExportNote] = useState<string | null>(null)
  const [browserScreenshot, setBrowserScreenshot] = useState<NonNullable<CapabilityCenterPageProps['browserScreenshot']> | null>(null)
  const [browserLiveFrame, setBrowserLiveFrame] = useState<NonNullable<CapabilityCenterPageProps['browserLiveFrame']> | null>(null)
  const [browserPickedElement, setBrowserPickedElement] = useState<{ tag?: string; type?: string; role?: string; placeholder?: string; text?: string } | null>(null)
  const [browserLiveBinding, setBrowserLiveBinding] = useState<string | null>(null)
  // X05 / V03: the client's own browser pane -- opened by itself when an order's page needs a person.
  const [browserPane, setBrowserPane] = useState<{ binding_key: string; task_id: string; note: string; anchor_session_id?: string; order_id?: string } | null>(null)
  const browserPaneRef = useRef<{ binding_key: string; task_id: string; note: string; anchor_session_id?: string; order_id?: string } | null>(null)
  browserPaneRef.current = browserPane
  function clearBrowserAccountInspection(): void {
    browserAccountInspectionRef.current = null
    setBrowserAccountInspection(null)
  }
  function inspectBrowserAccount(session: BrowserSession): void {
    if (!isOwnerConsole(authRef.current) || session.project_id !== getActiveProjectId()) return
    const request = browserAccountInspectRequest(session.project_id, session.task_id, session.binding_key, crypto.randomUUID())
    if (!request) return
    const next: BrowserAccountInspectionView = { request, session_signature: browserInspectionSessionSignature(session), status: 'pending' }
    const current = browserSessionsRef.current.find(row => row.binding_key === session.binding_key)
    if (!browserInspectionMatchesSession(next, current)) return
    browserAccountInspectionRef.current = next
    setBrowserAccountInspection(next)
    if (clientRef.current?.browserAccountInspect(request.project_id, request.task_id, request.binding_key, request.request_id) !== 'sent') {
      const failed: BrowserAccountInspectionView = { ...next, status: 'error', error: 'not_sent' }
      browserAccountInspectionRef.current = failed
      setBrowserAccountInspection(failed)
    }
  }
  function receiveBrowserAccountInspection(payload: Record<string, unknown>): void {
    const current = browserAccountInspectionRef.current
    if (!current || current.status !== 'pending' || !currentProject.accepts(payload)
      || !browserInspectionReplyMatches(payload, current.request)) return
    const row = browserSessionsRef.current.find(item => item.binding_key === current.request.binding_key)
    if (!browserInspectionMatchesSession(current, row)) { clearBrowserAccountInspection(); return }
    const inspection = payload.ok === true ? parseBrowserAccountInspection(payload.inspection) : null
    const next: BrowserAccountInspectionView = inspection ? { ...current, status: 'ready', inspection }
      : { ...current, status: 'error', error: payload.ok === true ? 'invalid_response' : 'refused' }
    browserAccountInspectionRef.current = next
    setBrowserAccountInspection(next)
  }
  function refreshBrowserAccountInspection(rows: BrowserSession[]): void {
    const current = browserAccountInspectionRef.current
    if (current && !browserInspectionMatchesSession(current, rows.find(row => row.binding_key === current.request.binding_key))) clearBrowserAccountInspection()
  }
  function timeoutBrowserAccountInspection(requestId: string): void {
    const current = browserAccountInspectionRef.current
    if (!current || current.status !== 'pending' || current.request.request_id !== requestId) return
    const next: BrowserAccountInspectionView = { ...current, status: 'error', error: 'timeout' }
    browserAccountInspectionRef.current = next
    setBrowserAccountInspection(next)
  }
  useEffect(() => {
    if (browserAccountInspection?.status !== 'pending') return
    const timer = window.setTimeout(() => timeoutBrowserAccountInspection(browserAccountInspection.request.request_id), 10_000)
    return () => window.clearTimeout(timer)
  }, [browserAccountInspection])
  function requestBrowserView(bindingKey: string, context?: BrowserControlContext): void {
    const wanted = browserPushScope({ project_id: getActiveProjectId(), binding_key: bindingKey, ...context })
    const row = browserSessionsRef.current.find(item => item.binding_key === bindingKey)
    if (!wanted || !row || !browserFrameMatchesSession(wanted, row)) {
      setBrowserSessionError('浏览器状态已变化，请刷新后重新打开画面。')
      return
    }
    browserViewRequestRef.current = wanted
    setBrowserLiveFrame(null)
    setBrowserPickedElement(null)
    const sent = clientRef.current?.browserViewStart(getActiveProjectId(), bindingKey, context)
    if (sent !== 'sent') {
      if (browserViewRequestRef.current === wanted) browserViewRequestRef.current = null
      setBrowserSessionError('画面请求未发出；不会在重连后自动重试。')
    }
  }
  function stopBrowserView(bindingKey: string, context?: BrowserControlContext): void {
    const wanted = browserPushScope({ project_id: getActiveProjectId(), binding_key: bindingKey, ...context })
    if (wanted && browserFrameMatchesSession(wanted, browserViewRequestRef.current)) {
      browserViewRequestRef.current = null
      setBrowserLiveFrame(null)
      setBrowserLiveBinding(null)
      setBrowserPickedElement(null)
    }
    if (clientRef.current?.browserViewStop(getActiveProjectId(), bindingKey, context) !== 'sent') {
      setBrowserSessionError('停止画面的请求未发出；未关闭浏览器或业务任务。')
    }
  }
  // bindings whose pane the person closed on purpose: a session list refresh does not reopen them
  const dismissedPanesRef = useRef<Set<string>>(new Set())
  const [pendingTurns, setPendingTurns] = useState<PendingTurn[]>([])
  const [pendingTurnError, setPendingTurnError] = useState<string | null>(null)
  const [turnLedger, setTurnLedger] = useState<TurnLedger | null>(null)
  const [solutionPackages, setSolutionPackages] = useState<Array<{ solution_id: string; version: string }>>([])
  const [rolloutError, setRolloutError] = useState<string | null>(null)
  const [processSpecs, setProcessSpecs] = useState<NonNullable<CapabilityCenterPageProps['processSpecs']>>([])
  const [selectedSpec, setSelectedSpec] = useState('')
  const selectedSpecRequest = useRef({ specId: '', version: '' })
  const [specReview, setSpecReview] = useState<CapabilityCenterPageProps['specReview']>(null)
  const [builderRounds, setBuilderRounds] = useState<NonNullable<CapabilityCenterPageProps['builderRounds']>>([])
  const [processDraft, setProcessDraft] = useState<CapabilityCenterPageProps['processDraft']>(null)
  const [specSaving, setSpecSaving] = useState(false)
  const [trialRequest, setTrialRequest] = useState<BuilderTrialRequest | null>(null)
  const trialRequestRef = useRef<BuilderTrialRequest | null>(null)
  // Readable from the socket callbacks, which close over stale state.
  const specSavingRef = useRef(false)
  useEffect(() => { specSavingRef.current = specSaving }, [specSaving])
  const [factoryError, setFactoryError] = useState<string | null>(null)
  const [processTemplates, setProcessTemplates] = useState<NonNullable<CapabilityCenterPageProps['processTemplates']>>([])
  const [templateError, setTemplateError] = useState<string | null>(null)
  useEffect(() => {
    if (trialRequest?.phase !== 'pending') return
    const requestId = trialRequest.requestId
    const timer = window.setTimeout(() => {
      const pending = trialRequestRef.current
      if (!pending || pending.requestId !== requestId) return
      const unknown: BuilderTrialRequest = { ...pending, phase: 'unknown' }
      trialRequestRef.current = unknown
      setTrialRequest(unknown)
    }, 150000)
    return () => window.clearTimeout(timer)
  }, [trialRequest])
  useEffect(() => {
    if (!specSaving) return
    const timer = window.setTimeout(() => {
      setSpecSaving(false)
      setFactoryError('操作回执超时，请刷新版本或工作流列表核对结果；不会自动重试。')
    }, 20000)
    return () => window.clearTimeout(timer)
  }, [specSaving])
  const [commsState, setCommsState] = useState<import('./lib/wsClient').CommsStatePayload | null>(null)
  const [commsMessage, setCommsMessage] = useState<import('./lib/wsClient').CommsMessagePayload | null>(null)
  const [reorgProposals, setReorgProposals] = useState<ReorgProposalInfo[]>([])
  const [marketPresets, setMarketPresets] = useState<any[]>([])
  const [marketPreviewData, setMarketPreviewData] = useState<any>(null)
  const [configExportYaml, setConfigExportYaml] = useState<string | null>(null)
  const [configImportPreview, setConfigImportPreview] = useState<{ roles_added: number; roles_removed: number; employees_changed: number } | null>(null)
  const [configImportError, setConfigImportError] = useState<string | null>(null)
  const [savedOrgsList, setSavedOrgsList] = useState<SavedOrgSummary[] | null>(null)
  const [activeSavedOrg, setActiveSavedOrg] = useState<string | null>(null)
  const [savedOrgVersionAtLoad, setSavedOrgVersionAtLoad] = useState<number | null>(null)
  const [orgCreatePending, setOrgCreatePending] = useState(false)
  const [orgCreateResult, setOrgCreateResult] = useState<(OrgSavedCreatePayload & { nonce: number }) | null>(null)
  const [orgToast, setOrgToast] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null)
  const timersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set())
  const replayedEventIds = useRef<Set<string>>(new Set())
  const swarmAgentsRef = useRef<AgentInfo[]>([])
  const globalExecModeRef = useRef<AppExecMode>('task')
  const globalNativeApprovalDefaultRef = useRef<NativeApprovalLevel>('auto')
  const kanbanCreateRef = useRef<((data: { title: string; description?: string; priority: null; assignee_id?: string }) => void) | null>(null)
  const chatStoreRef = useRef<ChatStoreState | null>(null)
  const boardStoreRef = useRef<BoardStoreState | null>(null)
  const sessionStoreRef = useRef<SessionStoreState | null>(null)
  const projectStoreRef = useRef<ProjectStoreState | null>(null)
  // The synchronous mirror of ProjectStore's active id, moved the moment a sync settles a switch (the store re-renders
  // later). The switch under way, its sequence and the view generation live in lib/currentProject (below).
  const activeProjectIdRef = useRef<string>('default')
  const userSelectedProjectRef = useRef<boolean>(false)
  const projectsHydratedRef = useRef<boolean>(false)
  const lastProjectIndexRefreshRef = useRef<number>(0)
  const pendingSessionCreateRef = useRef(false)
  const pendingSessionCreateProjectIdRef = useRef<string | null>(null)
  const pendingSessionCreateTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pendingSessionDetailRefreshRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
  // One session's detail is re-read at most every few seconds and never twice at once; a replayed message that
  // the page already shows asks for nothing (lib/sessionDetailRefresh.ts, 2026-09-26).
  const sessionDetailRefreshGateRef = useRef(new SessionDetailRefreshGate())
  const sessionMessageLedgerRef = useRef(new SessionMessageLedger())
  const [toastMessage, setToastMessage] = useState<string | null>(null)
  const [toastType, setToastType] = useState<'success' | 'error'>('success')
  const [executionPanelTaskId, setExecutionPanelTaskId] = useState<string | null>(null)
  const [executionPanelRuntimeOnly, setExecutionPanelRuntimeOnly] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  // Which service 配置 sent the person here for, so the dialog opens on that row rather than at its top. Declared up
  // here with every other hook: below the sign-in and employee returns it made a console with accounts crash
  // (React #300, fewer hooks on the sign-in render) -- found by the acceptance probe Previously,.
  const [modelsFocus, setModelsFocus] = useState('')
  const [settingsData, setSettingsData] = useState<SettingsData | null>(null)
  const [settingsError, setSettingsError] = useState<string | null>(null)
  const [settingsTestResult, setSettingsTestResult] = useState<SettingsTestResult | null>(null)
  const [settingsNotifyResult, setSettingsNotifyResult] = useState<SettingsNotifyResult | null>(null)
  const [dutyLog, setDutyLog] = useState<DutyLogState>({ configured: false, entries: [] })
  const [dutyLogError, setDutyLogError] = useState<string | null>(null)
  // 人员与职责 · 联系方式 (4.7-A07): the contact book as last read (null = not read), the bind in flight, and the
  // builder's 去绑定 asking the project page for its 人员与职责 tab.
  const [staffContacts, setStaffContacts] = useState<BookContact[] | null>(null)
  const [contactBind, setContactBind] = useState<ContactBindState>({ busyEmployeeId: '', error: null })
  const contactBindForRef = useRef('')
  // 配置 · 渠道连接 「企业微信（微信客服）」 (W3b): the channel's status as last read for a project, and the write in flight.
  const [wecomChannel, setWecomChannel] = useState<WecomChannelState>(EMPTY_WECOM_CHANNEL)
  const [customerService, setCustomerService] = useState<CustomerServiceState>(EMPTY_CUSTOMER_SERVICE)
  const [employeeHandoffs, setEmployeeHandoffs] = useState<EmployeeHandoffState>(EMPTY_EMPLOYEE_HANDOFFS)
  const [peopleTabRequest, setPeopleTabRequest] = useState<{ tab: 'collab'; collab: 'people'; seq: number } | null>(null)
  const [gateStatus, setGateStatus] = useState<GateStatus | null>(null)
  const [gateDetail, setGateDetail] = useState<GateDetail | null>(null)
  const [gateBusy, setGateBusy] = useState(false)
  const [gateError, setGateError] = useState<string | null>(null)
  const [workflows, setWorkflows] = useState<WorkflowSummary[] | null>(null)
  const [workflowError, setWorkflowError] = useState<string | null>(null)
  // The accepted list's project scope, not an individual shared workflow's owner.
  const [workflowsReadProjectId, setWorkflowsReadProjectId] = useState<string | null>(null)
  // Console accounts (architecture step 1): who is logged in on this connection.
  const [auth, setAuth] = useState<AuthState | null>(null)
  const authRef = useRef<AuthState | null>(null)
  const quietPrincipalRef = useRef('')
  const quietCompletionRef = useRef(false)
  const [quietCompletion, setQuietCompletion] = useState(false)
  const [quietSaveFailed, setQuietSaveFailed] = useState(false)
  const changeQuietCompletion = (next: boolean) => {
    let saved = false
    try { saved = saveQuietCompletion(window.localStorage, quietPrincipalRef.current, next) } catch { saved = false }
    setQuietSaveFailed(!saved)
    if (saved) { quietCompletionRef.current = next; setQuietCompletion(next) }
  }

  authRef.current = auth
  const customerPreviewRequestRef = useRef<CustomerPreviewRequest | null>(null)
  const [customerPreview, setCustomerPreview] = useState<{
    request: CustomerPreviewRequest; loading: boolean; error: string | null; view: CustomerPreviewView | null
  } | null>(null)
  const clearCustomerPreview = useCallback(() => {
    customerPreviewRequestRef.current = null
    setCustomerPreview(null)
  }, [])
  useEffect(() => {
    if (!customerPreview?.loading) return
    const request = customerPreview.request
    const timer = setTimeout(() => {
      if (customerPreviewRequestRef.current !== request) return
      customerPreviewRequestRef.current = null
      setCustomerPreview({ request, loading: false, view: null, error: 'unknown' })
    }, 15_000)
    return () => clearTimeout(timer)
  }, [customerPreview])
  useEffect(() => { if (!isOwnerConsole(auth)) clearCustomerPreview() }, [auth, clearCustomerPreview])
  // 搭建台's desktop step offers 「去打开」 only to who may open the channel; anyone else reads 「只有负责人能打开」.
  useEffect(() => { setDesktopChannelOpener(isOwnerConsole(auth)) }, [auth])
  const [authError, setAuthError] = useState('')
  const [authBusy, setAuthBusy] = useState(false)
  const [accounts, setAccounts] = useState<AuthAccount[] | null>(null)
  const [accountsError, setAccountsError] = useState('')
  const [myTasks, setMyTasks] = useState<MyTask[] | null>(null)
  const [myTasksError, setMyTasksError] = useState('')
  const [myReplyError, setMyReplyError] = useState('')
  // 员工页「我的活」 (S3-6B): the task opened from the list (its materials are gateDetail, the same state as the owner's
  // materials view) and what this person handed back since signing in -- no server list of a person's decisions
  // exists, so this is the session's own record and nothing older. The receipt handler reaches both through refs.
  const [myOpen, setMyOpen] = useState<MyTask | null>(null)
  const myOpenRef = useRef<MyTask | null>(null)
  myOpenRef.current = myOpen
  const [myReturned, setMyReturned] = useState<ReturnedWork[]>([])
  const employeeDecidedRef = useRef<(checkpointId: string, verb: ReturnedWork['verb']) => void>(() => undefined)
  // 客户页「我的单」 (S3-6B): the account's own orders as the customer projection, and what each 确认收货 came back with.
  const [customerOrders, setCustomerOrders] = useState<CustomerOrder[] | null>(null)
  const [customerOrdersError, setCustomerOrdersError] = useState('')
  const [customerAcceptErrors, setCustomerAcceptErrors] = useState<Record<string, string>>({})
  const [workflowInvoke, setWorkflowInvoke] = useState<WorkflowInvokeState | null>(null)
  const [workflowPreflight, setWorkflowPreflight] = useState<WorkflowPreflight | null>(null)
  const [preflightingWorkflowId, setPreflightingWorkflowId] = useState<string | null>(null)
  const [employeeTrialAck, setEmployeeTrialAck] = useState<EmployeeWorkflowTrialAck | null>(null)
  const [workflowNodeTrials, setWorkflowNodeTrials] = useState<WorkflowNodeTrial[]>([])
  const [workflowNodeTrialBusy, setWorkflowNodeTrialBusy] = useState(false)
  const [workflowFlowTrials, setWorkflowFlowTrials] = useState<Array<NonNullable<FlowTrialSummary>>>([])
  const [publishedFlowTrials, setPublishedFlowTrials] = useState<{ workflowId: string; rows: PublishedFlowTrialCandidate[]; error: string | null }>({ workflowId: '', rows: [], error: null })
  const flowTrialRequestIds = useRef<Record<string, string>>({})
  const [workflowFlowTrialBusy, setWorkflowFlowTrialBusy] = useState(false)
  const [nodeTrialPinning, setNodeTrialPinning] = useState<NodeTrialPinning | null>(null)
  const [refusedSave, setRefusedSave] = useState<RefusedSave | null>(null)
  // The last 提交测试 / 申请发布审批 the server refused: kept beside the button until the next action, not only toasted.
  const [admissionRefusal, setAdmissionRefusal] = useState<{ workflowId: string; action: 'submit_testing' | 'request_release'; error: string } | null>(null)
  const [trialArtifact, setTrialArtifact] = useState<TrialArtifactState>({ preview: null, loading: false, error: null })
  // The one file the person asked to open and has not closed. An answer for anything else is dropped.
  const trialArtifactWantedRef = useRef<{ specId: string; path: string } | null>(null)
  const workflowNodeTrialPollRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const workflowFlowTrialPollRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => {
    if (workflowNodeTrialPollRef.current) clearTimeout(workflowNodeTrialPollRef.current)
    if (workflowFlowTrialPollRef.current) clearTimeout(workflowFlowTrialPollRef.current)
  }, [])
  const [creatingDraftWorkflowId, setCreatingDraftWorkflowId] = useState<string | null>(null)
  const [recordingTestWorkflowId, setRecordingTestWorkflowId] = useState<string | null>(null)
  const [simulatingTestWorkflowId, setSimulatingTestWorkflowId] = useState<string | null>(null)
  const [submittingTestingWorkflowId, setSubmittingTestingWorkflowId] = useState<string | null>(null)
  const [publishingWorkflowId, setPublishingWorkflowId] = useState<string | null>(null)
  const [rollingBackWorkflowId, setRollingBackWorkflowId] = useState<string | null>(null)
  const [rolloutBusyWorkflowId, setRolloutBusyWorkflowId] = useState<string | null>(null)
  const [rolloutRollbackBusyWorkflowId, setRolloutRollbackBusyWorkflowId] = useState<string | null>(null)
  const requireObservedWorkflowRevision = (expectedRevision: string): boolean => {
    if (typeof expectedRevision === 'string' && expectedRevision.trim()) return true
    showToast(locale.startsWith('zh') ? '缺少已读取版本，请刷新工作流后再试。' : 'The observed revision is missing. Refresh this workflow before retrying.', 'error')
    return false
  }
  const [requestingReleaseWorkflowId, setRequestingReleaseWorkflowId] = useState<string | null>(null)
  const [decidingReleaseWorkflowId, setDecidingReleaseWorkflowId] = useState<string | null>(null)
  const [updatingWorkflowId, setUpdatingWorkflowId] = useState<string | null>(null)
  const [nodesSaved, setNodesSaved] = useState<{ workflowId: string; seq: number } | null>(null)
  // The builder's unsaved drafts outlive the 搭建台 page: going to 组件库 to pick a component and back keeps them.
  const nodeDraftState = useNodeDraftState()
  // Open drafts are kept in this browser across a reload, and leaving with one open asks first. Never a save.
  // One tab of this browser edits a workflow at a time; the others show it read-only and may take over.
  const editLock = useEditLock(nodeDraftState)
  useDraftPersistence(nodeDraftState, workflowId => editLock.stateOf(workflowId) === 'other')
  const nodeDraftStateRef = useRef(nodeDraftState)
  nodeDraftStateRef.current = nodeDraftState
  const [compositionRequest, setCompositionRequest] = useState<WorkflowCompositionRequest | null>(null)
  const compositionSeqRef = useRef(0)
  // 调用 from 组件库: the component taken to a workflow's builder shelf, until the owner dismisses it.
  const [broughtComponent, setBroughtComponent] = useState<{ kind: 'skill'; id: string; name: string; workflowId: string; seq: number; trial?: { slug: string; version: string; source?: string } } | null>(null)
  const broughtSeqRef = useRef(0)
  // 录入内部库 from a market card or the builder shelf: open 组件库 on that trial's card (seq changes per request).
  const [trialFocus, setTrialFocus] = useState<{ key: string; seq: number } | null>(null)
  const trialFocusSeqRef = useRef(0)
  // Master control's suggestions from outside the library, per workflow, once its proposal is applied to a draft.
  const [proposalCandidates, setProposalCandidates] = useState<Record<string, ProposalCandidate[]>>({})
  // 调用测试 on a suggestion not downloaded yet: when the download lands, the builder takes it as a trial.
  const candidateUseAfterDownloadRef = useRef<Record<string, { workflowId: string; name: string }>>({})
  // 调用 / 调用测试: take a component to a workflow's builder shelf, loading what the 搭建台 nav button loads.
  const openBuilderWith = (component: { kind: 'skill'; id: string; name: string; workflowId: string; trial?: { slug: string; version: string; source?: string } }) => {
    // The seq survives a dismissed component, so the next 调用 is always a new one for the builder page.
    broughtSeqRef.current += 1
    setBroughtComponent({ ...component, seq: broughtSeqRef.current })
    setActivePage('workflows')
    clientRef.current?.workflowList(getActiveProjectId()); clientRef.current?.solutionBlueprintList(getActiveProjectId())
    clientRef.current?.runBudgetList(getActiveProjectId()); clientRef.current?.orderMetrics(getActiveProjectId()); refreshOrderList()
  }
  const openBuilderWithRef = useRef(openBuilderWith)
  openBuilderWithRef.current = openBuilderWith
  // 调用测试 on a local-catalog card: once imported, the trial area opens on the new trial's card.
  const catalogOpenAfterImportRef = useRef<Set<string>>(new Set())
  // The trial actions both the 组件库 cards and the builder shelf use.
  const trialHandlers = {
    onMark: (trial: TrialSkill, outcome: 'passed' | 'failed', note: string) => {
      setMarketBusy(prev => ({ ...prev, [trialKey(trial)]: 'marking' }))
      clientRef.current?.skillTrialMark(getActiveProjectId(), trial.slug, trial.version, outcome, note, trial.source)
    },
    onAdmit: (trial: TrialSkill, note: string, licenseConfirmed: boolean, successCriteria: string[], listingMismatchAcknowledged = false, listingMismatchKey = '') => {
      setMarketBusy(prev => ({ ...prev, [trialKey(trial)]: 'admitting' }))
      clientRef.current?.skillTrialAdmit(getActiveProjectId(), trial.slug, trial.version, note, licenseConfirmed, successCriteria, trial.source, listingMismatchAcknowledged, listingMismatchKey)
    },
    onDiscard: (trial: TrialSkill) => {
      setMarketBusy(prev => ({ ...prev, [trialKey(trial)]: 'discarding' }))
      clientRef.current?.skillTrialDiscard(getActiveProjectId(), trial.slug, trial.version, trial.source)
    },
    onRestore: (trial: TrialSkill) => {
      setMarketBusy(prev => ({ ...prev, [trialKey(trial)]: 'restoring' }))
      clientRef.current?.skillTrialRestore(getActiveProjectId(), trial.slug, trial.version, trial.source)
    },
  }
  // 录入内部库 from the builder shelf: the admission form (success criteria, license) is on the trial's card.
  const openTrialCard = (trial: TrialSkill) => {
    trialFocusSeqRef.current += 1
    setTrialFocus({ key: trialKey(trial), seq: trialFocusSeqRef.current })
    setActivePage('components')
    setComponentCatalogLoading(true); setComponentCatalogError(null)
    clientRef.current?.componentCatalogList(getActiveProjectId()); clientRef.current?.workflowList(getActiveProjectId())
    clientRef.current?.skillMarketCategories(getActiveProjectId()); clientRef.current?.skillTrialList(getActiveProjectId())
    setSkillImprovements(null); setImprovementProposals(null); setSkillImprovementError(null)
    clientRef.current?.skillImprovementList(getActiveProjectId()); clientRef.current?.improvementProposalList(getActiveProjectId())
    if (isOwnerConsole(auth)) clientRef.current?.workflowDistributionInbox(getActiveProjectId())
    trialsSeenRef.current = true
  }
  const openTrialCardRef = useRef(openTrialCard)
  openTrialCardRef.current = openTrialCard
  const [pendingNodeProposal, setPendingNodeProposal] = useState<NodeProposal | null>(null)
  const [acceptanceBusyOrderId, setAcceptanceBusyOrderId] = useState<string | null>(null)
  const [updatingFdeControlPlaneOrderId, setUpdatingFdeControlPlaneOrderId] = useState<string | null>(null)
  // Workflow and order writes in flight. If the connection drops their ack never arrives and the outcome is
  // unknown (4.4 §2.5: never present an unconfirmed result as done), so onStatus clears these spinners, says
  // the outcome is unknown, and refreshes workflows and orders after reconnecting for the truth to be checked.
  // Nothing is re-sent: a write already on the wire is not in the client's pending queue.
  // S2-B : publish, 保存步骤 and the acceptance writes go through the request tracker (below), which says
  // the cut itself and asks for the recheck; only the flags not yet routed are still counted here.
  const workflowWritesInFlightRef = useRef({ busy: false, invoking: false })
  workflowWritesInFlightRef.current = {
    busy: [
      creatingDraftWorkflowId, recordingTestWorkflowId, simulatingTestWorkflowId, submittingTestingWorkflowId,
      rollingBackWorkflowId, rolloutBusyWorkflowId, rolloutRollbackBusyWorkflowId, requestingReleaseWorkflowId, decidingReleaseWorkflowId,
      updatingFdeControlPlaneOrderId,
    ].some(Boolean),
    invoking: workflowInvoke?.status === 'sending',
  }
  const recheckWorkflowsOnReconnectRef = useRef(false)
  const [orders, setOrders] = useState<WorkOrderSummary[] | null>(null)
  // Only an accepted order-list reply can supply this read scope. A screen
  // project/default is not evidence that the retained rows belong to it.
  const [ordersReadProjectId, setOrdersReadProjectId] = useState<string | null>(null)
  // The server listed only the latest part of the orders (order_list's `truncated`); 业务工单 says so.
  const [ordersTruncated, setOrdersTruncated] = useState(false)
  // 4.7-A08 「查看更早的工单」: where the next older page starts, and whether older pages were added (lib/orderQueues).
  const [orderPaging, setOrderPaging] = useState<OrderPaging>(NO_ORDER_PAGING)
  const orderPagingRef = useRef<OrderPaging>(NO_ORDER_PAGING)
  // 4.7-A08 分发授权单: the server's last answer about one order's authorizations (the drawer's card reads it).
  const [distributionReply, setDistributionReply] = useState<DistributionAuthorizationReply | null>(null)
  // order_metrics' `truncated`: the cost / saving / promise figures cover only the newest orders the store handed back.
  const [orderMetricsTruncated, setOrderMetricsTruncated] = useState(false)
  const ordersRef = useRef<WorkOrderSummary[] | null>(null)
  ordersRef.current = orders
  const [orderScope, setOrderScope] = useState<{ customer_id: string; environment_id: string }>({ customer_id: '', environment_id: '' })
  const [artifactIndex, setArtifactIndex] = useState<ArtifactIndex | null>(null)
  const [artifactPreview, setArtifactPreview] = useState<ArtifactPreview | null>(null)
  const [artifactQuoteDraft, setArtifactQuoteDraft] = useState<ArtifactQuoteDraft | null>(null)
  const [artifactLoading, setArtifactLoading] = useState(false)
  const [artifactError, setArtifactError] = useState<string | null>(null)
  const [artifactPreviewLoading, setArtifactPreviewLoading] = useState(false)
  const [artifactPreviewError, setArtifactPreviewError] = useState<string | null>(null)
  // 资料 (data phase 2): what the project's own file pages hold, keyed by the project they came from, and how much
  // space each project takes. Usage is kept per project on purpose -- a walk started before a switch still answers
  // the project it was started for.
  const [dataState, dispatchData] = useReducer(dataReducer, EMPTY_DATA)
  const [dataLocations, setDataLocations] = useState<DataLocations | null>(null)
  const [dataBackups, setDataBackups] = useState<BackupList | null>(null)
  const [cleanupPreview, setCleanupPreview] = useState<CleanupPreview | null>(null)
  const [removalPlan, setRemovalPlan] = useState<RemovalPlan | null>(null)
  // 项目文件夹: the chooser's own state. Keyed by project, thrown away on a switch, like the 资料 panel's.
  const [folderState, dispatchFolder] = useReducer(folderReducer, EMPTY_FOLDER_STATE)
  // The socket's callbacks are built once, in an effect keyed by the URL, so anything they read from a render
  // is frozen at the render that built them. 「更换默认根目录」 read its scope that way and always found
  // 'project', which pointed the current project at the folder the owner chose for new ones.
  const folderStateRef = useRef(folderState)
  folderStateRef.current = folderState
  // 回收区: what a model "deleted" in this project, for the owner to put back or clear. Follows the project too.
  const [recycleState, dispatchRecycle] = useReducer(recycleReducer, EMPTY_RECYCLE_STATE)
  // 客户记忆 (4.7-A09): what the project remembers about its customers, for the owner to read and delete. Follows
  // the project too.
  const [memoryState, dispatchMemory] = useReducer(customerMemoryReducer, EMPTY_CUSTOMER_MEMORY)
  const [usageByProject, dispatchUsage] = useReducer(
    (map: Record<string, UsageEntry>, ack: DataAck) => usageReducer(map, ack, (projectStoreRef.current?.projects ?? []).map(entry => entry.id), Date.now()),
    {} as Record<string, UsageEntry>,
  )
  const [blockers, setBlockers] = useState<DashboardBlocker[]>([])
  const [pendingApprovalBadgeState, setPendingApprovalBadgeState] = useState<PendingBadgeView | null>(null)
  useEffect(() => {
    if (status !== 'connected' && isV2SurfaceEnabled('f_frontend_pending_approval_badge')) {
      setPendingApprovalBadgeState(previous => stalePendingBadge(previous))
    }
  }, [status])
  const [masterToolActivity, setMasterToolActivity] = useState<MasterToolActivity[]>([])
  // threadId: the conversation the running turn is in, as the server said it (lib/masterThreads turnBelongsTo): null =
  // it named none (an older server), '' = the project's first conversation.
  const [masterTurn, setMasterTurn] = useState<{sessionId: string; messageId: string; turnId: string; threadId?: string | null} | null>(null)
  const masterTurnRef = useRef<{sessionId: string; messageId: string; turnId: string; threadId?: string | null} | null>(null)
  const [masterInterruptPending, setMasterInterruptPending] = useState(false)
  const [masterInterruptRequested, setMasterInterruptRequested] = useState(false)
  const masterInterruptRef = useRef<{requestId: string; sessionId: string} | null>(null)
  const [masterInterruptError, setMasterInterruptError] = useState<string | null>(null)
  // The 15 s wait for the interrupt's and the steer's receipts is the request tracker's (S2-B): a receipt
  // that never comes turns the request unknown there, and the tracker's onChange words it beside the control.
  // A steer belongs to one turn. Its receipt is matched by request id, and everything about it is dropped the moment
  // that turn is no longer the running one (returned, disconnected, another project).
  const [masterSteerPending, setMasterSteerPending] = useState(false)
  const [masterSteerNote, setMasterSteerNote] = useState<string | null>(null)
  // 来源定位 (4.7 A13): the page of a cited file the side panel last asked for, and what came back.
  const [masterSourcePreview, setMasterSourcePreview] = useState<SourcePreviewState | null>(null)
  // 取消 the server refused, counted per file (attachmentCancelKey): a refusal makes that file's 取消 usable again.
  const [masterCancelRefusals, setMasterCancelRefusals] = useState<Readonly<Record<string, number>>>({})
  const masterSteerRef = useRef<{requestId: string; sessionId: string} | null>(null)
  // 施工卡片 (总控施工模式 B2, policy; lib/masterBuild): per build job, the last step master_build_activity
  // said -- kept while the job runs, dropped when it leaves running --; the card's 停下 / 插话 (one live build per
  // project, B1), whose receipts are matched by request id and session like 总控's own; and what each answer to a
  // build's question came back with, by checkpoint. All of it is the project's (currentProject: masterBuild).
  const [masterBuildActivity, setMasterBuildActivity] = useState<BuildActivities>({})
  const [buildControl, setBuildControl] = useState<BuildControl>(IDLE_BUILD_CONTROL)
  const buildInterruptRef = useRef<{requestId: string; sessionId: string} | null>(null)
  const buildSteerRef = useRef<{requestId: string; sessionId: string} | null>(null)
  const [buildAnswers, setBuildAnswers] = useState<BuildAnswerMarks>({})
  // 总控对话栏 (policy, lib/masterThreads): each project's conversations as last heard (by the
  // project; a list of another project is kept under its own id, so nothing leaks across a switch), the projects whose
  // list was refused, the note a new conversation opens with, a first send waiting for its conversation to be made,
  // the project whose chat history the console holds (a named conversation is "opening" until it is in), and the
  // column: collapsed for this viewer (browser storage, guarded), open as a drawer at narrow widths.
  const [masterThreads, setMasterThreads] = useState<ThreadsByProject>({})
  const [masterThreadsUnread, setMasterThreadsUnread] = useState<Readonly<Record<string, boolean>>>({})
  // An older server without conversations (it answers unknown_message_type): 总控 stays on the project's one
  // conversation and draws no column, as before the column existed.
  const [masterThreadsUnsupported, setMasterThreadsUnsupported] = useState(false)
  const [masterThreadNote, setMasterThreadNote] = useState<string | null>(null)
  const masterCreateRef = useRef<{ projectId: string; fresh: number; messages: Array<{ content: string; extras?: MasterSendExtras }> } | null>(null)
  // A first send whose new conversation never came back (no receipt, a refusal, the connection dropped) was not sent:
  // its words go back into 总控's box instead of vanishing with the cleared input.
  const [masterRestore, setMasterRestore] = useState<{ text: string; seq: number } | null>(null)
  const masterRestoreSeqRef = useRef(0)
  const giveBackFirstSend = (pending: { messages: Array<{ content: string }> } | null): void => {
    const text = (pending?.messages ?? []).map(message => message.content).filter(Boolean).join('\n\n')
    if (!text) return
    masterRestoreSeqRef.current += 1
    setMasterRestore({ text, seq: masterRestoreSeqRef.current })
  }
  const [masterHistoryFor, setMasterHistoryFor] = useState('')
  const [threadsCollapsed, setThreadsCollapsed] = useState<boolean>(() => loadColumnCollapsed(viewerStorage()))
  const [threadsDrawer, setThreadsDrawer] = useState(false)
  // The step comes only to the socket that started or continued the job: after a cut this console hears no more of it.
  useEffect(() => { if (status !== 'connected') setMasterBuildActivity(current => (Object.keys(current).length ? {} : current)) }, [status])
  const masterTurnId = masterTurn?.turnId ?? ''
  useEffect(() => {
    masterSteerRef.current = null
    setMasterSteerPending(false)
    setMasterSteerNote(null)
  }, [masterTurnId])
  useEffect(() => {
    if (status === 'connected') return
    masterInterruptRef.current = null
    setMasterInterruptError(null)
    masterTurnRef.current = null
    setMasterTurn(null)
    setMasterInterruptPending(false)
    setMasterInterruptRequested(false)
    // A first send waiting for its new conversation: the reply to the create will not come on this connection. The
    // words were not sent; nothing is re-sent on its own after the reconnect -- they go back into the box.
    const abandoned = masterCreateRef.current
    masterCreateRef.current = null
    if (abandoned) {
      giveBackFirstSend(abandoned)
      showToast(translateFor(localeRef.current)('master.threads.createFailed', {
        reason: translateFor(localeRef.current)('master.threads.connectionLost'),
      }), 'error', 8000)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status])
  // What the cost record holds for this whole project, including the runtime
  // auxiliary tasks that spend real money and never appear as sessions.
  // null means the record could not be read -- not that nothing was spent.
  const [recordedCost, setRecordedCost] = useState<{ totalUsd: number | null; calls: number }>(
    { totalUsd: null, calls: 0 },
  )
  const [recordedCostObservation, setRecordedCostObservation] = useState<ProjectCostObservation | null>(null)
  // By checkpoint id: what the last answer on that card came back with, in words, for the card to keep (decideBlocker;
  // 总控's rows, the inbox's 「现在」 block, the browser pane). Cleared when the same card is answered again; gone with the card.
  const [blockerDecisionErrors, setBlockerDecisionErrors] = useState<Record<string, string>>({})
  // How many project switches have settled -- each one re-asks for the pauses, because the reply to an ask made while
  // the switch was pending is dropped by the scope guard and the cards would otherwise wait for the 20 s poll.
  const [blockersReask, setBlockersReask] = useState(0)
  // 总控's title line (S3-3B; policy cut 总控's top to it): the owner's cross-project counts as the server last
  // said them (owner_attention_list, S3-3A), read by lib/masterToday in render. null = nothing came yet, or an older
  // server that does not know the action: the title line then counts nothing. Not a project-scoped key: the counts
  // span projects, and the reply is gated on the view generation alone.
  const [ownerTodayRaw, setOwnerTodayRaw] = useState<Record<string, unknown> | null>(null)
  const [ownerTodayError, setOwnerTodayError] = useState<string | null>(null)
  // Where a deep link's card lives, as checkpoint_locate answered ({project_id, order_id}; `found` false and both ''
  // when it could not be located): its order is opened in 业务工单, or 业务工单 stands on 等你处理 (the effect below).
  // policy: 总控 selects no card any more.
  const [cardLocate, setCardLocate] = useState<{ found: boolean; projectId: string; orderId: string } | null>(null)

  const normalizeProjectId = useCallback((value: unknown): string => {
    const projectId = typeof value === 'string' ? value.trim() : ''
    return projectId || 'default'
  }, [])

  const getActiveProjectId = useCallback(() => normalizeProjectId(activeProjectIdRef.current), [normalizeProjectId])
  // One answer path for every card the owner answers (lib/answerPath, policy): one answer out per card, a
  // stable request id per (card, answer), no re-send on its own after a timeout or a dropped connection, receipts of
  // another project dropped, every refusal in words. Made once; it reads the socket, the project and the locale
  // through refs. A gate decision that never gets its receipt tells the gate panel so (the socket has no promise
  // for gate_decide, so the path keeps that clock).
  const answerPathRef = useRef<AnswerPath | null>(null)
  if (!answerPathRef.current) {
    answerPathRef.current = new AnswerPath({
      send: (projectId, request) => {
        const client = clientRef.current
        if (!client) return Promise.resolve({ ok: false, accepted: false, error: 'socket_unavailable', checkpoint_id: request.checkpointId, checkpoint_type: request.checkpointType, client_request_id: request.clientRequestId })
        return client.interactionReply(projectId, request)
      },
      askList: (projectId) => { clientRef.current?.blockersList(projectId) },
      activeProjectId: () => getActiveProjectId(),
      locale: () => localeRef.current,
    }, {
      onLost: (_checkpointId, origin) => {
        if (origin !== 'gate') return
        setGateBusy(false)
        setGateError(refusalInWords(localeRef.current, 'gate.refused', 'request_timeout'))
      },
    })
  }
  const answerPath = answerPathRef.current
  const answering = useAnswering(answerPath)
  // 屏幕上的项目 (lib/currentProject, policy): which state belongs to the project on screen, the switch under
  // way and its generation, and the one gate every reply handler asks (accepts). Made once; it reads the active id
  // through the synchronous mirror. In dev builds it is reachable as window.__ampulyCurrentProject for its counters.
  const currentProjectRef = useRef<CurrentProject | null>(null)
  if (!currentProjectRef.current) {
    currentProjectRef.current = new CurrentProject({ activeProjectId: () => getActiveProjectId() })
    if (import.meta.env.DEV) (globalThis as { __ampulyCurrentProject?: CurrentProject }).__ampulyCurrentProject = currentProjectRef.current
  }
  const currentProject = currentProjectRef.current
  // Every other write goes out through one request tracker (lib/requestTracker, policy, candidate 6): busy
  // while its receipt is out (the button says 「发出去了，等回执」 and is disabled -- only then), timeout when no receipt
  // came in the kind's time, unknown when it never left or the connection was cut in between; never re-sent on its
  // own. A receipt settles it through the socket's ack observer; the ad-hoc flags the receipt handlers still clear
  // only narrow it (stillOut), they no longer keep a button waiting for ever. The card answers keep lib/answerPath as
  // their layer; a gate decision sits on both. The toast and the session store are reached through refs because they
  // are made further down.
  const trackerToastRef = useRef<(message: string, type: 'success' | 'error') => void>(() => undefined)
  const restoreRuntimeControlRef = useRef<(taskId: string, action: string) => void>(() => undefined)
  const requestTrackerRef = useRef<RequestTracker | null>(null)
  if (!requestTrackerRef.current) {
    requestTrackerRef.current = new RequestTracker({
      locale: () => localeRef.current,
      onChange: (entry) => {
        if (entry.state !== 'timeout' && entry.state !== 'unknown') return
        // A build card's 停下 / 插话 (B2) keep their notes on the card, in the same words; 总控's row is not touched.
        const buildTurnRequest = entry.action === 'harness_turn_steer' ? buildSteerRef.current : entry.action === 'harness_turn_interrupt' ? buildInterruptRef.current : null
        if (buildTurnRequest && buildTurnRequest.requestId === entry.id) {
          if (entry.action === 'harness_turn_steer') { buildSteerRef.current = null; setBuildControl(current => ({ ...current, steerRequestId: '', steerNote: entry.words })) }
          else { buildInterruptRef.current = null; setBuildControl(current => ({ ...current, interruptRequestId: '', interruptError: entry.words })) }
          if (entry.code !== 'connection_lost') trackerToastRef.current(entry.words, 'error')
          return
        }
        // 总控's steer and interrupt keep their notes beside the control; a late receipt for them is then ignored, as before.
        if (entry.action === 'harness_turn_steer') { masterSteerRef.current = null; setMasterSteerNote(entry.words) }
        if (entry.action === 'harness_turn_interrupt') { masterInterruptRef.current = null; setMasterInterruptError(entry.words) }
        // A stop or resume whose receipt never came gives its button back, so the person can look and click again.
        if (entry.action === 'session_stop' || entry.action === 'session_resume') restoreRuntimeControlRef.current(entry.id, entry.action)
        // A list read that waited too long is said on its page (待审批's 「没收到回执」 body), not toasted on every poll;
        // the cut is toasted once, below, for everything it cut.
        if (entry.kind !== 'list' && entry.code !== 'connection_lost') trackerToastRef.current(entry.words, 'error')
      },
    })
  }
  const requestTracker = requestTrackerRef.current
  const tracked = useTracked(requestTracker)
  // Receipts settle the tracker: the socket tells it about every ack. Attached again on each connection (the client
  // is made in an effect below; the status changing is when it exists).
  useEffect(() => {
    const client = clientRef.current
    if (!client) return
    return client.addAckObserver(payload => { requestTracker.receipt(payload) })
  }, [requestTracker, status])
  // The connection cut: whatever was out is unknown now, said once; the lists are asked for again after the
  // reconnect (recheckWorkflowsOnReconnectRef, onStatus) so the truth can be checked. Nothing is re-sent.
  useEffect(() => {
    if (status === 'connected') return
    const cut = requestTracker.disconnected()
    if (cut.length === 0) return
    recheckWorkflowsOnReconnectRef.current = true
    if (cut.some(entry => entry.action === 'gate_decide')) answerPath.settleOrigin('gate', 'connection_lost')
    trackerToastRef.current(cut[0].words, 'error')
  }, [answerPath, requestTracker, status])
  // The gate panel waits only while the tracker still has the decision out: a timeout or a cut gives 放行 / 打回 back.
  const gateDecideState = gateDetail ? stateOf(tracked, keyOf('gate_decide', gateDetail.checkpoint_id)) : 'idle'
  const gateWaiting = gateBusy && gateDecideState !== 'timeout' && gateDecideState !== 'unknown'
  // 员工页 (S3-6B): a decision or a plain reply that landed closes the open task, is listed under 「我交回去的」 and asks
  // for the list again. Only for an employee account: the owner's materials view keeps its own ending.
  employeeDecidedRef.current = (checkpointId, verb) => {
    if (authRef.current?.account?.role !== 'employee') return
    const open = myOpenRef.current
    // Only the task whose card was answered closes: a receipt for card A must not close card B opened meanwhile.
    if (open && open.checkpoint_id && open.checkpoint_id !== checkpointId) { clientRef.current?.myTasksList(getActiveProjectId()); return }
    setMyOpen(null)
    setMyReturned(prev => [{ key: `${checkpointId}-${verb}`, title: open?.node_name || open?.title || '', verb, at: new Date().toISOString() }, ...prev.filter(item => item.key !== `${checkpointId}-${verb}`)].slice(0, 30))
    clientRef.current?.myTasksList(getActiveProjectId())
  }
  // A customer's page is its orders across the console, so no project is named: the server lists every project's when
  // none is asked, and a customer has no project switch (a named project bound to another customer would be refused).
  const askCustomerOrders = useCallback(() => {
    clientRef.current?.send({ type: 'customer_orders_view' })
  }, [])
  // 员工页 / 客户页 (S3-6B): neither page has a refresh button and neither account receives the server's pushes
  // (broadcast goes to owners), so the page's one list is asked for again quietly while connected -- every 45 s, and
  // after every decision -- never faster, and never anything else.
  useEffect(() => {
    if (status !== 'connected') return
    const account = auth?.account
    const ask = isCustomerAccount(account) ? askCustomerOrders
      : account?.role === 'employee' ? () => { clientRef.current?.myTasksList(getActiveProjectId()) }
      : null
    if (!ask) return
    const timer = window.setInterval(ask, PERSON_PAGE_REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [askCustomerOrders, auth, getActiveProjectId, status])
  // 资料: the project's own files, by order and by folder. One place asks for them -- the dashboard tab -- so
  // nothing is read until somebody opens it.
  const askDataIndex = useCallback(() => {
    const pid = getActiveProjectId()
    if (!pid) return
    // A download link carries the session cookie, not a header; re-posting it keeps a 12 h cookie from expiring
    // under an open console.
    syncSessionCookie(readAuthToken())
    dispatchData({ kind: 'project', projectId: pid })
    dispatchFolder({ kind: 'project', projectId: pid })
    dispatchRecycle({ kind: 'project', projectId: pid })
    clientRef.current?.dataProjectIndex(pid)
    clientRef.current?.dataUsage(pid)
  }, [getActiveProjectId])
  // 客户记忆 sits in the 资料 tab, owner console only: read when the tab opens, and again from its 刷新.
  const askCustomerMemory = useCallback(() => {
    const pid = getActiveProjectId()
    if (!pid) return
    dispatchMemory({ kind: 'project', projectId: pid })
    dispatchMemory({ kind: 'load' })
    clientRef.current?.memoryOverview(pid)
  }, [getActiveProjectId])
  const requestCustomerPreview = useCallback((orderId: string) => {
    if (!isOwnerConsole(auth) || currentProject.pendingProjectId) return
    const request = { projectId: getActiveProjectId(), orderId, requestId: newRequestId() }
    customerPreviewRequestRef.current = request
    setCustomerPreview({ request, loading: true, error: null, view: null })
    const disposition = clientRef.current?.customerViewPreview(request.projectId, orderId, request.requestId)
    if (disposition !== 'sent') {
      customerPreviewRequestRef.current = null
      setCustomerPreview({ request, loading: false, error: 'failed', view: null })
    }
  }, [auth, getActiveProjectId])

  const refreshOrderList = useCallback(() => {
    clientRef.current?.orderList(getActiveProjectId(), orderScope)
    // Wherever the orders are asked for, so are their allowances: several roads into the orders page asked for the
    // list alone, and an order that had an allowance was then shown as having none, with a grant button beside it.
    clientRef.current?.runBudgetList(getActiveProjectId())
  }, [getActiveProjectId, orderScope])
  const refreshOrderListRef = useRef(refreshOrderList)
  refreshOrderListRef.current = refreshOrderList
  // 4.7-A08 「查看更早的工单」: the next older page, with the filters on screen. One request at a time.
  const loadOlderOrders = useCallback(() => {
    const paging = orderPagingRef.current
    if (!paging.nextCursor || paging.loading) return
    const sent = clientRef.current?.orderList(getActiveProjectId(), orderScope, paging.nextCursor)
    if (sent !== 'sent' && sent !== 'queued') return
    orderPagingRef.current = { ...paging, loading: true }
    setOrderPaging(orderPagingRef.current)
  }, [getActiveProjectId, orderScope])

  // The pushes about a project's sessions and chat (not replies to a request) are held to the project on screen only;
  // a switch under way does not hold them back, since the stores they feed are replaced whole when it settles.
  const payloadMatchesActiveProject = useCallback((payload: Record<string, unknown> | null | undefined, allowMissing = false): boolean => {
    const projectId = projectIdOf(payload)
    if (!projectId) return allowMissing
    return projectId === getActiveProjectId()
  }, [getActiveProjectId])

  const shouldSuppressTaskNotFound = useCallback((payload: Record<string, unknown> | null | undefined): boolean => {
    if (!payload || payload.error !== 'task_not_found') return false
    if (!payloadMatchesActiveProject(payload, false)) return true
    const action = typeof payload.action === 'string' ? payload.action : ''
    if (action !== 'session_detail') return false
    const generation = viewGenerationOf(payload)
    if (generation !== null && generation !== currentProject.generation) return true
    const taskId = typeof payload.task_id === 'string' ? payload.task_id : ''
    if (!taskId) return true
    const sessions = sessionStoreRef.current?.sessions ?? []
    return !sessions.some(session => session.taskId === taskId)
  }, [currentProject, payloadMatchesActiveProject])

  const clearPendingSessionDetailRefreshes = useCallback(() => {
    for (const tid of pendingSessionDetailRefreshRef.current.values()) clearTimeout(tid)
    pendingSessionDetailRefreshRef.current.clear()
  }, [])

  const scheduleSessionDetailRefresh = useCallback((taskId: string, detailLevel: 'summary' | 'full' = 'full', force = false, projectId?: string) => {
    if (!taskId) return
    const scopedProjectId = projectId || getActiveProjectId()
    const generation = currentProject.generation
    // Check relevance using latest ref; skip non-live sessions unless forced.
    // Re-check inside the timer so that a session that *becomes* active during
    // the debounce window still gets its detail loaded.
    if (!force && !shouldRefreshLiveSession(taskId, sessionStoreRef.current)) return
    const timerKey = `${scopedProjectId}:${generation}:${taskId}`
    const gate = sessionDetailRefreshGateRef.current
    // A refresh already waiting for this session absorbs this one. It used to be pushed back instead, and a busy
    // session sent one more full read for every message and status change (: a core at 100%).
    gate.want(timerKey, detailLevel, force)
    if (pendingSessionDetailRefreshRef.current.has(timerKey)) return
    const fire = () => {
      pendingSessionDetailRefreshRef.current.delete(timerKey)
      if (scopedProjectId !== getActiveProjectId()) return
      if (generation !== currentProject.generation) return
      if (gate.isInFlight(timerKey)) {
        // The previous read of this session is still unanswered: look again shortly; the wish stays merged.
        pendingSessionDetailRefreshRef.current.set(timerKey, setTimeout(fire, gate.delayFor(timerKey, Date.now())))
        return
      }
      const wish = gate.take(timerKey)
      if (!wish) return
      // Re-check liveness at fire time (ref may have been updated by now)
      if (!wish.force && !shouldRefreshLiveSession(taskId, sessionStoreRef.current)) return
      const client = clientRef.current
      sessionStoreRef.current?.updateSession(taskId, {
        detailLoading: true,
        detailError: undefined,
        viewGeneration: generation,
      })
      if (!client) {
        sessionStoreRef.current?.updateSession(taskId, {
          detailLoading: false,
          detailError: 'connection_unavailable',
        })
        return
      }
      gate.markSent(timerKey, Date.now())
      // However the request ends, the session leaves "in flight". sessionDetail can also throw before anything goes
      // out (a missing project id); that used to leave the session in flight for good, looked at again every second.
      let requested = false
      try {
        void client.sessionDetail(scopedProjectId, taskId, {
          limit: 200,
          detailLevel: wish.detailLevel,
          include: wish.detailLevel === 'full'
            ? ['messages', 'session_state', 'progress', 'work_items', 'runtime_context']
            : ['messages', 'session_state'],
          viewGeneration: generation,
        }).then((payload) => {
          // Answered, failed or timed out: the next refresh of this session may go.
          gate.markSettled(timerKey)
          // Transport-local failures resolve the request Promise without
          // producing a websocket ACK.
          if (payload.ok !== false) return
          if (scopedProjectId !== getActiveProjectId()) return
          if (generation !== currentProject.generation) return
          sessionStoreRef.current?.updateSession(taskId, {
            detailLoading: false,
            detailError: String(payload.error ?? 'request_failed'),
          })
        }, () => {
          // Rejected inside the client: nothing will answer it either.
          gate.markSettled(timerKey)
          sessionStoreRef.current?.updateSession(taskId, { detailLoading: false, detailError: 'request_failed' })
        })
        requested = true
      } finally {
        if (!requested) {
          gate.markSettled(timerKey)
          sessionStoreRef.current?.updateSession(taskId, { detailLoading: false, detailError: 'request_failed' })
        }
      }
    }
    // The first refresh of a session waits the usual 180 ms; the next one waits until 5 s after the last went out.
    pendingSessionDetailRefreshRef.current.set(timerKey, setTimeout(fire, gate.delayFor(timerKey, Date.now())))
  }, [getActiveProjectId])

  // One toast at a time, and each gets its own three seconds: the timer of the toast before it used to run on and
  // clear the newer one early -- an error shown right after a success could vanish before anybody read it.
  const toastTimerRef = useRef<number | null>(null)
  const safetyToastUntilRef = useRef(0)
  // A toast that carries something the owner has to act on -- a path they still have to visit -- is given longer
  // than the three seconds a 「已保存」 needs.
  const showToast = useCallback((msg: string, type: 'success' | 'error' = 'success', ms: number = 3000) => {
    setToastMessage(msg); setToastType(type)
    if (toastTimerRef.current !== null) window.clearTimeout(toastTimerRef.current)
    toastTimerRef.current = window.setTimeout(() => { toastTimerRef.current = null; setToastMessage(null) }, ms)
    if (type === 'error') safetyToastUntilRef.current = Date.now() + ms
  }, [])
  // The request tracker (above) words a timeout or a cut through the same toast.
  trackerToastRef.current = showToast

  const clearPendingSessionCreate = useCallback(() => {
    if (pendingSessionCreateTimerRef.current) {
      clearTimeout(pendingSessionCreateTimerRef.current)
      pendingSessionCreateTimerRef.current = null
    }
    pendingSessionCreateRef.current = false
    pendingSessionCreateProjectIdRef.current = null
  }, [])
  // Nothing in the console asks for a session of its own any more (policy, R1 option B: 新建会话 and
  // "continue in a new chat" left with the free sessions), so nothing begins a pending create; the clear above stays
  // for the refusal and session_created paths that still call it.

  // ── Store hooks: MUST be declared before WebSocket effect so refs are available ──
  const boardStore = useBoardStore()
  const chatStore = useChatStore()
  const sessionStore = useSessionStore()
  const projectStore = useProjectStore()
  useEffect(() => { setArtifactQuoteDraft(null) }, [projectStore.activeProjectId, auth?.account?.login, auth?.required])
  useLayoutEffect(() => {
    observatoryTrace.setProject(projectStore.activeProjectId)
    return () => { observatoryTrace.setProject(''); observatoryHistory.cancel() }
  }, [projectStore.activeProjectId, observatoryTrace, observatoryHistory])
  useLayoutEffect(() => { clearCustomerPreview() }, [projectStore.activeProjectId, clearCustomerPreview])
  // Remembered only once the project index is in: before that the store holds its placeholder, and writing it would
  // forget the project this browser was really in.
  useEffect(() => { if (projectsHydratedRef.current) writeLastProjectId(projectStore.activeProjectId) }, [projectStore.activeProjectId])
  // 施工卡片 (B2): a job whose latest message says its turn ended loses its last step, so a job continued later never
  // shows the old turn's step before the new turn's first one comes in (lib/masterBuild settledBuildJobs).
  // 总控对话栏: a build posts in the conversation that started it, so every conversation of the project is read.
  const masterChannelMessages = useMemo(() => chatStore.messages.filter(message => isProjectMasterChannel(message.channelId, projectStore.activeProjectId)),
    [chatStore.messages, projectStore.activeProjectId])
  const settledBuilds = useMemo(() => settledBuildJobs(masterChannelMessages).join('\n'), [masterChannelMessages])
  useEffect(() => {
    if (settledBuilds) setMasterBuildActivity(current => dropSettledActivity(current, settledBuilds.split('\n')))
  }, [settledBuilds])
  const externalTeamActivityStore = useExternalTeamActivityStore()
  // resetExternalTeamProject drops every cached team-activity record and selected-invocation pointer that does NOT
  // belong to the project passed in. It is the `teamActivity` reset of lib/currentProject (handed in below), so it
  // runs on every road a switch begins by -- an earlier standalone "reset project scoped view" helper had zero call
  // sites and silently held the only reset of this cache while the panel kept rendering the previous customer's team;
  // lib/currentProject.test.ts holds every scoped key to its reset.
  const {
    resetProject: resetExternalTeamProject,
    markLoading: markExternalTeamLoading,
    applyEvent: applyExternalTeamEvent,
    applySnapshot: applyExternalTeamSnapshot,
  } = externalTeamActivityStore

  // Per-session recruitment (role_id -> recruited names) for the org canvas.
  // The org_info payload is project-global, so the canvas otherwise can't show
  // the *selected* session's hires; this derives them from that session's chat
  // recruitment checkpoint (display-only — no backend change). Null when the
  // selected session has no recruitment loaded -> canvas falls back to global.
  const activeSessionId = sessionStore.activeSessionId
  const sessionRecruitmentByRole = useMemo(() => {
    if (!activeSessionId) return null
    return extractSessionRecruitmentByRole(chatStore.getChannelMessages(sessionChannelId(activeSessionId)))
  }, [activeSessionId, chatStore])

  // What belongs to the project on screen, and how each of it is emptied when a switch to another project begins
  // (lib/currentProject PROJECT_SCOPED_KEYS: the type holds every key to a reset). Handed in on every render so the
  // resets read the setters of this render; all of them are stable. Busy flags of writes in flight are not here: a
  // switch settling clears them where it always did.
  currentProject.own({
    customerPreview: () => clearCustomerPreview(),
    masterTurn: () => {
      masterInterruptRef.current = null
      setMasterInterruptError(null)
      masterTurnRef.current = null
      setMasterTurn(null)
      setMasterInterruptPending(false)
      setMasterInterruptRequested(false)
      setTurnLedger(null)
    },
    masterTools: () => setMasterToolActivity([]),
    masterBuild: () => {
      buildInterruptRef.current = null
      buildSteerRef.current = null
      setBuildControl(IDLE_BUILD_CONTROL)
      setMasterBuildActivity({})
      setBuildAnswers({})
    },
    pendingTurns: () => { setPendingTurns([]); setPendingTurnError(null) },
    blockers: () => { setBlockers([]); setPendingApprovalBadgeState(null) },
    blockerDecisionErrors: () => setBlockerDecisionErrors({}),
    answering: () => answerPath.forgetAll(),
    sessionDetailRefreshes: () => clearPendingSessionDetailRefreshes(),
    orders: () => {
      // Project monetary observations share the synchronous project reset.
      setRecordedCost({ totalUsd: null, calls: 0 })
      setRecordedCostObservation(null)
      setOrders(null)
      setOrdersReadProjectId(null)
      setOrdersTruncated(false)
      orderPagingRef.current = NO_ORDER_PAGING
      setOrderPaging(NO_ORDER_PAGING)
      setDistributionReply(null)
    },
    orderMetrics: () => { setOrderMetrics([]); setOrderMetricsTruncated(false); setOrderUsage([]) },
    orderTimeline: () => { setOrderTimeline(null); setOrderTimelineError(null); setOrderModelPlan(null) },
    workflows: () => { setWorkflows(null); setWorkflowError(null); setWorkflowsReadProjectId(null) },
    gateDetail: () => { setGateDetail(null); setGateError(null) },
    runBudgets: () => { setRunBudgets(null); setPendingReservations([]) },
    browserSessions: () => { clearBrowserAccountInspection(); browserSessionsRef.current = []; browserViewRequestRef.current = null; browserHandoffNoticesRef.current.clear(); setBrowserSessions([]); setBrowserLiveFrame(null); setBrowserLiveBinding(null); setBrowserPickedElement(null); setBrowserSessionError(null); setBrowserCandidateNote(null); setBrowserSealedDemo(null) },
    // Governance policy and candidates belong to the installation, not the project: they stay; only what this
    // console said about the previous project's actions is dropped.
    governanceError: () => setGovernanceError(null),
    restorePreflight: () => { setRestorePreflight(null); setRestorePreflightError(null) },
    processSpec: () => {
      selectedSpecRequest.current = { specId: '', version: '' }
      setSelectedSpec(''); setProcessDraft(null); setSpecReview(null); setBuilderRounds([])
    },
    executionPanel: () => {
      setExecutionPanelTaskId(null); setExecutionPanelRuntimeOnly(false)
      observatoryTrace.setProject(''); observatoryHistory.cancel()
    },
    teamActivity: (nextProjectId) => resetExternalTeamProject(nextProjectId),
    comms: () => { setCommsState(null); setCommsMessage(null) },
    preflight: () => setWorkflowPreflight(null),
    nodeTrials: () => {
      setWorkflowNodeTrials([])
      setWorkflowFlowTrials([])
      setPublishedFlowTrials({ workflowId: '', rows: [], error: null })
      flowTrialRequestIds.current = {}
      setNodeTrialPinning(null)
      setRefusedSave(null)
      trialArtifactWantedRef.current = null
      setTrialArtifact({ preview: null, loading: false, error: null })
      if (workflowNodeTrialPollRef.current) clearTimeout(workflowNodeTrialPollRef.current)
      workflowNodeTrialPollRef.current = null
      if (workflowFlowTrialPollRef.current) clearTimeout(workflowFlowTrialPollRef.current)
      workflowFlowTrialPollRef.current = null
      setAdmissionRefusal(null)
    },
    artifacts: () => { setArtifactIndex(null); setArtifactPreview(null); setArtifactError(null); setArtifactPreviewError(null) },
    projectPanorama: () => { setProjectProfile(null); setProjectProfileState(EMPTY_READ); setProjectConnections(null); setProjectConnectionsState(EMPTY_READ) },
  })

  // What a settled switch asks for again (lib/currentProject REISSUE_ON_SETTLE), by name: the pauses (the answer to
  // the ask made while the switch was pending was dropped by the gate, and 待审批 would spin until the 20 s poll --
  // lib/approvalsLoad.ts), the owner's browser sessions (X05: a page handed to a person is still there after a
  // reload) and the quiet re-ask of the page's lists (lib/restoredPageLists). Read through a ref: it runs from the
  // socket's handlers, which are built once.
  const reissueOnSettle: Record<ReissueRequest, (projectId: string) => void> = {
    blockers: () => setBlockersReask(count => count + 1),
    browserSessions: (projectId) => { if (isOwnerConsole(authRef.current)) clientRef.current?.browserSessionList(projectId) },
    pageLists: (projectId) => { if (isOwnerConsole(authRef.current)) requestOwnerPageListsRef.current(projectId) },
  }
  const reissueOnSettleRef = useRef(reissueOnSettle)
  reissueOnSettleRef.current = reissueOnSettle

  // Published versions belong to the selected flow/project. Reconnect re-reads the catalog; it never repeats a write.
  useLayoutEffect(() => {
    const workflowId = builderFlow?.id ?? ''
    setPublishedFlowTrials({ workflowId, rows: [], error: workflowId ? '正在读取已发布版本…' : null })
    if (workflowId && status === 'connected' && isOwnerConsole(auth) && !currentProject.pendingProjectId) {
      clientRef.current?.workflowFlowTrialList(getActiveProjectId(), workflowId)
    }
  }, [builderFlow?.id, builderFlow?.version, projectStore.activeProjectId, status, isOwnerConsole(auth)])

  // 总控's counts read (S3-3B): the request tracker's list read (5 s, then 「没收到回执」), the owner's only, asked with the
  // view generation because its reply is gated on that alone -- the counts span projects, so no project id could gate
  // them. Polled with the pauses (below), asked again on opening 总控 and after every card answer.
  const askOwnerToday = useCallback(() => {
    if (!isOwnerConsole(authRef.current)) return
    requestTracker.forget('owner_attention_list')
    requestTracker.track('owner_attention_list', () => clientRef.current?.ownerAttentionList(getActiveProjectId(), currentProject.generation, TODAY_FETCH_LIMIT) ?? 'send-failed', { kind: 'list' })
  }, [currentProject, getActiveProjectId, requestTracker])
  const askOwnerTodayRef = useRef(askOwnerToday)
  askOwnerTodayRef.current = askOwnerToday
  // ...and its reply read into the title line's counts (lib/masterToday). Nothing is counted here. A hook, so it sits
  // above the sign-in and employee returns. The reply's rows are not read: the 今天 block they filled left 总控
  // (policy) -- those orders are handled in 业务工单 and on the Feishu cards.
  const todayView = useMemo(() => ({
    counts: todayCountsFrom(ownerTodayRaw),
    partial: todayPartial(ownerTodayRaw),
  }), [ownerTodayRaw])

  // Every road a switch begins by (the selector, 项目中心, a deep link, a deleted or archived project) comes through
  // here: the one reset and the switch's bookkeeping are lib/currentProject's; this only remembers that the owner
  // chose, and says so in the status line.
  const beginProjectSwitch = useCallback((projectId: string): string => {
    userSelectedProjectRef.current = true
    const begun = currentProject.beginSwitch(projectId)
    setStatusDetail(`Switching to ${begun.projectId}...`)
    return begun.switchSeq
  }, [currentProject])

  // 总控对话栏 (policy, lib/masterThreads). The owner's console only: the conversations are the
  // owner's (the server answers owner_required otherwise), and an older server without them keeps 总控 as it was.
  const masterThreadsOn = isOwnerConsole(auth) && !masterThreadsUnsupported
  // Where the console stands, for the open conversation (read in render: the switch under way is lib/currentProject's).
  const masterStand = { activeProjectId: projectStore.activeProjectId, pendingProjectId: currentProject.pendingProjectId, historyProjectId: masterHistoryFor }
  // The project on screen's conversations, asked for once it has settled and after every reconnect; the answers of
  // any later change come by themselves (the server re-sends the list to the socket that caused it).
  useEffect(() => {
    if (status !== 'connected' || !isOwnerConsole(auth) || currentProject.pendingProjectId || masterHistoryFor !== projectStore.activeProjectId) return
    clientRef.current?.masterThreadList(projectStore.activeProjectId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, isOwnerConsole(auth), projectStore.activeProjectId, masterHistoryFor])
  // The open conversation checked against what is known now: a switch that moved the console, the address's
  // conversation found (or gone / archived: a new one, with a note). Nothing moves before the project has settled.
  useEffect(() => {
    // Not before the console has opened on its real project: on a refresh the placeholder project (default) is on
    // screen for a moment before the project list lands and the console moves to the project it was last in. Checked
    // then, the address's conversation was pinned to the placeholder and dropped as soon as the console moved
    // ( live check: a refresh left the conversation for a new one).
    if (!projectsHydratedRef.current) return
    const resolved = resolveOpen(masterOpen, masterStand, masterThreads)
    if (resolved.open !== masterOpen) setMasterOpen(resolved.open)
    if (resolved.note) setMasterThreadNote(t(resolved.note === 'thread_archived' ? 'master.threads.wasArchived' : 'master.threads.gone'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [masterOpen, masterThreads, projectStore.activeProjectId, masterHistoryFor])
  // 安安's side panel on another page (P1: the project's most recently active conversation): the one that was latest
  // when the panel opened stays while it is open, so a line landing in another conversation does not swap it under the
  // owner's words. Closed, or on 总控, nothing is held.
  const [panelThreadId, setPanelThreadId] = useState('')
  useEffect(() => {
    const showing = assistantOpen && activePage !== 'master' && masterThreadsOn
    const list = masterThreads[projectStore.activeProjectId] ?? []
    const held = !!panelThreadId && list.some(item => item.threadId === panelThreadId && !item.archived)
    const next = !showing ? '' : held ? panelThreadId : (latestThread(list)?.threadId ?? '')
    if (next !== panelThreadId) setPanelThreadId(next)
  }, [assistantOpen, activePage, masterThreadsOn, masterThreads, projectStore.activeProjectId, panelThreadId])

  // Sync store refs via useLayoutEffect — runs synchronously after commit,
  // BEFORE any async callbacks (WebSocket messages) can fire.
  // This eliminates the race condition where handlers read null refs.
  const storesReadyRef = useRef(false)
  useLayoutEffect(() => {
    chatStoreRef.current = chatStore
    boardStoreRef.current = boardStore
    sessionStoreRef.current = sessionStore
    projectStoreRef.current = projectStore
    kanbanCreateRef.current = (data) => {
      if (!boardStore.activeBoardId || boardStore.activeBoardColumns.length === 0) return
      const todoCol = boardStore.activeBoardColumns.find(c => c.name === 'Todo')
      const colId = todoCol?.id ?? boardStore.activeBoardColumns[0].id
      boardStore.createTask({
        boardId: boardStore.activeBoardId,
        columnId: colId,
        title: data.title,
        priority: data.priority,
        assigneeIds: data.assignee_id ? [data.assignee_id] : [],
      })
    }
    storesReadyRef.current = true
  })

  useEffect(() => {
    swarmAgentsRef.current = swarmAgents
  }, [swarmAgents])

  // Refresh the deliverable index whenever the console opens or the project
  // changes: roles write files between visits, so a stale list is misleading.
  useEffect(() => {
    // 业务工单 reads the order's own folder from the same index (交付物, one source); 执行区's 文件 tab asks for it when
    // it is opened (openExecution).
    if (!['dashboard', 'orders'].includes(activePage) || status !== 'connected' || !isOwnerConsole(auth)) return
    setArtifactLoading(true)
    setArtifactError(null)
    clientRef.current?.artifactsList(projectStore.activeProjectId)
  }, [activePage, status, projectStore.activeProjectId, isOwnerConsole(auth)])

  // 项目采用业务流: read on entering the project centre and on switching project; the owner's only.
  useEffect(() => {
    if (activePage !== 'dashboard' || status !== 'connected' || !isOwnerConsole(auth)) return
    setProjectAdoptions(null)
    clientRef.current?.projectWorkflowAdoptions(projectStore.activeProjectId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePage, status, projectStore.activeProjectId, isOwnerConsole(auth)])

  // 人员与职责 · 联系方式 and the builder's 已绑定 hint (4.7-A07): the contact book, read where it is shown; the owner's only.
  useEffect(() => {
    if (!['dashboard', 'workflows'].includes(activePage) || status !== 'connected' || !isOwnerConsole(auth)) return
    clientRef.current?.contactList(projectStore.activeProjectId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePage, status, projectStore.activeProjectId, isOwnerConsole(auth)])
  // 去绑定 asks for 人员与职责 once: leaving the project page drops the ask, so the next visit opens on 概览 as always.
  useEffect(() => {
    if (activePage !== 'dashboard') setPeopleTabRequest(null)
  }, [activePage])

  useEffect(() => {
    if (status === 'connected') return
    setArtifactLoading(false)
    setArtifactPreviewLoading(false)
  }, [status])

  // Feishu's "完整审计" button carries the exact project/checkpoint.  Once
  // the project index and the scoped sync are ready, land in 业务工单 and
  // open the order that card lives on (policy: 总控 lists no cards
  // any more), or stand on 等你处理 when no order can be found for it.  A
  // retired page key (S3-7) lands where lib/shellRoute says: 业务工单 on
  // 等你处理, 总控 with 执行区 open over the run a workspace link names, or
  // 配置's section.  The URL is only navigation state; the server still
  // authenticates and scope-checks the locate request.
  useEffect(() => {
    const link = deepLinkRef.current
    if (deepLinkOpened || status !== 'connected' || !projectsHydratedRef.current) return
    if (link.kind === 'none') {
      // A plain console load carries no deep-link state (readConsoleDeepLink
      // returned its fallback).  It must land on the normal home page; only a
      // URL that actually named a card or a retired page may redirect.
      setDeepLinkOpened(true)
      return
    }
    if (link.projectId && !projectStore.projects.some(project => project.id === link.projectId)) {
      // A deep link must never be used to probe an unknown project.  The
      // scoped project index is the authority; land on the page without
      // issuing a cross-project request when it is absent -- a card's link on
      // 业务工单's 等你处理, since its order cannot be looked for.
      setActivePage(link.page)
      if (link.checkpointId) setCardLocate({ found: false, projectId: '', orderId: '' })
      setDeepLinkOpened(true)
      return
    }
    if (link.projectId && getActiveProjectId() !== link.projectId) {
      if (currentProject.pendingProjectId !== link.projectId) {
        const switchSeq = beginProjectSwitch(link.projectId)
        clientRef.current?.switchProject(link.projectId, switchSeq)
      }
      return
    }
    if (currentProject.pendingProjectId) return
    setActivePage(link.page)
    if (link.checkpointId) {
      // Which order the card lives on is the server's to say (checkpoint_locate): its answer opens that order in
      // 业务工单 (the effect on cardLocate). A locate that did not go out leaves 业务工单 on 等你处理 at once.
      const sent = clientRef.current?.checkpointLocate(getActiveProjectId(), link.checkpointId)
      if (sent !== 'sent' && sent !== 'queued') setCardLocate({ found: false, projectId: '', orderId: '' })
    }
    if (link.landing?.section) setConfigEntry(link.landing.section)
    // ?page=workspace&task_id=… (S3-7): 执行区's side window over that run, on 记录. The session is looked for in
    // this project's store; the server scope-checks its detail request as it does any other.
    if (link.landing?.taskId) openExecutionRef.current(link.landing.taskId)
    setDeepLinkOpened(true)
  }, [beginProjectSwitch, deepLinkOpened, getActiveProjectId, projectStore.activeProjectId, projectStore.projects, status])

  useEffect(() => {
    if (!deepLinkOpened || !projectsHydratedRef.current || currentProject.pendingProjectId || !isOwnerConsole(auth)) return
    try {
      const next = searchForSettledProject(window.location.search, projectStore.activeProjectId)
      if (next !== window.location.search) window.history.replaceState(window.history.state, '', `${window.location.pathname}${next}${window.location.hash}`)
    } catch {
      // Embedded consoles may forbid history changes; the active project remains authoritative.
    }
  }, [deepLinkOpened, projectStore.activeProjectId, currentProject.pendingProjectId, isOwnerConsole(auth)])

  // The located card (policy): its order is opened in 业务工单 -- after a switch to its project when it is
  // another's, the inbox opening the order once that project's list is in (the road 打开这张单 always took). A card
  // that names no order, is gone, belongs to a project this console does not list, or that this server cannot locate
  // leaves 业务工单 on 等你处理, where what waits on the owner is: never an empty page, never a guess.
  useEffect(() => {
    if (!cardLocate) return
    setCardLocate(null)
    const { found, projectId, orderId } = cardLocate
    const listed = !projectId || projectId === getActiveProjectId() || projectStore.projects.some(project => project.id === projectId)
    if (!orderId || !listed) {
      if (found && !orderId) showToast(t('master.today.noOrder'), 'error')
      setOrderQueueRequest(current => ({ queue: 'all', state: 'mine', query: '', open: '', flow: '', seq: (current?.seq ?? 0) + 1 }))
      openPageRef.current?.('orders')
      return
    }
    if (projectId && projectId !== getActiveProjectId() && currentProject.pendingProjectId !== projectId) {
      const switchSeq = beginProjectSwitch(projectId)
      clientRef.current?.switchProject(projectId, switchSeq)
    }
    setOrderQueueRequest(current => ({ queue: 'all', query: '', open: orderId, flow: '', seq: (current?.seq ?? 0) + 1 }))
    openPageRef.current?.('orders')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cardLocate])

  // The one way to ask for the pauses: every ask restarts the "nothing back yet" clock -- the request tracker's list
  // read (lib/approvalsLoad.ts, 5 s); a read that never left (not connected) is 「没收到回执」 at once, never 正在加载.
  const askBlockers = useCallback((projectId: string) => {
    requestTracker.forget('blockers_list')
    requestTracker.track('blockers_list', () => clientRef.current?.blockersList(projectId) ?? 'send-failed', { kind: 'list' })
  }, [requestTracker])

  // Blockers are polled, not pushed: a run waiting on an approval emits no
  // events at all, so nothing would arrive to trigger a refresh. Polling is
  // what makes "paused, waiting for you" visible instead of looking like a
  // hang (: two stations stalled for tens of minutes this way).
  // It also asks again on (re)connect and each time a project switch settles
  // (blockersReask), so the first open no longer waits for the next poll.
  useEffect(() => {
    if (status !== 'connected' || !isOwnerConsole(auth)) return
    const projectId = projectStore.activeProjectId
    const poll = () => {
      askBlockers(projectId)
      // 总控's title-line counts (S3-3B) ride the same poll; the owner's only, so the poll restarts once the
      // console is known to be the owner's.
      askOwnerToday()
      clientRef.current?.dutyLogList(projectId)
    }
    poll()
    const timer = window.setInterval(poll, 20_000)
    return () => window.clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, projectStore.activeProjectId, blockersReask, askBlockers, askOwnerToday, isOwnerConsole(auth)])

  useEffect(() => {
    globalExecModeRef.current = globalExecMode
  }, [globalExecMode])

  // ── Runtime-delta coalescing ────────────────────────────────────────
  // assistant_delta / thinking_delta arrive at token frequency; writing each
  // one straight into the stores re-renders the whole app once per token.
  // Buffer them per task and flush at most every 80ms. Any non-delta event
  // for the same task flushes first, so store-write ordering (e.g. clearDraft
  // on turn boundaries) is preserved exactly.
  const pendingDeltaFlushRef = useRef<Map<string, {
    draftText: string
    draftIteration?: number
    draftTurnId?: string
    sessionPatch: Partial<import('./types/kanban').Session>
    kanbanPatch: Partial<KanbanTask>
  }>>(new Map())
  const deltaFlushTimerRef = useRef<number | null>(null)

  const flushPendingDeltas = useCallback((onlyTaskId?: string) => {
    const pending = pendingDeltaFlushRef.current
    if (pending.size === 0) return
    const ids = onlyTaskId ? [onlyTaskId] : Array.from(pending.keys())
    for (const taskId of ids) {
      const entry = pending.get(taskId)
      if (!entry) continue
      pending.delete(taskId)
      const ss = sessionStoreRef.current
      if (entry.draftText) {
        ss?.appendDraft(taskId, entry.draftText, entry.draftIteration, entry.draftTurnId)
      }
      ss?.updateSession(taskId, entry.sessionPatch)
      if (Object.keys(entry.kanbanPatch).length > 0) {
        boardStoreRef.current?.updateTask(taskId, entry.kanbanPatch)
      }
    }
    if (pending.size === 0 && deltaFlushTimerRef.current !== null) {
      window.clearTimeout(deltaFlushTimerRef.current)
      deltaFlushTimerRef.current = null
    }
  }, [])

  const scheduleDeltaFlush = useCallback(() => {
    if (deltaFlushTimerRef.current !== null) return
    deltaFlushTimerRef.current = window.setTimeout(() => {
      deltaFlushTimerRef.current = null
      flushPendingDeltas()
    }, 80)
  }, [flushPendingDeltas])

  useEffect(() => {
    // One server change arrives as several pushes; together they ask for one full sync (lib/collabSyncCoalescer.ts).
    const pushedCollabSync = new CollabSyncCoalescer(() => {
      clientRef.current?.collabSync(getActiveProjectId(), undefined, currentProject.generation)
    })
    const client = new VisualSocketClient(wsUrl, {
      onAuth: (payload) => {
        const next: AuthState = {
          required: payload.required === true,
          bootstrap_needed: payload.bootstrap_needed === true,
          account: (payload.account as unknown as AuthAccount | null) ?? null,
        }
        authRef.current = next
        quietPrincipalRef.current = next.account?.login?.trim()
          ? `account:${next.account.login}` : (!next.required && !next.bootstrap_needed ? 'local-owner' : '')
        let quiet = false
        try { quiet = loadQuietCompletion(window.localStorage, quietPrincipalRef.current) } catch { quiet = false }
        quietCompletionRef.current = quiet
        setQuietCompletion(quiet)
        setQuietSaveFailed(false)
        setPendingApprovalBadgeState(null)
        setAuth(next)
        if (!isOwnerConsole(next)) { observatoryTrace.setProject(''); observatoryHistory.cancel() }
        setAuthBusy(false)
        if (next.required && !next.account) {
          const token = readAuthToken()
          if (token) client.authToken(getActiveProjectId(), token)
        }
        if (isCustomerAccount(next.account)) {
          // 客户页 (S3-6B): the account's own orders across the console, as the customer projection (no project named:
          // a customer has no project switch); nothing else is asked for a customer.
          client.send({ type: 'customer_orders_view' })
        } else if (next.account?.role === 'employee') {
          client.myTasksList(getActiveProjectId())
        }
        // X05: a page handed to a person must still be there after a reload -- the list says so, and the pane reopens from it.
        if (isOwnerConsole(next)) client.browserSessionList(getActiveProjectId())
        if (isOwnerConsole(next)) requestOwnerPageListsRef.current(getActiveProjectId())
        // Transport-open is not authentication. Bootstrap protected reads once this socket's auth says owner
        // (including the no-account local console), and again after any reconnect/sign-in.
        const signedInOwner = isOwnerConsole(next)
        if (signedInOwner && !ownerSignedInRef.current) {
          const projectId = getActiveProjectId()
          client.listProjects()
          client.orgInfo()
          client.orgSavedList()
          client.collabSync(projectId, undefined, currentProject.generation)
          if (recheckWorkflowsOnReconnectRef.current) {
            recheckWorkflowsOnReconnectRef.current = false
            client.workflowList(projectId)
            refreshOrderListRef.current()
          }
          if (trialsSeenRef.current) client.skillTrialList(projectId)
        }
        ownerSignedInRef.current = signedInOwner
      },
      onSnapshot: (data) => {
        if (!currentProject.acceptsSwitchReply(data as unknown as Record<string, unknown>)) return
        setSnapshot(data)
        const timeline = data.timeline.slice(-MAX_LOG_ITEMS)
        eventTimeline.replace(timeline)
        const traceProjectId = String(data.project_id || getActiveProjectId())
        observatoryTrace.setProject(traceProjectId)
        observatoryTrace.replace(data.timeline, traceProjectId)

        const ids = new Set<string>()
        for (const evt of timeline) ids.add(evt.event_id)
        replayedEventIds.current = ids

        const agentEntries = Object.entries(data.agents ?? {})
        if (agentEntries.length > 0) {
          const infos = mapAgentListPayload(
            agentEntries.map(([id, info]) => ({
              ...((info && typeof info === 'object') ? info as Record<string, unknown> : {}),
              agent_id: id,
            })),
            swarmAgentsRef.current,
          )
          swarmAgentsRef.current = infos
          setSwarmAgents(infos)
        }
        // Restore exec_mode / company_profile from snapshot (survives reconnection)
        if (data.exec_mode) setGlobalExecMode(normalizeExecMode(data.exec_mode))
        if (data.company_profile) setGlobalCompanyProfile(normalizeCompanyProfile(data.company_profile))
        if (data.task_preferred_agent) setGlobalTaskPreferredAgent(normalizeTaskPreferredAgent(data.task_preferred_agent))
        if (data.native_approval_default) {
          globalNativeApprovalDefaultRef.current = data.native_approval_default
          setGlobalNativeApprovalDefault(data.native_approval_default)
        }

      },
      onEvent: (evt) => {
        try {
          if (!payloadMatchesActiveProject(evt as unknown as Record<string, unknown>, false)) return
          if (replayedEventIds.current.has(evt.event_id)) {
            replayedEventIds.current.delete(evt.event_id)
            return
          }
          eventTimeline.append(evt)
          observatoryTrace.ingest(evt, getActiveProjectId())

          // task_routed: create kanban card only if task_id is provided (#6 — no title-matching)
          if (evt.type === 'task_routed' && evt.agent_id) {
            if (globalExecModeRef.current === 'company' || globalExecModeRef.current === 'org') {
              return
            }
            const taskId = evt.data?.task_id as string | undefined
            if (taskId && !boardStoreRef.current?.tasks.find(t => t.id === taskId)) {
              const preview = typeof evt.data?.content_preview === 'string'
                ? evt.data.content_preview.slice(0, 80)
                : t('common.task')
              kanbanCreateRef.current?.({
                title: preview,
                priority: null,
                assignee_id: evt.agent_id,
              })
            }
          }

          if ([
            'turn_started',
            'assistant_delta',
            'thinking_delta',
            'status_snapshot',
            'tool_started',
            'tool_progress',
            'tool_completed',
            'permission_requested',
            'permission_resolved',
            'cost_update',
            'context_usage',
            'context_warning',
            'subagent_started',
            'subagent_updated',
            'subagent_completed',
            'member_inbox_updated',
            'compaction_applied',
            'checkpoint_saved',
            'turn_completed',
            'turn_failed',
          ].includes(evt.type)) {
            const data = evt.data as Record<string, unknown>
            const taskId = typeof data.task_id === 'string' ? data.task_id : ''
            if (taskId) {
              const isDeltaEvent = evt.type === 'assistant_delta' || evt.type === 'thinking_delta'
              const bufferedDraftTurnId = pendingDeltaFlushRef.current.get(taskId)?.draftTurnId
              // Store-write ordering guarantee: everything buffered for this
              // task lands before a non-delta event. A matching terminal
              // message replaces the draft later; flushing here must not make
              // a completion boundary remove the visible turn prematurely.
              if (!isDeltaEvent) flushPendingDeltas(taskId)
              const ss = sessionStoreRef.current
              const bs = boardStoreRef.current
              const existingSession = ss?.sessions.find(session => session.taskId === taskId)
              const projectionId = typeof data.work_item_projection_id === 'string'
                ? data.work_item_projection_id
                : ''
              const executionMode = typeof data.execution_mode === 'string' ? data.execution_mode : ''
              const isTaskModeRuntime = executionMode === 'task_mode' || projectionId === 'task_mode_execution'
              // Drafts represent one logical assistant turn. The shared
              // resolver deliberately prefers conversation identity over the
              // iteration-scoped runtime turn id.
              const turnId = resolveCanonicalTurnId(data) || undefined
              const marksCompanyRuntime =
                !!projectionId && projectionId !== 'task_mode_execution' && !isTaskModeRuntime
              if (marksCompanyRuntime && !isDeltaEvent && existingSession?.isCompanyRuntime !== true) {
                ss?.setCompanyRuntime(taskId, true)
              }

              const runtimePartial: Partial<import('./types/kanban').Session> = {
                lastRuntimeEventType: evt.type,
                ...(evt.type === 'member_inbox_updated' ? {} : { updatedAt: Date.now() }),
                ...sessionRuntimePatchFromPayload(data),
              }
              const toolName = typeof data.tool_name === 'string' ? data.tool_name : undefined
              const kanbanPatch: Partial<KanbanTask> = {
                ...kanbanRuntimePatchFromPayload(data),
                // currentTool is active-only (clears between tools); displayTool
                // is sticky and only updates on a real, non-empty tool name.
                ...(toolName !== undefined ? { currentTool: toolName || undefined } : {}),
                ...(toolName ? { displayTool: toolName } : {}),
              }
              if (isDeltaEvent) {
                const pending = pendingDeltaFlushRef.current
                let entry = pending.get(taskId)
                const deltaText = evt.type === 'assistant_delta' && typeof data.text === 'string'
                  ? data.text
                  : ''
                // A turn boundary inside the buffer would corrupt the draft
                // reset logic (APPEND_DRAFT resets on turnId change) — flush
                // the previous turn's chunk before starting a new one.
                if (
                  entry && deltaText && entry.draftText &&
                  entry.draftTurnId !== undefined && turnId !== undefined &&
                  entry.draftTurnId !== turnId
                ) {
                  flushPendingDeltas(taskId)
                  entry = undefined
                }
                if (!entry) {
                  entry = { draftText: '', sessionPatch: {}, kanbanPatch: {} }
                  pending.set(taskId, entry)
                }
                if (deltaText) {
                  entry.draftText += deltaText
                  entry.draftIteration = typeof data.iteration === 'number'
                    ? data.iteration
                    : entry.draftIteration ?? existingSession?.draftIteration
                  if (turnId !== undefined) entry.draftTurnId = turnId
                }
                entry.sessionPatch = {
                  ...entry.sessionPatch,
                  ...runtimePartial,
                  ...(marksCompanyRuntime ? { isCompanyRuntime: true } : {}),
                }
                entry.kanbanPatch = { ...entry.kanbanPatch, ...kanbanPatch }
                scheduleDeltaFlush()
              } else {
                const activeDraftTurnId = String(
                  bufferedDraftTurnId ?? existingSession?.draftTurnId ?? '',
                ).trim()
                const startsNewCanonicalTurn = evt.type === 'turn_started'
                  && !!turnId
                  && !!activeDraftTurnId
                  && turnId !== activeDraftTurnId
                if (evt.type === 'turn_failed' || startsNewCanonicalTurn) {
                  ss?.clearDraft(taskId)
                }
                ss?.updateSession(taskId, runtimePartial)
                bs?.updateTask(taskId, kanbanPatch)
              }
              const skipDetailRefresh = (
                isTaskModeRuntime && TASK_MODE_LOW_VALUE_RUNTIME_EVENTS.has(evt.type)
              ) || SESSION_DETAIL_REFRESH_LOW_VALUE_RUNTIME_EVENTS.has(evt.type)
              if (evt.type !== 'assistant_delta' && !skipDetailRefresh) {
                scheduleSessionDetailRefresh(taskId)
              }
            }
          }

          if (evt.type === 'agent_removed' && evt.agent_id) {
            const agentId = evt.agent_id
            const cs = chatStoreRef.current
            if (cs) {
              cs.markSenderDeleted(agentId)
              cs.removeParticipant(agentId)
            }
          }

        } catch (e) { console.error('[onEvent] Error:', e, evt) }
      },
      onAck: (payload) => {
        observatoryHistory.receive(payload, getActiveProjectId())
        crmAcks.receive(payload)
        // The bounded inspector observes the existing ACK above this handler.
        // It owns correlation/timeout and never enters legacy decision paths.
        if (payload.action === 'human_review_rework_inspect') return
        if ((Object.values(EMPLOYEE_WORKFLOW_TRIAL_ACTIONS) as unknown[]).includes(payload.action)) {
          if (currentProject.accepts(payload) && payload.project_id === getActiveProjectId()
            && typeof payload.request_id === 'string' && typeof payload.ok === 'boolean')
            setEmployeeTrialAck(payload as unknown as EmployeeWorkflowTrialAck)
          return
        }
        if ((EMPLOYEE_ASSET_ACTIONS as readonly string[]).includes(String(payload.action ?? ''))) {
          employeeAssets.receive(payload)
          return
        }
        if (payload.action === 'customer_view_preview') {
          if (!currentProject.accepts(payload) || !acceptsCustomerPreview(payload, customerPreviewRequestRef.current, getActiveProjectId())) return
          const request = customerPreviewRequestRef.current!
          customerPreviewRequestRef.current = null
          const view = payload.ok === true ? parseCustomerPreviewView(payload.view, request.orderId) : null
          setCustomerPreview({ request, loading: false, error: view ? null : 'failed', view })
          return
        }
        try {
          if (payload.action === 'master_turn_activity') {
            if (!currentProject.accepts(payload)) return
            const sessionId = String(payload.session_id ?? '')
            const messageId = String(payload.message_id ?? '')
            const turnId = String(payload.turn_id ?? '')
            if (!sessionId || !messageId || !turnId || sessionId.length > 128 || messageId.length > 128 || turnId.length > 128) return
            if (payload.state === 'starting') {
              // Which conversation the turn is in (总控对话栏): its stop / steer row shows in that conversation only.
              const threadId = typeof payload.thread_id === 'string' ? payload.thread_id.trim() : null
              masterInterruptRef.current = null
              setMasterInterruptError(null)
              masterTurnRef.current = {sessionId, messageId, turnId, threadId}
              setMasterTurn({sessionId, messageId, turnId, threadId})
              setMasterInterruptPending(false)
              setMasterInterruptRequested(false)
            } else if (payload.state === 'returned' && masterTurnRef.current?.messageId === messageId
              && masterTurnRef.current.sessionId === sessionId && masterTurnRef.current.turnId === turnId) {
              masterInterruptRef.current = null
              setMasterInterruptError(null)
              masterTurnRef.current = null
              setMasterTurn(null)
              setMasterInterruptPending(false)
              setMasterInterruptRequested(false)
            }
            return
          }
          // A build card's 停下 (B2): its own receipt, matched by request id and session; the words are 总控's row's.
          if (payload.action === 'harness_turn_interrupt'
            && currentProject.accepts(payload)
            && acceptsInterruptReceipt(payload, buildInterruptRef.current)) {
            buildInterruptRef.current = null
            const requested = payload.ok === true && payload.requested === true
            setBuildControl(current => ({ ...current, interruptRequestId: '', interruptRequested: requested, interruptError: requested ? null : t('master.interruptFailed') }))
          }
          if (payload.action === 'harness_turn_interrupt'
            && currentProject.accepts(payload)
            && acceptsInterruptReceipt(payload, masterInterruptRef.current)
            && masterTurnRef.current?.sessionId === payload.session_id) {
            masterInterruptRef.current = null
            setMasterInterruptPending(false)
            setMasterInterruptRequested(payload.ok === true && payload.requested === true)
            setMasterInterruptError(payload.ok === true && payload.requested === true ? null : t('master.interruptFailed'))
          }
          // A build card's 插话 (B2): its own receipt, said in 总控's words (delivered is never "applied").
          if (payload.action === 'harness_turn_steer' && acceptsInterruptReceipt(payload, buildSteerRef.current)) {
            if (!currentProject.accepts(payload)) return
            buildSteerRef.current = null
            setBuildControl(current => ({ ...current, steerRequestId: '', steerNote: steerReceiptWords(payload, t) }))
            return
          }
          if (payload.action === 'harness_turn_steer') {
            if (!currentProject.accepts(payload)) return
            const expected = masterSteerRef.current
            // A receipt for a steer this console is not waiting on (an older turn, another tab) changes nothing here.
            if (!expected || payload.request_id !== expected.requestId || payload.session_id !== expected.sessionId) return
            masterSteerRef.current = null
            setMasterSteerPending(false)
            if (payload.ok === true && payload.delivery === 'accepted') {
              setMasterSteerNote(t('master.steerAccepted'))
            } else {
              const reason = String(payload.error ?? payload.reason ?? payload.detail ?? t('app.requestFailed'))
              const alternative = String(payload.alternative ?? '')
              setMasterSteerNote(`${t('master.steerNotConfirmed', { reason })}${alternative ? ` ${t('master.steerAlternative', { alternative })}` : ''}`)
            }
            return
          }
          if (payload.action === 'governance_policy_list' || payload.action === 'governance_policy_set') {
            // A refusal that carries no project (the dispatcher's own fallback) must still be seen; anything that
            // names a project is held to it like every other answer.
            if (!currentProject.acceptsOrBareRefusal(payload)) return
            if (payload.ok && payload.action === 'governance_policy_list') {
              setGovernancePolicy({
                profile: String(payload.profile ?? ''),
                policy: (payload.policy ?? {}) as Record<string, unknown>,
                writable: Array.isArray(payload.writable) ? payload.writable.map(String) : [],
                stored_only: Array.isArray(payload.stored_only) ? payload.stored_only.map(String) : [],
                stored_only_note: String(payload.stored_only_note ?? ''),
                changes: Array.isArray(payload.changes) ? payload.changes as GovernancePolicyView['changes'] : [],
              })
              setGovernanceError(null)
            } else if (payload.ok) {
              const unrecorded = String(((payload.change ?? {}) as Record<string, unknown>).unrecorded ?? '')
              if (unrecorded) {
                // The value did change; the note about it did not get written. Saying "recorded" would be a lie.
                setGovernanceError(t('capability.policy.changedUnrecorded', { field: String(payload.field ?? ''), reason: unrecorded }))
              } else {
                showToast(t('capability.policy.changed', { field: String(payload.field ?? '') }))
                setGovernanceError(null)
              }
              clientRef.current?.governancePolicyList(getActiveProjectId())
            } else {
              // The panel says why next to the switch; the generic failure toast below would only repeat it.
              setGovernanceError(String(payload.error ?? t('app.requestFailed')))
            }
            return
          }
          if (payload.action === 'browser_candidate_control') {
            if (!currentProject.accepts(payload)) return
            const command = String(payload.command ?? '')
            if (!payload.ok) {
              setBrowserCandidateNote(`${command || 'candidate'}: ${String(payload.error ?? t('app.requestFailed'))}`)
              return
            }
            if (command === 'list') {
              setBrowserCandidates(Array.isArray(payload.candidates) ? payload.candidates as BrowserCandidateRow[] : [])
            } else if (command === 'compile') {
              const effects = Array.isArray(payload.external_effect_steps) ? payload.external_effect_steps.length : 0
              setBrowserCandidateNote(locale.startsWith('zh')
                ? `已编译 ${String(payload.candidate_id ?? '')}：${Number(payload.steps ?? 0)} 步，其中 ${effects} 步是外部动作${effects ? `（步序号 ${(payload.external_effect_steps as unknown[]).join('、')}，准入时各需一条已批准的审批）` : ''}`
                : `compiled ${String(payload.candidate_id ?? '')}: ${Number(payload.steps ?? 0)} steps, ${effects} external-effect${effects ? ` (steps ${(payload.external_effect_steps as unknown[]).join(', ')}; each needs an approved approval at admission)` : ''}`)
              clientRef.current?.browserCandidateControl(getActiveProjectId(), 'list')
            } else if (command === 'admit') {
              setBrowserCandidateNote(locale.startsWith('zh')
                ? `已准入 ${String(payload.candidate_id ?? '')}（${String(payload.admitted_by ?? '')}）`
                : `admitted ${String(payload.candidate_id ?? '')} (${String(payload.admitted_by ?? '')})`)
              clientRef.current?.browserCandidateControl(getActiveProjectId(), 'list')
            }
            return
          }
          if (payload.action === 'restore_preflight') {
            if (payload.ok && payload.preflight && typeof payload.preflight === 'object') {
              const report = payload.preflight as Partial<RestorePreflightReport>
              setRestorePreflight({
                archive: String(report.archive ?? ''), archive_name: String(report.archive_name ?? ''),
                project_id: String(report.project_id ?? ''), taken_at: String(report.taken_at ?? ''),
                entries: Number(report.entries ?? 0), bytes: Number(report.bytes ?? 0),
                has_workplace: report.has_workplace === true, has_memory: report.has_memory === true,
                refusals: Array.isArray(report.refusals) ? report.refusals.map(String) : [],
                refusal_details: Array.isArray(report.refusal_details)
                  ? report.refusal_details.map(item => Object.fromEntries(Object.entries(item ?? {}).map(([key, value]) => [key, String(value ?? '')])))
                  : [],
                not_included: Array.isArray(report.not_included) ? report.not_included.map(String) : [],
                would_be_refused: report.would_be_refused === true,
                content_verified: report.content_verified === true,
                bound_customer_id: String(report.bound_customer_id ?? ''),
              })
              setRestorePreflightError(null)
              setRestoreBackupState({ phase: 'idle' })
            } else {
              setRestorePreflight(null)
              setRestorePreflightError(String(payload.error ?? t('app.requestFailed')))
            }
            return
          }
          if (payload.action === 'restore_backup') {
            // Not filtered by the open project: a restore names the project it brings back, never the open one.
            if (restoreBackupTimerRef.current) { clearTimeout(restoreBackupTimerRef.current); restoreBackupTimerRef.current = null }
            if (payload.ok) {
              const restored = String(payload.project_id ?? '')
              const notRestored = Array.isArray(payload.not_restored)
                ? (payload.not_restored as NotRestored[]).filter(item => item && item.member)
                : []
              setRestoreBackupState({ phase: 'done', projectId: restored, notRestored })
              setRestorePreflight(null)  // that report now describes a project that exists again
              showToast(t('project.restoreBackupDone', { project: restored }))
              clientRef.current?.listProjects()  // the receipt carries no project list
            } else {
              // The server's refusal is an English sentence; the owner reads the console's language. The code says
              // which refusal it is, and the sentence stays as the detail for the ones that carry specifics.
              const said = translateMaybe('project.restoreError', String(payload.code ?? ''))
              const raw = String(payload.error ?? t('app.requestFailed'))
              setRestoreBackupState({ phase: 'failed', error: said && said !== String(payload.code ?? '') ? `${said}（${raw}）` : raw })
            }
            return
          }
          if (payload.action === 'project_lifecycle') {
            // A trail nobody is looking at any more is dropped. A refusal names no project at all (the server's error
            // receipts carry only the action), and dropping those too left the panel on "loading" for ever.
            const named = String(payload.project_id ?? '')
            const open = lifecycleProjectIdRef.current
            if (open === null) return
            if (payload.ok ? named !== open : (named !== '' && named !== open)) return
            if (payload.ok) {
              setLifecycleTrail(normalizeLifecycleTrail(String(payload.project_id ?? ''), payload.lifecycle, payload.events))
              setLifecycleError(null)
            } else {
              setLifecycleTrail(null)
              setLifecycleError(String(payload.error ?? t('app.requestFailed')))
            }
            return
          }
          // 总控对话栏 (policy, lib/masterThreads): the four conversation replies, before the project
          // gate -- every list and thread names its own project and is kept under it, so a reply for another project
          // (its group opened in the column, or the one being switched to) lands where it belongs, never on the project
          // on screen. A refusal is said in words, never as the server's code. An older server that has no
          // conversations (unknown_message_type) leaves 总控 on the project's one conversation, as before.
          if (payload.action === 'master_thread_list') {
            const named = String(payload.project_id ?? '').trim()
            if (payload.ok === true) {
              setMasterThreadsUnsupported(false)
              setMasterThreads(current => mergeThreadList(current, payload))
              if (named) setMasterThreadsUnread(current => (current[named] ? { ...current, [named]: false } : current))
            } else if (payload.error === 'unknown_message_type') {
              setMasterThreadsUnsupported(true)
            } else if (named) {
              setMasterThreadsUnread(current => ({ ...current, [named]: true }))
            }
            return
          }
          if (payload.action === 'master_thread_create') {
            const pending = masterCreateRef.current
            masterCreateRef.current = null
            const thread = payload.ok === true ? threadFrom(payload.thread, String(payload.project_id ?? '')) : null
            if (!thread) {
              if (payload.error === 'unknown_message_type') {
                setMasterThreadsUnsupported(true)
                // Nothing to make on this server: what was said goes to the project's one conversation, as before.
                for (const message of pending?.messages ?? []) {
                  clientRef.current?.masterSend(pending!.projectId, message.content, message.extras?.references ?? [], message.extras?.attachments ?? [])
                }
                return
              }
              if (pending) {
                const reason = refusalInWords(localeRef.current, 'master.threads.refused', payload.error ?? 'request_failed')
                showToast(translateFor(localeRef.current)('master.threads.createFailed', { reason }), 'error', 8000)
                giveBackFirstSend(pending)
              }
              return
            }
            setMasterThreads(current => upsertThread(current, thread))
            if (!pending || pending.projectId !== thread.projectId) return
            // The new conversation that asked for it becomes this one -- the same page, not mounted again -- and what
            // the owner said in it goes out now, into it.
            setMasterOpen(current => (current.threadId === '' && current.fresh === pending.fresh
              ? { ...current, projectId: thread.projectId, threadId: thread.threadId } : current))
            for (const message of pending.messages) {
              clientRef.current?.masterSend(thread.projectId, message.content, message.extras?.references ?? [], message.extras?.attachments ?? [], thread.threadId)
            }
            return
          }
          if (payload.action === 'master_thread_rename' || payload.action === 'master_thread_archive') {
            const thread = payload.ok === true ? threadFrom(payload.thread, String(payload.project_id ?? '')) : null
            if (thread) setMasterThreads(current => upsertThread(current, thread))
            else showToast(refusalInWords(localeRef.current, 'master.threads.refused', payload.error ?? 'request_failed'), 'error')
            return
          }
          if (payload.action === 'master_send' && payload.ok !== true && (payload.error === 'thread_not_found' || payload.error === 'thread_archived')) {
            // The conversation no longer takes messages (archived or gone, perhaps from another tab): the message was not
            // stored. 总控 opens a new conversation of the project on screen and says so; the column is read again.
            const projectId = getActiveProjectId()
            const refused = String(payload.thread_id ?? '').trim()
            setMasterOpen(current => (current.threadId && (!refused || refused === current.threadId) ? newConversation(projectId, current) : current))
            const words = translateFor(localeRef.current)('master.threads.notSent')
            setMasterThreadNote(words)
            showToast(words, 'error', 8000)
            if (isOwnerConsole(authRef.current)) clientRef.current?.masterThreadList(projectId)
            return
          }
          if (payload.action === 'master_tool_activity') {
            if (!currentProject.accepts(payload)) return
            const state = String(payload.state ?? '')
            const sequence = Number(payload.sequence)
            const messageId = String(payload.message_id ?? '')
            const toolName = String(payload.tool_name ?? '')
            if (!['started', 'finished', 'refused', 'failed', 'cancelled', 'outcome_unknown'].includes(state)
              || !Number.isSafeInteger(sequence) || sequence < 1 || !messageId || messageId.length > 128
              || !toolName.startsWith('master_') || toolName.length > 96) return
            setMasterToolActivity(previous => {
              if (previous.some(event => event.messageId === messageId && event.sequence >= sequence)) return previous
              return [...previous, {messageId, sequence, toolName, state: state as MasterToolActivity['state']}].slice(-20)
            })
            return
          }
          if (payload.action === 'master_build_activity') {
            // 施工卡片 (B2): the step a build job is on, for its card -- kept while the job runs, dropped the moment it
            // leaves running (lib/masterBuild keepBuildActivity). Nothing is drawn above the conversation.
            if (!currentProject.accepts(payload)) return
            const step = buildActivityFrom(payload)
            if (step) setMasterBuildActivity(current => keepBuildActivity(current, step))
            return
          }
          // 总控 attachments (4.7 A13): handled before the generic refusal below, which would toast the server's code.
          if (payload.action === 'master_attachment_page') {
            // Only the page still asked for lands; a late answer for one the panel has moved on from is dropped.
            const answered = sourcePreviewFromAck(payload as Record<string, unknown>)
            // A refusal that names no file (not allowed at all) ends whatever is loading. The side panel says why, in words.
            setMasterSourcePreview(current => (current && ((current.attachmentId === answered.attachmentId && current.page === answered.page)
              || (!answered.attachmentId && current.loading)) ? { ...answered, attachmentId: current.attachmentId, page: current.page } : current))
            return
          }
          if (payload.action === 'master_attachment_cancel') {
            // What a cancel did shows in the status line under the message (the server restates it). A refusal is said
            // in words, and that file's 取消 can be pressed again.
            if (payload.ok === false) {
              const messageId = String(payload.message_id ?? '')
              const attachmentId = String(payload.attachment_id ?? '')
              if (messageId && attachmentId) {
                const key = attachmentCancelKey(messageId, attachmentId)
                setMasterCancelRefusals(current => ({ ...current, [key]: (current[key] ?? 0) + 1 }))
              }
              const error = String(payload.error ?? '')
              showToast(cancelRefusedNote(error, localeRef.current.startsWith('zh')), error === 'not_running' ? 'success' : 'error')
            }
            return
          }
          // The pauses, and the list the answer path looks at before re-sending (listArrived below): the project on
          // screen's only. A refusal that names no project is dropped too, as it always was: 待审批 has its own
          // "nothing back yet" (lib/approvalsLoad) and the settled switch asks again.
          if (payload.action === 'blockers_list' && !currentProject.accepts(payload)) return
          if (payload.action === 'owner_attention_list') {
            // 总控's title-line counts (S3-3B): cross-project by design, so gated on the view generation alone
            // (lib/currentProject), never on a project id. An older server that does not know the action leaves the
            // title line counting nothing -- quietly, not as an error on every poll.
            const generation = viewGenerationOf(payload)
            if (generation !== null && generation !== currentProject.generation) return
            if (payload.ok) { setOwnerTodayRaw(payload); setOwnerTodayError(null) }
            else if (todayUnsupported(payload)) { setOwnerTodayRaw(null); setOwnerTodayError(null) }
            // A refusal in words (never the server's code as the owner reads it), the road every refusal takes.
            else setOwnerTodayError(refusalInWords(localeRef.current, 'master.today.refused', payload.error ?? 'request_failed'))
            return
          }
          if (payload.action === 'checkpoint_locate') {
            // A deep link's card: where it lives -- {checkpoint_id, project_id, order_id} -- so its order is opened in
            // 业务工单 (policy: 总控 lists no cards; the effect on cardLocate). A card that is gone is said in
            // the answer path's own words for it, and 业务工单 stands on 等你处理; so does an older server's refusal.
            if (!currentProject.acceptsOrBareRefusal(payload)) return
            if (payload.ok) {
              setCardLocate({ found: true, projectId: String(payload.project_id ?? '').trim(), orderId: String(payload.order_id ?? '').trim() })
            } else {
              if (!todayUnsupported(payload)) showToast(refusalInWords(localeRef.current, 'approvals.refused', payload.error ?? 'checkpoint_not_found'), 'error')
              setCardLocate({ found: false, projectId: '', orderId: '' })
            }
            return
          }
          if (payload.ok === false) {
            if (payload.action === 'create_session') {
              clearPendingSessionCreate()
            }
            if (shouldSuppressTaskNotFound(payload)) return
            if (payload.action === 'switch_project') {
              // A refused switch must not leave the console half-switched:
              // the pending marker blocks later navigation and the selector
              // keeps showing a project the server never activated.
              currentProject.cancelSwitch()
              const active = activeProjectIdRef.current
              if (active) projectStoreRef.current?.setActiveProject(active)
              // The switch emptied the project's state before the server refused it (lib/currentProject): the project
              // it stays on is asked for again -- its pauses, its browser sessions and its page lists, the quiet re-ask
              // forgetting that it already asked this page.
              restoredPageKeyRef.current = ''
              for (const name of REISSUE_ON_SETTLE) reissueOnSettleRef.current[name](active)
              const reason = String(payload.error ?? t('app.requestFailed'))
              const kind = classifyPageError(reason)
              setStatusDetail('')
              showToast(
                kind === 'permission' ? `${t('pageError.permission')}: ${reason}`
                  : kind === 'scope' ? `${t('pageError.scope')}: ${reason}`
                  : reason,
                'error',
              )
              return
            }
            if (payload.action === 'session_detail' && typeof payload.task_id === 'string') {
              sessionStoreRef.current?.updateSession(payload.task_id, {
                detailLoading: false,
                detailError: String(payload.error ?? 'request_failed'),
              })
            }
            setStatusDetail(String(payload.error ?? 'request_failed'))
            showToast(String(payload.error ?? t('app.requestFailed')), 'error')
          }
          // Employee deployed to office
          if (payload.ok && payload.action === 'employee_imported') {
            showToast(t('app.employeeDeployed'))
          }
          if (payload.ok && (payload.action === 'archive_project' || payload.action === 'destroy_project') && payload.switched_to) {
            // The server left the active project before changing it; follow it.
            const target = String(payload.switched_to)
            const switchSeq = beginProjectSwitch(target)
            client.switchProject(target, switchSeq)
          }
          if (payload.ok && (payload.action === 'destroy_project' || payload.action === 'delete_project')
              && payload.leftovers) {
            // The project lived in a folder the owner named, and they had kept things of their own in it. Those
            // were not deleted, and a toast that only says 「已删除」 would leave them looking for files we still
            // have. Say where they are; it is the one thing they have to do by hand.
            const left = payload.leftovers as { path?: unknown; entries?: unknown }
            showToast(locale.startsWith('zh')
              ? `项目已删除。你自己放在 ${String(left.path ?? '')} 里的 ${Number(left.entries ?? 0)} 项没有删，文件夹还在。`
              : `Project deleted. ${Number(left.entries ?? 0)} item(s) of yours were left in ${String(left.path ?? '')}.`,
              'success', 12000)
          }
          if (payload.ok && payload.action === 'archive_project') {
            showToast(t('project.archiveButton'))
          }
          if (payload.ok && payload.action === 'restore_project') {
            showToast(t('project.restoreButton'))
          }
          if (payload.ok && payload.action === 'export_project') {
            showToast(`${t('project.exportButton')}: ${String(payload.path ?? '')}`)
          }
          if (payload.ok && payload.action === 'backup_project') {
            showToast(`${t('project.backupButton')}: ${String(payload.path ?? '')}`)
            setLastBackupPath(String(payload.path ?? ''))
          }
          if (payload.ok && payload.action === 'portfolio_export') {
            const exported = (payload.export ?? {}) as Record<string, unknown>
            showToast(`${t('orders.acceptance.exportPortfolio')}: ${String(exported.path ?? '')}`)
          }
          if (payload.ok && payload.action === 'workflow_redistribution_authorize') {
            const grant = (payload.authorization ?? {}) as Record<string, unknown>
            showToast(t('orders.acceptance.redistributionAuthorized', { version: String(grant.version ?? '') }))
          }
          if (payload.ok && payload.action === 'architecture_reset') {
            setActiveSavedOrg(null)
            setSavedOrgVersionAtLoad(null)
          }
          if (payload.ok && typeof payload.action === 'string'
            && ['settings_get', 'settings_set_key', 'settings_set_routing', 'settings_set_model_tiers',
                'settings_set_flag'].includes(payload.action)) {
            setSettingsError(null)
            setSettingsData({
              providers: (payload.providers as SettingsData['providers']) ?? [],
              gen_providers: (payload.gen_providers as SettingsData['gen_providers']) ?? [],
              judge_providers: (payload.judge_providers as SettingsData['judge_providers']) ?? [],
              gen_tools_not_built: (payload.gen_tools_not_built as SettingsData['gen_tools_not_built']) ?? [],
              runtime_flags: (payload.runtime_flags as SettingsData['runtime_flags']) ?? [],
              flags_writable: payload.flags_writable === true,
              default_model: String(payload.default_model ?? ''),
              routing: (payload.routing as Record<string, string>) ?? {},
              route_health: (payload.route_health as SettingsData['route_health']) ?? undefined,
              keys_writable: payload.keys_writable === true,
              notify: (payload.notify as SettingsData['notify']) ?? undefined,
              // 模型分配 (routing phase 1). Named one by one, like every field above: this object is assembled by
              // hand, so a key the server sends but nobody lists here simply never reaches the page.
              tiers: (payload.tiers as Record<string, string>) ?? {},
              tier_params: (payload.tier_params as Record<string, Record<string, unknown>>) ?? {},
              task_tiers: (payload.task_tiers as Record<string, string>) ?? {},
              residency: String(payload.residency ?? 'cn_only'),
              no_training_optout: (payload.no_training_optout as Record<string, string>) ?? {},
              work_window_cap: Number(payload.work_window_cap ?? 0),
            })
          }
          if (!payload.ok && typeof payload.action === 'string'
            && ['settings_get', 'settings_set_key', 'settings_set_routing', 'settings_set_model_tiers',
                'settings_set_flag'].includes(payload.action)) {
            setSettingsError(String(payload.error ?? t('app.requestFailed')))
          }
          if (payload.action === 'project_set_customer') {
            showToast(payload.ok ? `${t('project.customerSave')} ✓` : String(payload.error ?? t('app.requestFailed')), payload.ok ? 'success' : 'error')
            if (payload.ok) clientRef.current?.projectIndex(getActiveProjectId())
          }
          if (payload.ok && (payload.action === 'credential_reference_list' || payload.action === 'credential_reference_set' || payload.action === 'credential_reference_delete')) {
            setCredentialReferences((payload.references as CredentialReference[]) ?? [])
          }
          if (payload.ok && payload.action === 'order_metrics') {
            setOrderMetrics((payload.metrics as OrderMetricsRow[]) ?? [])
            setOrderMetricsTruncated(payload.truncated === true)
            const usage = payload.usage_by_purpose as { orders?: OrderUsage[] } | null | undefined
            setOrderUsage(Array.isArray(usage?.orders) ? usage.orders : [])
          }
          if (payload.action === 'process_spec_list') {
            setProcessTemplates(payload.ok ? (payload.templates as NonNullable<CapabilityCenterPageProps['processTemplates']> ?? []) : [])
            setTemplateError(payload.ok ? String(payload.template_error ?? '') : 'template_catalog_unavailable')
          }
          if (payload.ok && (payload.action === 'process_spec_list' || payload.action === 'process_spec_save')) {
            setProcessSpecs((payload.specs as NonNullable<CapabilityCenterPageProps['processSpecs']>) ?? [])
          }
          if (payload.action === 'process_spec_save') {
            setSpecSaving(false)
            if (!payload.ok) setFactoryError(String(payload.error ?? t('app.requestFailed')))
            else {
              setFactoryError(null)
              const saved = payload.spec as { spec_id: string; version: string }
              selectedSpecRequest.current = { specId: saved.spec_id, version: saved.version }
              setSelectedSpec(saved.spec_id)
              setSpecReview(null)
              setBuilderRounds([])
              client.processSpecGet(getActiveProjectId(), saved.spec_id, saved.version)
            }
          }
          if (!payload.ok && (payload.action === 'process_spec_get' || payload.action === 'process_spec_list')) {
            setFactoryError(String(payload.error ?? t('app.requestFailed')))
          }
          if (payload.action === 'process_spec_candidate') {
            setSpecSaving(false)
            setFactoryError(payload.ok ? null : String(payload.error ?? t('app.requestFailed')))
            if (payload.ok) showToast(`候选包 ${String(payload.draft_id)} 已生成，尚未安装或发布。请到工作流列表查看。`, 'success')
          }
          if (payload.ok && payload.action === 'process_spec_get') {
            const spec = payload.spec as { spec_id?: string; version?: string } | undefined
            if (spec?.spec_id === selectedSpecRequest.current.specId && spec?.version === selectedSpecRequest.current.version) {
              setProcessDraft((payload.spec as { spec: CapabilityCenterPageProps['processDraft'] }).spec)
              setSpecReview((payload.review as CapabilityCenterPageProps['specReview']) ?? null)
              setBuilderRounds((payload.rounds as NonNullable<CapabilityCenterPageProps['builderRounds']>) ?? [])
              if (canReconcileBuilderTrial(trialRequestRef.current, String(payload.project_id ?? ''),
                String(spec.spec_id), String(spec.version), (payload.rounds as Array<{ outcome: string }>) ?? [])) {
                trialRequestRef.current = null
                setTrialRequest(null)
              }
            }
          }
          if ((payload.action === 'builder_trial' || payload.action === 'builder_trial_resume') && matchesBuilderTrialReceipt(trialRequestRef.current, payload)) {
            trialRequestRef.current = null
            setTrialRequest(null)
            if (payload.ok) {
              if (payload.spec_id === selectedSpecRequest.current.specId && payload.version === selectedSpecRequest.current.version)
                setBuilderRounds((payload.rounds as NonNullable<CapabilityCenterPageProps['builderRounds']>) ?? [])
            } else {
              setFactoryError(String(payload.error ?? t('app.requestFailed')))
              // Read durable claims after a refusal; never replay the write.
              const selected = selectedSpecRequest.current
              if (selected.specId) client.processSpecGet(getActiveProjectId(), selected.specId, selected.version)
            }
          }
          if ((payload.action === 'builder_trial' || payload.action === 'builder_trial_resume') && !payload.ok && !('request_id' in payload) && trialRequestRef.current) {
            // A refusal from the dispatcher carries no correlation. Only one
            // trial is ever in flight, so it is that trial's; the alternative
            // is a button that waits 150s to say "unknown" about a known refusal.
            trialRequestRef.current = null
            setTrialRequest(null)
            setFactoryError(String(payload.error ?? t('app.requestFailed')))
            const selected = selectedSpecRequest.current
            if (selected.specId) client.processSpecGet(getActiveProjectId(), selected.specId, selected.version)
          }
          if (payload.action === 'harness_turn_actions') {
            if (!currentProject.accepts(payload)) return
            if (payload.ok) setTurnLedger(payload as unknown as TurnLedger)
            else setPendingTurnError(String(payload.error ?? t('app.requestFailed')))
          }
          if (payload.action === 'harness_pending_list' || payload.action === 'harness_turn_settle' || payload.action === 'harness_turn_interrupt') {
            if (!currentProject.accepts(payload)) return
            // A refusal to settle is the useful half: "still running here" is
            // what the person needs to read before trying anything else.
            if (payload.ok) setPendingTurns((payload.pending as PendingTurn[]) ?? [])
            setPendingTurnError(payload.ok ? null : String(payload.error ?? t('app.requestFailed')))
          }
          if (payload.action === 'skill_market_categories' && payload.ok) {
            setMarketCategories((payload.categories as MarketCategory[]) ?? [])
          }
          if (payload.action === 'skill_market_browse') {
            if (payload.ok) {
              setMarketBrowse({
                skills: (payload.skills as MarketBrowseState['skills']) ?? [],
                total: Number(payload.total ?? 0),
                keyword: String(payload.keyword ?? ''),
                category: String(payload.category ?? ''),
                sort: payload.sort === 'downloads' ? 'downloads' : 'stars',
                loading: false,
                error: null,
              })
            } else {
              // The panel says the reason in words; the server's own message is kept, never shown as it is.
              setMarketBrowse(prev => ({ ...prev, loading: false, error: String(payload.error ?? 'failed'), reason: String(payload.reason ?? '') }))
            }
          }
          if (String(payload.action ?? '').startsWith('skill_trial_')) {
            const slug = String(payload.slug ?? '')
            const version = String(payload.version ?? '')
            const source = String(payload.source ?? 'skillhub')
            if (payload.action !== 'skill_trial_list' && payload.action !== 'skill_trial_file') {
              setMarketBusy(prev => {
                // A failure reply that names no skill (a dispatcher-level error) cannot say which card it was for: stop them all.
                if (!slug) return Object.keys(prev).length ? {} : prev
                const next = { ...prev }
                delete next[trialKey({ slug, version, source })]
                // The listing's download/import key for this source only: SkillHub's bare slug, a catalog skill's catalog:<id>.
                // One id in both sources stays two cards, so a catalog reply never re-enables a SkillHub download in flight.
                delete next[trialKey({ slug, source })]
                return next
              })
            }
            if (payload.ok) {
              if (Array.isArray(payload.trials)) setSkillTrials(payload.trials as TrialSkill[])
              if (payload.action !== 'skill_trial_list') setSkillTrialError(null)
              if (payload.action === 'skill_trial_file') {
                setTrialFileView({ key: trialKey({ slug, version, source }), path: String(payload.path ?? ''), text: String(payload.text ?? ''), truncated: Boolean(payload.truncated), binary: Boolean(payload.binary) })
              }
              if (payload.action === 'skill_trial_download') {
                // SkillHub's file list and its package differed: filed all the same (the package counts), and said so.
                const drifted = hasListingMismatch(payload.listing_mismatch as ListingMismatch | undefined)
                showToast(t(drifted ? 'market.toast.downloadedMismatch' : 'market.toast.downloaded', { name: slug }), 'success', drifted ? 6000 : 3000)
                const waiting = candidateUseAfterDownloadRef.current[slug]
                if (waiting && version) {
                  delete candidateUseAfterDownloadRef.current[slug]
                  openBuilderWithRef.current({ kind: 'skill', id: slug, name: waiting.name, workflowId: waiting.workflowId, trial: { slug, version, source } })
                }
              }
              if (payload.action === 'skill_trial_admit') {
                showToast(t('market.toast.admitted', { name: slug }), 'success')
                // The skill is the internal library's now: the catalog lists it and packages can declare it.
                clientRef.current?.componentCatalogList(getActiveProjectId())
                clientRef.current?.workflowList(getActiveProjectId())
              }
              if (payload.action === 'skill_trial_discard') showToast(t('market.toast.discarded', { name: slug }), 'success')
              if (payload.action === 'skill_trial_import') {
                showToast(t('market.toast.imported', { name: slug }), 'success')
                // The 调用测试 intent is spent by this reply whichever way it goes.
                const openCard = catalogOpenAfterImportRef.current.delete(slug)
                const waiting = candidateUseAfterDownloadRef.current[`catalog:${slug}`]
                const imported = (Array.isArray(payload.trials) ? payload.trials as TrialSkill[] : []).find(item => item.slug === slug && item.source === 'catalog' && item.version === version)
                if (waiting && version) {
                  delete candidateUseAfterDownloadRef.current[`catalog:${slug}`]
                  openBuilderWithRef.current({ kind: 'skill', id: slug, name: waiting.name, workflowId: waiting.workflowId, trial: { slug, version, source: 'catalog' } })
                } else if (imported && openCard && activePageRef.current === 'components') {
                  // Only while the owner is still on 组件库: an import can take a while, and from another page the toast
                  // (and the card's 去试用卡片) is enough -- the page is not pulled away from what they went on to do.
                  openTrialCardRef.current(imported)
                }
              }
            } else {
              // In words, never the server's own (English) message for SkillHub's refusals. Kept as the reason (the panel
              // words it when shown); the toast, gone in seconds, is worded now.
              const refusal: MarketRefusal = { reason: String(payload.reason ?? ''), said: String(payload.error ?? ''), preferSaid: payload.action === 'skill_trial_import' }
              const message = marketRefusalWords(translateFor(localeRef.current), refusal.reason, refusal.said, { preferSaid: refusal.preferSaid })
              if (payload.action === 'skill_trial_admit' && (payload.reason === 'listing_mismatch_unacknowledged' || payload.reason === 'listing_mismatch_changed')) {
                // The page's trial list was older than the trial, or the yes was to other differences: show the trial's
                // current differences (and their key, which the next yes carries) and ask on that card.
                const mismatch = payload.listing_mismatch as ListingMismatch | undefined
                const mismatchKey = typeof payload.listing_mismatch_key === 'string' ? payload.listing_mismatch_key : ''
                if (mismatch && typeof mismatch === 'object') {
                  setSkillTrials(prev => prev.map(item => (trialKey(item) === trialKey({ slug, version, source }) ? { ...item, listing_mismatch: mismatch, listing_mismatch_key: mismatchKey } : item)))
                }
                trialMismatchAskSeqRef.current += 1
                setTrialMismatchAsk({ key: trialKey({ slug, version, source }), seq: trialMismatchAskSeqRef.current, changed: payload.reason === 'listing_mismatch_changed' })
                setSkillTrialError(null)
              } else {
                setSkillTrialError(refusal)
                if (payload.action === 'skill_trial_download') delete candidateUseAfterDownloadRef.current[slug]
                if (payload.action === 'skill_trial_import') { delete candidateUseAfterDownloadRef.current[`catalog:${slug}`]; catalogOpenAfterImportRef.current.delete(slug); showToast(message, 'error') }
                // The builder shelf has no trial card to show the refusal on: say it where the owner is.
                if (payload.action === 'skill_trial_download' || activePageRef.current === 'workflows') showToast(message, 'error')
              }
            }
          }
          if (payload.action === 'project_workflow_adoptions') {
            if (currentProject.accepts(payload)) {
              setProjectAdoptions(payload.ok ? ((payload.adoptions as WorkflowAdoption[]) ?? []) : [])
            }
          }
          if (payload.action === 'workflow_versions') {
            const id = String(payload.workflow_id ?? '')
            if (id) setFlowVersionRecords(prev => ({ ...prev, [id]: payload.ok ? ((payload.versions as FlowVersions) ?? null) : null }))
          }
          if (payload.action === 'project_workflow_adopt' || payload.action === 'project_workflow_adoption_status') {
            if (!payload.ok) showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            clientRef.current?.projectWorkflowAdoptions(getActiveProjectId())
          }
          if (payload.action === 'workflow_component_candidate_target' || payload.action === 'workflow_adopt_component_draft') {
            setComponentDraftAck(payload as Record<string, unknown>)
            if (payload.project_id === getActiveProjectId() && payload.action === 'workflow_adopt_component_draft' && payload.ok === true && payload.outcome === 'adopted'
              && typeof payload.effects_produced === 'boolean' && payload.external_effects_produced === false) {
              clientRef.current?.workflowList(getActiveProjectId())
              const adopted = payload.workflow as { id?: string } | undefined
              if (adopted?.id) nodeDraftState.setBuilderWorkflowId(adopted.id)
            }
          }
          if (payload.action === 'workflow_create_from_flow_draft') {
            setFlowDraftOpening('')
            if (payload.ok) {
              // The new draft package is empty; the compiled steps become its unsaved draft, as a canvas edit would.
              // A card opened again answers with the draft it made (existing): a draft already saved with steps is only
              // opened, and one never saved gets the steps only where this browser holds no unsaved draft of it yet.
              const created = (payload.workflow ?? {}) as { id?: string; name?: string; version?: string; draft_revision?: string; nodes?: unknown[] }
              const id = String(created.id ?? payload.draft_id ?? '')
              const nodes = Array.isArray(payload.nodes) ? (payload.nodes as unknown as WorkflowNode[]) : []
              const savedSteps = Array.isArray(created.nodes) ? created.nodes.length : 0
              if (id && !(payload.existing && savedSteps > 0)) {
                const keep = Boolean(payload.existing)
                nodeDraftState.setNodeDrafts(prev => (keep && prev[id] ? prev : { ...prev, [id]: nodes }))
                nodeDraftState.setNodeBaseVersions(prev => (keep && prev[id] !== undefined ? prev : { ...prev, [id]: String(created.version ?? '0.1.0') }))
                nodeDraftState.setNodeBaseRevisions(prev => (keep && prev[id] !== undefined ? prev : { ...prev, [id]: String(created.draft_revision ?? '') }))
                nodeDraftState.setNodeVersions(prev => (keep && prev[id] !== undefined ? prev : { ...prev, [id]: nextPatchVersion(String(created.version ?? '0.1.0')) }))
                nodeDraftState.setNodeChangeNotes(prev => (keep && prev[id] !== undefined ? prev : { ...prev, [id]: t('master.flowDraft.changeNote') }))
                nodeDraftState.setEditingWorkflowId(id)
              }
              if (id) nodeDraftState.setBuilderWorkflowId(id)
              clientRef.current?.workflowList(getActiveProjectId())
              setActivePage('workflows')
              showToast(t(payload.existing ? 'master.flowDraft.reopened' : 'master.flowDraft.opened', { name: String(created.name ?? id) }), 'success')
            } else {
              const reason = payload.error === 'flow_draft_not_found' ? t('master.flowDraft.notFound')
                : payload.error === 'team_required' ? t('master.flowDraft.teamRequired')
                  : String(payload.message ?? payload.error ?? t('app.requestFailed'))
              showToast(t('master.flowDraft.openFailed', { reason }), 'error')
            }
          }
          if (payload.action === 'workflow_derive_for_project') {
            // A project's own draft of a global flow: opened on the builder, where the crumb names the project.
            if (payload.ok) {
              const derived = (payload.workflow ?? {}) as { id?: string; name?: string }
              showToast(t(payload.existing ? 'workflows.derive.existing' : 'workflows.derive.created', { name: String(derived.name ?? derived.id ?? '') }), 'success')
              clientRef.current?.workflowList(getActiveProjectId())
              if (derived.id) { nodeDraftState.setBuilderWorkflowId(derived.id); openPageRef.current('workflows') }
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'workflow_create_blank') {
            setCreatingBlank(false)
            if (payload.ok) {
              const created = (payload.workflow ?? {}) as { id?: string; name?: string }
              showToast(t('workflows.blank.created', { name: String(created.name ?? payload.draft_id ?? '') }), 'success')
              clientRef.current?.workflowList(getActiveProjectId())
              if (created.id) nodeDraftState.setBuilderWorkflowId(created.id)
            } else {
              showToast(String(payload.error === 'org_not_found' ? t('workflows.blank.teamNone') : payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'master_send') {
            // A reference or files the server would not take: the message was not stored, so say why where it was typed.
            const problems = Array.isArray(payload.problems) ? (payload.problems as unknown[]).map(String) : []
            const quoteRefused = problems.some(problem => problem.includes('artifact_reference_'))
            if (!payload.ok) showToast(t('master.sendRefused', { reason: quoteRefused
              ? (localeRef.current.startsWith('zh') ? '引用的产物已变化或当前无法读取，请重新打开产物选区后再发。输入的文字可在引用中恢复。'
                : 'The artifact changed or cannot be read. Reopen it and select again. Your last typed text remains available in the quote.')
              : problems.join('；') || String(payload.error ?? '') }), 'error')
            const partly = Array.isArray(payload.attachment_problems) ? (payload.attachment_problems as unknown[]).map(String) : []
            if (payload.ok && partly.length) showToast(t('master.attachPartly', { files: partly.join('；') }), 'error')
          }
          if (payload.action === 'component_detail') {
            // S3-5: through the current-project gate (a bare refusal still lands, so the panel does not read for ever).
            if (!currentProject.acceptsOrBareRefusal(payload)) return
            // Only the answer for the detail still open lands; a late answer for one already closed or replaced is dropped.
            const answered = String(payload.component_id ?? '')
            setComponentDetail(current => (current && current.id === answered
              ? (payload.ok
                ? { id: answered, detail: (payload.detail as ComponentDetail) ?? null, loading: false, error: null }
                : { id: answered, detail: null, loading: false, error: String(payload.message ?? payload.error ?? t('app.requestFailed')) })
              : current))
          }
          if (payload.action === 'component_catalog_list') {
            // S3-5: a catalog asked under another project (or an older switch) is dropped, not shown under this one.
            if (!currentProject.acceptsOrBareRefusal(payload)) return
            setComponentCatalogLoading(false)
            if (payload.ok) {
              setComponentCatalog((payload.catalog as ComponentCatalog) ?? null)
              setComponentCatalogError(null)
            } else {
              setComponentCatalogError(String(payload.error ?? t('app.requestFailed')))
            }
          }
          if (String(payload.action ?? '').startsWith('workflow_distribution_')) {
            if (!currentProject.accepts(payload)) return
            if (!payload.ok) {
              // The failure toast below says why; the inbox must not sit on "loading" for ever because of it.
              if (payload.action === 'workflow_distribution_inbox') setDistributionInbox(current => current ?? [])
            } else if (payload.action === 'workflow_distribution_inbox') {
              setDistributionInbox((payload.inbox as DistributionInboxRecord[]) ?? [])
            } else if (payload.action === 'workflow_distribution_export') {
              const candidate = (payload.candidate ?? {}) as Record<string, unknown>
              showToast(t('components.distribution.exported', { digest: String(candidate.sha256 ?? '').slice(0, 12) }), 'success')
            } else if (payload.action === 'workflow_distribution_receive') {
              const record = (payload.record ?? {}) as Record<string, unknown>
              showToast(t(record.status === 'accepted' ? 'components.distribution.receivedAccepted' : 'components.distribution.receivedRejected',
                { draft: String(record.draft_id ?? '') }), 'success')
              clientRef.current?.workflowDistributionInbox(getActiveProjectId())
              clientRef.current?.workflowList(getActiveProjectId())
            }
          }
          // 组件 · 编辑 / 测试 (4.7 A2-4): the open skill's versions, and what an edit, a verdict or a review changed.
          if (payload.action === 'component_versions') {
            if (!currentProject.acceptsOrBareRefusal(payload)) return
            const answered = String(payload.component_id ?? '')
            setComponentVersionsState(current => (current.id !== answered ? current : payload.ok
              ? { ...current, versions: (payload.versions as ComponentVersions) ?? null, loading: false, error: null }
              : { ...current, versions: null, loading: false, error: String(payload.error ?? t('app.requestFailed')) }))
          }
          // 4.7-A03: every step of the chain answers here; a refusal stays on its candidate's card, in words.
          const componentChainToasts = {
            component_edit: 'components.work.edited', component_candidate_test: 'components.work.tested',
            component_candidate_review: 'components.work.reviewed', component_candidate_revise: 'components.work.revised',
            component_candidate_evaluation_spec: 'components.work.evaluationSpec',
            component_candidate_evaluation_sessions: 'components.work.evaluationSessions',
            component_candidate_evaluation_record: 'components.work.evaluated',
          } as const
          const chainAction = String(payload.action ?? '') as keyof typeof componentChainToasts
          if (chainAction in componentChainToasts) {
            const actedOn = String(payload.candidate_id ?? '')
            setComponentVersionsState(current => ({
              ...current, busy: '',
              refusal: payload.ok
                ? (current.refusal && current.refusal.candidateId === actedOn ? null : current.refusal)
                : (actedOn ? { candidateId: actedOn, message: String(payload.error ?? '') } : current.refusal),
            }))
            if (payload.ok) {
              showToast(t(componentChainToasts[chainAction]), 'success')
              const open = componentWorkRef.current
              if (open) clientRef.current?.componentVersions(getActiveProjectId(), open.id)
              clientRef.current?.skillImprovementList(getActiveProjectId())
            } else {
              showToast(componentRefusal(String(payload.error ?? t('app.requestFailed'))), 'error')
            }
          }
          if (String(payload.action ?? '').startsWith('skill_improvement_') && payload.action !== 'skill_improvement_list' && componentWorkRef.current) {
            // Staged, activated or rolled back from the skill's own 发布 page: read its versions again, and its detail
            // once the library itself changed.
            const open = componentWorkRef.current
            const activation = (payload.activation ?? {}) as Record<string, unknown>
            const actedOn = String(payload.candidate_id ?? activation.candidate_id ?? '')
            setComponentVersionsState(current => ({
              ...current, busy: '',
              refusal: payload.ok
                ? (current.refusal && current.refusal.candidateId === actedOn ? null : current.refusal)
                : (actedOn ? { candidateId: actedOn, message: String(payload.error ?? '') } : current.refusal),
            }))
            clientRef.current?.componentVersions(getActiveProjectId(), open.id)
            if (payload.ok && payload.action !== 'skill_improvement_stage') clientRef.current?.componentDetail(getActiveProjectId(), open.id)
          }
          if (String(payload.action ?? '').startsWith('skill_improvement_')) {
            if (!currentProject.accepts(payload)) return
            setSkillImprovementBusy('')
            if (payload.ok) {
              setSkillImprovementError(null)
              if (payload.action === 'skill_improvement_list') {
                setSkillImprovements((payload.improvements as SkillImprovement[]) ?? [])
              } else {
                clientRef.current?.skillImprovementList(getActiveProjectId())
                showToast(t('components.improvementUpdated'), 'success')
              }
            } else {
              const message = String(payload.error ?? t('app.requestFailed'))
              setSkillImprovementError(message)
              showToast(message, 'error')
            }
          }
          if (String(payload.action ?? '').startsWith('improvement_proposal_')) {
            if (!currentProject.accepts(payload)) return
            // 4.7-A11 提一个改进: the answer stays on the detail that asked (the candidate made, or why not).
            if (payload.action === 'improvement_proposal_start') {
              const started = payload.ok ? {
                proposal: payload.proposal as ImprovementStart['proposal'],
                editor: payload.editor as ImprovementEditor,
                not_carried: (payload.not_carried as string[]) ?? [],
              } : null
              setImprovementStart(current => ({ ...current, busy: false, result: started, refusal: payload.ok ? null : String(payload.error ?? t('app.requestFailed')) }))
              if (payload.ok) {
                clientRef.current?.improvementProposalList(getActiveProjectId())
                clientRef.current?.workflowList(getActiveProjectId())
              }
              return
            }
            if (payload.action !== 'improvement_proposal_list') setImprovementBusy('')
            if (payload.ok) {
              setSkillImprovementError(null)
              if (payload.action === 'improvement_proposal_list') {
                setImprovementProposals((payload.proposals as ImprovementProposal[]) ?? [])
              } else {
                setImprovementRefusal(current => (current && current.proposalId === String(payload.proposal_id ?? '') ? null : current))
                clientRef.current?.improvementProposalList(getActiveProjectId())
                showToast(t('components.proposalUpdated'), 'success')
              }
            } else {
              const message = String(payload.error ?? t('app.requestFailed'))
              if (payload.proposal_id) {
                // Refused on one proposal: the reason stays on its card (a negative benefit also changed it to 已打回).
                setImprovementRefusal({ proposalId: String(payload.proposal_id), message })
                clientRef.current?.improvementProposalList(getActiveProjectId())
              } else {
                setSkillImprovementError(message)
              }
              showToast(message, 'error')
            }
          }
          if (payload.ok && (payload.action === 'connector_switch_list' || payload.action === 'connector_switch_set')) {
            setConnectorSwitches((payload.switches as ConnectorSwitch[]) ?? [])
          }
          if (String(payload.action ?? '').startsWith('connector_candidate_')) {
            if (!currentProject.accepts(payload)) return
            if (payload.ok) {
              setConnectorCandidateError(null)
              if (payload.action === 'connector_candidate_list') {
                setConnectorCandidates((payload.candidates as ConnectorCandidate[]) ?? [])
                setConnectorRegistrations((payload.registrations as ConnectorRegistration[]) ?? [])
              } else {
                clientRef.current?.connectorCandidateList(getActiveProjectId())
              }
            } else {
              setConnectorCandidateError(String(payload.error ?? t('app.requestFailed')))
            }
          }
          if (payload.action === 'browser_view_start' || payload.action === 'browser_view_stop') {
            if (!currentProject.accepts(payload)) return
            if (payload.ok) {
              const row = browserSessionsRef.current.find(item => item.binding_key === payload.binding_key)
              if (!browserFrameMatchesSession(payload, row)) return
              if (!browserFrameMatchesSession(payload, browserViewRequestRef.current)) return
              setBrowserLiveBinding(payload.action === 'browser_view_start' ? String(payload.binding_key ?? '') : null)
              if (payload.action === 'browser_view_stop') setBrowserLiveFrame(null)
            } else {
              setBrowserSessionError(String(payload.error ?? t('app.requestFailed')))
            }
          }
          if (payload.action === 'browser_view_input' && !payload.ok) {
            if (!currentProject.accepts(payload)) return
            setBrowserSessionError(String(payload.error ?? t('app.requestFailed')))
          }
          if (payload.action === 'browser_view_pick') {
            if (!currentProject.accepts(payload)) return
            const row = browserSessionsRef.current.find(item => item.binding_key === payload.binding_key)
            if (payload.ok && browserFrameMatchesSession(payload, row) && browserFrameMatchesSession(payload, browserViewRequestRef.current)) setBrowserPickedElement((payload.element as { tag?: string; type?: string; role?: string; placeholder?: string; text?: string }) ?? null)
            else if (payload.ok) return
            else setBrowserSessionError(String(payload.error ?? t('app.requestFailed')))
          }
          if (payload.action === 'browser_screenshot_read') {
            if (!currentProject.accepts(payload)) return
            if (payload.ok) {
              setBrowserScreenshot({ binding_key: String(payload.binding_key ?? ''), name: String(payload.name ?? ''), data_url: String(payload.data_url ?? '') })
            } else {
              setBrowserSessionError(String(payload.error ?? t('app.requestFailed')))
            }
          }
          if (payload.action === 'effect_look_back') {
            if (!currentProject.accepts(payload)) return
            if (payload.ok) {
              const released = Array.isArray(payload.budget_holds_released) ? payload.budget_holds_released.length : 0
              const effectId = String(payload.effect_id ?? '').slice(0, 8)
              setBrowserLookBackNote(payload.resolved
                ? `回查 ${effectId}：已判定为已发生（${String(payload.status ?? '')}），解除预算保留 ${released} 笔`
                : `回查 ${effectId}：证据不足（${String(payload.status ?? '')}），仍为结果未知，未解除任何预算保留`)
              // the trail this answer belongs to is read again, so the open question and the effect state follow
              const key = browserTrail?.binding_key
              if (key) clientRef.current?.browserAuditTrail(getActiveProjectId(), key)
            } else {
              setBrowserSessionError(String(payload.error ?? t('app.requestFailed')))
            }
          }
          if (payload.action === 'browser_audit_trail') {
            if (!currentProject.accepts(payload)) return
            if (payload.ok) {
              setBrowserTrail((payload.trail as NonNullable<CapabilityCenterPageProps['browserTrail']>) ?? null)
              setBrowserSessionError(null)
            } else {
              setBrowserSessionError(String(payload.error ?? t('app.requestFailed')))
            }
          }
          if (payload.action === 'browser_audit_export') {
            if (!currentProject.accepts(payload)) return
            if (payload.ok) {
              const digest = String(payload.sha256 ?? '').slice(0, 12)
              const reused = payload.created ? '' : '（内容未变，复用已有文件）'
              setBrowserAuditExportNote(`审计包已保存到订单 ${String(payload.order_id ?? '')}：${String(payload.name ?? '')} · SHA-256 ${digest}${reused}`)
              setBrowserSessionError(null)
            } else {
              setBrowserSessionError(String(payload.error ?? t('app.requestFailed')))
            }
          }
          if (payload.action === 'desktop_session_control') {
            if (!currentProject.accepts(payload)) return
            setDesktopSessionBusy(false)
            if (payload.ok) {
              setDesktopSessionError(null)
              setDesktopSessionStatus({
                state: String(payload.state ?? 'none'),
                controller: String(payload.controller ?? 'none'),
                fresh_look_required: Boolean(payload.fresh_look_required),
                window_title: payload.window_title != null ? String(payload.window_title) : undefined,
                screen_hash: payload.screen_hash != null ? String(payload.screen_hash) : undefined,
                frame_delivered: Boolean(payload.frame_delivered),
                last_refusal: payload.last_refusal != null ? String(payload.last_refusal) : undefined,
                app: payload.app != null ? String(payload.app) : undefined,
              })
            } else {
              setDesktopSessionError(String(payload.error ?? payload.detail ?? t('app.requestFailed')))
              if (payload.state !== undefined) {
                setDesktopSessionStatus({
                  state: String(payload.state ?? 'none'),
                  controller: String(payload.controller ?? 'none'),
                  fresh_look_required: Boolean(payload.fresh_look_required),
                })
              }
            }
          }
          if (payload.action === 'browser_account_inspect') {
            receiveBrowserAccountInspection(payload)
            return
          }
          if (payload.action === 'browser_session_list' || payload.action === 'browser_session_control') {
            if (!currentProject.accepts(payload)) return
            if (payload.ok) {
              setBrowserSessionError(null)
              const rows = ((payload.sessions as BrowserSession[]) ?? []).filter(row => row.project_id === getActiveProjectId())
              refreshBrowserAccountInspection(rows)
              browserSessionsRef.current = rows
              setBrowserSessions(rows)
              const stream = browserViewRequestRef.current
              if (stream && !rows.some(row => row.live && browserFrameMatchesSession(stream, row))) {
                browserViewRequestRef.current = null
                setBrowserLiveFrame(null)
                setBrowserLiveBinding(null)
                setBrowserPickedElement(null)
              }
              for (const row of rows) {
                const notice = browserHandoffNoticesRef.current.get(row.binding_key)
                if (row.state === 'awaiting_human' && browserFrameMatchesSession(notice, row)) dismissedPanesRef.current.delete(row.binding_key)
              }
              browserHandoffNoticesRef.current.clear()
              // X07: the sealed demonstration's id exists only in this receipt; a compile needs nothing else.
              const sealed = payload.command === 'demo_stop' ? ((payload.state as Record<string, unknown> | undefined)?.demo as Record<string, unknown> | undefined) : undefined
              if (sealed && typeof sealed.demo_id === 'string' && sealed.demo_id) {
                setBrowserSealedDemo({ demo_id: sealed.demo_id, steps: Number(sealed.steps ?? 0) })
              }
              // X05: after a reload the pane reopens on a page still handed to a person, unless the person closed it themselves.
              const waiting = rows.find(row => row.state === 'awaiting_human' && row.live && !dismissedPanesRef.current.has(row.binding_key))
              if (waiting && !browserPaneRef.current) {
                const pane = { binding_key: waiting.binding_key, task_id: waiting.task_id, note: waiting.note, anchor_session_id: waiting.anchor_session_id, order_id: waiting.order_id }
                browserPaneRef.current = pane
                setBrowserPane(pane)
                requestBrowserView(waiting.binding_key, browserControlContext(waiting) ?? undefined)
                // FE-EB-03：等人接管时同时展开执行工作台并聚焦浏览器表面（0 次额外点击看到画面位）
                openExecutionRef.current(waiting.task_id, 'browser')
              } else if (browserPaneRef.current && !browserViewRequestRef.current) {
                const opened = rows.find(row => row.binding_key === browserPaneRef.current?.binding_key && row.live)
                if (opened) requestBrowserView(opened.binding_key, browserControlContext(opened) ?? undefined)
              }
              if (payload.limits) setBrowserLimits(payload.limits as NonNullable<CapabilityCenterPageProps['browserLimits']>)
              if (payload.combination) setBrowserCombination(payload.combination as NonNullable<CapabilityCenterPageProps['browserCombination']>)
            } else {
              const reason = String(payload.error ?? t('app.requestFailed'))
              setBrowserSessionError(reason === 'browser_hosts_update_unknown'
                ? '域名配置可能已保存，但后续状态未确认。请刷新并核对后再继续；不会自动重试。'
                : reason === 'browser_control_changed' ? '浏览器控制状态已变化，请刷新后重新操作。' : reason)
            }
          }
          if (payload.ok && payload.action === 'deployment_list') {
            setDeployments((payload.deployments as Deployment[]) ?? [])
            setRollouts((payload.rollouts as Rollout[]) ?? [])
            setRolloutsHasMore(payload.rollouts_has_more === true)
            setSolutionPackages((payload.packages as Array<{ solution_id: string; version: string }>) ?? [])
          }
          if (String(payload.action ?? '').startsWith('deployment_rollout_')) {
            // A refusal is the useful half here: "two targets still to
            // establish" is what the operator needs to read, not a toast.
            setRolloutError(payload.ok ? null : String(payload.error ?? t('app.requestFailed')))
            if (payload.ok) {
              if (payload.deployments) setDeployments(payload.deployments as Deployment[])
              clientRef.current?.deploymentList(getActiveProjectId())
            }
          }
          if (payload.action === 'connector_switch_set' && !payload.ok) {
            showToast(String(payload.error ?? t('app.requestFailed')), 'error')
          }
          if (payload.action === 'run_budget_create' || payload.action === 'run_budget_close') {
            if (!currentProject.accepts(payload)) return
            if (payload.ok && payload.action === 'run_budget_close') {
              // A running order is never left unheld: its ceilings are lifted in place, the allowance stays.
              showToast(payload.lifted === true ? t('orders.budget.lifted') : t('orders.budget.closed_', { n: Number(payload.released_tasks ?? 0) }), 'success')
            } else if (payload.ok && payload.in_place === true) {
              showToast(t('orders.budget.ceilingsSet'), 'success')
            } else if (payload.ok) {
              // The server never leaves an allowance it could not bind: ok means the run (when there is one) is pinned.
              showToast(payload.run_started === true
                ? t('orders.budget.grantedRunning', { n: Number(payload.bound_tasks ?? 0) })
                : t('orders.budget.granted'), 'success')
            }
            // Either way the row shows what is true now; a refusal also falls through to the failure toast below.
            clientRef.current?.runBudgetList(getActiveProjectId())
          }
          if (payload.action === 'run_budget_raise') {
            if (!currentProject.accepts(payload)) return
            if (payload.ok) {
              if (payload.budgets) setRunBudgets(payload.budgets as RunBudget[])
              showToast(t('orders.budget.raised'), 'success')
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'run_budget_list' || payload.action === 'run_budget_resolve') {
            // The allowances of the project on screen only; a refusal that names no project is still said.
            if (!currentProject.acceptsOrBareRefusal(payload)) return
            if (payload.ok) {
              if (payload.budgets) setRunBudgets(payload.budgets as RunBudget[])
              if (Array.isArray(payload.pending_reservations)) setPendingReservations(payload.pending_reservations as typeof pendingReservations)
              // Settling one changes the list; the resolve receipt carries budgets only, so ask for it again.
              if (payload.action === 'run_budget_resolve') clientRef.current?.runBudgetList(getActiveProjectId())
              if (payload.action === 'run_budget_resolve') showToast(t('orders.budget.resolved'), 'success')
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'channel_config_check') {
            if (payload.ok) setChannelCheck({ muted: payload.muted === true, channels: (payload.channels as ChannelCheck['channels']) ?? [] })
            else showToast(String(payload.error ?? t('app.requestFailed')), 'error')
          }
          if (payload.action === 'workflow_release_evidence') {
            if (payload.ok) setReleaseEvidence(payload.evidence as ReleaseEvidence)
            else showToast(String(payload.error ?? t('app.requestFailed')), 'error')
          }
          if (payload.ok && payload.action === 'solution_blueprint_list') {
            setBlueprints((payload.blueprints as SolutionBlueprint[]) ?? [])
          }
          if (payload.action === 'solution_blueprint_save' || payload.action === 'solution_blueprint_apply') {
            if (payload.ok) {
              // A plan proved on an older version of the workflow is still
              // reusable, but saying nothing would present it as current.
              const versionMoved = payload.action === 'solution_blueprint_apply' && payload.workflow_version_changed === true
              showToast(
                t(payload.action === 'solution_blueprint_save' ? 'orders.blueprintSave' : 'orders.blueprintApply') + ' ✓'
                  + (versionMoved ? ` · ${t('orders.blueprintVersionMoved', { from: String(payload.workflow_version ?? ''), to: String(payload.target_workflow_version ?? '') })}` : ''),
                versionMoved ? 'error' : 'success',
              )
              client.solutionBlueprintList(getActiveProjectId())
              client.orderList(getActiveProjectId())
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'order_timeline') {
            // One order's timeline, of the project on screen only; a refusal that names no project is still said.
            if (!currentProject.acceptsOrBareRefusal(payload)) return
            if (payload.ok) { setOrderTimelineError(null); setOrderTimeline(payload as unknown as OrderTimeline) }
            else { setOrderTimeline(null); setOrderTimelineError(String(payload.error ?? t('app.requestFailed'))) }
          }
          if (payload.action === 'order_model_plan') {
            // null when the order has none: it was placed before plans existed, and the drawer says so rather
            // than showing today's settings as though they were its own.
            setOrderModelPlan(payload.ok ? ((payload.plan as ModelPlan) ?? null) : null)
          }
          if (payload.action === 'capability_reprobe') {
            setCapabilityReprobing(false)
            if (payload.ok) showToast(t('capability.reprobeDone', { n: Number(payload.available ?? 0), total: Number(payload.probed ?? 0) }), 'success')
            else setOrgInfoError(String(payload.error ?? t('app.requestFailed')))
          }
          if (!payload.ok && payload.action === 'org_info') {
            setOrgInfoError(String(payload.error ?? t('app.requestFailed')))
            setCapabilityLoading(false)
          }
          if (payload.action === 'pending_approval_badge_read'
            && (payload.project_id == null || payload.project_id === getActiveProjectId())
            && isV2SurfaceEnabled('f_frontend_pending_approval_badge')) {
            if (!isOwnerConsole(authRef.current)) setPendingApprovalBadgeState(null)
            else if (payload.ok && payload.skipped !== true) {
              setPendingApprovalBadgeState(previous => pendingBadgeServerObservation(
                payload, getActiveProjectId(), authRef.current?.account?.login ?? 'owner', previous,
              ))
            } else setPendingApprovalBadgeState(previous => stalePendingBadge(previous))
          }
          if (payload.ok && payload.action === 'blockers_list') {
            const list = (payload.blockers as DashboardBlocker[]) ?? []
            setBlockers(list)
            // The answer path looks at this list before re-sending an answer whose receipt never came.
            answerPathRef.current?.listArrived(list.map(item => item.checkpoint_id))
            // Top5 item5: derive badge from blockers channel when FE flag is on (no new poll loop).
            if (isOwnerConsole(authRef.current) && isV2SurfaceEnabled('f_frontend_pending_approval_badge')) {
              setPendingApprovalBadgeState(previous => pendingBadgeServerObservation(
                payload.pending_approval_badge, getActiveProjectId(), authRef.current?.account?.login ?? 'owner', previous,
              ))
            } else {
              setPendingApprovalBadgeState(null)
            }
          }
          if (!payload.ok && payload.action === 'blockers_list') {
            if (isV2SurfaceEnabled('f_frontend_pending_approval_badge')) setPendingApprovalBadgeState(previous => stalePendingBadge(previous))
            // The approval centre that showed this refusal is gone (S3-7); the cards it fed are drawn elsewhere, so the
            // refusal is said here rather than swallowed.
            showToast(String(payload.error ?? t('app.requestFailed')), 'error')
          }
          if (payload.action === 'blocker_snooze' || payload.action === 'blocker_unsnooze') {
            if (payload.ok) showToast(t(payload.action === 'blocker_snooze' ? 'approvals.held' : 'approvals.released'), 'success')
            else showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            clientRef.current?.blockersList(getActiveProjectId())
          }
          if (payload.ok && payload.action === 'duty_log_list') {
            setDutyLogError(null)
            setDutyLog({
              configured: payload.configured === true,
              entries: (payload.entries as DutyLogEntry[]) ?? [],
            })
          }
          if (!payload.ok && payload.action === 'duty_log_list') {
            setDutyLogError(String(payload.error ?? t('app.requestFailed')))
          }
          if (payload.action === 'auth_login' || payload.action === 'auth_token' || payload.action === 'account_bootstrap_owner') {
            setAuthBusy(false)
            if (payload.ok) {
              if (typeof payload.token === 'string' && payload.token) writeAuthToken(payload.token)
              setAuthError('')
              if (payload.action === 'account_bootstrap_owner') client.accountList(getActiveProjectId())
            } else {
              if (payload.action === 'auth_token') {
                const failure = authTokenFailurePolicy(payload)
                if (failure.clearCredential) writeAuthToken('')
                const blockedAuth: AuthState = { required: true, bootstrap_needed: false, account: null }
                authRef.current = blockedAuth
                ownerSignedInRef.current = false
                setPendingApprovalBadgeState(null)
                setAuth(blockedAuth)
                setAuthError(failure.message)
              } else setAuthError(String(payload.error ?? t('login.failed')))
              if (payload.action === 'account_bootstrap_owner') setAccountsError(String(payload.error ?? ''))
            }
          }
          if (payload.action === 'auth_logout') {
            writeAuthToken('')
            setMyTasks(null)
            setMyOpen(null)
            setMyReturned([])
            setCustomerOrders(null)
          }
          if (payload.action === 'account_list') {
            setAccounts(payload.ok ? ((payload.accounts as AuthAccount[]) ?? []) : [])
          }
          if (payload.action === 'account_create' || payload.action === 'account_set_disabled' || payload.action === 'account_set_password') {
            setAccountsError(payload.ok ? '' : String(payload.error ?? t('app.requestFailed')))
            if (payload.ok) client.accountList(getActiveProjectId())
          }
          if (payload.action === 'employee_add') {
            showToast(payload.ok ? t('org.addEmployee.done') : String(payload.error ?? t('app.requestFailed')), payload.ok ? 'success' : 'error')
          }
          // 人员与职责 · 联系方式 (4.7-A07): both answers carry the whole book; a refused bind names the member it was for.
          if (payload.action === 'contact_list' || payload.action === 'contact_bind') {
            const book = payload.ok ? contactsFrom(payload.contacts) : null
            if (book) setStaffContacts(book)
            if (payload.action === 'contact_bind') {
              const employeeId = contactBindForRef.current
              contactBindForRef.current = ''
              setContactBind({ busyEmployeeId: '', error: payload.ok ? null : { employeeId, error: String(payload.error ?? '') } })
            }
          }
          // 企业微信（微信客服）(W3b): the three owner-only actions answer with the channel's status for one project.
          if (payload.action === 'wecom_channel_status' || payload.action === 'wecom_channel_set' || payload.action === 'wecom_kf_account_set') {
            if (!currentProject.acceptsOrBareRefusal(payload)) return
            setWecomChannel(prev => wecomChannelAck(prev, payload))
          }
          // 客服验收 (4.8): nine owner-only suite/channel actions answer with
          // the server's result; the card carries the suite id to the next step.
          if (typeof payload.action === 'string' && payload.action.startsWith('customer_service_')) {
            setCustomerService(prev => customerServiceAck(prev, payload))
          }
          if (payload.action === 'employee_handoff_list' || payload.action === 'employee_handoff_decide'
              || payload.action === 'employee_handoff_rework' || payload.action === 'employee_handoff_continue'
              || payload.action === 'employee_handoff_check' || payload.action === 'employee_handoff_node_list') {
            setEmployeeHandoffs(prev => employeeHandoffsAck(prev, payload))
          }
          if (payload.action === 'my_tasks_list') {
            setMyTasksError(payload.ok ? '' : String(payload.error ?? t('app.requestFailed')))
            setMyTasks(payload.ok ? ((payload.tasks as MyTask[]) ?? []) : null)
          }
          // 客户页 (S3-6B): the account's own orders, and the receipt of a 确认收货. A reply that names a project is held to
          // the project on screen (lib/currentProject); one that names none (the dispatcher's own refusal) is still said.
          if (payload.action === 'customer_orders_view') {
            if (projectIdOf(payload) && !currentProject.accepts(payload)) return
            setCustomerOrdersError(payload.ok ? '' : String(payload.error ?? t('app.requestFailed')))
            setCustomerOrders(payload.ok ? parseCustomerOrders(payload) : null)
          }
          if (payload.action === 'customer_accept') {
            if (projectIdOf(payload) && !currentProject.accepts(payload)) return
            const orderId = String(payload.order_id ?? '').trim()
            setCustomerAcceptErrors(prev => {
              const next = { ...prev }
              if (payload.ok) delete next[orderId]
              else next[orderId] = String(payload.error ?? '') || 'unknown'
              return next
            })
            // Whichever way it ended, the list says where the order stands now.
            clientRef.current?.send({ type: 'customer_orders_view' })
          }
          if (payload.action === 'gate_detail') {
            // The gate's materials view, of the project on screen only; a refusal that names no project is still said.
            if (!currentProject.acceptsOrBareRefusal(payload)) return
            if (payload.ok) {
              setGateDetail(payload as unknown as GateDetail)
              setGateError(null)
            } else {
              setGateError(String(payload.error ?? t('app.requestFailed')))
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'gate_decide') {
            // A receipt for another project's gate is not this page's (lib/answerPath): dropped, nothing said.
            if (typeof payload.project_id === 'string' && payload.project_id && payload.project_id !== getActiveProjectId()) return
            // The answer is no longer out; a receipt that names no gate settles every gate decision begun here.
            if (typeof payload.checkpoint_id === 'string' && payload.checkpoint_id) answerPathRef.current?.settle(payload.checkpoint_id)
            else answerPathRef.current?.settleOrigin('gate')
            setGateBusy(false)
            if (payload.ok) {
              setGateDetail(null)
              setGateError(null)
              showToast(String(payload.decision) === 'approve' ? t('gate.approved') : t('gate.rejected'), 'success')
              setBlockers(prev => prev.filter(item => item.checkpoint_id !== String(payload.checkpoint_id ?? '')))
              askOwnerTodayRef.current()
              // 员工页 (S3-6B): the same receipt ends the employee's open task (a no-op on the owner's console).
              employeeDecidedRef.current(String(payload.checkpoint_id ?? ''), String(payload.decision) === 'reject' ? 'reject' : 'approve')
            } else {
              // The panel stays open with the reason on it: a refused decision
              // is a thing to act on, not a message that scrolls away. The
              // server names the refusal by a code (already_decided,
              // not_the_assignee ...); the person reads it in words, and a code
              // with no words yet keeps the code in brackets for whoever looks.
              const refusal = refusalInWords(localeRef.current, 'gate.refused', payload.error)
              setGateError(refusal)
              showToast(refusal, 'error')
            }
            clientRef.current?.blockersList(getActiveProjectId())
          }
          if (payload.ok && payload.action === 'gate_status') {
            setGateStatus(payload as unknown as GateStatus)
          }
          if (payload.action === 'workflow_list') {
            // Only the list of the project on screen: a late one from the project left behind is dropped, while the
            // first load's settling of the project already on screen keeps its list, and a refusal that names no
            // project still reaches the page (lib/currentProject acceptsOrBareRefusal).
            if (!currentProject.acceptsOrBareRefusal(payload)) return
            setWorkflowError(payload.ok ? null : String(payload.error ?? t('app.requestFailed')))
            setWorkflows(payload.ok ? ((payload.workflows as WorkflowSummary[]) ?? []) : [])
            setWorkflowsReadProjectId(payload.ok && typeof payload.project_id === 'string' ? payload.project_id : null)
            if (payload.ok && isOwnerConsole(authRef.current)) {
              client.workflowNodeTrialList(String(payload.project_id ?? getActiveProjectId()))
            }
          }
          if (payload.action === 'project_order_overview') {
            // A cross-project read: its reply names every visible project and no one project, so the project gate
            // has nothing to match; it is held back only while a switch is under way (lib/currentProject), and one
            // that does name a project goes through the gate. Refused (a customer-bound account) or failed: the rows
            // then say nothing, which is the truth.
            if (projectIdOf(payload) ? !currentProject.acceptsOrBareRefusal(payload) : currentProject.pendingProjectId !== null) return
            setProjectOverview(payload.ok ? overviewFrom(payload.projects) : null)
          }
          // 项目中心 (S3-2B): the open project's business boundary and its connection lanes -- the project on screen
          // only; a refusal that names no project is still said. `needs_migration` (the store is behind) is said as
          // such, never drawn as an empty boundary.
          if (payload.action === 'project_profile_get' || payload.action === 'project_profile_set') {
            if (!currentProject.acceptsOrBareRefusal(payload)) return
            const behind = needsMigration(payload)
            if (payload.ok) setProjectProfile(profileFrom(payload.profile) ?? null)
            setProjectProfileState({ loaded: true, needsMigration: behind, why: migrationWhy(payload), error: payload.ok || behind ? '' : String(payload.error ?? t('app.requestFailed')) })
            if (payload.action === 'project_profile_set' && payload.ok) clientRef.current?.listProjects()  // the rows carry the profile too
          }
          if (payload.action === 'project_connections_get' || payload.action === 'project_connections_set') {
            if (!currentProject.acceptsOrBareRefusal(payload)) return
            const behind = needsMigration(payload)
            if (payload.ok) setProjectConnections(connectionsFrom(payload.lanes))
            setProjectConnectionsState({ loaded: true, needsMigration: behind, why: migrationWhy(payload), error: payload.ok || behind ? '' : String(payload.error ?? t('app.requestFailed')) })
          }
          if (payload.action === 'order_list') {
            // The orders of the project on screen only: a page of the project left behind used to land here (the list
            // had no project check at all); a refusal that names no project is still said.
            if (!currentProject.acceptsOrBareRefusal(payload)) return
            if (!payload.ok) {
              setOrdersReadProjectId(null)
              setWorkflowError(String(payload.error ?? t('app.requestFailed')))
            }
            // 4.7-A08: an older page goes below the list, a first page keeps the older pages already added; a page
            // answering a cursor the list no longer waits on is dropped (lib/orderQueues foldOrderPage).
            const folded = foldOrderPage<WorkOrderSummary>(ordersRef.current, orderPagingRef.current, payload)
            if (folded) {
              setOrdersReadProjectId(payload.ok === true && typeof payload.project_id === 'string'
                && currentProject.accepts(payload) ? payload.project_id : null)
              orderPagingRef.current = folded.paging
              setOrderPaging(folded.paging)
              // The list's acceptance is the unverified summary. Taking it whole made the history files, the byte
              // comparison and the exports vanish after every decision; keep verified detail that is still current,
              // and ask again for the orders whose acceptance has moved on.
              const merged = mergeOrderList(ordersRef.current, folded.orders)
              setOrders(merged.orders)
              // Only the latest part is listed while the server says there is more; a receipt without the field listed them all.
              setOrdersTruncated(folded.truncated)
              for (const orderId of merged.reverify) clientRef.current?.acceptanceGet(getActiveProjectId(), orderId)
            }
          }
          if (String(payload.action ?? '').startsWith('distribution_authorization_')) {
            const answer = distributionReplyFrom(payload)
            if (answer) setDistributionReply(answer)
          }
          if (payload.action === 'order_review') {
            if (!payload.ok) showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            refreshOrderList()
          }
          if (payload.action === 'order_cancel') {
            const idsOf = (value: unknown) => (Array.isArray(value)
              ? value.map(item => String((item as Record<string, unknown>)?.order_id ?? '')).filter(Boolean)
              : [])
            // Cancelling an order leaves the orders its run started running; say so, by name.
            const children = idsOf(payload.running_children)
            const cancelled = idsOf(payload.cancelled_children)
            const failed = idsOf(payload.failed_children)
            if (!payload.ok) {
              // A refusal that arrives after the children went first still cancelled them; the error alone would hide that.
              const refused = String(payload.error ?? t('app.requestFailed'))
              showToast(cancelled.length > 0
                ? `${refused} ${t('orders.cancel.childrenCancelledBeforeRefusal', { count: cancelled.length, orders: cancelled.join(', ') })}`
                : refused, 'error')
            } else {
              const notStopped = Array.from(new Set([...failed, ...children]))
              if (failed.length > 0) showToast(t('orders.cancel.childrenFailed', { count: notStopped.length, orders: notStopped.join(', ') }), 'error')
              else if (children.length > 0) showToast(t('orders.cancel.childrenStillRunning', { count: children.length, orders: children.join(', ') }), 'error')
              else if (cancelled.length > 0) showToast(t('orders.cancel.childrenCancelled', { count: cancelled.length }), 'success')
            }
            refreshOrderList()
          }
          if (payload.action === 'order_handling_update') {
            setUpdatingHandlingOrderId(null)
            if (payload.ok && payload.order && typeof payload.order === 'object') {
              // The answer is the order as it now is: put it in place rather than asking for the whole list again.
              const next = payload.order as WorkOrderSummary
              setOrders(prev => prev?.map(order => (order.order_id === next.order_id ? { ...order, handling: next.handling ?? null, updated_at: next.updated_at } : order)) ?? prev)
            } else if (!payload.ok) {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'fde_control_plane_update') {
            setUpdatingFdeControlPlaneOrderId(null)
            if (payload.ok) {
              refreshOrderList()
              showToast(t('orders.fde.save') + ' ✓', 'success')
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'acceptance_get') {
            if (payload.ok) {
              const orderId = String(payload.acceptance && typeof payload.acceptance === 'object'
                ? (payload.acceptance as Record<string, unknown>).order_id ?? ''
                : '')
              if (orderId) {
                setOrders(prev => prev?.map(order => order.order_id === orderId
                  ? { ...order, acceptance: payload.acceptance as WorkOrderSummary['acceptance'] }
                  : order) ?? prev)
              }
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (['acceptance_define', 'acceptance_evidence', 'acceptance_submit', 'acceptance_decide', 'acceptance_rework'].includes(String(payload.action ?? ''))) {
            setAcceptanceBusyOrderId(null)
            if (!payload.ok) showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            // A decision and a rework answer with the verified acceptance (frozen files, byte comparison): show it
            // now instead of dropping it and leaving the panel without its history until it is reopened.
            const decided = payload.acceptance && typeof payload.acceptance === 'object' ? payload.acceptance as NonNullable<WorkOrderSummary['acceptance']> & { order_id?: string } : null
            const decidedOrderId = String(decided?.order_id ?? payload.order_id ?? '')
            if (decided && decidedOrderId) {
              setOrders(prev => prev?.map(order => order.order_id === decidedOrderId ? { ...order, acceptance: decided } : order) ?? prev)
            }
            refreshOrderList()
            // A rework whose run could not be reopened answers ok:false with no acceptance at all -- yet the round has
            // moved and the rejected round was frozen. Read the verified acceptance rather than show the summary only.
            if (!decided && (payload.action === 'acceptance_decide' || payload.action === 'acceptance_rework') && payload.order_id) {
              clientRef.current?.acceptanceGet(getActiveProjectId(), String(payload.order_id))
            }
          }
          if (payload.action === 'acceptance_round_export') {
            if (payload.ok) {
              const exported = payload.export as Record<string, unknown> | undefined
              showToast(`${t('orders.acceptance.exportRound')}: ${String(exported?.path ?? '')}`, 'success')
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'workflow_invoke') {
            const workflowId = String(payload.workflow_id ?? '')
            if (payload.ok && typeof payload.task_id === 'string' && payload.task_id) {
              setWorkflowInvoke(null)
              refreshOrderList()
              sessionStoreRef.current?.setActiveSession(payload.task_id)
              // Placed from 业务工单: stay on the list and open the new order there. From the builder, the run's record
              // in 执行区's side window (S3-7: the workspace page is gone; the window is its body).
              const placedOrder = typeof payload.order_id === 'string' ? payload.order_id : ''
              if (activePageRef.current === 'orders' && placedOrder) {
                setOrderQueueRequest(current => ({ queue: 'all', query: '', open: placedOrder, flow: '', seq: (current?.seq ?? 0) + 1 }))
              } else {
                openExecutionRef.current(payload.task_id)
              }
              showToast(t('workflows.placed'), 'success')
            } else {
              setWorkflowInvoke({ workflowId, status: 'error', error: String(payload.error ?? t('app.requestFailed')) })
            }
          }
          if (payload.action === 'workflow_preflight') {
            setPreflightingWorkflowId(null)
            if (payload.ok) {
              setWorkflowPreflight(payload.preflight as WorkflowPreflight)
            } else {
              setWorkflowPreflight(null)
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'workflow_node_trial_start') {
            setWorkflowNodeTrialBusy(false)
            if (payload.ok) {
              const trial = payload.trial as WorkflowNodeTrial
              setWorkflowNodeTrials(prev => [trial, ...prev.filter(item => item.spec_id !== trial.spec_id)])
              if (workflowNodeTrialPollRef.current) clearTimeout(workflowNodeTrialPollRef.current)
              workflowNodeTrialPollRef.current = setTimeout(() => {
                client.workflowNodeTrialList(String(payload.project_id ?? getActiveProjectId()))
              }, 500)
              showToast(payload.accepted ? '真实试跑已进入预算内执行' : '已找到同一试跑请求，不会重复执行', 'success')
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'workflow_node_trial_answer' || payload.action === 'workflow_node_trial_abandon') {
            setWorkflowNodeTrialBusy(false)
            if (payload.ok) {
              const trial = payload.trial as WorkflowNodeTrial
              setWorkflowNodeTrials(prev => [trial, ...prev.filter(item => item.spec_id !== trial.spec_id)])
              if (workflowNodeTrialPollRef.current) clearTimeout(workflowNodeTrialPollRef.current)
              workflowNodeTrialPollRef.current = setTimeout(() => {
                client.workflowNodeTrialList(String(payload.project_id ?? getActiveProjectId()))
              }, 500)
              showToast(payload.action === 'workflow_node_trial_answer' ? '已收到回答，试跑继续' : '已放弃这次试跑，预算已关闭', 'success')
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'workflow_node_trial_artifact'
            && trialArtifactWantedRef.current?.specId === String(payload.spec_id ?? '')
            && trialArtifactWantedRef.current?.path === String(payload.path ?? '')) {
            setTrialArtifact(payload.ok
              ? { preview: payload as unknown as TrialArtifactState['preview'], loading: false, error: null }
              : { preview: null, loading: false, error: String(payload.error ?? t('app.requestFailed')) })
          }
          if (payload.action === 'workflow_node_trial_list' && payload.ok && payload.node_id) {
            // Asked for one node: the answer is that node's pin plan, not the project's list of trials.
            const plan = payload.pinning as Omit<NodeTrialPinning, 'workflow_id' | 'node_id'> | null
            const where = { workflow_id: String(payload.workflow_id ?? ''), node_id: String(payload.node_id) }
            // No plan is an answer too: say why instead of "checking..." forever.
            setNodeTrialPinning(plan ? { ...plan, ...where } : {
              ...where, required: [], optional: [], released_by_assumption: [], upstream: [],
              blocked: String(payload.pinning_error || 'no plan'), blocked_code: 'unavailable',
            })
          } else if (payload.action === 'workflow_node_trial_list') {
            if (payload.ok) {
              const trials = (payload.trials as WorkflowNodeTrial[]) ?? []
              setWorkflowNodeTrials(trials)
              if (workflowNodeTrialPollRef.current) clearTimeout(workflowNodeTrialPollRef.current)
              workflowNodeTrialPollRef.current = null
              if (trials.some(item => item.state === 'queued' || item.state === 'running')) {
                workflowNodeTrialPollRef.current = setTimeout(() => {
                  client.workflowNodeTrialList(String(payload.project_id ?? getActiveProjectId()))
                }, 900)
              }
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'workflow_flow_trial_start' || payload.action === 'workflow_flow_trial_continue' || payload.action === 'workflow_flow_trial_stop') {
            if (!currentProject.accepts(payload)) return
            setWorkflowFlowTrialBusy(false)
            if (payload.ok) {
              const trial = payload.flow_trial as NonNullable<FlowTrialSummary>
              if (trial?.flow_id) {
                const recorded = recordedFlowTrialTarget(trial)
                const key = recorded ? flowTrialTargetKey(trial.workflow_id, recorded) : null
                if (key && trial.request_id && trial.phase && trial.phase !== 'finished') {
                  flowTrialRequestIds.current[key] = trial.request_id
                } else if (key && trial.phase === 'finished' && flowTrialRequestIds.current[key] === trial.request_id) {
                  delete flowTrialRequestIds.current[key]
                }
                setWorkflowFlowTrials(prev => [trial, ...prev.filter(item => item.flow_id !== trial.flow_id)])
              }
              if (workflowFlowTrialPollRef.current) clearTimeout(workflowFlowTrialPollRef.current)
              workflowFlowTrialPollRef.current = setTimeout(() => {
                client.workflowFlowTrialList(String(payload.project_id ?? getActiveProjectId()), String(payload.workflow_id ?? ''))
              }, 500)
              showToast(payload.accepted ? '整流程正式试跑已开始' : '已找到同一整流程试跑请求，不会重复执行', 'success')
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'workflow_flow_trial_list') {
            if (!currentProject.accepts(payload)) return
            const workflowId = String(payload.workflow_id ?? '')
            const selected = workflowId !== '' && workflowId === builderFlowRef.current?.id
            if (selected) {
              const candidates = payload.ok ? readPublishedFlowCandidates(payload.published_candidates, workflowId) : null
              const error = typeof payload.published_candidates_error === 'string' ? payload.published_candidates_error.slice(0, 512) : null
              setPublishedFlowTrials({ workflowId, rows: !error && candidates ? candidates : [],
                error: error || (candidates ? null : '已发布目录暂时不可用，请刷新后再试。') })
            }
            if (payload.ok) {
              const rows = (payload.flow_trials as Array<NonNullable<FlowTrialSummary>>) ?? []
              for (const row of rows) {
                const recorded = recordedFlowTrialTarget(row)
                const key = recorded ? flowTrialTargetKey(row.workflow_id, recorded) : null
                if (key && row.request_id && row.phase && row.phase !== 'finished') {
                  flowTrialRequestIds.current[key] = row.request_id
                } else if (key && row.phase === 'finished' && flowTrialRequestIds.current[key] === row.request_id) {
                  delete flowTrialRequestIds.current[key]
                }
              }
              setWorkflowFlowTrials(prev => workflowId ? [...rows, ...prev.filter(row => row.workflow_id !== workflowId)] : rows)
              if (workflowFlowTrialPollRef.current) clearTimeout(workflowFlowTrialPollRef.current)
              workflowFlowTrialPollRef.current = null
              if (rows.some(item => item.state === 'queued' || item.state === 'running' || item.state === 'waiting_for_answer')) {
                workflowFlowTrialPollRef.current = setTimeout(() => {
                  client.workflowFlowTrialList(String(payload.project_id ?? getActiveProjectId()), String(payload.workflow_id ?? ''))
                }, 900)
              }
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'workflow_create_draft') {
            setCreatingDraftWorkflowId(null)
            if (payload.ok) {
              client.workflowList(getActiveProjectId())
              showToast(t('workflows.createDraft') + ' ✓', 'success')
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'workflow_record_test') {
            setRecordingTestWorkflowId(null)
            if (payload.ok) {
              client.workflowList(getActiveProjectId())
              showToast(t('workflows.saveTestRecord') + ' ✓', 'success')
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'workflow_simulate_test') {
            setSimulatingTestWorkflowId(null)
            if (payload.ok) {
              client.workflowList(getActiveProjectId())
              setWorkflowPreflight(payload.preflight as WorkflowPreflight)
              showToast(t('workflows.simulateTest') + ' ✓', 'success')
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'workflow_submit_testing') {
            setSubmittingTestingWorkflowId(null)
            if (payload.ok) {
              setAdmissionRefusal(null)
              client.workflowList(getActiveProjectId())
              showToast(t('workflows.submitTesting') + ' ✓', 'success')
            } else {
              setAdmissionRefusal({ workflowId: String(payload.workflow_id ?? ''), action: 'submit_testing', error: String(payload.error ?? t('app.requestFailed')) })
              // The toast in the owner's words too, not only the note beside the button (refusalWords knows the shapes).
              showToast(refusalWords(String(payload.error ?? t('app.requestFailed')))[locale.startsWith('zh') ? 0 : 1], 'error')
            }
          }
          if (payload.action === 'workflow_publish') {
            setPublishingWorkflowId(null)
            if (payload.ok) {
              client.workflowList(getActiveProjectId())
              showToast(t('workflows.publish') + ' ✓', 'success')
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'workflow_rollback') {
            setRollingBackWorkflowId(null)
            if (payload.ok) {
              client.workflowList(getActiveProjectId())
              showToast(t('workflows.rollback') + ' ✓', 'success')
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'workflow_rollout_start' || payload.action === 'workflow_rollout_observe' || payload.action === 'workflow_rollout_finalize' || payload.action === 'workflow_rollout_rollback') {
            if (payload.action !== 'workflow_rollout_rollback') setRolloutBusyWorkflowId(null)
            else setRolloutRollbackBusyWorkflowId(null)
            if (payload.ok) {
              client.workflowList(getActiveProjectId())
              showToast('Rollout evidence updated ✓', 'success')
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'workflow_rollout_evaluate') {
            if (!payload.ok) showToast(String(payload.error ?? t('app.requestFailed')), 'error')
          }
          if (payload.action === 'workflow_request_release') {
            setRequestingReleaseWorkflowId(null)
            if (payload.ok) {
              setAdmissionRefusal(null)
              client.workflowList(getActiveProjectId())
              showToast(t('workflows.requestRelease') + ' ✓', 'success')
            } else {
              setAdmissionRefusal({ workflowId: String(payload.workflow_id ?? ''), action: 'request_release', error: String(payload.error ?? t('app.requestFailed')) })
              showToast(refusalWords(String(payload.error ?? t('app.requestFailed')))[locale.startsWith('zh') ? 0 : 1], 'error')
            }
          }
          if (payload.action === 'workflow_decide_release') {
            setDecidingReleaseWorkflowId(null)
            if (payload.ok) {
              client.workflowList(getActiveProjectId())
              showToast(t('workflows.approveRelease') + ' ✓', 'success')
            } else {
              showToast(String(payload.error ?? t('app.requestFailed')), 'error')
            }
          }
          if (payload.action === 'workflow_skill_review') {
            if (payload.ok) client.workflowList(getActiveProjectId())
            showToast(payload.ok ? '技能评审已记录：谁看的、什么时候，写进了技能文件。' : String(payload.error ?? t('app.requestFailed')), payload.ok ? 'success' : 'error')
          }
          if (payload.action === 'workflow_update_nodes') {
            setUpdatingWorkflowId(null)
            if (payload.ok) {
              // The answer's package is drawn now (lib/savedWorkflow): the draft goes below, and the list read after it
              // would otherwise leave the canvas on the package from before this save until it arrives.
              setWorkflows(prev => (prev ? withSavedWorkflow(prev, payload.workflow) : prev))
              setNodesSaved(prev => ({ workflowId: String(payload.workflow_id ?? ''), seq: (prev?.seq ?? 0) + 1 }))
              setRefusedSave(null)
              // Here, not only on the page: the answer may arrive while the builder is not mounted, and a draft that
              // stayed open would keep asking "leave site?" after a save and come back later as an older draft.
              dropDraft(nodeDraftStateRef.current, String(payload.workflow_id ?? ''))
              client.workflowList(getActiveProjectId())
              showToast(t('workflows.nodesSaved'), 'success')
            } else {
              // The sentence goes to the toast; which step and which field go to the canvas, which points at them.
              // The server words a refused save in Chinese itself (refused_save_text); anything else it says goes
              // through refusalWords, so the toast never shows an English sentence the owner cannot act on.
              setRefusedSave({ workflowId: String(payload.workflow_id ?? ''), blockers: Array.isArray(payload.node_blockers) ? payload.node_blockers as NodeBlocker[] : [] })
              showToast(refusalWords(String(payload.error ?? t('app.requestFailed')))[locale.startsWith('zh') ? 0 : 1], 'error')
            }
          }
          if (payload.action === 'work_item_retry') {
            if (payload.ok) showToast(t('workspace.graph.retryStarted'), 'success')
            else showToast(String(payload.error ?? t('app.requestFailed')), 'error')
          }
          if (payload.action === 'artifacts_list') {
            // 交付物 of the project on screen only; a refusal that names no project still ends the wait.
            if (!currentProject.acceptsOrBareRefusal(payload)) return
            setArtifactLoading(false)
            if (payload.ok) {
              setArtifactError(null)
              setArtifactIndex({
                workspace_root: String(payload.workspace_root ?? ''),
                files: (payload.files as ArtifactIndex['files']) ?? [],
                groups: (payload.groups as ArtifactIndex['groups']) ?? [],
                truncated: payload.truncated === true,
              })
            } else {
              setArtifactError(String(payload.error ?? t('app.requestFailed')))
            }
          }
          if (payload.action === 'artifact_read') {
            if (!currentProject.acceptsOrBareRefusal(payload)) return
            setArtifactPreviewLoading(false)
            if (payload.ok) {
              setArtifactPreviewError(null)
              setArtifactPreview(payload as unknown as ArtifactPreview)
            } else {
              setArtifactPreview(null)
              setArtifactPreviewError(String(payload.error ?? t('app.requestFailed')))
            }
          }
          if (payload.action === 'artifact_reveal' && !payload.ok) {
            showToast(String(payload.error ?? t('app.requestFailed')), 'error')
          }
          // 资料 (data phase 2): every read answers for one project, and is shown only while that project is the
          // one on screen. 资料 never shows another project's files because a reply arrived late.
          if (payload.action === 'data_project_index' || payload.action === 'data_order_view'
            || payload.action === 'data_folder_list' || payload.action === 'data_preview'
            || payload.action === 'data_attachments') {
            if (!currentProject.accepts(payload)) return
            dispatchData({ kind: 'ack', ack: payload as DataAck })
          }
          if (payload.action === 'data_reveal' && payload.ok === false) {
            // Opening a folder happens on the machine the console runs on; when it cannot, say why in words.
            showToast(errorLabel(String(payload.error ?? 'error'), locale.startsWith('zh')), 'error')
          }
          if (String(payload.action ?? '').startsWith('data_dir_') || String(payload.action ?? '').startsWith('data_folder_')) {
            if (!currentProject.accepts(payload)) return
            dispatchFolder({ kind: 'ack', ack: payload as FolderAck })
            if (payload.ok && payload.action === 'data_dir_pick') {
              const picked = String(payload.path ?? '')
              if (picked) clientRef.current?.dataFolderPlan(getActiveProjectId(), folderStateRef.current.scope, picked)
            }
            if (payload.ok && payload.action === 'data_dir_create') {
              // Show the folder that was just made, where it was made.
              const made = String((payload.entry as { path?: string } | undefined)?.path ?? '')
              // Both separators: create_dir answers with a Windows path, so a rule that only knew '/' matched
              // nothing and the chooser walked into the new folder instead of showing it where it was made.
              const parent = made.replace(/[\\/][^\\/]+$/, '')
              if (parent) clientRef.current?.dataDirBrowse(getActiveProjectId(), parent)
            }
            if (payload.ok && payload.locations) setDataLocations(payload.locations as DataLocations)
            if (payload.ok && (payload.action === 'data_folder_set' || payload.action === 'data_folder_import')) {
              askDataIndex()   // the project's files are somewhere else now
            }
          }
          if (String(payload.action ?? '').startsWith('data_recycle_')) {
            if (!currentProject.accepts(payload)) return
            dispatchRecycle({ kind: 'ack', ack: payload as FolderAck })
            // A file came back, or files went away for good: 资料 shows the project's files and has to re-read them.
            if (payload.ok && payload.action !== 'data_recycle_list') askDataIndex()
          }
          if (payload.action === 'memory_overview' || payload.action === 'memory_delete') {
            if (!currentProject.accepts(payload)) return
            // A delete's reply carries the fresh overview, so the card is re-drawn from it: nothing to ask again.
            dispatchMemory({ kind: 'ack', ack: payload as Record<string, unknown> })
          }
          if (payload.action === 'data_locations') {
            if (!currentProject.accepts(payload)) return
            setDataLocations(payload.ok ? (payload as unknown as DataLocations) : null)
          }
          if (payload.action === 'data_backups') {
            if (!currentProject.accepts(payload)) return
            setDataBackups(payload.ok ? (payload as unknown as BackupList) : null)
          }
          if (payload.action === 'plan_removal') {
            setRemovalPlan(payload.ok ? (payload as unknown as RemovalPlan) : null)
          }
          if (payload.action === 'data_cleanup_preview') {
            if (!currentProject.accepts(payload)) return
            setCleanupPreview(payload.ok ? (payload as unknown as CleanupPreview) : null)
          }
          if (payload.action === 'data_usage') {
            // Usage is kept per project: a walk started before a switch still answers the project it measured, and
            // the owner's disk page asks about projects other than the active one.
            dispatchUsage(payload as DataAck)
          }
          if (payload.action === 'notify_test') {
            setSettingsNotifyResult({
              ok: payload.ok === true,
              error: typeof payload.error === 'string' ? payload.error : undefined,
              results: Array.isArray(payload.results) ? (payload.results as SettingsNotifyResult['results']) : [],
            })
          }
          if (payload.ok && payload.action === 'settings_test_model') {
            setSettingsTestResult({
              model: String(payload.model ?? ''),
              ok: payload.test_ok === true,
              latencyMs: typeof payload.latency_ms === 'number' ? payload.latency_ms : undefined,
              error: typeof payload.test_error === 'string' ? payload.test_error : undefined,
              answeredBy: typeof payload.answered_by === 'string' && payload.answered_by ? payload.answered_by : undefined,
            })
          }
          if (payload.ok && payload.action === 'session_detail') {
            if (!payloadMatchesActiveProject(payload, false)) return
            const detailGeneration = viewGenerationOf(payload)
            if (detailGeneration !== null && detailGeneration !== currentProject.generation) return
            const detailTaskId = typeof payload.task_id === 'string' ? payload.task_id : ''
            const detailMessages = Array.isArray(payload.messages)
              ? payload.messages.map(mapBackendMessage)
              : []
            const totalMessageCount = typeof payload.message_count === 'number'
              ? payload.message_count
              : detailMessages.length
            const detailLevel = payload.detail_level === 'full' ? 'full' : 'summary'
            const cs = chatStoreRef.current
            if (cs && detailMessages.length > 0) {
              cs.mergeMessagesFromBackend(detailMessages)
            }
            const ss = sessionStoreRef.current
            if (ss && detailTaskId) {
              const existingSession = ss.sessions.find(session => session.taskId === detailTaskId)
              const previousHasMore = detailLevel === 'full'
                ? existingSession?.fullHasMore
                : existingSession?.summaryHasMore
              const detailHasMore = mergeSessionDetailHasMore(
                previousHasMore,
                payload.has_more === true,
                payload.client_history_page === true,
              )
              const draftTurnId = String(existingSession?.draftTurnId ?? '').trim()
              const detailHasFinalForDraft = !!draftTurnId
                && !!cs
                && detailMessages.some(message => terminalAssistantTurnId(message) === draftTurnId)
              if (detailHasFinalForDraft) {
                ss.clearDraft(detailTaskId)
              }
              const rawSessionState = payload.session_state
              const sessionPatch = rawSessionState && typeof rawSessionState === 'object'
                ? mapBackendSession(rawSessionState)
                : null
              ss.updateSession(detailTaskId, {
                ...(sessionPatch ?? {}),
                ...(typeof payload.handoff_context === 'string' ? { handoffContext: payload.handoff_context } : {}),
                ...(typeof payload.handoff_to === 'string' ? { handoffTo: payload.handoff_to } : {}),
                messageCount: totalMessageCount,
                detailLoaded: true,
                ...(detailLevel === 'full' ? { fullLoaded: !detailHasMore } : {}),
                hasMore: detailHasMore,
                ...(detailLevel === 'full'
                  ? { fullHasMore: detailHasMore }
                  : { summaryHasMore: detailHasMore }),
                detailLoading: false,
                detailError: undefined,
                viewGeneration: detailGeneration ?? currentProject.generation,
              })
            }
          }
          if (Array.isArray(payload.agents)) {
            const nextAgents = mapAgentListPayload(payload.agents as unknown[], swarmAgentsRef.current)
            swarmAgentsRef.current = nextAgents
            setSwarmAgents(nextAgents)
          }
          // Handle project list response
          if (payload.ok && Array.isArray(payload.projects)) {
            setProjectIdPolicy(compileProjectIdPolicy(payload.project_id_policy))
            if (Array.isArray(payload.customers)) {
              setKnownCustomers((payload.customers as unknown[]).map(String).filter(Boolean))
            }
            const ps = projectStoreRef.current
            if (ps) {
              const previousActiveId = getActiveProjectId()
              const initialHydration = !projectsHydratedRef.current
              const shouldUseBackendActive = !projectsHydratedRef.current && !userSelectedProjectRef.current
              const backendActiveId = typeof payload.active_project_id === 'string'
                ? payload.active_project_id.trim()
                : ''
              const createdProjectId = payload.action === 'create_project' && typeof payload.project_id === 'string'
                ? payload.project_id.trim()
                : ''
              // Opening: the project the URL names, else the one this browser was last in (while it still exists),
              // else the server's -- which after a restart is always default.
              const activeId = shouldUseBackendActive
                ? normalizeProjectId(createdProjectId || startProjectId(payload.projects as { id: string }[]) || backendActiveId || activeProjectIdRef.current)
                : (createdProjectId ? normalizeProjectId(createdProjectId) : getActiveProjectId())
              if (createdProjectId) userSelectedProjectRef.current = true
              projectsHydratedRef.current = true
              // The first load "switches" to the project it opens on, by the same road as every switch. Begun before
              // the active id moves, so lib/currentProject knows whether the project on screen changes: opening on the
              // project already there throws nothing away (the lists asked for at connect time still land); opening
              // on the one this browser was last in empties what was read for the placeholder.
              const switching = activeId !== previousActiveId || initialHydration
              const switchSeq = switching ? currentProject.beginSwitch(activeId).switchSeq : ''
              activeProjectIdRef.current = activeId
              ps.initFromBackend(
                payload.projects as { id: string; name: string }[],
                activeId,
              )
              if (switching) {
                setStatusDetail(`Switching to ${activeId}...`)
                clientRef.current?.switchProject(activeId, switchSeq)
              }
            }
          }
          // Handle collab_sync response
          if (payload.ok && Array.isArray(payload.channels)) {
            if (!currentProject.acceptsSwitchReply(payload)) return
            const syncData = mapCollabSyncPayload(payload)
            // A partial push (the project index) carries no cost fields. Keep
            // what the last full sync said rather than blanking the headline
            // into "could not read" every time an index arrives.
            if (typeof syncData.recordedCostTotalUsd === 'number') {
              setRecordedCost({
                totalUsd: syncData.recordedCostTotalUsd,
                calls: syncData.recordedCostCallCount ?? 0,
              })
            } else if ('recorded_cost_total_usd' in payload) {
              // Present and not a number: the server read the record and could
              // not answer. That is a real "unknown" and must be shown.
              setRecordedCost({ totalUsd: null, calls: 0 })
            }
            const syncProjectId = projectIdOf(payload)
            if (!syncProjectId) return
            setRecordedCostObservation(previous => applyProjectCostObservation(previous, syncData.recordedCostObservation, syncProjectId))
            // The switch is settled -- said to lib/currentProject before the active id moves, so it can tell a sync
            // that applies a switch from one of the project already on screen. The project's state was emptied when
            // the switch began; what it threw away is asked for again now (REISSUE_ON_SETTLE: the pauses, the owner's
            // browser sessions, the page's lists).
            const settled = currentProject.settle(syncProjectId)
            activeProjectIdRef.current = syncProjectId
            projectStoreRef.current?.setActiveProject(syncProjectId)
            for (const name of settled.reissue) reissueOnSettleRef.current[name](syncProjectId)
            if (settled.applied) {
              // Writes that were in flight when the switch began answer for the previous project: their busy flags
              // are cleared here, where they always were.
              setPreflightingWorkflowId(null)
              setWorkflowNodeTrialBusy(false)
              setWorkflowFlowTrialBusy(false)
              setCreatingDraftWorkflowId(null)
              setRecordingTestWorkflowId(null)
              setSubmittingTestingWorkflowId(null)
              setRollingBackWorkflowId(null)
              setRequestingReleaseWorkflowId(null)
              setDecidingReleaseWorkflowId(null)
              setUpdatingFdeControlPlaneOrderId(null)
            }
            setStatusDetail('')
            const cs = chatStoreRef.current
            if (cs) {
              cs.initFromBackend(syncProjectId, syncData.channels, syncData.messages)
            }
            // 总控对话栏: this project's conversations are on hand now (lib/masterThreads ConsoleStand).
            setMasterHistoryFor(syncProjectId)
            const bs = boardStoreRef.current
            if (bs) {
              bs.initFromBackend(syncProjectId, syncData.boards, syncData.columns, syncData.tasks)
            }
            const ss = sessionStoreRef.current
            if (ss) {
              ss.initFromBackend(syncProjectId, syncData.sessions)
              // After sync, refresh active session detail so content is loaded
              const activeId = ss.activeSessionId
              if (activeId) {
                scheduleSessionDetailRefresh(activeId, 'full', true)
              }
            }
          }
        } catch (e) { console.error('[onAck] Error:', e) }
      },
      onStatus: (next, detail) => {
        if (next !== 'connected') clearBrowserAccountInspection()
        employeeAssets.connectionChanged(next === 'connected')
        observatoryTrace.setConnection(next)
        if (next !== 'connected') observatoryHistory.cancel()
        setStatus(next)
        if (next !== 'connected') {
          // A reconnect must obtain a fresh row and frame before any browser input.
          // The same server resource may survive the socket, but the old picture is stale.
          browserViewRequestRef.current = null
          browserHandoffNoticesRef.current.clear()
          const staleBrowserRows = browserSessionsRef.current.map(row => ({ ...row, resource_id: undefined, control_fence: undefined }))
          browserSessionsRef.current = staleBrowserRows
          setBrowserSessions(staleBrowserRows)
          setBrowserLiveFrame(null)
          setBrowserLiveBinding(null)
          setBrowserPickedElement(null)
          clearCustomerPreview()
          if (workflowNodeTrialPollRef.current) clearTimeout(workflowNodeTrialPollRef.current)
          workflowNodeTrialPollRef.current = null
          // A write that was in flight has an unknown outcome now, and
          // silence would read as "nothing was sent".
          if (specSavingRef.current || trialRequestRef.current?.phase === 'pending')
            setFactoryError('连接断开：刚才的保存或试跑结果未知。重连后请刷新本版本核验；不会自动重发。')
          setSpecSaving(false)
          const inFlight = workflowWritesInFlightRef.current
          if (inFlight.busy || inFlight.invoking) {
            recheckWorkflowsOnReconnectRef.current = true
            showToast('连接断开：进行中的工作流/工单操作结果未知。重连后会刷新列表供核对，不会自动重发。', 'error')
          }
          if (inFlight.invoking) {
            setWorkflowInvoke(prev => (prev?.status === 'sending'
              ? { ...prev, status: 'error', error: '连接断开：发起结果未知。重连后请在工单列表核对，不会自动重发。' }
              : prev))
          }
          for (const clearBusy of [
            setCreatingDraftWorkflowId, setRecordingTestWorkflowId, setSimulatingTestWorkflowId, setSubmittingTestingWorkflowId,
            setPublishingWorkflowId, setRollingBackWorkflowId, setRolloutBusyWorkflowId, setRolloutRollbackBusyWorkflowId,
            setRequestingReleaseWorkflowId, setDecidingReleaseWorkflowId, setUpdatingWorkflowId, setAcceptanceBusyOrderId,
            setUpdatingFdeControlPlaneOrderId, setPreflightingWorkflowId,
          ]) clearBusy(null)
          setWorkflowNodeTrialBusy(false)
          setWorkflowFlowTrialBusy(false)
        }
        setStatusDetail(detail ?? '')
        // A new socket must prove its principal again. Clearing auth also stops protected polls during reconnect;
        // initialization reads happen in onAuth, never on the transport's open event.
        if (next !== 'connected') {
          ownerSignedInRef.current = false
          authRef.current = null
          setAuth(null)
        }
        if (next !== 'connected') setProjectIdPolicy(null)
        if (next !== 'connected') setCapabilityLoading(false)
        // A read, not a write: nothing is unknown, the page just stops waiting and offers 刷新 again.
        if (next !== 'connected') setComponentCatalogLoading(false)
        if (next !== 'connected') {
          // A download, test mark or admission in flight has an unknown outcome now: forget the trial rows (a card
          // must not offer 准入 for a version admitted meanwhile) and the open third-party file; reconnecting re-reads them.
          setMarketBusy(prev => (Object.keys(prev).length ? {} : prev))
          setMarketBrowse(prev => (prev.loading ? { ...prev, loading: false } : prev))
          setSkillTrials(prev => (prev.length ? [] : prev))
          // A download waiting to open the builder has an unknown outcome now; a later download must not jump there.
          candidateUseAfterDownloadRef.current = {}
          catalogOpenAfterImportRef.current.clear()
          setTrialFileView(null)
          setSkillTrialError(null)
        }
      },
      onCollabMessage: (type, payload) => {
        try {
          if (type === 'project_index_push') {
            if (!currentProject.acceptsSwitchReply(payload)) return
          } else if (!payloadMatchesActiveProject(payload, false)) return
          const cs = chatStoreRef.current
          if (type === 'chat_new_message') {
            if (cs) cs.addMessageFromBackend(mapBackendMessage(payload))
          } else if (type === 'chat_channel_created') {
            if (cs) cs.addChannelFromBackend(mapBackendChannel(payload))
          } else if (type === 'session_runtime_control') {
            const ss = sessionStoreRef.current
            if (ss) {
              const taskIds = Array.isArray(payload.task_ids)
                ? payload.task_ids.map(String).filter(Boolean)
                : []
              const patch: Partial<import('./types/kanban').Session> = {
                runtimeControlState: String(payload.runtime_control_state ?? payload.runtimeControlState ?? 'idle') as any,
                canStop: Boolean(payload.can_stop ?? payload.canStop),
                canResume: Boolean(payload.can_resume ?? payload.canResume),
                resumeParentSessionId: String(payload.resume_parent_session_id ?? payload.resumeParentSessionId ?? ''),
                pendingRuntimeCheckpointId: String(payload.pending_runtime_checkpoint_id ?? payload.pendingRuntimeCheckpointId ?? ''),
                stopIntentId: String(payload.stop_intent_id ?? payload.stopIntentId ?? ''),
              }
              for (const taskId of taskIds) {
                ss.updateSession(taskId, patch)
              }
            }
          } else if (type === 'board_task_status_changed') {
            const taskId = String(payload.task_id ?? '')
            const columnId = String(payload.column_id ?? '')
            const statusStr = String(payload.status ?? '')
            // An order follows its own production run on the server (started -> producing, finished -> delivering):
            // when that run moves, the order list on screen is asked for again rather than left saying "launched".
            if (taskId && (statusStr === 'running' || statusStr === 'done')
              && (ordersRef.current ?? []).some(order => order.launched_task_id === taskId && (order.status === 'launched' || order.status === 'producing'))) {
              refreshOrderListRef.current()
            }
            const isTerminal = ['done', 'failed', 'cancelled'].includes(statusStr)
            if (taskId && columnId) {
              const bs = boardStoreRef.current
              if (bs) {
                const task = bs.tasks.find(t => t.id === taskId)
                if (task) {
                  const partial: Partial<import('./types/kanban').KanbanTask> = {}
                  if (task.columnId !== columnId) {
                    bs.moveTask(taskId, columnId, 0)
                  }
                  if (statusStr === 'running') {
                    if (!task.agentStatus || task.agentStatus === 'idle') {
                      partial.agentStatus = 'reflecting'
                    }
                  } else if (task.agentStatus || task.currentTool || task.displayTool) {
                    partial.agentStatus = undefined
                    partial.currentTool = undefined
                    partial.displayTool = undefined
                  }
                  if (Object.keys(partial).length > 0) {
                    bs.updateTask(taskId, partial)
                  }
                }
              }
              const ss = sessionStoreRef.current
              if (ss) {
                const session = ss.sessions.find(s => s.taskId === taskId)
                const sessionPatch: Partial<import('./types/kanban').Session> = {
                  columnId,
                  status: statusStr || columnId,
                }
                if (statusStr === 'running') {
                  if (!session?.agentStatus || session.agentStatus === 'idle') {
                    sessionPatch.agentStatus = 'reflecting'
                  }
                } else if (session?.agentStatus || session?.currentTool || session?.displayTool || isTerminal) {
                  sessionPatch.agentStatus = undefined
                  sessionPatch.currentTool = undefined
                  sessionPatch.displayTool = undefined
                }
                Object.assign(
                  sessionPatch,
                  companyRuntimeControlPatchForBoardStatus(session, statusStr),
                )
                ss.updateSession(taskId, {
                  ...sessionPatch,
                })
              }
            }
          } else if (type === 'execution_mode_resolved') {
            const mode = String(payload.mode ?? '')
            const profile = String(payload.profile ?? '')
            // Update UI mode selector to reflect what engine actually decided (#13)
            if (mode) {
              const normalizedMode = normalizeExecMode(mode)
              setGlobalExecMode(normalizedMode)
              setGlobalCompanyProfile(
                normalizedMode === 'org'
                  ? 'custom'
                  : normalizedMode === 'company'
                    ? normalizeCompanyProfile(profile)
                    : 'corporate',
              )
            } else if (profile) {
              setGlobalCompanyProfile(normalizeCompanyProfile(profile))
            }
          } else if (type === 'project_run_updated' || type === 'seat_digest_updated' || type === 'work_item_batch_updated') {
            pushedCollabSync.request()
          } else if (type === 'collab_sync_push' || type === 'project_index_push') {
            const syncScope = String((payload as Record<string, unknown>).sync_scope ?? (payload as Record<string, unknown>).syncScope ?? '').toLowerCase()
            const isProjectIndexPush = type === 'project_index_push' || syncScope === 'index'
            if (!currentProject.acceptsSwitchReply(payload)) return
            const syncData = mapCollabSyncPayload(payload)
            // A partial push (the project index) carries no cost fields. Keep
            // what the last full sync said rather than blanking the headline
            // into "could not read" every time an index arrives.
            if (typeof syncData.recordedCostTotalUsd === 'number') {
              setRecordedCost({
                totalUsd: syncData.recordedCostTotalUsd,
                calls: syncData.recordedCostCallCount ?? 0,
              })
            } else if ('recorded_cost_total_usd' in payload) {
              // Present and not a number: the server read the record and could
              // not answer. That is a real "unknown" and must be shown.
              setRecordedCost({ totalUsd: null, calls: 0 })
            }
            const syncProjectId = projectIdOf(payload)
            if (!syncProjectId) return
            setRecordedCostObservation(previous => applyProjectCostObservation(previous, syncData.recordedCostObservation, syncProjectId))
            // A switch can settle here first (a pushed sync): the same settle as the onAck path above, with the same
            // re-asks. The reset itself ran when the switch began (lib/currentProject), on every road alike.
            const settled = currentProject.settle(syncProjectId)
            activeProjectIdRef.current = syncProjectId
            projectStoreRef.current?.setActiveProject(syncProjectId)
            for (const name of settled.reissue) reissueOnSettleRef.current[name](syncProjectId)
            setStatusDetail('')
            if (isProjectIndexPush) {
              const ss2 = sessionStoreRef.current
              if (ss2) {
                ss2.initFromBackend(syncProjectId, syncData.sessions, {
                  preserveExistingWhenIncomingPartial: true,
                  preserveActiveWhenMissing: true,
                })
              }
              clientRef.current?.collabSync(syncProjectId, undefined, currentProject.generation)
              return
            }
            const cs2 = chatStoreRef.current
            if (cs2) {
              cs2.initFromBackend(syncProjectId, syncData.channels, syncData.messages)
            }
            setMasterHistoryFor(syncProjectId)
            const bs2 = boardStoreRef.current
            if (bs2) {
              bs2.initFromBackend(syncProjectId, syncData.boards, syncData.columns, syncData.tasks)
            }
            const ss2 = sessionStoreRef.current
            if (ss2) {
              ss2.initFromBackend(syncProjectId, syncData.sessions)
              // Re-sync active session detail after full state push
              const activeId = ss2.activeSessionId
              if (activeId) {
                scheduleSessionDetailRefresh(activeId, 'full', true)
              }
            }
          }
        } catch (e) { console.error('[onCollabMessage] Error:', e) }
      },
      onAgentRuntimeUpdate: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        if (payload.agent_id) {
          setSwarmAgents((prev) => {
            const next = prev.map((agent) => (
              agent.agent_id === payload.agent_id
                ? {
                    ...agent,
                    status: payload.status,
                    runtime_status: payload.status,
                    current_tool: payload.current_tool ?? undefined,
                    current_task_id: payload.task_id ?? undefined,
                  }
                : agent
            ))
            swarmAgentsRef.current = next
            return next
          })
        }
        const bs = boardStoreRef.current
        if (!bs) return
        // Only update the specific task the agent is working on (#2)
        if (payload.task_id) {
          const rawPayload = payload as unknown as Record<string, unknown>
          const boardRuntimePatch = kanbanRuntimePatchFromPayload(rawPayload)
          const sessionRuntimePatch = sessionRuntimePatchFromPayload(rawPayload)
          if (hasOwnPayloadField(rawPayload, 'current_tool')) {
            const currentTool = typeof rawPayload.current_tool === 'string' && rawPayload.current_tool.trim()
              ? rawPayload.current_tool
              : undefined
            boardRuntimePatch.currentTool = currentTool
            sessionRuntimePatch.currentTool = currentTool
            if (currentTool) {
              boardRuntimePatch.displayTool = currentTool
              sessionRuntimePatch.displayTool = currentTool
            }
          }
          if (hasOwnPayloadField(rawPayload, 'display_tool')) {
            const displayTool = typeof rawPayload.display_tool === 'string' && rawPayload.display_tool.trim()
              ? rawPayload.display_tool
              : undefined
            if (displayTool) {
              boardRuntimePatch.displayTool = displayTool
              sessionRuntimePatch.displayTool = displayTool
            }
            // An empty display_tool arriving between tools keeps the sticky last
            // command; only the terminal-status clear below resets it. This is
            // what stops the header tool-pill from flickering once per tool call.
          }
          if (runtimeStatusClearsDisplayTool(payload.status)) {
            boardRuntimePatch.displayTool = undefined
            sessionRuntimePatch.displayTool = undefined
          }
          if (hasOwnPayloadField(rawPayload, 'tool_elapsed_ms')) {
            const toolElapsedMs = typeof rawPayload.tool_elapsed_ms === 'number' ? rawPayload.tool_elapsed_ms : undefined
            boardRuntimePatch.toolElapsedMs = toolElapsedMs
            sessionRuntimePatch.toolElapsedMs = toolElapsedMs
          }
          if (hasOwnPayloadField(rawPayload, 'last_tool_summary')) {
            const lastToolSummary = typeof rawPayload.last_tool_summary === 'string' ? rawPayload.last_tool_summary : undefined
            boardRuntimePatch.lastToolSummary = lastToolSummary
            sessionRuntimePatch.lastToolSummary = lastToolSummary
          }
          if (typeof payload.iteration === 'number') boardRuntimePatch.iterationCount = payload.iteration
          bs.updateTask(payload.task_id, {
            agentStatus: payload.status,
            ...boardRuntimePatch,
          })
          // Also update session sidebar status
          const ss = sessionStoreRef.current
          if (ss) {
            ss.updateSession(payload.task_id, {
              agentStatus: payload.status,
              ...sessionRuntimePatch,
              updatedAt: Date.now(),
            })
          }
          scheduleSessionDetailRefresh(payload.task_id)
        }
      },
      onRuntimeStatusSync: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const ss = sessionStoreRef.current
        const bs = boardStoreRef.current
        if (!ss) return
        // Periodic reconciliation against the backend's authoritative status.
        // Diff before dispatching: a tick where nothing drifted must not
        // trigger a single store update (and therefore no re-render).
        for (const entry of payload.sessions ?? []) {
          const taskId = String(entry.task_id ?? '').trim()
          if (!taskId) continue
          const session = ss.sessions.find((s) => s.taskId === taskId)
          if (!session) continue
          const status = String(entry.status ?? '').trim()
          const patch: Partial<import('./types/kanban').Session> = {}
          if (status && status !== session.status) patch.status = status
          const rawAgentStatus = typeof entry.agent_status === 'string' ? entry.agent_status.trim() : ''
          if (rawAgentStatus === 'idle' || rawAgentStatus === 'reflecting' || rawAgentStatus === 'tool_active') {
            if (rawAgentStatus !== session.agentStatus) patch.agentStatus = rawAgentStatus
            const tool = typeof entry.current_tool === 'string' && entry.current_tool.trim()
              ? entry.current_tool
              : undefined
            if (tool !== session.currentTool) patch.currentTool = tool
            if (runtimeStatusClearsDisplayTool(rawAgentStatus) && session.displayTool !== undefined) {
              patch.displayTool = undefined
            }
          } else {
            // No live tracker for this task: only clear stale indicators when
            // the backend says the task is no longer running (mirrors the
            // mergeLiveRuntimeField semantics used by collab_sync).
            const controlActive = session.runtimeControlState === 'running'
              || session.runtimeControlState === 'suspending'
              || session.runtimeControlState === 'resuming'
            if (status && status !== 'running' && !controlActive) {
              if (session.agentStatus !== undefined) patch.agentStatus = undefined
              if (session.currentTool !== undefined) patch.currentTool = undefined
              if (session.displayTool !== undefined) patch.displayTool = undefined
            }
          }
          if (Object.keys(patch).length === 0) continue
          ss.updateSession(taskId, patch)
          if (bs && ('agentStatus' in patch || 'currentTool' in patch || 'displayTool' in patch)) {
            const boardPatch: Partial<KanbanTask> = {}
            if ('agentStatus' in patch) boardPatch.agentStatus = patch.agentStatus as KanbanTask['agentStatus']
            if ('currentTool' in patch) boardPatch.currentTool = patch.currentTool
            if ('displayTool' in patch) boardPatch.displayTool = patch.displayTool
            bs.updateTask(taskId, boardPatch)
          }
        }
      },
      onWorkerNotification: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const data = payload as Record<string, unknown>
        const taskId = typeof payload.task_id === 'string' ? payload.task_id : ''
        if (!taskId) return
        const notification = data as import('./types/kanban').WorkerNotification
        // This presentation-only preference never filters the stores or safety observations below.
        if (quietPrincipalRef.current && Date.now() >= safetyToastUntilRef.current
          && shouldNotifyBackgroundCompletion({ payload: data, projectId: getActiveProjectId(),
            activeTaskId: sessionStoreRef.current?.activeSessionId ?? '', quiet: quietCompletionRef.current })) {
          showToast(translateFor(localeRef.current)('prefs.quiet.completed', { task: taskId }), 'success')
        }
        sessionStoreRef.current?.updateSession(taskId, {
          ...sessionRuntimePatchFromPayload(data),
          latestNotification: notification,
          updatedAt: Date.now(),
        })
        boardStoreRef.current?.updateTask(taskId, {
          ...kanbanRuntimePatchFromPayload(data),
          latestNotification: notification,
        })
        scheduleSessionDetailRefresh(taskId)
      },
      onSessionProgress: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const ss = sessionStoreRef.current
        if (!ss || !payload.task_id || !payload.entry) return
        ss.appendProgress(payload.task_id, {
          type: payload.entry.type as any,
          summary: payload.entry.summary,
          detail: payload.entry.detail,
          timestamp: payload.entry.timestamp * 1000,
          turnId: typeof payload.entry.turn_id === 'string'
            ? payload.entry.turn_id
            : typeof payload.entry.turnId === 'string'
              ? payload.entry.turnId
              : undefined,
          itemId: typeof payload.entry.item_id === 'string'
            ? payload.entry.item_id
            : typeof payload.entry.itemId === 'string'
              ? payload.entry.itemId
              : undefined,
          streamId: typeof payload.entry.stream_id === 'string'
            ? payload.entry.stream_id
            : typeof payload.entry.streamId === 'string'
              ? payload.entry.streamId
              : undefined,
          toolCallId: typeof payload.entry.tool_call_id === 'string'
            ? payload.entry.tool_call_id
            : typeof payload.entry.toolCallId === 'string'
              ? payload.entry.toolCallId
              : undefined,
          permissionGroupKey: typeof payload.entry.permission_group_key === 'string'
            ? payload.entry.permission_group_key
            : typeof payload.entry.permissionGroupKey === 'string'
              ? payload.entry.permissionGroupKey
              : undefined,
          seq: typeof payload.entry.seq === 'number' ? payload.entry.seq : undefined,
          executionMode: typeof payload.entry.execution_mode === 'string'
            ? payload.entry.execution_mode
            : typeof payload.entry.executionMode === 'string'
              ? payload.entry.executionMode
              : undefined,
        })
        if (payload.entry.type === 'tool_call') {
          const toolLabel = String(payload.entry.summary ?? '').trim()
          ss.updateSession(payload.task_id, {
            ...(toolLabel ? { currentTool: toolLabel, displayTool: toolLabel } : {}),
            updatedAt: payload.entry.timestamp * 1000,
          })
        }
        if (payload.entry.type !== 'thinking' && payload.entry.type !== 'verification') {
          scheduleSessionDetailRefresh(payload.task_id)
        }
      },
      onBoardEvent: (payload) => {
        if (!payloadMatchesActiveProject(payload, false)) return
        const bs = boardStoreRef.current
        if (!bs) return
        const taskId = String(payload.task_id ?? '')
        if (!taskId) return
        const ss = sessionStoreRef.current
        if (ss) {
          const session = ss.sessions.find(s => s.taskId === taskId)
          if (session?.mode === 'child') return
        }
        const assigneeIds = Array.isArray(payload.assignee_ids) ? payload.assignee_ids.map(String) : []
        const workItemIdentity = workItemIdentityPatchFromPayload(payload)
        const existing = bs.tasks.find(t => t.id === taskId)
        if (existing) {
          const partial: Partial<import('./types/kanban').KanbanTask> = {}
          if (payload.title) partial.title = String(payload.title)
          if (payload.display_id) partial.displayId = String(payload.display_id)
          if (assigneeIds.length > 0) partial.assigneeIds = assigneeIds
          Object.assign(partial, workItemIdentity)
          if (Object.keys(partial).length > 0) {
            bs.updateTask(taskId, partial)
          }
          return
        }
        const boardId = String(payload.board_id ?? bs.activeBoardId ?? 'default')
        // Find the real Todo column ID for this board
        const boardCols = bs.columns.filter(c => c.boardId === boardId)
        const todoCol = boardCols.find(c => c.name === 'Todo')
        const columnId = todoCol?.id ?? boardCols[0]?.id ?? ''
        if (!columnId) return // no columns exist yet — skip
        bs.createTask({
          boardId,
          columnId,
          title: String(payload.title ?? t('arch.untitled')),
          taskId,
          displayId: payload.display_id ? String(payload.display_id) : undefined,
          assigneeIds,
        })
        if (Object.keys(workItemIdentity).length > 0) {
          bs.updateTask(taskId, workItemIdentity)
        }
      },
      onSessionCreated: (payload) => {
        try {
          if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
          const ss = sessionStoreRef.current
          if (!ss) return
          const taskId = String(payload.task_id ?? '')
          if (!taskId) return
          const eventProjectId = projectIdOf(payload as unknown as Record<string, unknown>)
          if (!eventProjectId) return
          const existing = taskId ? ss.sessions.find(s => s.taskId === taskId) : undefined
          const workItemIdentity = workItemIdentityPatchFromPayload(payload)
          const normalizedSessionExecMode = normalizeExecMode(payload.exec_mode ?? existing?.execMode)
          const payloadCompanyProfile = companyProfileForExecMode(
            normalizedSessionExecMode,
            payload.company_profile ?? existing?.companyProfile,
          )
          const payloadOrgId = orgIdForExecMode(
            normalizedSessionExecMode,
            payload.org_id ?? payload.organization_id ?? existing?.orgId,
          )
          if (normalizedSessionExecMode === 'org' && payloadOrgId) setActiveSavedOrg(payloadOrgId)
          ss.createSession({
            projectId: eventProjectId,
            taskId,
            channelId: payload.channel_id,
            sessionId: payload.session_id,
            parentSessionId: payload.parent_session_id,
            originTaskId: payload.origin_task_id ?? existing?.originTaskId ?? taskId,
            mode: payload.parent_session_id ? 'child' : 'primary',
            execMode: normalizedSessionExecMode,
            companyProfile: payloadCompanyProfile,
            orgId: payloadOrgId,
            preferredAgent: normalizeTaskPreferredAgent(payload.preferred_agent ?? existing?.preferredAgent),
            nativeApprovalLevel: payload.native_approval_level ?? existing?.nativeApprovalLevel ?? globalNativeApprovalDefaultRef.current,
            nativePermissionScopeId: payload.native_permission_scope_id ?? existing?.nativePermissionScopeId,
            title: payload.title,
            status: payload.status,
            columnId: existing?.columnId ?? 'todo',
            assigneeIds: Array.isArray(payload.assignee_ids)
              ? payload.assignee_ids.map(String)
              : (existing?.assigneeIds ?? []),
            priority: existing?.priority ?? null,
            tags: existing?.tags ?? [],
            progressLog: existing?.progressLog ?? [],
            createdAt: typeof payload.created_at === 'number'
              ? payload.created_at * 1000
              : (existing?.createdAt ?? Date.now()),
            updatedAt: Date.now(),
            messageCount: 0,
            ...workItemIdentity,
          })
          // Ensure a kanban card exists for this session (1 session = 1 card)
          const bs = boardStoreRef.current
          const execMode = normalizedSessionExecMode
          if (bs && execMode === 'task' && !payload.parent_session_id && taskId && !bs.tasks.find(t => t.id === taskId) && bs.activeBoardId) {
            const boardCols = bs.columns.filter(c => c.boardId === bs.activeBoardId)
            const todoCol = boardCols.find(c => c.name === 'Todo')
            if (todoCol) {
              bs.createTask({
                boardId: bs.activeBoardId,
                columnId: todoCol.id,
                title: payload.title,
                taskId,
                assigneeIds: existing?.assigneeIds ?? [],
              })
            }
          }
          // Only auto-select if the user explicitly created this session.
          if (pendingSessionCreateRef.current) {
            if (pendingSessionCreateProjectIdRef.current !== eventProjectId) return
            clearPendingSessionCreate()
            ss.setActiveSession(payload.task_id)
            // Force a detail refresh so content loads immediately for the new active session
            scheduleSessionDetailRefresh(payload.task_id, 'full', true)
          }
        } catch (e) { console.error('[onSessionCreated] Error:', e) }
      },
      onSessionUpdated: (payload) => {
        try {
          if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
          const ss = sessionStoreRef.current
          if (!ss || !payload.task_id) return
          const nextExecMode = normalizeExecMode(payload.exec_mode ?? ss.sessions.find(s => s.taskId === payload.task_id)?.execMode)
          ss.updateSession(payload.task_id, {
            ...(payload.exec_mode ? { execMode: nextExecMode } : {}),
            ...(payload.exec_mode || payload.company_profile ? { companyProfile: companyProfileForExecMode(nextExecMode, payload.company_profile) } : {}),
            ...('org_id' in payload || 'organization_id' in payload ? { orgId: orgIdForExecMode(nextExecMode, payload.org_id ?? payload.organization_id) } : {}),
            ...(payload.preferred_agent ? { preferredAgent: normalizeTaskPreferredAgent(payload.preferred_agent) } : {}),
            ...(payload.selected_execution_agent ? { selectedExecutionAgent: normalizeTaskPreferredAgent(payload.selected_execution_agent) } : {}),
            ...(payload.native_approval_level ? { nativeApprovalLevel: payload.native_approval_level } : {}),
            ...(payload.native_permission_scope_id ? { nativePermissionScopeId: payload.native_permission_scope_id } : {}),
            ...(payload.workflow_contract ? { workflowContract: payload.workflow_contract } : {}),
          })
          if ((payload.exec_mode === 'org' || payload.exec_mode === 'custom') && (payload.org_id || payload.organization_id)) {
            setActiveSavedOrg(String(payload.org_id ?? payload.organization_id))
          }
        } catch (e) { console.error('[onSessionUpdated] Error:', e) }
      },
      onNativePermissionDefaultUpdated: (payload) => {
        if (payload.native_approval_default) {
          globalNativeApprovalDefaultRef.current = payload.native_approval_default
          setGlobalNativeApprovalDefault(payload.native_approval_default)
        }
      },
      onBrowserSessionChanged: (payload) => {
        if (!currentProject.accepts(payload)) return
        if (browserAccountInspectionRef.current?.request.binding_key === payload.binding_key) clearBrowserAccountInspection()
        const notice = browserPushScope(payload)
        if (!notice) return
        const known = browserSessionsRef.current.find(row => row.binding_key === notice.binding_key)
        if (known && !browserFrameMatchesSession(notice, known)) {
          // A conflicting notice invalidates old input immediately, but cannot
          // authorize the replacement resource before the list is refreshed.
          const stale = browserSessionsRef.current.map(row => row.binding_key === notice.binding_key
            ? { ...row, resource_id: undefined, control_fence: undefined } : row)
          browserSessionsRef.current = stale
          setBrowserSessions(stale)
          if (browserViewRequestRef.current?.binding_key === notice.binding_key) {
            browserViewRequestRef.current = null
            setBrowserLiveFrame(null)
            setBrowserLiveBinding(null)
            setBrowserPickedElement(null)
          }
        }
        // A push is a refresh notice, never authority to act on a new resource.
        // Only a freshly read row matching this notice may reopen a handoff.
        if (payload.state === 'awaiting_human') browserHandoffNoticesRef.current.set(notice.binding_key, notice)
        clientRef.current?.browserSessionList(getActiveProjectId())
      },
      onBrowserFrame: (payload) => {
        if (!currentProject.accepts(payload)) return
        const scope = browserPushScope(payload)
        const row = browserSessionsRef.current.find(item => item.binding_key === scope?.binding_key)
        if (!scope || !browserFrameMatchesSession(scope, row) || !browserFrameMatchesSession(scope, browserViewRequestRef.current)) return
        // Keep only a requested picture of the exact current resource and control generation.
        setBrowserLiveFrame({ ...scope, data: String(payload.data ?? ''),
          width: typeof payload.width === 'number' ? payload.width : null, height: typeof payload.height === 'number' ? payload.height : null })
      },
      onSessionMessage: (payload) => {
        try {
          if (!payloadMatchesActiveProject(payload, false)) return
          const cs = chatStoreRef.current
          if (!cs) return
          const mapped = mapBackendMessage(payload)
          // After every sync the server replays the owner's open cards as session messages. A message the page
          // already shows, or has already handled in exactly this form, is not news: it asks for no detail refresh
          // (: three replayed cards re-read three whole transcripts every few seconds).
          const shownInStore = storeAlreadyShows(cs.getChannelMessages(mapped.channelId), mapped)
          const handledBefore = sessionMessageLedgerRef.current.seenBefore(mapped)
          if (!shownInStore) cs.addMessageFromBackend(mapped)
          if (shownInStore || handledBefore) return
          if (mapped.channelId.startsWith('master:')) {
            // 总控对话栏: a message moves its conversation up the column; a build's line may change its dot, so the
            // project's list is read again (the server also re-sends it to the socket that caused the change).
            setMasterThreads(current => touchThread(current, mapped.channelId, mapped.timestamp))
            const meta = mapped.metadata as Record<string, unknown> | undefined
            if (meta?.type === 'master_build' && isOwnerConsole(authRef.current)) clientRef.current?.masterThreadList(getActiveProjectId())
          }
          console.debug('[onSessionMessage]', mapped.sender, mapped.channelId, mapped.content?.slice(0, 60))
          const taskId = mapped.channelId.startsWith('session:') ? mapped.channelId.slice('session:'.length) : ''
          const terminalTurnId = terminalAssistantTurnId(mapped)
          const activeDraftTurnId = String(
            sessionStoreRef.current?.sessions.find(session => session.taskId === taskId)?.draftTurnId ?? '',
          ).trim()
          if (taskId && mapped.sender !== 'user') {
            if (terminalTurnId && terminalTurnId === activeDraftTurnId) {
              sessionStoreRef.current?.clearDraft(taskId)
            }
            // Force refresh — session messages are critical content that must sync
            scheduleSessionDetailRefresh(taskId, 'full', true)
          }
        } catch (e) { console.error('[onSessionMessage] Error:', e) }
      },
      onSessionTitleUpdated: (payload) => {
        try {
          if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
          const ss = sessionStoreRef.current
          if (ss) ss.updateSession(payload.task_id, { title: payload.title })
          // Also update the matching kanban board's name so the title shown
          // on top of the kanban stays in sync with the sidebar.  In company
          // mode the board id equals the primary session's task_id (or
          // origin_task_id for child sessions).
          const bs = boardStoreRef.current
          if (bs) {
            const session = ss?.sessions.find(s => s.taskId === payload.task_id)
            const boardId = session?.originTaskId ?? payload.task_id
            const board = bs.boards.find(b => b.id === boardId)
            if (board && board.name !== payload.title) {
              bs.updateBoardName(boardId, payload.title)
            }
          }
        } catch (e) { console.error('[onSessionTitleUpdated] Error:', e) }
      },
      onSessionDeleted: (payload) => {
        try {
          if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
          const ss = sessionStoreRef.current
          if (!ss) return
          if (ss.activeSessionId === payload.task_id) {
            ss.setActiveSession(null)
          }
          ss.deleteSession(payload.task_id)
          // Remove kanban card entirely — delete means delete
          const bs = boardStoreRef.current
          if (bs) {
            bs.deleteTask(payload.task_id)
          }
          // Clean up chat messages + channel for this session
          const cs = chatStoreRef.current
          if (cs) {
            cs.removeSessionData(payload.task_id)
          }
        } catch (e) { console.error('[onSessionDeleted] Error:', e) }
      },
      onProjectSwitched: (payload) => {
        if (!currentProject.acceptsSwitchReply(payload as unknown as Record<string, unknown>)) return
        const projectId = typeof payload.project_id === 'string' ? payload.project_id.trim() : ''
        if (!projectId) return
        // The switch this console began already emptied the project's state (lib/currentProject beginSwitch); one the
        // server began on its own -- moving this connection -- is begun here, so the same one reset runs. A spec save
        // in flight answers for the previous project (busy flag, cleared where it always was).
        if (currentProject.pendingProjectId !== projectId) currentProject.beginSwitch(projectId)
        setSpecSaving(false)
        setStatusDetail(`Switching to ${projectId}...`)
        // project_index_push seeds the project view; collab_sync hydrates full runtime state.
      },
      onProjectDeleted: (payload) => {
        const ps = projectStoreRef.current
        if (ps) {
          ps.removeProject(payload.project_id)
        }
        // The server falls back to the default project, but the sync it pushes for it is scoped to "default" while
        // this connection is still registered on the project that just went: the push is filtered out, and the
        // console kept sending every later request in the name of a project that no longer exists (the selector
        // said "default" all the while). Follow the server with a real switch, which re-registers the connection.
        // ... unless a switch to another project is already under way: that switch re-registers the connection by
        // itself, and overriding it sent an owner who had picked "beta" to "default".
        const deletedId = normalizeProjectId(String(payload.project_id ?? ''))
        const pending = currentProject.pendingProjectId
        if (deletedId === getActiveProjectId() && (!pending || pending === deletedId)) {
          const switchSeq = beginProjectSwitch('default')
          clientRef.current?.switchProject('default', switchSeq)
        }
      },
      onOrgInfo: (payload) => {
        const normalized = normalizeOrgInfoPayload(payload)
        setOrgInfoError(null)
        setOrgInfoData(normalized)
        setCapabilityLoading(false)
        // Capture versionAtLoad: the first org_version we see after a
        // saved-org Load/SaveAs becomes the dirty-detection baseline.
        setSavedOrgVersionAtLoad(prev =>
          prev === null ? (normalized?.org_version ?? 0) : prev,
        )
        if (payload?.project_run?.execution_model === 'multi_team_org' || (Array.isArray(payload?.work_items) && payload.work_items.length > 0)) {
          pushedCollabSync.request()
        }
      },
      onCommsState: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        setCommsState(payload)
      },
      onCommsMessage: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, true)) return
        setCommsMessage(payload)
      },
      onReorgList: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        setReorgProposals(payload.proposals ?? [])
      },
      onOrgConfigExport: (payload) => {
        setConfigExportYaml(payload.yaml ?? '')
      },
      onOrgConfigImport: (payload) => {
        // Fires only on manual YAML import via ConfigImportExportPanel.
        // Saved-org Load uses the dedicated onOrgSavedLoad handler, which
        // calls _apply_org_config directly on the backend — no spurious
        // "Dry run OK" banner side-effect.
        if (payload.ok) {
          setConfigImportPreview(payload.preview ?? null)
          setConfigImportError(null)
          if (!payload.dry_run) {
            setActiveSavedOrg(null)
            setSavedOrgVersionAtLoad(null)
          }
        } else {
          setConfigImportError(payload.error ?? t('app.importFailed'))
          setConfigImportPreview(null)
        }
      },
      onOrgSavedList: (payload) => {
        setSavedOrgsList(payload.orgs ?? [])
        if ('active_name' in payload) {
          setActiveSavedOrg(payload.active_name ?? null)
        }
      },
      onOrgSavedSaveAs: (payload) => {
        if (payload.ok) {
          clientRef.current?.orgSavedList()
          setActiveSavedOrg(payload.name)
          // Overwriting (or freshly naming) the active org resets the
          // dirty baseline — the next onOrgInfo captures versionAtLoad.
          setSavedOrgVersionAtLoad(null)
          setOrgToast({ kind: 'ok', text: `Saved "${payload.name}"` })
        } else {
          setOrgToast({ kind: 'error', text: `Save failed: ${payload.error ?? 'unknown'}` })
        }
      },
      onOrgSavedCreate: (payload) => {
        setOrgCreatePending(false)
        setOrgCreateResult({ ...payload, nonce: Date.now() })
        if (payload.ok) {
          const orgId = payload.organization_id || payload.name
          setActiveSavedOrg(orgId)
          setGlobalExecMode('org')
          setGlobalCompanyProfile('custom')
          clientRef.current?.orgSavedList()
          clientRef.current?.orgInfo()
          setSavedOrgVersionAtLoad(null)
          setOrgToast({ kind: 'ok', text: `Created "${payload.organization_name || orgId}"` })
        } else {
          setOrgToast({ kind: 'error', text: `Create failed: ${payload.error ?? 'unknown'}` })
        }
      },
      onOrgSavedLoad: (payload) => {
        if (payload.ok) {
          setActiveSavedOrg(payload.name)
          setGlobalExecMode('org')
          setGlobalCompanyProfile('custom')
          clientRef.current?.orgSavedList()
          // versionAtLoad is captured by the next onOrgInfo — see that handler.
          setSavedOrgVersionAtLoad(null)
          setOrgToast({ kind: 'ok', text: `Loaded "${payload.name}"` })
        } else {
          setOrgToast({ kind: 'error', text: `Load failed: ${payload.error ?? 'unknown'}` })
        }
      },
      onOrgSavedDelete: (payload) => {
        if (payload.ok) {
          clientRef.current?.orgSavedList()
          // Clear the active indicator when the deleted name matches.
          // (Closure over state is stale, so use functional update.)
          setActiveSavedOrg(prev => {
            if (prev === payload.name) {
              setSavedOrgVersionAtLoad(null)
              return null
            }
            return prev
          })
          setOrgToast({ kind: 'ok', text: `Deleted "${payload.name}"` })
        } else {
          setOrgToast({ kind: 'error', text: `Delete failed: ${payload.error ?? 'unknown'}` })
        }
      },
      onMarketBrowse: (payload) => {
        setMarketPresets((payload as any).presets ?? [])
      },
      onMarketPreview: (payload) => {
        setMarketPreviewData(payload as any)
      },
      onChildSessionCreated: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const ss = sessionStoreRef.current
        if (!ss) return
        const workItemIdentity = workItemIdentityPatchFromPayload(payload as Record<string, unknown>)
        const childProjectId = projectIdOf(payload as unknown as Record<string, unknown>)
        if (!childProjectId) return
        // child_session_created adds a child session to the sidebar
        // The backend also broadcasts session_created which handles kanban card creation
        ss.createSession({
          projectId: childProjectId,
          taskId: payload.task_id,
          channelId: (payload as any).channel_id ?? `session:${payload.task_id}`,
          sessionId: payload.session_id,
          parentSessionId: payload.parent_session_id,
          originTaskId: payload.origin_task_id ?? payload.task_id,
          mode: 'child',
          orgId: String((payload as any).org_id ?? (payload as any).organization_id ?? '').trim() || undefined,
          title: payload.title,
          status: 'pending',
          columnId: 'todo',
          assigneeIds: payload.agent_id ? [payload.agent_id] : [],
          priority: null,
          tags: [],
          createdAt: Date.now(),
          updatedAt: Date.now(),
          messageCount: 0,
          progressLog: [],
          ...workItemIdentity,
        })
        // Mark parent session as company runtime (Company mode child sessions)
        if (payload.parent_session_id) {
          const parent = ss.sessions.find(s =>
            s.sessionId === payload.parent_session_id || s.taskId === payload.parent_session_id,
          )
          if (parent && !parent.isCompanyRuntime) {
            ss.setCompanyRuntime(parent.taskId, true)
          }
        }
      },
      onWorkItemProgress: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const ss = sessionStoreRef.current
        if (!ss || !payload.task_id || !payload.entry) return
        const entryProjectionId = typeof payload.entry.work_item_projection_id === 'string'
          ? payload.entry.work_item_projection_id
          : ''
        const projectionId = entryProjectionId && entryProjectionId !== 'company_runtime'
          ? entryProjectionId
          : undefined
        const runtimeTaskId = typeof payload.entry.runtime_task_id === 'string' && payload.entry.runtime_task_id
          ? payload.entry.runtime_task_id
          : typeof payload.runtime_task_id === 'string' && payload.runtime_task_id
            ? payload.runtime_task_id
            : typeof payload.entry.execution_turn_id === 'string' && payload.entry.execution_turn_id
              ? payload.entry.execution_turn_id
              : typeof payload.execution_turn_id === 'string' && payload.execution_turn_id
                ? payload.execution_turn_id
                : undefined
        const executionTurnId = typeof payload.entry.execution_turn_id === 'string' && payload.entry.execution_turn_id
          ? payload.entry.execution_turn_id
          : typeof payload.execution_turn_id === 'string' && payload.execution_turn_id
            ? payload.execution_turn_id
            : runtimeTaskId
        ss.updateSession(payload.task_id, {
          isCompanyRuntime: true,
          ...(projectionId ? { workItemProjectionId: projectionId } : {}),
        })
        const bs = boardStoreRef.current
        if (bs && projectionId) {
          bs.updateTask(payload.task_id, { workItemProjectionId: projectionId })
        }
        ss.appendWorkItemProgress(payload.task_id, {
          timestamp: payload.entry.timestamp * 1000,
          type: payload.entry.type as any,
          workItemProjectionId: projectionId,
          workItemTurnType: typeof payload.entry.work_item_turn_type === 'string' ? payload.entry.work_item_turn_type : undefined,
          workItemProjectionTitle: typeof payload.entry.work_item_projection_title === 'string' ? payload.entry.work_item_projection_title : undefined,
          runtimeTaskId,
          executionTurnId,
          roleName: payload.entry.role_name,
          detail: payload.entry.detail,
        })
      },
      onExternalTeamActivityEvent: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        applyExternalTeamEvent(payload)
      },
      onExternalTeamActivitySnapshot: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const generation = viewGenerationOf(payload as unknown as Record<string, unknown>)
        if (generation !== null && generation !== currentProject.generation) return
        applyExternalTeamSnapshot(payload)
      },
    })
    clientRef.current = client
    client.connect()
    return () => {
      pushedCollabSync.cancel()
      client.disconnect()
      for (const tid of timersRef.current) clearTimeout(tid)
      timersRef.current.clear()
      for (const tid of pendingSessionDetailRefreshRef.current.values()) clearTimeout(tid)
      pendingSessionDetailRefreshRef.current.clear()
      if (deltaFlushTimerRef.current !== null) {
        window.clearTimeout(deltaFlushTimerRef.current)
        deltaFlushTimerRef.current = null
      }
      pendingDeltaFlushRef.current.clear()
    }
  }, [wsUrl])

  useEffect(() => {
    const refreshProjectState = () => {
      if (!isOwnerConsole(authRef.current)) return
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      const now = Date.now()
      if (now - lastProjectIndexRefreshRef.current < 1_500) return
      lastProjectIndexRefreshRef.current = now
      clientRef.current?.collabSync(getActiveProjectId(), undefined, currentProject.generation)
    }
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') refreshProjectState()
    }
    window.addEventListener('focus', refreshProjectState)
    document.addEventListener('visibilitychange', handleVisibilityChange)
    return () => {
      window.removeEventListener('focus', refreshProjectState)
      document.removeEventListener('visibilitychange', handleVisibilityChange)
    }
  }, [getActiveProjectId])

  // Auto-clear the org-toast after 3s.
  useEffect(() => {
    if (!orgToast) return
    const t = setTimeout(() => setOrgToast(null), 3000)
    return () => clearTimeout(t)
  }, [orgToast])

  // Stable client-call refs for OrgTab → TeamView → OrgVersionSwitcher.
  // useCallback so downstream effects don't churn on every App re-render.
  const handleSavedOrgsList = useCallback(() => {
    clientRef.current?.orgSavedList()
  }, [])
  const handleSavedOrgSaveAs = useCallback((name: string, overwrite: boolean) => {
    clientRef.current?.orgSavedSaveAs(name, overwrite)
  }, [])
  const handleSavedOrgCreate = useCallback((organizationName: string, members: OrgCreateMemberInput[]) => {
    setOrgCreatePending(true)
    setOrgCreateResult(null)
    clientRef.current?.orgSavedCreate(organizationName, members)
  }, [])
  const handleSavedOrgLoad = useCallback((name: string) => {
    clientRef.current?.orgSavedLoad(name)
  }, [])
  const handleSavedOrgDelete = useCallback((name: string) => {
    clientRef.current?.orgSavedDelete(name)
  }, [])
  const handleSelectCorporateOrg = useCallback(() => {
    setGlobalExecMode('company')
    setGlobalCompanyProfile('corporate')
    setSavedOrgVersionAtLoad(null)
    clientRef.current?.setExecutionMode('company', 'corporate', globalTaskPreferredAgent)
    clientRef.current?.orgInfo()
  }, [globalTaskPreferredAgent])

  const handleOpenExecutionPanel = useCallback((taskId: string) => {
    if (!taskId) return
    setExecutionPanelRuntimeOnly(false)
    setExecutionPanelTaskId(taskId)
    const projectId = getActiveProjectId()
    const isTeamTarget = sessionStore.sessions.some(session => (
      ((session.taskId === taskId || getExecutionTurnId(session) === taskId) && isJiuwenOpaqueTeam(session))
      || Object.values(session.roleWorkItems ?? {}).some(role => role.workItems.some(item => item.executionTurnId === taskId && isJiuwenOpaqueTeam(item)))
    ))
    if (!isTeamTarget) return
    markExternalTeamLoading(projectId, taskId)
    clientRef.current?.externalTeamActivityGet(projectId, taskId, {
      limit: 100,
      viewGeneration: currentProject.generation,
    })
  }, [getActiveProjectId, markExternalTeamLoading, sessionStore.sessions])

  const handleSelectTeamInvocation = useCallback((taskId: string, invocationId: string) => {
    const projectId = getActiveProjectId()
    markExternalTeamLoading(projectId, taskId, invocationId)
    clientRef.current?.externalTeamActivityGet(projectId, taskId, {
      externalInvocationId: invocationId,
      limit: 100,
      viewGeneration: currentProject.generation,
    })
  }, [getActiveProjectId, markExternalTeamLoading])

  const handleLoadOlderTeamActivity = useCallback((taskId: string, record: ExternalTeamActivityRecord) => {
    const cursor = record.nextCursor
    if (!cursor) return
    clientRef.current?.externalTeamActivityGet(record.projectId, taskId, {
      externalInvocationId: record.externalInvocationId,
      beforeCreatedAt: cursor.beforeCreatedAt,
      beforeEventId: cursor.beforeEventId,
      limit: 100,
      viewGeneration: currentProject.generation,
    })
  }, [])
  const officeMap = useMemo(() => {
    const result: Record<string, string> = {}
    for (const agent of swarmAgents) {
      if (agent.office_id) result[agent.agent_id] = agent.office_id
    }
    return result
  }, [swarmAgents])

  // Board initialization is driven entirely by collab_sync from backend (#7)
  // No local ensureDefaultBoards — avoids column ID mismatch with backend IDs

  const applyWsUrl = () => {
    const next = wsUrlInput.trim()
    if (!next || next === wsUrl) return
    setWsUrl(next)
  }

  const handleSessionModeChange = useCallback((taskId: string, mode: string, _profile?: string, orgId?: string) => {
    const existingSession = sessionStore.sessions.find(session => session.taskId === taskId)
    const normalizedMode = normalizeExecMode(mode)
    const currentProfile = normalizeCompanyProfile(existingSession?.companyProfile ?? globalCompanyProfile)
    const nextProfile = normalizedMode === 'org'
      ? 'custom'
      : normalizedMode === 'company'
        ? 'corporate'
        : 'corporate'
    const currentOrgId = String(existingSession?.orgId ?? activeSavedOrg ?? '').trim()
    const nextOrgId = orgIdForExecMode(normalizedMode, orgId ?? existingSession?.orgId ?? activeSavedOrg)
    const currentSessionMode = normalizeExecMode(existingSession?.execMode)
    const currentGlobalProfile = normalizeCompanyProfile(globalCompanyProfile)

    if (
      currentSessionMode === normalizedMode
      && currentProfile === nextProfile
      && currentOrgId === String(nextOrgId ?? '').trim()
      && globalExecMode === normalizedMode
      && currentGlobalProfile === nextProfile
    ) {
      return
    }

    sessionStore.updateSession(taskId, {
      execMode: normalizedMode,
      companyProfile: nextProfile,
      orgId: nextOrgId,
      preferredAgent: existingSession?.preferredAgent ?? globalTaskPreferredAgent,
    })
    setGlobalExecMode(normalizedMode)
    setGlobalCompanyProfile(nextProfile)
    if (normalizedMode === 'org' && nextOrgId) setActiveSavedOrg(nextOrgId)

    const nextPreferredAgent = existingSession?.preferredAgent ?? globalTaskPreferredAgent
    const runtimeProfile = normalizedMode === 'task' ? undefined : nextProfile
    clientRef.current?.sessionUpdateConfig(getActiveProjectId(), taskId, normalizedMode, runtimeProfile, nextPreferredAgent, nextOrgId)
    clientRef.current?.setExecutionMode(normalizedMode, runtimeProfile, nextPreferredAgent, nextOrgId)
  }, [sessionStore, globalExecMode, globalCompanyProfile, globalTaskPreferredAgent, activeSavedOrg, getActiveProjectId])

  const handleSessionTaskAgentChange = useCallback((taskId: string, preferredAgent: TaskPreferredAgent) => {
    const existingSession = sessionStore.sessions.find(session => session.taskId === taskId)
    const normalizedPreferredAgent = normalizeTaskPreferredAgent(preferredAgent)
    const normalizedMode = normalizeExecMode(existingSession?.execMode)
    const nextProfile = normalizedMode === 'org'
      ? 'custom'
      : normalizedMode === 'company'
        ? 'corporate'
        : 'corporate'

    sessionStore.updateSession(taskId, {
      preferredAgent: normalizedPreferredAgent,
      selectedExecutionAgent: normalizedPreferredAgent,
    })
    setGlobalTaskPreferredAgent(normalizedPreferredAgent)

    const runtimeProfile = normalizedMode === 'task' ? undefined : nextProfile
    const orgId = orgIdForExecMode(normalizedMode, existingSession?.orgId ?? activeSavedOrg)
    clientRef.current?.sessionUpdateConfig(getActiveProjectId(), taskId, normalizedMode, runtimeProfile, normalizedPreferredAgent, orgId)
    clientRef.current?.setExecutionMode(normalizedMode, runtimeProfile, normalizedPreferredAgent, orgId)
  }, [sessionStore, globalCompanyProfile, activeSavedOrg, getActiveProjectId])

  // "Continue in a new chat" (a free session in another mode, from the composer's locked-mode chip) left with the free
  // sessions (policy, R1 option B): a session starts only with an order.

  const handleSessionNativeApprovalLevelChange = useCallback((
    taskId: string,
    level: NativeApprovalLevel,
  ) => {
    // Server confirmation is authoritative.  The session_updated event is
    // the only path that changes visible state, which also makes reconnects
    // and rejected updates deterministic.
    clientRef.current?.sessionUpdateNativePermission(getActiveProjectId(), taskId, level)
  }, [getActiveProjectId])

  const handleSetNativeApprovalDefault = useCallback((level: NativeApprovalLevel) => {
    clientRef.current?.setNativePermissionDefault(level)
  }, [])

  const markRuntimeControlForTask = useCallback((
    taskId: string,
    patch: Partial<import('./types/kanban').Session>,
  ) => {
    const session = sessionStore.sessions.find(s => s.taskId === taskId)
    const parentSessionId = session?.resumeParentSessionId ?? session?.parentSessionId ?? session?.sessionId
    for (const candidate of sessionStore.sessions) {
      if (
        candidate.taskId === taskId
        || (!!parentSessionId && (candidate.parentSessionId === parentSessionId || candidate.sessionId === parentSessionId))
      ) {
        sessionStore.updateSession(candidate.taskId, {
          ...patch,
          resumeParentSessionId: parentSessionId,
        })
      }
    }
  }, [sessionStore])
  // A stop or resume whose receipt never came (the request tracker's timeout, or the connection cut): the marker set
  // at the click is taken back, so the button is not 「停止中」 for ever -- the person looks, then clicks again.
  restoreRuntimeControlRef.current = (taskId, action) => {
    const session = sessionStore.sessions.find(s => s.taskId === taskId)
    const state = session?.runtimeControlState
    if (action === 'session_stop' && state === 'suspending') markRuntimeControlForTask(taskId, { runtimeControlState: 'running', canStop: true, canResume: false })
    if (action === 'session_resume' && state === 'resuming') markRuntimeControlForTask(taskId, { runtimeControlState: 'suspended', canStop: false, canResume: true })
  }

  // Stop and resume go through the request tracker (S2-B): sent, the header says 停止中 until the receipt; not sent
  // (not connected) it is said at once and nothing is marked; no receipt in 15 s gives the button back.
  const handleSessionStop = useCallback((taskId: string) => {
    const session = sessionStore.sessions.find(s => s.taskId === taskId)
    const isCompanyRuntime = session?.execMode === 'company'
      || session?.execMode === 'org'
      || session?.execMode === 'custom'
      || !!session?.isCompanyRuntime
      || !!session?.parentSessionId
      || !!session?.companyProfile
    requestTracker.track(keyOf('session_stop', taskId), () => {
      const disposition = clientRef.current?.sessionStop(getActiveProjectId(), taskId) ?? 'send-failed'
      if (disposition === 'sent' && isCompanyRuntime) {
        markRuntimeControlForTask(taskId, {
          runtimeControlState: 'suspending',
          canStop: false,
          canResume: false,
        })
      }
      return disposition
    }, { kind: 'write' })
  }, [sessionStore.sessions, markRuntimeControlForTask, getActiveProjectId, requestTracker])

  const handleSessionResume = useCallback((taskId: string, runtimeSessionId?: string, checkpointId?: string) => {
    const session = sessionStore.sessions.find(s => s.taskId === taskId)
    const isCompanyRuntime = session?.execMode === 'company'
      || session?.execMode === 'org'
      || session?.execMode === 'custom'
      || !!session?.isCompanyRuntime
      || !!session?.parentSessionId
      || !!session?.companyProfile
    requestTracker.track(keyOf('session_resume', taskId), () => {
      const disposition = clientRef.current?.sessionResume(
        getActiveProjectId(),
        taskId,
        runtimeSessionId ?? session?.resumeParentSessionId ?? session?.parentSessionId ?? session?.sessionId,
        checkpointId ?? session?.pendingRuntimeCheckpointId,
      ) ?? 'send-failed'
      if (disposition === 'sent' && isCompanyRuntime) {
        markRuntimeControlForTask(taskId, {
          runtimeControlState: 'resuming',
          canStop: false,
          canResume: false,
        })
      }
      return disposition
    }, { kind: 'write' })
  }, [sessionStore.sessions, markRuntimeControlForTask, getActiveProjectId, requestTracker])

  // Whether the current mode allows agent creation/editing
  const isOrgMode = globalExecMode === 'org'

  // The page's name, as the rail shows it (lib/shellNav): 安安's side panel says where the person is.
  const activePageLabel = t(PAGE_LABEL_KEYS[activePage])


  if (auth?.required && !auth.account) {
    return (
      <LoginScreen
        auth={auth}
        connected={status === 'connected'}
        error={authError}
        busy={authBusy}
        onLogin={(login, password) => { setAuthBusy(true); setAuthError(''); clientRef.current?.authLogin(getActiveProjectId(), login, password) }}
        onBootstrapOwner={(login, password, displayName) => { setAuthBusy(true); setAuthError(''); clientRef.current?.accountBootstrapOwner(getActiveProjectId(), login, password, displayName) }}
      />
    )
  }
  // The employee's and the customer's pages return below decideGate (their decisions take that road); no hook sits
  // between here and there (lib/appHookOrder.test.ts).

  // Both the dashboard and the approval center use the same artifact drawer.
  // Keeping one instance prevents the “review here, preview somewhere else”
  // split that makes an approval impossible to complete in place.
  const filesPanel = (
    <DataBrowser
      // One panel per project: switching throws away where the reader was, so project A's order id can never
      // appear as a breadcrumb inside project B.
      key={projectStore.activeProjectId}
      projectId={projectStore.activeProjectId}
      data={dataState}
      usage={usageByProject[projectStore.activeProjectId] ?? null}
      connected={status === 'connected'}
      loading={dataState.index === null}
      onRefreshIndex={askDataIndex}
      onOpenOrder={(orderId) => clientRef.current?.dataOrderView(getActiveProjectId(), orderId)}
      onOpenFolder={(folder, offset) => clientRef.current?.dataFolderList(getActiveProjectId(), folder, offset ?? 0)}
      onPreview={(path) => clientRef.current?.dataPreview(getActiveProjectId(), path)}
      onClosePreview={() => dispatchData({ kind: 'clearPreview' })}
      onOpenAttachments={(offset) => clientRef.current?.dataAttachments(getActiveProjectId(), offset)}
      onRefreshUsage={() => clientRef.current?.dataUsage(getActiveProjectId(), true)}
      onReveal={(folder) => clientRef.current?.dataReveal(getActiveProjectId(), folder)}
    />
  )
  const projectFolderPanel = (
    <ProjectFolderPanel
      key={projectStore.activeProjectId}
      locations={dataLocations}
      folder={folderState}
      connected={status === 'connected'}
      onPick={(scope) => {
        // Windows' own folder dialog opens on the owner's screen: they browse and create there, and only the
        // path they picked comes back. Then the same checks run as for any other path.
        dispatchFolder({ kind: 'picking', scope })
        clientRef.current?.dataDirPick(getActiveProjectId(), '')
      }}
      onOpenChooser={(scope) => {
        dispatchFolder({ kind: 'open', scope })
        clientRef.current?.dataDirBrowse(getActiveProjectId(), '')
      }}
      onCloseChooser={() => dispatchFolder({ kind: 'close' })}
      onBrowse={(path) => {
        dispatchFolder({ kind: 'looking' })
        clientRef.current?.dataDirBrowse(getActiveProjectId(), path)
      }}
      onCreate={(parent, name) => clientRef.current?.dataDirCreate(getActiveProjectId(), parent, name)}
      onSelect={(path) => dispatchFolder({ kind: 'select', path })}
      onPlan={(scope, target) => clientRef.current?.dataFolderPlan(getActiveProjectId(), scope, target)}
      onSet={(scope, target, token) => clientRef.current?.dataFolderSet(getActiveProjectId(), scope, target, token)}
      onImport={(source, token) => clientRef.current?.dataFolderImport(getActiveProjectId(), source, token)}
      onReveal={(folder, legacy) => clientRef.current?.dataReveal(getActiveProjectId(), folder, legacy)}
      onLegacy={() => clientRef.current?.dataFolderLegacy(getActiveProjectId())}
    />
  )
  const recyclePanel = (
    <RecyclePanel
      key={`recycle-${projectStore.activeProjectId}`}
      recycle={recycleState}
      connected={status === 'connected'}
      onRefresh={() => {
        dispatchRecycle({ kind: 'load' })
        clientRef.current?.dataRecycleList(getActiveProjectId())
      }}
      onRestore={(id) => {
        dispatchRecycle({ kind: 'restoring', id })
        clientRef.current?.dataRecycleRestore(getActiveProjectId(), id)
      }}
      onAskClear={() => dispatchRecycle({ kind: 'askClear' })}
      onCancelClear={() => dispatchRecycle({ kind: 'cancelClear' })}
      onClear={(token) => {
        // The owner has just answered the second question in the card; only now does anything get deleted.
        dispatchRecycle({ kind: 'clearing' })
        clientRef.current?.dataRecycleClear(getActiveProjectId(), token)
      }}
    />
  )
  const customerMemoryPanel = (
    <CustomerMemoryPanel
      key={`memory-${projectStore.activeProjectId}`}
      memory={memoryState}
      connected={status === 'connected'}
      onRefresh={askCustomerMemory}
      onAskDelete={(customer, id) => dispatchMemory({ kind: 'askDelete', customer, id })}
      onCancelDelete={() => dispatchMemory({ kind: 'cancelDelete' })}
      onDelete={(customer, id) => {
        // The owner has just answered the second question on the card; only now is anything deleted.
        dispatchMemory({ kind: 'deleting', id })
        clientRef.current?.memoryDelete(getActiveProjectId(), customer, id)
      }}
    />
  )
  const artifactsPanel = (
    <ArtifactsPanel
      index={artifactIndex}
      preview={artifactPreview}
      connected={status === 'connected'}
      loading={artifactLoading}
      error={artifactError}
      previewLoading={artifactPreviewLoading}
      previewError={artifactPreviewError}
      onRefresh={() => {
        setArtifactLoading(true)
        setArtifactError(null)
        clientRef.current?.artifactsList(getActiveProjectId())
      }}
      onOpen={(path) => {
        setArtifactPreviewLoading(true)
        setArtifactPreviewError(null)
        setArtifactPreview(null)
        clientRef.current?.artifactRead(getActiveProjectId(), path)
      }}
      onReveal={(path) => clientRef.current?.artifactReveal(getActiveProjectId(), path)}
      onQuote={isOwnerConsole(auth) ? selection => {
        const projectId = getActiveProjectId()
        if (currentProject.pendingProjectId || selection.reference.project_id !== projectId || status !== 'connected') return
        const principal = authRef.current?.account?.login ?? (authRef.current?.required ? '' : 'local-owner')
        if (!principal || !isOwnerConsole(authRef.current)) return
        setArtifactQuoteDraft({ ...selection, key: crypto.randomUUID(), principal, threadId: masterView.threadId })
        setArtifactPreview(null)
        setExecutionPanelOpen(false)
        setAssistantOpen(true)
      } : undefined}
      onClosePreview={() => {
        setArtifactPreview(null)
        setArtifactPreviewError(null)
        setArtifactPreviewLoading(false)
      }}
    />
  )

  const openSessionFromApproval = (taskId: string, revealPendingDecision = false) => {
    openExecutionRef.current(taskId)
    if (!revealPendingDecision) return
    let attempts = 0
    const reveal = () => {
      const target = document.querySelector('[data-pending-checkpoint="true"]')
      if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'center' })
        const row = target.querySelector('.msg-row-pending-decision')
        row?.classList.add('msg-row-decision-flash')
        window.setTimeout(() => row?.classList.remove('msg-row-decision-flash'), 2400)
        return
      }
      if (++attempts < 20) window.setTimeout(reveal, 150)
    }
    window.setTimeout(reveal, 150)
  }

  // X05: one road for answering a pause, wherever it is shown (approval centre, master page, project centre, browser
  // pane) -- the one answer path (lib/answerPath, policy): one answer out per card (a second click sends
  // nothing), a stable request id per (card, answer), no re-send on its own after a timeout or a dropped connection
  // (the next click looks at the list first), receipts of another project dropped.
  const decideBlocker = (blocker: DashboardBlocker, optionId: string, origin = 'blocker') => {
    const label = blocker.options.find(option => option.id === optionId)?.label || optionId
    // A refusal stays on the card, in words, the way the card in the conversation keeps its own (MessageList's
    // submitInteraction): the toast alone was gone in three seconds and the click looked like nothing happened
    // (B5-0003,, 「点了没反应」). The server names the refusal by a code; refusalInWords says it. The
    // reason beside the code is either a code of its own (missing:checkpoint_id -- left out of the sentence) or, on
    // a budget card, the refusal already in the owner's words, which refusalInWords says as it is.
    setBlockerDecisionErrors(prev => {
      if (!(blocker.checkpoint_id in prev)) return prev
      const next = { ...prev }
      delete next[blocker.checkpoint_id]
      return next
    })
    const refused = (code: unknown, reason?: unknown) => {
      const words = refusalInWords(localeRef.current, 'approvals.refused', code, reason)
      setBlockerDecisionErrors(prev => ({ ...prev, [blocker.checkpoint_id]: words }))
      showToast(words, 'error')
    }
    void answerPath.answer({
      checkpointId: blocker.checkpoint_id,
      checkpointType: blocker.checkpoint_type,
      optionId,
      origin,
      projectId: blocker.project_id,
      requesterTaskId: blocker.task_id || undefined,
      requesterSessionId: blocker.session_id || undefined,
      decision: { text: label, option_id: optionId },
    }).then((outcome) => {
      if (outcome.kind === 'accepted') setBlockers(prev => prev.filter(item => item.checkpoint_id !== blocker.checkpoint_id))
      else if (outcome.kind === 'refused' || outcome.kind === 'not_sent') refused(outcome.code, outcome.kind === 'refused' ? outcome.reason : undefined)
      // A second click while the first is out changes nothing and asks for nothing; every other ending re-reads the list.
      if (outcome.kind === 'accepted' || outcome.code !== 'duplicate_request_in_flight') { clientRef.current?.blockersList(getActiveProjectId()); askOwnerToday() }
    }).catch((error) => {
      refused(error instanceof Error ? error.message : error)
      clientRef.current?.blockersList(getActiveProjectId())
      askOwnerToday()
    })
  }

  // A gate's 放行 / 打回修改 (GatePanel, the materials view) goes the same way: one decision out per gate, the next click
  // after a lost receipt looks at the list first, a receipt of another project is dropped (the gate_decide ack handler).
  const decideGate = (decision: GateDecision) => {
    const checkpointId = decision.checkpoint_id
    const started = answerPath.begin(checkpointId, `gate-${decision.decision}`, 'gate')
    if (!started.fresh) return
    // The send sits on the request tracker too (回答卡 30 s): a cut connection or a send that never left is said by it
    // and gives the panel's buttons back (gateWaiting); the answer path keeps the card-level rules above. A send
    // refused here is worded here (socket_unavailable), so the tracker is told `false` and says nothing of its own.
    const send = (): SendDisposition | false => {
      setGateBusy(true)
      setGateError(null)
      const client = clientRef.current
      const disposition = client ? client.gateDecide(getActiveProjectId(), decision as unknown as Record<string, unknown>) : 'send-failed'
      if (disposition !== 'sent') {
        answerPath.settle(checkpointId)
        setGateBusy(false)
        setGateError(refusalInWords(localeRef.current, 'gate.refused', 'socket_unavailable'))
        return false
      }
      return disposition
    }
    requestTracker.track(keyOf('gate_decide', checkpointId), () => {
      if (!started.verify) return send()
      setGateBusy(true)
      return answerPath.lookAtList(checkpointId, getActiveProjectId()).then((state) => {
        if (state === 'open') return send()
        answerPath.settle(checkpointId, state === 'closed' ? undefined : 'request_timeout')
        setGateBusy(false)
        setGateError(refusalInWords(localeRef.current, 'gate.refused', state === 'closed' ? 'gate_not_found' : 'request_timeout'))
        return false
      })
    }, { kind: 'answer' })
  }

  // 客户页「我的单」 (S3-6B, policy): a customer account sees this page and nothing of the console. Its one
  // write, 确认收货, goes out through the request tracker (kind write: busy / no receipt / not sent are its words);
  // the server records the acceptance and the owner closes the order. No link, no mail, no account is made here.
  const customerAccount = auth?.account && isCustomerAccount(auth.account) ? auth.account : null
  if (customerAccount) {
    return (
      <CustomerPage
        account={customerAccount}
        orders={customerOrders}
        error={customerOrdersError}
        connected={status === 'connected'}
        tracked={tracked}
        acceptErrors={customerAcceptErrors}
        onRetry={askCustomerOrders}
        onAccept={(orderId, note) => {
          setCustomerAcceptErrors(prev => {
            if (!(orderId in prev)) return prev
            const next = { ...prev }
            delete next[orderId]
            return next
          })
          // 确认收货 is the one decision the page offers (the store's word: accepted; 打回 and 带例外通过 are not offered
          // here). The order's project is the order's own, so none is named.
          requestTracker.track(keyOf('customer_accept', orderId), () => clientRef.current?.send({ type: 'customer_accept', order_id: orderId, decision: 'accepted', note }) ?? 'send-failed', { kind: 'write' })
        }}
        onLogout={() => { clientRef.current?.authLogout(getActiveProjectId(), readAuthToken()); writeAuthToken('') }}
      />
    )
  }
  // 员工页「我的活」 (S3-6B): the list (my_tasks_list), the open task's materials (gate_detail, the same state as the
  // owner's materials view) with 放行 / 打回修改 through decideGate, and the plain reply through the answer path.
  if (auth?.account?.role === 'employee') {
    return (
      <EmployeeWorkbench
        account={auth.account}
        tasks={myTasks}
        tasksError={myTasksError}
        connected={status === 'connected'}
        open={myOpen}
        materials={gateDetail as Materials | null}
        materialsError={gateError}
        gateBusy={gateWaiting}
        sending={answering}
        replyError={myReplyError}
        returned={myReturned}
        onOpen={(task) => {
          setMyOpen(task)
          setMyReplyError('')
          setGateDetail(null)
          setGateError(null)
          if (task.checkpoint_id) clientRef.current?.gateDetail(getActiveProjectId(), task.checkpoint_id)
        }}
        onClose={() => { setMyOpen(null); setGateDetail(null); setGateError(null) }}
        onDecideGate={decideGate}
        onReply={(item, text) => {
          // The same answer path as the owner's cards (lib/answerPath): one reply out per card, a stable request id,
          // the refusal in words; the list of work is asked for again whichever way it ended.
          // The card's type is required by the server; the page keeps the button off while the list does not carry it.
          const checkpointId = item.checkpoint_id ?? ''
          const checkpointType = item.checkpoint_type ?? ''
          if (!checkpointId || !checkpointType) return
          setMyReplyError('')
          void answerPath.answer({
            checkpointId,
            checkpointType,
            optionId: 'reply',
            origin: 'employee',
            requesterTaskId: item.task_id || undefined,
            decision: { text },
          }).then((outcome) => {
            if (outcome.kind === 'accepted') employeeDecidedRef.current(checkpointId, 'reply')
            else setMyReplyError(outcome.words)
            if (outcome.kind !== 'accepted' && outcome.kind !== 'not_sent') clientRef.current?.myTasksList(getActiveProjectId())
          }).catch((error) => {
            setMyReplyError(refusalInWords(localeRef.current, 'approvals.refused', error instanceof Error ? error.message : error))
            clientRef.current?.myTasksList(getActiveProjectId())
          })
        }}
        onLogout={() => { clientRef.current?.authLogout(getActiveProjectId(), readAuthToken()); writeAuthToken('') }}
      />
    )
  }

  // One road to a page, for the rail, the top bar and the earlier navigation alike: opening a page also asks the
  // server for the lists that page shows. An entry that only switched the page would open it empty.
  const openModelsDialog = (providerId = '') => {
    setModelsFocus(providerId)
    setShowSettings(true)
    setSettingsData(null)
    setSettingsError(null)
    setSettingsTestResult(null)
    clientRef.current?.settingsGet(getActiveProjectId())
    clientRef.current?.accountList(getActiveProjectId())
  }
  const showPage = (page: AppPage) => {
    setActivePage(page)
    // The click asks; the quiet re-ask that follows the next sync has nothing left to do for this project and page.
    restoredPageKeyRef.current = askedKey(getActiveProjectId(), page)
    requestPageLists(page, getActiveProjectId())
  }
  // 从图标栏进总控 (policy): 总控 is entered on a new conversation of the project on screen -- the
  // landing, no conversation made until its first send; the earlier ones are a click away in the column.
  const beginMasterConversation = (projectId: string) => {
    setMasterOpen(current => newConversation(projectId, current))
    setMasterThreadNote(null)
    setThreadsDrawer(false)
  }
  const openPage = (page: AppPage) => {
    if (page === 'master') beginMasterConversation(getActiveProjectId())
    showPage(page)
    // Explicit navigation refreshes the same server facts; no background poll.
    if (page === 'master' && isOwnerConsole(authRef.current)
      && isV2SurfaceEnabled('f_frontend_pending_approval_badge')) {
      const sent = clientRef.current?.pendingApprovalBadgeRead(getActiveProjectId())
      if (sent !== 'sent') setPendingApprovalBadgeState(previous => stalePendingBadge(previous))
    }
  }
  openPageRef.current = openPage
  // Default to the direct browser surface. Inspection requests are separate dialogs; no second application shell.
  const openExecution = (taskId?: string, tab: ExecutionTab = 'browser') => {
    if (taskId) sessionStoreRef.current?.setActiveSession(taskId)
    const nextTab = (tab === 'observatory' && !isOwnerConsole(auth)) ? 'record' : tab
    if (nextTab === 'browser' || nextTab === 'desktop') {
      setExecutionTab(nextTab)
      setExecutionDetailTab(null)
    } else setExecutionDetailTab(nextTab)
    setAssistantOpen(false)
    setExecutionPanelOpen(true)
    if (tab === 'files' || tab === 'subsessions') { setArtifactLoading(true); setArtifactError(null); clientRef.current?.artifactsList(getActiveProjectId()) }
    if (tab === 'browser') {
      clientRef.current?.browserSessionList(getActiveProjectId())
      const row = browserForOrder(browserSessionsRef.current, taskId || sessionStoreRef.current?.activeSessionId)
      if (row && !browserFrameMatchesSession(browserViewRequestRef.current, row)) {
        const pane = { binding_key: row.binding_key, task_id: row.task_id, note: row.note, anchor_session_id: row.anchor_session_id, order_id: row.order_id }
        browserPaneRef.current = pane
        setBrowserPane(pane)
        dismissedPanesRef.current.delete(row.binding_key)
        requestBrowserView(row.binding_key, browserControlContext(row) ?? undefined)
      }
    }
    if (tab === 'desktop') {
      setDesktopSessionStatus(null)
      const desktopTaskId = String(taskId || sessionStoreRef.current?.activeSessionId || '').trim()
      setDesktopSessionError(null)
      if (desktopTaskId && isOwnerConsole(auth)) {
        setDesktopSessionBusy(true)
        const disposition = clientRef.current?.desktopSessionControl(getActiveProjectId(), desktopTaskId, 'status')
        if (disposition !== 'sent') {
          setDesktopSessionBusy(false)
          setDesktopSessionError('未连接，桌面状态没有发出；不会自动重试。')
        }
      } else {
        setDesktopSessionStatus(null)
      }
    }
  }
  openExecutionRef.current = openExecution
  // 配置 on a named section (S3-7): 服务与连接's sections, where 控制面 lives now, and 组织与角色, where 组织 does.
  // The page asks for a capability section's lists itself when the section opens (ConfigPage's section effect).
  const openConfigSection = (section: ConfigSection) => {
    setConfigEntry(section)
    openPage('config')
  }
  // 4.7-A11 提一个改进: start from a detail, then open the candidate where it is edited -- a draft flow on the builder
  // canvas, a skill candidate on the component workbench's 测试 page, a connector candidate in 能力中心.
  const startImprovement = (key: string, target: ImprovementTarget, request: string) => {
    setImprovementStart({ key, busy: true, result: null, refusal: null })
    if (clientRef.current?.improvementProposalStart(getActiveProjectId(), target.kind, target.id, request) !== 'sent') {
      setImprovementStart({ key, busy: false, result: null, refusal: t('app.notSent') })
    }
  }
  const openImprovementEditor = (editor: ImprovementEditor) => {
    if (editor.kind === 'process' && editor.workflow_id) {
      const workflowId = editor.workflow_id
      setOpenFlowRequest(current => ({ workflowId, seq: (current?.seq ?? 0) + 1 }))
      openPage('workflows')
      return
    }
    if (editor.kind === 'skill' && editor.skill_id) {
      const skillId = editor.skill_id
      setComponentWork({ id: skillId, tab: 'test' })
      setComponentDetail({ id: skillId, detail: null, loading: true, error: null })
      setComponentVersionsState({ id: skillId, versions: null, loading: true, error: null, busy: '' })
      clientRef.current?.componentDetail(getActiveProjectId(), skillId)
      clientRef.current?.componentVersions(getActiveProjectId(), skillId)
      openPage('components')
      return
    }
    openConfigSection('connect')
  }
  const improvementStarter = (key: string, targets: ImprovementTarget[]) => (
    <ImprovementStarter
      zh={locale.startsWith('zh')}
      targets={targets}
      connected={status === 'connected'}
      busy={improvementStart.key === key && improvementStart.busy}
      result={improvementStart.key === key ? improvementStart.result : null}
      refusal={improvementStart.key === key ? improvementStart.refusal : null}
      onStart={(target, request) => startImprovement(key, target, request)}
      onOpenEditor={openImprovementEditor}
      onOpen={() => {
        // What can be picked: the project's registered connectors, and the library's skill names for the labels.
        clientRef.current?.connectorCandidateList(getActiveProjectId())
        if (!componentCatalog) clientRef.current?.componentCatalogList(getActiveProjectId())
      }}
    />
  )
  const improvementActions: ImprovementProposalActionHandlers = {
    connected: status === 'connected',
    busy: improvementBusy,
    refusal: improvementRefusal,
    onEvaluate: (row, sampleSize) => {
      setImprovementBusy(row.proposal_id)
      if (clientRef.current?.improvementProposalRecordEvaluation(getActiveProjectId(), row.proposal_id, row.proposal_fingerprint, sampleSize) !== 'sent') {
        setImprovementBusy(''); showToast(t('app.notSent'), 'error')
      }
    },
    onApprove: (row, sampleSize) => {
      setImprovementBusy(row.proposal_id)
      if (clientRef.current?.improvementProposalApprove(getActiveProjectId(), row.proposal_id, row.proposal_fingerprint, sampleSize) !== 'sent') {
        setImprovementBusy(''); showToast(t('app.notSent'), 'error')
      }
    },
    onOpenCandidate: (row) => {
      const editor = editorOfProposal(row)
      if (editor) openImprovementEditor(editor)
    },
    onBindStandard: (row, standard) => {
      setImprovementBusy(row.proposal_id)
      if (clientRef.current?.improvementProposalBindEvaluation(getActiveProjectId(), row.proposal_id, row.proposal_fingerprint, standard) !== 'sent') {
        setImprovementBusy(''); showToast(t('app.notSent'), 'error')
      }
    },
    onBindSessions: (row, variant, sessionIds) => {
      setImprovementBusy(row.proposal_id)
      if (clientRef.current?.improvementProposalBindHarnessSessions(getActiveProjectId(), row.proposal_id, row.proposal_fingerprint, variant, sessionIds) !== 'sent') {
        setImprovementBusy(''); showToast(t('app.notSent'), 'error')
      }
    },
    onRecordSessions: (row, baselineSessionIds, candidateSessionIds, note) => {
      setImprovementBusy(row.proposal_id)
      if (clientRef.current?.improvementProposalRecordHarnessEvaluation(getActiveProjectId(), row.proposal_id, row.proposal_fingerprint, baselineSessionIds, candidateSessionIds, note) !== 'sent') {
        setImprovementBusy(''); showToast(t('app.notSent'), 'error')
      }
    },
  }
  openDesktopChannelRef.current = () => {
    const owner = isOwnerConsole(auth)
    setConfigEntry('channels')
    setDesktopChannelFocus(owner)
    openPage('config')
    // The switch is shown to the owner only; its row must say what the server holds, not the closed default.
    if (owner) requestCapabilityLists(getActiveProjectId())
  }
  // 控制面's lists (S3-7: its panels live under 配置 · 服务与连接): asked for when such a section opens.
  function requestCapabilityLists(projectId: string) {
    const client = clientRef.current
    if (!client) return
    client.credentialReferenceList(projectId); client.deploymentList(projectId); client.connectorSwitchList(projectId); client.connectorCandidateList(projectId); client.browserSessionList(projectId); client.harnessPendingList(projectId)
    if (isOwnerConsole(auth)) { client.governancePolicyList(projectId); client.browserCandidateControl(projectId, 'list') }
    client.processSpecList(projectId); setCapabilityLoading(true); setOrgInfoError(null); client.orgInfo()
  }
  function requestPageLists(page: AppPage, projectId: string) {
    const client = clientRef.current
    if (!client) return
    // 总控 and 搭建台 both say which model answers or runs a step; ask the settings once if the console has none yet.
    if ((page === 'workflows' || page === 'master') && !settingsData && isOwnerConsole(auth)) client?.settingsGet(projectId)
    if (page === 'workflows' || page === 'orders') {
      client?.workflowList(projectId); client?.solutionBlueprintList(projectId); client?.runBudgetList(projectId); client?.orderMetrics(projectId); refreshOrderList()
    } else if (page === 'dashboard' || page === 'master') {
      client?.workflowList(projectId); refreshOrderList()
      // 总控's title-line counts (S3-3B): what waits on the owner, asked again on every entry (the poll covers the rest).
      if (page === 'master') askOwnerToday()
      // ...and its conversation column (总控对话栏): the project on screen's conversations, read again on every entry.
      if (page === 'master' && isOwnerConsole(auth)) client?.masterThreadList(projectId)
    } else if (page === 'components') {
      setComponentCatalogLoading(true); setComponentCatalogError(null)
      client?.componentCatalogList(projectId); client?.workflowList(projectId)
      if (isOwnerConsole(auth)) {
        setSkillImprovements(null); setImprovementProposals(null); setSkillImprovementError(null)
        client?.skillImprovementList(projectId)
        client?.improvementProposalList(projectId)
        setDistributionInbox(null); client?.workflowDistributionInbox(projectId)
        // The market and trial area are the owner's: the ranked top list, categories and the trials on this machine.
        client?.skillMarketCategories(projectId)
        client?.skillTrialList(projectId)
        trialsSeenRef.current = true
        if (marketBrowse.skills.length === 0) {
          setMarketBrowse(prev => ({ ...prev, loading: true, error: null }))
          client?.skillMarketBrowse(projectId, { keyword: '', category: '', sort: 'stars', limit: MARKET_TOP_LIMIT })
        }
      }
    } else if (page === 'config') {
      // The page shows what the server reports about models, generation tools and channels: a read, nothing more.
      // A section made of 控制面's panels asks for their lists when it opens (requestCapabilityLists).
      setSettingsError(null); client?.settingsGet(projectId)
    } else if (page === 'settings') {
      // 账号 and 模型与密钥 are on this page for the owner console, including the first owner (bootstrap_needed).
      if (isOwnerConsole(auth)) {
        setSettingsError(null)
        client?.settingsGet(projectId)
        client?.accountList(projectId)
      }
    }
  }

  // 总控 as a page and 安安 as a side panel are one conversation: the same element, the same channel, the same
  // handlers. Only one of the two is ever on screen.
  // 继续上次的工作: from what the console already holds -- nothing is asked for and nothing is remembered for it.
  // One line of links on 总控 (S3-3B): three at most, so the line stays a line.
  const recentItems = recentWork(workflows, orders, {
    draftFlowIds: nodeDraftState.editingWorkflowId ? [nodeDraftState.editingWorkflowId] : [],
    lastBuilderFlowId: nodeDraftState.builderWorkflowId,
  }, 3).map(item => {
    const zh = locale.startsWith('zh')
    if (item.kind === 'order') {
      const statusKey = `orders.status.${item.status}` as Parameters<typeof t>[0]
      return { key: `order:${item.orderId}`, title: item.title, sub: `${zh ? '工单' : 'Order'} ${item.orderId} · ${t(statusKey) === statusKey ? item.status : t(statusKey)}`, onOpen: () => { setOrderQueueRequest(current => ({ queue: 'all', query: '', open: item.orderId, flow: '', seq: (current?.seq ?? 0) + 1 })); openPage('orders') } }
    }
    const sub = item.kind === 'draft'
      ? (zh ? `搭建台 · v${item.version} · 有未保存的改动` : `Builder · v${item.version} · unsaved changes`)
      : (zh ? `搭建台 · v${item.version} · 继续搭建` : `Builder · v${item.version} · keep building`)
    return { key: `${item.kind}:${item.flowId}`, title: item.title, sub, onOpen: () => { nodeDraftState.setBuilderWorkflowId(item.flowId); openPage('workflows') } }
  })
  // The rail's dot only (S3-3B retired this client rule for 总控 itself): the orders waiting on a person, from the
  // orders, pauses and allowances the console already holds. The rail is step 1's, in the design's order (S3-7).
  const attentionFound = attentionItems(orders, blockers, runBudgets, id => (orgInfoData?.employees ?? []).find(person => person.employee_id === id)?.name || id, locale.startsWith('zh'))
  // Each number in 总控's title line opens the inbox on the group with the same filter (lib/ordersInbox INBOX_STATES).
  const openInboxGroup = (state: TodayGroup) => {
    setOrderQueueRequest(current => ({ queue: 'all', state, query: '', open: '', flow: '', seq: (current?.seq ?? 0) + 1 }))
    openPage('orders')
  }
  // 组织 -- roles, roster, structure, runtime teams -- is one element shown in two places: 配置 · 组织与角色 (S3-7,
  // where the 组织 page went) and 项目中心's all-projects level, where the owner asked for it to live. Nothing is copied.
  const orgBoard = (
    <div className="org-page">
      <OrgTab
        data={orgInfoData}
        error={orgInfoError}
        connected={status === 'connected'}
        sessionRecruitmentByRole={sessionRecruitmentByRole}
        reorgProposals={reorgProposals}
        isCustomMode={isOrgMode}
        onRequestData={() => clientRef.current?.orgInfo()}
        onAddEmployee={(payload) => clientRef.current?.employeeAdd(getActiveProjectId(), payload)}
        onImportEmployee={(empId) => clientRef.current?.importEmployeeAsAgent(empId)}
        onRequestReorgList={() => {
          const projectId = getActiveProjectId()
          clientRef.current?.reorgList(projectId)
        }}
        // A reorg proposal's 批准 / 拒绝 is a card answered like every other (lib/answerPath, S3-7): one answer out per
        // card, a request id stable per (card, answer) rather than minted from the clock on every click, a card of
        // another project refused before anything is sent, no re-send on its own; the receipt speaks as before.
        onReorgDecide={(proposal, approved, notes) => {
          const checkpointId = proposal.checkpoint_id
          const checkpointType = proposal.checkpoint_type || 'company_reorg_pending'
          const optionId = approved ? 'approve' : 'deny'
          if (!checkpointId || proposal.checkpoint_status !== 'pending') {
            return Promise.resolve({ ok: false, accepted: false, error: 'reorg_checkpoint_unavailable', checkpoint_id: checkpointId, checkpoint_type: checkpointType, client_request_id: '' })
          }
          return answerPath.answer({
            checkpointId,
            checkpointType,
            optionId,
            origin: 'org-reorg',
            projectId: proposal.project_id || undefined,
            requesterTaskId: proposal.requester_task_id || undefined,
            requesterSessionId: proposal.requester_session_id || undefined,
            decision: { text: notes?.trim() || optionId, option_id: optionId },
          }).then((outcome) => {
            if (outcome.kind === 'accepted') clientRef.current?.reorgList(getActiveProjectId())
            return receiptOf(outcome, { checkpointId, checkpointType, clientRequestId: answerPath.entry(checkpointId)?.clientRequestId ?? '' })
          })
        }}
        answering={answering}
        onMarketExport={(data) => clientRef.current?.marketExport(data)}
        onMarketInstall={(path, strategy) => clientRef.current?.marketInstall(path, strategy)}
        onMarketUninstall={(pkgId) => clientRef.current?.marketUninstall(pkgId)}
        marketPresets={marketPresets}
        marketPreviewData={marketPreviewData}
        onMarketBrowse={() => clientRef.current?.marketBrowse()}
        onMarketPreview={(id) => clientRef.current?.marketPreview(id)}
        onMarketApplyPreset={(id, strategy) => clientRef.current?.marketApplyPreset(id, strategy)}
        onMarketClearPreview={() => setMarketPreviewData(null)}
        onAddRole={(rid, name, resp, rt, icon) => clientRef.current?.addRole(rid, name, resp, rt, icon)}
        onBulkAddRoles={(roles) => clientRef.current?.bulkAddRoles(roles)}
        onUpdateRole={(rid, updates) => clientRef.current?.updateRole(rid, updates)}
        onDeleteRole={(rid) => clientRef.current?.deleteRole(rid)}
        onBindExternalTeam={(data) => clientRef.current?.bindExternalTeam(data)}
        onUnbindExternalTeam={(data) => clientRef.current?.unbindExternalTeam(data)}
        onUpdateOrgStrategy={(data) => clientRef.current?.updateOrgStrategy(data)}
        onUpdateRuntimePolicy={(policy) => clientRef.current?.updateRuntimePolicy(policy)}
        onResetArchitecture={() => clientRef.current?.resetArchitecture()}
        onConfigExport={() => clientRef.current?.orgConfigExport()}
        onConfigImport={(yaml, dryRun) => clientRef.current?.orgConfigImport(yaml, dryRun)}
        configExportYaml={configExportYaml}
        configImportPreview={configImportPreview}
        configImportError={configImportError}
        onSavedOrgsList={handleSavedOrgsList}
        onSavedOrgSaveAs={handleSavedOrgSaveAs}
        onSavedOrgCreate={handleSavedOrgCreate}
        onSavedOrgLoad={handleSavedOrgLoad}
        onSavedOrgDelete={handleSavedOrgDelete}
        savedOrgsList={savedOrgsList}
        activeSavedOrg={activeSavedOrg}
        activeSavedOrgVersionAtLoad={savedOrgVersionAtLoad}
        orgCreatePending={orgCreatePending}
        orgCreateResult={orgCreateResult}
        onSelectCorporate={handleSelectCorporateOrg}
      />
    </div>
  )
  // 总控对话栏 (policy, lib/masterThreads): which conversation the element shows. On 总控 (the
  // owner's) the open one -- a new one has no channel until its first send makes it. 安安's side panel on another page,
  // and a console with no conversations (not the owner's, an older server), show the project's most recently active
  // conversation, else its first one (`master:{pid}`), as P1 has it.
  // (Before the sign-in state is in, 总控 is taken as the owner's: its landing, never an old conversation first.)
  const masterOnPage = activePage === 'master' && !masterThreadsUnsupported && (auth === null || isOwnerConsole(auth))
  const masterOpenProject = masterOpen.projectId || projectStore.activeProjectId
  const masterOpenRow = masterOpen.threadId ? (masterThreads[masterOpenProject] ?? []).find(item => item.threadId === masterOpen.threadId) ?? null : null
  const masterPanelRow = masterThreadsOn ? ((masterThreads[projectStore.activeProjectId] ?? []).find(item => item.threadId === panelThreadId && !item.archived)
    ?? latestThread(masterThreads[projectStore.activeProjectId])) : null
  const masterView = masterOnPage
    ? {
        channelId: masterOpen.threadId ? (masterOpenRow?.channelId ?? masterChannelId(masterOpenProject, masterOpen.threadId)) : '',
        threadId: masterOpen.threadId,
        legacy: !!masterOpenRow?.legacy,
        key: `master:${masterOpenProject}:${masterOpen.key}`,
      }
    : {
        channelId: masterPanelRow?.channelId ?? masterChannelId(projectStore.activeProjectId),
        threadId: masterPanelRow?.threadId ?? '',
        legacy: masterPanelRow ? masterPanelRow.legacy : true,
        key: `master:${projectStore.activeProjectId}:${masterPanelRow?.threadId ?? 'first'}`,
      }
  const masterMessages = masterView.channelId ? chatStore.getChannelMessages(masterView.channelId) : []
  // A first send of a new conversation makes it (master_thread_create) and goes out when it is back; one that gets no
  // receipt is said once and dropped, never re-sent on its own.
  const createMasterThread = (projectId: string, fresh: number, message: { content: string; extras?: MasterSendExtras }): boolean => {
    if (clientRef.current?.masterThreadCreate(projectId) !== 'sent') return false
    const waiting = { projectId, fresh, messages: [message] }
    masterCreateRef.current = waiting
    window.setTimeout(() => {
      if (masterCreateRef.current !== waiting) return
      masterCreateRef.current = null
      showToast(t('master.threads.createFailed', { reason: t('master.threads.noReceipt') }), 'error', 8000)
      giveBackFirstSend(waiting)
    }, 15_000)
    return true
  }
  // What the input sends (lib/masterThreads sendPlan): into the open conversation; a new one's first send makes it; a
  // second before it is back waits for it; nothing while the console is still switching to the conversation's project.
  // Returns false when nothing went out, so the words stay in the box.
  const sendToMaster = (content: string, extras?: MasterSendExtras): boolean => {
    const client = clientRef.current
    if (!client || status !== 'connected') return false
    const projectId = getActiveProjectId()
    const references = extras?.references ?? []
    const attachments = extras?.attachments ?? []
    if (!masterOnPage) {
      const sent = client.masterSend(projectId, content, references, attachments, masterView.threadId)
      return sent === 'sent' || sent === 'queued'
    }
    setMasterThreadNote(null)
    const plan = sendPlan(masterOpen, projectId, masterCreateRef.current)
    if (plan.kind === 'wait') return false
    if (plan.kind === 'queue') { masterCreateRef.current?.messages.push({ content, extras }); return true }
    if (plan.kind === 'create') return createMasterThread(projectId, masterOpen.fresh, { content, extras })
    const sent = client.masterSend(projectId, content, references, attachments, plan.threadId)
    return sent === 'sent' || sent === 'queued'
  }
  // A request made elsewhere for 安安 (组件库's 让安安起草一个): a new conversation of its own on 总控, made by that request.
  const startMasterConversation = (content: string): boolean => {
    const client = clientRef.current
    const projectId = getActiveProjectId()
    if (!client || status !== 'connected' || currentProject.pendingProjectId) return false
    if (!masterThreadsOn) {
      const sent = client.masterSend(projectId, content)
      return sent === 'sent' || sent === 'queued'
    }
    const next = newConversation(projectId, masterOpenRef.current)
    if (!createMasterThread(projectId, next.fresh, { content })) return false
    setMasterOpen(next)
    setMasterThreadNote(null)
    return true
  }
  // The column's two ways to hide: collapsed at full width (remembered in this browser), a drawer at narrow widths.
  const threadsNarrow = () => {
    try { return window.matchMedia('(max-width: 760px)').matches } catch { return false }
  }
  const masterColumn = masterOnPage && masterThreadsOn ? (
    <MasterThreadColumn
      groups={groupThreads(projectStore.projects, masterThreads, projectStore.activeProjectId)}
      openThreadId={masterOpen.threadId}
      connected={status === 'connected'}
      now={Date.now()}
      unread={masterThreadsUnread}
      onNew={(projectId) => {
        // 「＋」 of another project: the console switches to it first (the one project-switch road), then its new
        // conversation is on screen; the conversation and its project always go together.
        if (projectId !== getActiveProjectId() && currentProject.pendingProjectId !== projectId) {
          const switchSeq = beginProjectSwitch(projectId)
          clientRef.current?.switchProject(projectId, switchSeq)
        }
        beginMasterConversation(projectId)
      }}
      onOpen={(thread: MasterThread) => {
        // A conversation of another project: the console switches to that project first, then shows it whole.
        const plan = openPlan(thread, getActiveProjectId())
        if (plan.switchTo && currentProject.pendingProjectId !== plan.switchTo) {
          const switchSeq = beginProjectSwitch(plan.switchTo)
          clientRef.current?.switchProject(plan.switchTo, switchSeq)
        }
        setMasterOpen(current => openThread(thread, current))
        setMasterThreadNote(null)
        setThreadsDrawer(false)
      }}
      onRename={(thread, title) => {
        if (clientRef.current?.masterThreadRename(thread.projectId, thread.threadId, title) !== 'sent') showToast(t('app.notSent'), 'error')
      }}
      onArchive={(thread, archived) => {
        if (clientRef.current?.masterThreadArchive(thread.projectId, thread.threadId, archived) !== 'sent') showToast(t('app.notSent'), 'error')
      }}
      onExpandProject={(projectId) => { clientRef.current?.masterThreadList(projectId) }}
      // 「新项目」: projects are made in 项目中心 (its all-projects level), the road the top bar's project chip takes.
      onNewProject={() => { setProjectsHomeSeq(seq => seq + 1); openPage('dashboard') }}
      onHide={() => {
        if (threadsNarrow()) setThreadsDrawer(false)
        else { setThreadsCollapsed(true); saveColumnCollapsed(viewerStorage(), true) }
      }}
    />
  ) : null
  const masterControl = (
    <MasterControlPage
      artifactQuote={quoteForConversation(artifactQuoteDraft, {
        principal: auth?.account?.login ?? (auth?.required ? '' : 'local-owner'),
        projectId: getActiveProjectId(), threadId: masterView.threadId,
      })}
      onClearArtifactQuote={key => setArtifactQuoteDraft(current => current?.key === key ? null : current)}
      recent={recentItems}
      // The title line's counts (policy: the top of 总控 is the title line alone; the 今天 rows and their card
      // left): on 总控 itself and for the owner only; 安安's side panel on another page is the conversation alone.
      today={activePage === 'master' && isOwnerConsole(auth) ? {
        counts: todayView.counts,
        partial: todayView.partial,
        stale: listWaitedOut(stateOf(tracked, 'owner_attention_list')),
        error: ownerTodayError,
        onOpenGroup: openInboxGroup,
        onRefresh: () => { setOwnerTodayError(null); askOwnerToday() },
      } : undefined}
      defaultModel={settingsData?.default_model ?? ''}
      onOpenModelConfig={isOwnerConsole(auth) ? () => openPage('config') : undefined}
      // 总控对话栏 (policy; it replaced the 9-28 fold of lib/masterVisit): the conversation shown,
      // whole. Each conversation (the key) is a page of its own; a new conversation keeps its key when its first send
      // makes it, so the page is not mounted again under the owner's first message.
      messages={masterMessages}
      key={masterView.key}
      opening={masterOnPage && conversationOpening(masterOpen, masterMessages.length, masterStand)}
      threadNote={masterOnPage ? masterThreadNote : null}
      threadsToggle={masterColumn ? {
        label: t('master.threads.show'),
        expanded: threadsDrawer || !threadsCollapsed,
        onToggle: () => {
          if (threadsNarrow()) setThreadsDrawer(open => !open)
          else { setThreadsCollapsed(false); saveColumnCollapsed(viewerStorage(), false) }
        },
      } : undefined}
      connected={status === 'connected'}
      onOpenNodeProposal={isOwnerConsole(auth) ? (proposal) => {
        // Master control only proposes (policy): the builder previews it; the owner applies and saves.
        setPendingNodeProposal(proposal)
        setActivePage('workflows')
        clientRef.current?.workflowList(getActiveProjectId())
      } : undefined}
      toolActivity={masterToolActivity}
      // The running turn's stop / steer row, in the conversation the turn is in (lib/masterThreads turnBelongsTo).
      canInterrupt={!!masterTurn && turnBelongsTo(masterTurn.threadId ?? null, masterView)}
      // Pending only while the request tracker still has the receipt out (15 s, or the cut): the flag alone never
      // keeps the control waiting; the tracker's onChange (above) puts the words beside it.
      interruptPending={masterInterruptPending && latestOf(tracked, 'harness_turn_interrupt')?.state === 'busy'}
      interruptRequested={masterInterruptRequested}
      interruptError={masterInterruptError}
      steerPending={masterSteerPending && latestOf(tracked, 'harness_turn_steer')?.state === 'busy'}
      steerNote={masterSteerNote}
      onSteer={isOwnerConsole(auth) ? (text) => {
        const client = clientRef.current
        if (!client || !masterTurn || status !== 'connected' || anyBusy(tracked, 'harness_turn_steer')) return false
        const requestId = crypto.randomUUID()
        masterSteerRef.current = {requestId, sessionId: masterTurn.sessionId}
        const entry = requestTracker.track(keyOf('harness_turn_steer', requestId),
          () => client.harnessTurnSteer(getActiveProjectId(), masterTurn.sessionId, masterTurn.turnId, text, requestId), { kind: 'write' })
        const sent = entry.state === 'busy'
        setMasterSteerPending(sent)
        setMasterSteerNote(sent ? null : entry.words)
        if (!sent) masterSteerRef.current = null
        return sent
      } : undefined}
      onInterrupt={() => {
        const client = clientRef.current
        if (!client || !masterTurn || status !== 'connected' || anyBusy(tracked, 'harness_turn_interrupt') || masterInterruptRequested || masterInterruptError) return
        const requestId = crypto.randomUUID()
        masterInterruptRef.current = {requestId, sessionId: masterTurn.sessionId}
        const entry = requestTracker.track(keyOf('harness_turn_interrupt', requestId),
          () => client.harnessTurnInterrupt(getActiveProjectId(), masterTurn.sessionId, requestId, masterTurn.turnId), { kind: 'write' })
        const sent = entry.state === 'busy'
        setMasterInterruptPending(sent)
        if (!sent) {
          masterInterruptRef.current = null
          setMasterInterruptError(entry.words)
        }
      }}
      // policy: no card is answered, reviewed, opened or held on 总控 any more (the 今天 block left): those
      // orders are handled in 业务工单 and on the Feishu cards.
      // 总控对话栏: into the conversation shown; a new conversation is made by its first send (sendToMaster).
      onSend={sendToMaster}
      restoreDraft={masterOnPage ? masterRestore : null}
      onCancelAttachment={isOwnerConsole(auth) ? (messageId, attachmentId) => clientRef.current?.masterAttachmentCancel(getActiveProjectId(), messageId, attachmentId) === 'sent' : undefined}
      cancelRefusals={masterCancelRefusals}
      onLoadSource={(source) => {
        const page = source.kind === 'page' ? Math.trunc(Number(source.page ?? 0)) : 0
        setMasterSourcePreview({ attachmentId: source.attachment_id, page, loading: true })
        const sent = clientRef.current?.masterAttachmentPage(getActiveProjectId(), source.attachment_id, page)
        if (sent !== 'sent' && sent !== 'queued') setMasterSourcePreview({ attachmentId: source.attachment_id, page, loading: false, error: 'unreadable' })
      }}
      sourcePreview={masterSourcePreview}
      // 施工卡片 (总控施工模式 B2, policy), the owner's console only. 停下 / 插话 go out on the job's own session
      // and turn through the existing harness_turn_interrupt / harness_turn_steer, on the request tracker like 总控's row;
      // an answer to a build's question goes down the one answer path (interaction_reply) with exactly the option
      // pressed; 在搭建台打开这个草稿 is the builder's own open-a-flow request (the builder itself is unchanged).
      build={isOwnerConsole(auth) ? {
        activity: masterBuildActivity,
        control: buildControl,
        isBusy: (key) => stateOf(tracked, key) === 'busy',
        answers: buildAnswers,
        answering,
        onStop: (card, turnId) => {
          const client = clientRef.current
          const held = controlFor(buildControl, card.jobId, turnId)
          if (!client || status !== 'connected' || !card.sessionId || held.interruptRequested || held.interruptError
            || (held.interruptRequestId && stateOf(tracked, keyOf('harness_turn_interrupt', held.interruptRequestId)) === 'busy')) return
          const requestId = crypto.randomUUID()
          buildInterruptRef.current = { requestId, sessionId: card.sessionId }
          const entry = requestTracker.track(keyOf('harness_turn_interrupt', requestId),
            () => client.harnessTurnInterrupt(getActiveProjectId(), card.sessionId, requestId, turnId), { kind: 'write' })
          const sent = entry.state === 'busy'
          if (!sent) buildInterruptRef.current = null
          setBuildControl(current => beginBuildInterrupt(current, card.jobId, turnId, requestId, sent, entry.words))
        },
        onSteer: (card, turnId, text) => {
          const client = clientRef.current
          const held = controlFor(buildControl, card.jobId, turnId)
          if (!client || status !== 'connected' || !card.sessionId || !turnId
            || (held.steerRequestId && stateOf(tracked, keyOf('harness_turn_steer', held.steerRequestId)) === 'busy')) return false
          const requestId = crypto.randomUUID()
          buildSteerRef.current = { requestId, sessionId: card.sessionId }
          const entry = requestTracker.track(keyOf('harness_turn_steer', requestId),
            () => client.harnessTurnSteer(getActiveProjectId(), card.sessionId, turnId, text, requestId), { kind: 'write' })
          const sent = entry.state === 'busy'
          if (!sent) buildSteerRef.current = null
          setBuildControl(current => beginBuildSteer(current, card.jobId, turnId, requestId, sent, entry.words))
          return sent
        },
        onAnswer: (question, option) => {
          const checkpointId = question.checkpointId
          setBuildAnswers(current => (current[checkpointId] && !current[checkpointId].done
            ? Object.fromEntries(Object.entries(current).filter(([id]) => id !== checkpointId)) : current))
          void answerPath.answer({
            checkpointId,
            checkpointType: question.checkpointType,
            optionId: option.id,
            origin: 'master-build',
            requesterTaskId: question.requesterTaskId || undefined,
            requesterSessionId: question.requesterSessionId || undefined,
            decision: { text: option.label || option.id, option_id: option.id },
          }).then((outcome) => {
            const mark = buildAnswerMark(outcome, option.id)
            if (mark) setBuildAnswers(current => ({ ...current, [checkpointId]: mark }))
            // As decideBlocker: every ending but a second click re-reads what waits, so the counts follow.
            if (outcome.kind === 'accepted' || outcome.code !== 'duplicate_request_in_flight') { clientRef.current?.blockersList(getActiveProjectId()); askOwnerToday() }
          }).catch((error) => {
            const words = refusalInWords(localeRef.current, 'approvals.refused', error instanceof Error ? error.message : error)
            setBuildAnswers(current => ({ ...current, [checkpointId]: { optionId: option.id, done: false, words } }))
          })
        },
        onOpenDraft: (draftId) => {
          setOpenFlowRequest(current => ({ workflowId: draftId, seq: (current?.seq ?? 0) + 1 }))
          openPage('workflows')
        },
      } : undefined}
      onOpenFlowDraft={isOwnerConsole(auth) ? (messageId, team, sourceReference, expectedMaterialized) => {
        setFlowDraftOpening(messageId)
        clientRef.current?.workflowCreateFromFlowDraft(getActiveProjectId(), messageId, nextFlowId((workflows ?? []).map(item => item.id)), team, sourceReference, expectedMaterialized)
      } : undefined}
      flowDraftOpening={flowDraftOpening}
      onComponentDraftAction={isOwnerConsole(auth) ? payload => {
        if (payload.project_id !== getActiveProjectId()) return { guaranteedNotSent: true as const, reason: '当前项目已切换，本次没有发出。' }
        const targetId = (payload.expected_materialized as { package_id?: string } | undefined)?.package_id
        if (payload.type === 'workflow_adopt_component_draft' && payload.check_only !== true && targetId
          && nodeDraftStateRef.current.nodeDrafts[targetId] !== undefined) {
          showToast('请先保存或撤销当前画布上的未保存修改，再采用组件草稿。', 'error')
          return { guaranteedNotSent: true as const, reason: '请先保存或撤销当前画布上的未保存修改。' }
        }
        if (!clientRef.current) return { guaranteedNotSent: true as const, reason: '尚未连接，本次没有发出。' }
        return clientRef.current.send(payload, { queue: false }) === 'sent'
      } : undefined}
      componentDraftAck={componentDraftAck}
      // 4.7 A05: a candidate 安安 drafted for a missing component is tested, reviewed and registered where every
      // connector candidate is (配置 · 服务与连接 → 连接器与凭据, since S3-7); opening the section asks for the list again.
      onOpenConnectorCandidate={isOwnerConsole(auth) ? () => openConfigSection('connect') : undefined}
      teams={(savedOrgsList ?? []).map(org => ({ name: org.name, label: org.organization_name || org.name }))}
      // 安安 · 引用: on the builder, what it is on -- the open step, else the flow -- offered with the next message.
      reference={activePage === 'workflows' && builderFocus ? (builderFocus.nodeId
        ? { label: `${builderFocus.workflowName} · ${builderFocus.nodeName}`, value: { kind: 'node', workflow_id: builderFocus.workflowId, node_id: builderFocus.nodeId, ...(builderFocus.draftRevision ? { draft_revision: builderFocus.draftRevision } : {}) } }
        : { label: builderFocus.workflowName, value: { kind: 'workflow', workflow_id: builderFocus.workflowId, ...(builderFocus.draftRevision ? { draft_revision: builderFocus.draftRevision } : {}) } })
        : null}
      canAttach={isOwnerConsole(auth)}
      onOpenOrder={(taskId) => openExecution(taskId)}
      // 安安起草的工单 (policy, M1): the drafted order in 业务工单, where it waits 「待接单」 for the owner.
      onOpenDraftedOrder={(orderId) => {
        setOrderQueueRequest(current => ({ queue: 'all', query: '', open: orderId, flow: '', seq: (current?.seq ?? 0) + 1 }))
        openPage('orders')
      }}
    />
  )

  // The same project selector in either shell: every request the console sends names a project.
  // 控制面's panels, made once: its own page shows them all, 配置 shows one group at a time.
  const capabilityCenter = (group?: CapabilityGroup) => (
      <CapabilityCenterPage
      group={group}
        data={orgInfoData}
        snapshot={snapshot}
        connected={status === 'connected'}
        loading={capabilityLoading}
        error={orgInfoError}
        projectId={projectStore.activeProjectId}
        onRefresh={() => { setCapabilityLoading(true); setOrgInfoError(null); clientRef.current?.orgInfo() }}
        onReprobe={() => { setCapabilityReprobing(true); setOrgInfoError(null); clientRef.current?.capabilityReprobe(getActiveProjectId()) }}
        onCheckChannels={() => { setChannelCheck(null); clientRef.current?.channelConfigCheck(getActiveProjectId()) }}
        credentialReferences={credentialReferences}
        deployments={deployments}
        rollouts={rollouts}
        rolloutsHasMore={rolloutsHasMore}
        connectorSwitches={connectorSwitches}
        connectorCandidates={connectorCandidates}
        connectorRegistrations={connectorRegistrations}
        connectorCandidateError={connectorCandidateError}
        onRefreshConnectorCandidates={() => clientRef.current?.connectorCandidateList(getActiveProjectId())}
        onTestConnectorCandidate={isOwnerConsole(auth) ? candidateId => {
          setConnectorCandidateError(null)
          if (clientRef.current?.connectorCandidateTest(getActiveProjectId(), candidateId) !== 'sent') setConnectorCandidateError('未连接，本地合同测试没有发出；重连后请手动重试。')
        } : undefined}
        onReviewConnectorCandidate={isOwnerConsole(auth) ? (candidateId, fingerprint, approved, note) => {
          setConnectorCandidateError(null)
          if (clientRef.current?.connectorCandidateReview(getActiveProjectId(), candidateId, fingerprint, approved, note) !== 'sent') setConnectorCandidateError('未连接，审查决定没有发出；重连后请手动重试。')
        } : undefined}
        onRegisterConnectorCandidate={isOwnerConsole(auth) ? (candidateId, fingerprint) => {
          setConnectorCandidateError(null)
          if (clientRef.current?.connectorCandidateRegister(getActiveProjectId(), candidateId, fingerprint) !== 'sent') setConnectorCandidateError('未连接，注册请求没有发出；重连后请手动重试。')
        } : undefined}
        onRevokeConnectorCandidate={isOwnerConsole(auth) ? (connector, fingerprint, reason) => {
          setConnectorCandidateError(null)
          if (clientRef.current?.connectorCandidateRevoke(getActiveProjectId(), connector, fingerprint, reason) !== 'sent') setConnectorCandidateError('未连接，撤销请求没有发出；重连后请手动重试。')
        } : undefined}
        browserSessions={browserSessions}
        browserSessionError={browserSessionError}
        browserAccountInspection={browserAccountInspection}
        onInspectBrowserAccount={isOwnerConsole(auth) ? inspectBrowserAccount : undefined}
        browserLimits={browserLimits}
        browserCombination={browserCombination}
        browserTrail={browserTrail}
        browserLookBackNote={browserLookBackNote}
        browserAuditExportNote={browserAuditExportNote}
        browserScreenshot={browserScreenshot}
        browserLiveFrame={browserLiveFrame}
        browserLiveBinding={browserLiveBinding}
        onBrowserViewStart={isOwnerConsole(auth) ? (bindingKey, context) => { setBrowserSessionError(null); requestBrowserView(bindingKey, context) } : undefined}
        onBrowserViewStop={isOwnerConsole(auth) ? (bindingKey, context) => { stopBrowserView(bindingKey, context) } : undefined}
        onBrowserViewInput={isOwnerConsole(auth) ? (bindingKey, event, context) => { clientRef.current?.browserViewInput(getActiveProjectId(), bindingKey, event, context) } : undefined}
        onBrowserScreenshot={isOwnerConsole(auth) ? (bindingKey, name) => { setBrowserSessionError(null); clientRef.current?.browserScreenshotRead(getActiveProjectId(), bindingKey, name) } : undefined}
        onBrowserAuditTrail={isOwnerConsole(auth) ? (bindingKey) => { setBrowserSessionError(null); setBrowserLookBackNote(null); setBrowserAuditExportNote(null); clientRef.current?.browserAuditTrail(getActiveProjectId(), bindingKey) } : undefined}
        onBrowserAuditExport={isOwnerConsole(auth) ? (bindingKey) => {
          setBrowserSessionError(null)
          setBrowserAuditExportNote(null)
          if (clientRef.current?.browserAuditExport(getActiveProjectId(), bindingKey) !== 'sent') setBrowserSessionError('未连接，审计导出没有执行；不会自动重试。')
        } : undefined}
        onBrowserLookBack={isOwnerConsole(auth) ? (effectId) => {
          setBrowserSessionError(null)
          if (clientRef.current?.effectLookBack(getActiveProjectId(), effectId) !== 'sent') setBrowserSessionError('未连接，回查请求没有发出；不会自动重试。')
        } : undefined}
        onRefreshBrowserSessions={() => clientRef.current?.browserSessionList(getActiveProjectId())}
        browserCandidates={isOwnerConsole(auth) ? browserCandidates : undefined}
        browserCandidateNote={browserCandidateNote}
        browserSealedDemo={browserSealedDemo}
        onBrowserCandidate={isOwnerConsole(auth) ? (command, args) => {
          // A refresh keeps what the last compile or admission said.
          if (command !== 'list') setBrowserCandidateNote(null)
          if (clientRef.current?.browserCandidateControl(getActiveProjectId(), command, args) !== 'sent' && command !== 'list') {
            setBrowserCandidateNote(t('app.notSent'))
          }
        } : undefined}
        governancePolicy={isOwnerConsole(auth) ? governancePolicy : undefined}
        governanceError={governanceError}
        onRefreshGovernancePolicy={() => clientRef.current?.governancePolicyList(getActiveProjectId())}
        onSetGovernancePolicy={isOwnerConsole(auth) ? (field, value, reason) => {
          setGovernanceError(null)
          if (clientRef.current?.governancePolicySet(getActiveProjectId(), field, value, reason) !== 'sent') setGovernanceError(t('app.notSent'))
        } : undefined}
        onControlBrowserSession={isOwnerConsole(auth) ? (bindingKey, command, note, ok, context) => {
          setBrowserSessionError(null)
          const disposition = clientRef.current?.browserSessionControl(getActiveProjectId(), bindingKey, command, note, ok, context)
          if (disposition !== 'sent') setBrowserSessionError('未连接，浏览器接管操作没有发出；不会自动重试。')
        } : undefined}
        processSpecs={processSpecs}
        selectedSpec={selectedSpec}
        specReview={specReview}
        builderRounds={builderRounds}
        processDraft={processDraft}
        specSaving={specSaving}
        trialState={trialRequest?.phase}
        factoryError={factoryError}
        processTemplates={processTemplates}
        templateError={templateError}
        onRefreshTemplates={() => { setProcessTemplates([]); setTemplateError(null); clientRef.current?.processSpecList(getActiveProjectId()) }}
        onSpecCandidate={isOwnerConsole(auth) ? (templateId, draftId, generateSkills) => {
          const selected = selectedSpecRequest.current
          const disposition = clientRef.current?.processSpecCandidate(getActiveProjectId(), selected.specId, selected.version, templateId, draftId, generateSkills)
          setFactoryError(disposition === 'sent' ? null : '连接不可用，未创建候选包；不会自动重试。')
          setSpecSaving(disposition === 'sent')
        } : undefined}
        onSaveSpec={isOwnerConsole(auth) ? spec => {
          setFactoryError(null)
          const disposition = clientRef.current?.processSpecSave(getActiveProjectId(), spec)
          setSpecSaving(disposition === 'sent')
          if (disposition !== 'sent') setFactoryError('未连接，候选没有发出。重连后请手动保存。')
        } : undefined}
        onNewSpec={isOwnerConsole(auth) ? () => { selectedSpecRequest.current = { specId: '', version: '' }; setSelectedSpec(''); setProcessDraft(null); setSpecReview(null); setBuilderRounds([]); setFactoryError(null) } : undefined}
        onSelectSpec={(specId, version) => { selectedSpecRequest.current = { specId, version }; setSelectedSpec(specId); setProcessDraft(null); setFactoryError(null); setSpecReview(null); setBuilderRounds([]); clientRef.current?.processSpecGet(getActiveProjectId(), specId, version) }}
        onBuilderTrial={isOwnerConsole(auth) ? specId => {
          if (specSaving || trialRequestRef.current || builderRounds.some(round => round.outcome === 'running')) return
          const request: BuilderTrialRequest = { requestId: newRequestId(), projectId: getActiveProjectId(),
            specId, version: selectedSpecRequest.current.version, phase: 'pending' }
          trialRequestRef.current = request
          setTrialRequest(request)
          const disposition = clientRef.current?.builderTrial(request.projectId, specId, request.version, request.requestId)
          if (disposition !== 'sent') { trialRequestRef.current = null; setTrialRequest(null) }
          setFactoryError(disposition === 'sent' ? null : '未连接，检查请求没有发出。重连后请重试。')
        } : undefined}
        onBuilderTrialResume={isOwnerConsole(auth) ? (specId, attempt, decision) => {
          if (specSaving || trialRequestRef.current || builderRounds.some(round => round.outcome === 'running')) return
          const request: BuilderTrialRequest = { requestId: newRequestId(), projectId: getActiveProjectId(),
            specId, version: selectedSpecRequest.current.version, phase: 'pending' }
          trialRequestRef.current = request
          setTrialRequest(request)
          const disposition = clientRef.current?.builderTrialResume(request.projectId, specId, request.version, attempt, decision, request.requestId)
          if (disposition !== 'sent') { trialRequestRef.current = null; setTrialRequest(null) }
          setFactoryError(disposition === 'sent' ? null : '未连接，继续试跑的请求没有发出。重连后请重试。')
        } : undefined}
        solutionPackages={solutionPackages}
        rolloutError={rolloutError}
        // Owner-only, as the server decides it: another account sees the switches' state and 「只有负责人能打开」, not
        // buttons the server refuses with owner_required.
        onSwitchConnector={isOwnerConsole(auth) ? (connector, enabled, reason) => clientRef.current?.connectorSwitchSet(getActiveProjectId(), connector, enabled, reason) : undefined}
        focusDesktopChannel={desktopChannelFocus}
        onDesktopChannelFocused={() => setDesktopChannelFocus(false)}
        pendingTurns={pendingTurns}
        pendingTurnError={pendingTurnError}
        turnLedger={turnLedger}
        onTurnActions={isOwnerConsole(auth) ? (sessionId, turnId) => { setTurnLedger(null); clientRef.current?.harnessTurnActions(getActiveProjectId(), sessionId, turnId) } : undefined}
        onSettleTurn={isOwnerConsole(auth) ? (sessionId, reason, expectedTurnId) => {
          const turnId = pendingTurns.find(turn => turn.session_id === sessionId)?.turn_id
          if (!expectedTurnId || turnId !== expectedTurnId) { setPendingTurnError('回合已变化，请刷新后重新核验。'); return }
          const sent = clientRef.current?.harnessTurnSettle(getActiveProjectId(), sessionId, reason, expectedTurnId) === 'sent'
          setPendingTurnError(sent ? null : t('app.requestFailed'))
        } : undefined}
        onInterruptTurn={isOwnerConsole(auth) ? (sessionId, expectedTurnId) => {
          const turnId = pendingTurns.find(turn => turn.session_id === sessionId)?.turn_id
          if (!expectedTurnId || turnId !== expectedTurnId) { setPendingTurnError('回合已变化，请刷新后重新选择。'); return }
          const sent = clientRef.current?.harnessTurnInterrupt(getActiveProjectId(), sessionId, '', expectedTurnId) === 'sent'
          setPendingTurnError(sent ? null : t('app.requestFailed'))
        } : undefined}
        onPlanRollout={(solutionId, version, targets) => { setRolloutError(null); clientRef.current?.planRollout(getActiveProjectId(), solutionId, version, targets) }}
        onAdvanceRollout={(rolloutId, customerId, environmentId) => { setRolloutError(null); clientRef.current?.advanceRollout(getActiveProjectId(), rolloutId, customerId, environmentId) }}
        onRolloutHealth={(rolloutId, customerId, environmentId, passed) => { setRolloutError(null); clientRef.current?.rolloutHealth(getActiveProjectId(), rolloutId, customerId, environmentId, passed) }}
        onPromoteRollout={rolloutId => { setRolloutError(null); clientRef.current?.promoteRollout(getActiveProjectId(), rolloutId) }}
        onRollbackRollout={(rolloutId, note) => { setRolloutError(null); clientRef.current?.rollbackRollout(getActiveProjectId(), rolloutId, note) }}
        channelCheck={channelCheck}
        reprobing={capabilityReprobing}
      />
  )

  const projectSelector = (parts?: 'switch' | 'manage' | 'create') => (
    <ProjectSelector
      parts={parts}
      projects={projectStore.projects}
      activeId={projectStore.activeProjectId}
      projectIdPolicy={status === 'connected' ? projectIdPolicy : null}
      connected={status === 'connected'}
      onSelect={(id) => {
        const switchSeq = beginProjectSwitch(id)
        clientRef.current?.switchProject(id, switchSeq)
      }}
      onCreate={(id) => {
        clientRef.current?.createProject(id)
      }}
      onDelete={(id, confirmation) => clientRef.current?.deleteProject(id, confirmation)}
      onAskRemovalPlan={(id) => { setRemovalPlan(null); clientRef.current?.planProjectRemoval(id) }}
      removalPlan={removalPlan}
      onArchive={(id) => clientRef.current?.archiveProject(id)}
      onRestore={(id) => clientRef.current?.restoreProject(id)}
      onExport={(id) => clientRef.current?.exportProject(id)}
      onBackup={(id) => clientRef.current?.backupProject(id)}
      onRestorePreflight={isOwnerConsole(auth) ? (archivePath, targetProjectId) => {
        setRestorePreflight(null)
        setRestorePreflightError(null)
        clientRef.current?.restorePreflight(getActiveProjectId(), archivePath, targetProjectId)
      } : undefined}
      restorePreflight={restorePreflight}
      restorePreflightError={restorePreflightError}
      lastBackupPath={lastBackupPath}
      onRestoreBackup={isOwnerConsole(auth) ? (archive, projectId) => {
        const sent = clientRef.current?.restoreBackup(archive, projectId) === 'sent'
        if (!sent) { setRestoreBackupState({ phase: 'failed', error: t('project.restoreBackupNotSent') }); return }
        setRestoreBackupState({ phase: 'pending' })
        if (restoreBackupTimerRef.current) clearTimeout(restoreBackupTimerRef.current)
        // A lost receipt must not leave the button spinning for ever, and must not invite a blind retry.
        restoreBackupTimerRef.current = setTimeout(() => {
          restoreBackupTimerRef.current = null
          setRestoreBackupState(current => current.phase === 'pending' ? { phase: 'failed', error: t('project.restoreBackupNoReceipt') } : current)
          clientRef.current?.listProjects()
        }, 60_000)
      } : undefined}
      restoreBackupState={restoreBackupState}
      onOpenLifecycle={isOwnerConsole(auth) ? (projectId) => {
        lifecycleProjectIdRef.current = projectId
        setLifecycleProjectId(projectId); setLifecycleTrail(null); setLifecycleError(null)
        clientRef.current?.projectLifecycle(projectId)
      } : undefined}
      onCloseLifecycle={() => { lifecycleProjectIdRef.current = null; setLifecycleProjectId(null); setLifecycleTrail(null); setLifecycleError(null) }}
      lifecycleProjectId={lifecycleProjectId}
      lifecycleTrail={lifecycleTrail}
      lifecycleError={lifecycleError}
      onSetCustomer={(id, customerId) => clientRef.current?.projectSetCustomer(id, customerId)}
      customers={knownCustomers}
    />
  )

  // The execution window contains only the active browser/application surface.
  const executionPages = executionEntry(browserSessions).sessions
  const executionFocused = (browserPane && executionPages.find(row => row.binding_key === browserPane.binding_key))
    || browserForOrder(browserSessions, sessionStore.activeSessionId)
  const closeExecutionWindow = () => {
    // Stop the view subscription, never the task or its lease.
    if (browserPane) {
      dismissedPanesRef.current.add(browserPane.binding_key)
      stopBrowserView(browserPane.binding_key, browserControlContext(browserSessions.find(row => row.binding_key === browserPane.binding_key)) ?? undefined)
      browserPaneRef.current = null
      setBrowserPane(null)
    }
    setExecutionDetailTab(null)
    setExecutionPanelOpen(false)
  }
  const focusExecutionBrowser = (session?: BrowserSessionRow) => {
    const row = session ?? executionFocused
    if (row) {
      if (browserPane && browserPane.binding_key !== row.binding_key) {
        stopBrowserView(browserPane.binding_key, browserControlContext(browserSessions.find(item => item.binding_key === browserPane.binding_key)) ?? undefined)
      }
      dismissedPanesRef.current.delete(row.binding_key)
      setBrowserPane({ binding_key: row.binding_key, task_id: row.task_id, note: row.note, anchor_session_id: row.anchor_session_id, order_id: row.order_id })
      requestBrowserView(row.binding_key, browserControlContext(row) ?? undefined)
    }
    openExecution(row?.anchor_session_id || row?.task_id, 'browser')
  }

  // The run's record (记录 / 步骤 / 步骤图): sessions, the line into an order's session, work-item retry and completion.
  // Since the R2 flat execution window it opens only in the separate ExecutionInspector, never inside the viewport.
  const workspaceView: WorkspaceView = executionDetailTab === 'steps' || executionDetailTab === 'graph' ? executionDetailTab : 'record'
  const workspaceBody = (
    <WorkspacePage
      view={workspaceView}
      onRetryWorkItem={(workItemId, anchorTaskId, reason) => {
        clientRef.current?.workItemRetry(getActiveProjectId(), workItemId, anchorTaskId, reason)
      }}
      boardStore={boardStore}
      chatStore={chatStore}
      sessionStore={sessionStore}
      agents={swarmAgents}
      officeMap={officeMap}
      execMode={globalExecMode}
      companyProfile={globalCompanyProfile}
      taskPreferredAgent={globalTaskPreferredAgent}
      nativeApprovalDefault={globalNativeApprovalDefault}
      projectId={projectStore.activeProjectId}
      orgInfoData={orgInfoData}
      savedOrgsList={savedOrgsList}
      activeSavedOrg={activeSavedOrg}
      onSavedOrgsList={handleSavedOrgsList}
      onSavedOrgLoad={handleSavedOrgLoad}
      commsState={commsState}
      commsMessage={commsMessage}
      onCommsRefresh={(opts) => {
        const { project_id: _ignoredProjectId, ...scopedOpts } = opts ?? {}
        clientRef.current?.commsState(getActiveProjectId(), scopedOpts)
      }}
      onCommsReadMessage={(path) => clientRef.current?.commsReadMessage(
        getActiveProjectId(),
        path,
        commsState,
      )}
      // policy (R1, option B): the old board, free sessions and the secretary channel left the console. Work
      // starts from 业务工单 (workflow_invoke starts the order's session on the server); questions and drafting go to
      // 安安 on 总控. Nothing here sends run_task, kanban_create_task, kanban_move_task, create_session or
      // secretary_send any more -- the server keeps those actions (lib/wsClient keeps its senders), so they can come
      // back without a kernel change. A line into a session that belongs to an order still goes out below.
      onSessionSend={(taskId, content, attachments, metadata) => {
        // A chat message expects no receipt (its reply is the message itself), so it is not on the tracker; but a
        // message the connection refused (not connected) is said at once with the tracker's words, and the
        // composer is told so it keeps the draft and shows no row for it. Nothing is re-sent on its own.
        const disposition: SendDisposition = clientRef.current?.sessionSend(getActiveProjectId(), taskId, content, attachments, metadata) ?? 'send-failed'
        if (disposition === 'send-failed' || disposition === 'queue-full') showToast(t('request.notSent'), 'error')
        return disposition
      }}
      onUpdateRole={(rid, updates) => clientRef.current?.updateRole(rid, updates)}
      onInteractionReply={(request) => {
        // The conversation's card keeps its own request id (stable per card) and speaks receipts; the send itself
        // goes through the one answer path (lib/answerPath), so it obeys the same pending, project and re-send rules.
        return answerPath.answer({
          checkpointId: request.checkpointId,
          checkpointType: request.checkpointType,
          optionId: String(request.decision.option_id ?? 'reply'),
          origin: 'conversation',
          clientRequestId: request.clientRequestId,
          requesterTaskId: request.requesterTaskId,
          requesterSessionId: request.requesterSessionId,
          decision: request.decision,
        }).then(outcome => receiptOf(outcome, request))
      }}
      // 去总控 on a session no order launched (it reads, it takes no new line): 总控, where 安安 is; the side window
      // closes so the page is in view.
      onGoToMaster={() => { setExecutionDetailTab(null); setExecutionPanelOpen(false); openPage('master') }}
      onDeleteSession={(taskId) => clientRef.current?.deleteSession(getActiveProjectId(), taskId)}
      onTitleChange={(taskId, title) => clientRef.current?.sessionUpdateTitle(getActiveProjectId(), taskId, title)}
      onSessionConfigChange={handleSessionModeChange}
      onSessionTaskAgentChange={handleSessionTaskAgentChange}
      onSessionNativeApprovalLevelChange={handleSessionNativeApprovalLevelChange}
      onSetNativeApprovalDefault={handleSetNativeApprovalDefault}
      onSessionStop={handleSessionStop}
      onSessionResume={handleSessionResume}
      onSessionComplete={(taskId) => clientRef.current?.sessionComplete(getActiveProjectId(), taskId)}
      onLoadSessionDetail={(taskId, opts) => {
        const client = clientRef.current
        if (!client) return
        return client.sessionDetail(
          getActiveProjectId(),
          taskId,
          {
            ...opts,
            include: opts?.detailLevel === 'full'
              ? ['messages', 'session_state', 'progress', 'work_items', 'runtime_context']
              : ['messages', 'session_state'],
            viewGeneration: currentProject.generation,
          },
        ).then((payload) => {
          if (payload.ok === false) {
            throw new Error(String(payload.error ?? 'session_detail failed'))
          }
        })
      }}
      onOpenExecutionPanel={handleOpenExecutionPanel}
      getExternalTeamActivity={(taskId, invocationId) => externalTeamActivityStore.getForTask(projectStore.activeProjectId, taskId, invocationId)}
      onCollabSync={() => clientRef.current?.collabSync(getActiveProjectId(), undefined, currentProject.generation)}
      connected={status === 'connected'}
    />
  )

  // 账号: one section, used on 设置 and still inside the models dialog. The fallback treats a console that has not
  // reported auth yet the way the dialog always did — bootstrap — but 设置 only mounts it for the owner console,
  // and bootstrap_needed is that console (lib/ownerConsole).
  const accountsSection = () => (
    <AccountsSection
      auth={auth ?? { required: false, bootstrap_needed: true, account: null }}
      accounts={accounts}
      employees={orgInfoData?.employees ?? []}
      lastError={accountsError}
      onRefresh={() => clientRef.current?.accountList(getActiveProjectId())}
      onBootstrapOwner={(login, password, displayName) => clientRef.current?.accountBootstrapOwner(getActiveProjectId(), login, password, displayName)}
      onCreate={(login, password, employeeId, displayName) => clientRef.current?.accountCreate(getActiveProjectId(), login, password, employeeId, displayName)}
      onSetDisabled={(login, disabled) => clientRef.current?.accountSetDisabled(getActiveProjectId(), login, disabled)}
      onSetPassword={(login, password) => clientRef.current?.accountSetPassword(getActiveProjectId(), login, password)}
      onSetCustomerScope={(login, customerIds) => clientRef.current?.accountSetCustomerScope(login, customerIds)}
      onLogout={() => { clientRef.current?.authLogout(getActiveProjectId(), readAuthToken()); writeAuthToken('') }}
      connected={status === 'connected'}
    />
  )

  return (
    <div className={`app-shell shell-ampuly${executionPanelOpen ? ' has-assistant has-execution' : assistantOpen && activePage !== 'master' ? ' has-assistant' : ''}`}
      // 官方 is the light surface with the brand's dark rail: it renders on the light theme's rules and names the
      // palette separately; the theme registry and what is stored stay exactly as they were. (The earlier dark 官方
      // belonged to the earlier top-bar shell, retired with S3-7.)
      data-theme={theme === 'superones' ? 'paper' : theme}
      data-palette={theme}>
      {orgToast && (
        <div className={`org-toast org-toast--${orgToast.kind}`} role="status" aria-live="polite">
          {orgToast.text}
        </div>
      )}
      {/* The rail's dot on 业务工单: orders waiting on the owner, by 总控's 需要关注 (lib/attention), not the count of pauses. */}
      <ShellRail activePage={activePage} ordersWaiting={attentionFound.total} onOpenPage={openPage}
        pendingApprovalBadge={isV2SurfaceEnabled('f_frontend_pending_approval_badge') ? pendingApprovalBadgeState : null} />
      <ShellTopbar
        activePage={activePage}
        connection={status === 'connected' ? 'connected' : status === 'connecting' ? 'connecting' : 'disconnected'}
        connectionDetail={`${status}${statusDetail ? ` — ${statusDetail}` : ''}\n${wsUrl}`}
        // Which project the pages are showing -- and the way to where projects are chosen. Not a switcher: a
        // project is business content, managed in 项目中心, not a level above the system (policy).
        projectSlot={(
          <>
          <button type="button" className="shell-project-chip" data-testid="shell-project-chip" onClick={() => { setProjectsHomeSeq(seq => seq + 1); openPage('dashboard') }}
            title={locale.startsWith('zh') ? '当前项目。到项目中心切换或新建项目' : 'The current project. Switch or create one in the project centre'}>
            <small>{locale.startsWith('zh') ? '项目' : 'Project'}</small>
            <span>{projectStore.projects.find(project => project.id === projectStore.activeProjectId)?.name || projectStore.activeProjectId}</span>
          </button>
          </>
        )}
        builderFlow={activePage === 'workflows' ? builderFlow : null}
        observatory={isOwnerConsole(auth) ? { onOpen: () => { setObservatoryOpenSequence(value => value + 1) } } : undefined}
        assistant={activePage === 'master' ? undefined : { open: assistantOpen && !executionPanelOpen, onToggle: () => { setExecutionPanelOpen(false); setAssistantOpen(open => (executionPanelOpen ? true : !open)) } }}
        execution={isOwnerConsole(auth) ? {
          entry: executionEntry(browserSessions),
          // The same two steps a reload takes for a page handed to a person: show the pane, start the view.
          onOpen: (bindingKey) => {
            const session = browserSessions.find(row => row.binding_key === bindingKey)
            if (!session) return
            dismissedPanesRef.current.delete(bindingKey)
            setBrowserPane({ binding_key: session.binding_key, task_id: session.task_id, note: session.note, anchor_session_id: session.anchor_session_id, order_id: session.order_id })
            requestBrowserView(session.binding_key, browserControlContext(session) ?? undefined)
            // FE-EB-01/03：顶栏点会话时同步打开执行区浏览器表面
            openExecution(session.task_id, 'browser')
          },
          // 执行区's side window (S3-7): 记录 / 步骤 / 步骤图 / 浏览器 / 桌面 / 终端 / 文件 over the run being looked at. The
          // live pages are the rows above; their limits, audit and candidates are under 配置 · 服务与连接.
          onOpenAll: () => openExecution(),
        } : undefined}
      />
      {executionPanelOpen && (
        <ExecutionWindow
          zh={locale.startsWith('zh')}
          surface={executionTab}
          sessions={executionPages}
          bindingKey={executionFocused?.binding_key}
          desktopTitle={executionTab === 'desktop' ? desktopSessionStatus?.app || undefined : undefined}
          onBrowser={focusExecutionBrowser}
          onDesktop={() => {
            if (browserPane) stopBrowserView(browserPane.binding_key, browserControlContext(executionFocused) ?? undefined)
            openExecution(undefined, 'desktop')
          }}
          onDetails={(tab) => openExecution(undefined, tab)}
          onClose={closeExecutionWindow}
          onStop={sessionStore.activeSession?.canStop ? () => handleSessionStop(sessionStore.activeSession!.taskId) : undefined}
          onResume={sessionStore.activeSession?.canResume ? () => handleSessionResume(sessionStore.activeSession!.taskId) : undefined}
        >
            {executionTab === 'browser' && (() => {
              // FE-EB-01/02：执行区浏览器页签 = 嵌入画面位；列表为次级。仍按 Q05 B5 优先当前工单 awaiting_human。
              const livePages = executionEntry(browserSessions).sessions
              const mine = browserForOrder(browserSessions, sessionStore.activeSessionId)
              const focused = (browserPane && livePages.find(row => row.binding_key === browserPane.binding_key)) || mine
              const attachSession = (session: NonNullable<typeof mine>) => {
                dismissedPanesRef.current.delete(session.binding_key)
                setBrowserPane({ binding_key: session.binding_key, task_id: session.task_id, note: session.note, anchor_session_id: session.anchor_session_id, order_id: session.order_id })
                requestBrowserView(session.binding_key, browserControlContext(session) ?? undefined)
              }
              return (
                <ExecutionBrowserWorkspace
                  zh={locale.startsWith('zh')}
                  sessions={livePages}
                  focused={focused}
                  frame={browserLiveFrame && focused && browserLiveFrame.binding_key === focused.binding_key ? browserLiveFrame : null}
                  connected={status === 'connected'}
                  canInput={isOwnerConsole(auth)}
                  emptyText={t('shell.executionBrowserNone')}
                  listHint={t('shell.executionSome')}
                  onFocusSession={(session) => attachSession(session)}
                  surfaceOnly
                  onViewPick={(bindingKey, x, y, context) => clientRef.current?.browserViewPick(getActiveProjectId(), bindingKey, x, y, context)}
                  pickedElement={browserPickedElement}
                  onControl={(bindingKey, command, note, ok, context) => { clientRef.current?.browserSessionControl(getActiveProjectId(), bindingKey, command, note, ok, context) }}
                  onInput={isOwnerConsole(auth) ? (bindingKey, event, context) => { clientRef.current?.browserViewInput(getActiveProjectId(), bindingKey, event, context) } : undefined}
                />
              )
            })()}
            {executionTab === 'desktop' && (
              <DesktopSessionControls
                surfaceOnly
                projectId={getActiveProjectId()}
                authToken={auth?.account?.role === 'owner' ? readAuthToken() : ''}
                zh={locale.startsWith('zh')}
                taskId={sessionStore.activeSessionId}
                canControl={isOwnerConsole(auth)}
                status={desktopSessionStatus}
                error={desktopSessionError}
                busy={desktopSessionBusy}
                onRefresh={() => {
                  const taskId = String(sessionStore.activeSessionId || '').trim()
                  setDesktopSessionError(null)
                  if (!taskId) return
                  setDesktopSessionBusy(true)
                  const disposition = clientRef.current?.desktopSessionControl(getActiveProjectId(), taskId, 'status')
                  if (disposition !== 'sent') {
                    setDesktopSessionBusy(false)
                    setDesktopSessionError('未连接，桌面状态没有发出；不会自动重试。')
                  }
                }}
                onTakeover={() => {
                  const taskId = String(sessionStore.activeSessionId || '').trim()
                  setDesktopSessionError(null)
                  if (!taskId) return
                  setDesktopSessionBusy(true)
                  const disposition = clientRef.current?.desktopSessionControl(getActiveProjectId(), taskId, 'takeover', locale.startsWith('zh') ? '我来操作' : 'taking over')
                  if (disposition !== 'sent') {
                    setDesktopSessionBusy(false)
                    setDesktopSessionError('未连接，桌面接管操作没有发出；不会自动重试。')
                  }
                }}
                onHandBack={() => {
                  const taskId = String(sessionStore.activeSessionId || '').trim()
                  setDesktopSessionError(null)
                  if (!taskId) return
                  setDesktopSessionBusy(true)
                  const disposition = clientRef.current?.desktopSessionControl(getActiveProjectId(), taskId, 'hand_back', locale.startsWith('zh') ? '我操作完了' : 'done')
                  if (disposition !== 'sent') {
                    setDesktopSessionBusy(false)
                    setDesktopSessionError('未连接，桌面交还操作没有发出；不会自动重试。')
                  }
                }}
              />
            )}
        </ExecutionWindow>
      )}
      {executionDetailTab && (
        <ExecutionInspector title={t(`shell.executionTab.${executionDetailTab}`)} onClose={() => setExecutionDetailTab(null)}>
          {executionDetailTab === 'record' && sessionStore.activeSessionId && <button type="button" className="shell-btn"
            onClick={() => { const taskId = sessionStore.activeSessionId; setExecutionDetailTab(null); if (taskId) handleOpenExecutionPanel(taskId) }}>
            {locale.startsWith('zh') ? '查看步骤详情' : 'View step details'}
          </button>}
          {(executionDetailTab === 'record' || executionDetailTab === 'steps' || executionDetailTab === 'graph') && (
            <section className="shell-execution-record" data-testid="shell-execution-inspector-workspace">{workspaceBody}</section>
          )}
            {(executionDetailTab === 'terminal' || executionDetailTab === 'record' || executionDetailTab === 'steps' || executionDetailTab === 'graph') && (() => {
              const mine = browserForOrder(browserSessions, sessionStore.activeSessionId)
              const correlated = mine?.binding_key ?? browserPane?.binding_key ?? null
              return (
                <ExecutionTerminalView
                  zh={locale.startsWith('zh')}
                  entries={sessionStore.activeSession?.progressLog}
                  correlatedBrowserBindingKey={correlated}
                  artifactFiles={artifactIndex?.files ?? null}
                  onLocateBrowser={(bindingKey) => {
                    const sessions = executionEntry(browserSessions).sessions
                    const row = sessions.find(s => s.binding_key === bindingKey) || mine
                    if (row) {
                      dismissedPanesRef.current.delete(row.binding_key)
                      setBrowserPane({ binding_key: row.binding_key, task_id: row.task_id, note: row.note, anchor_session_id: row.anchor_session_id, order_id: row.order_id })
                      requestBrowserView(row.binding_key, browserControlContext(row) ?? undefined)
                    }
                    openExecution(sessionStore.activeSessionId || undefined, 'browser')
                  }}
                  onPreviewArtifact={(path) => {
                    setArtifactPreviewLoading(true)
                    setArtifactPreviewError(null)
                    setArtifactPreview(null)
                    clientRef.current?.artifactRead(getActiveProjectId(), path)
                    openExecution(undefined, 'files')
                  }}
                />
              )
            })()}
            {executionDetailTab === 'files' && <div className="shell-execution-files">{artifactsPanel}</div>}
            {executionDetailTab === 'subsessions' && (() => {
              // FE-EB-11：子会话页签收敛到执行工作台（进度+消息摘要），一键切浏览器表面；不复制看板 ExecutionPanel。
              const children = getWorkItemChildSessions(sessionStore.activeSession, sessionStore.sessions)
              const mine = browserForOrder(browserSessions, sessionStore.activeSessionId)
              const correlated = mine?.binding_key ?? browserPane?.binding_key ?? null
              const openBrowserSurface = (taskId?: string, bindingKey?: string | null) => {
                const key = String(bindingKey || correlated || '').trim()
                const sessions = executionEntry(browserSessions).sessions
                const row = (key && sessions.find(s => s.binding_key === key)) || mine || sessions[0]
                if (row) {
                  dismissedPanesRef.current.delete(row.binding_key)
                  setBrowserPane({ binding_key: row.binding_key, task_id: row.task_id, note: row.note, anchor_session_id: row.anchor_session_id, order_id: row.order_id })
                  requestBrowserView(row.binding_key, browserControlContext(row) ?? undefined)
                }
                openExecution(taskId || sessionStore.activeSessionId || undefined, 'browser')
              }
              const previewArtifact = (path: string) => {
                setArtifactPreviewLoading(true)
                setArtifactPreviewError(null)
                setArtifactPreview(null)
                clientRef.current?.artifactRead(getActiveProjectId(), path)
                openExecution(undefined, 'files')
              }
              return (
                <ExecutionSubSessionsPanel
                  zh={locale.startsWith('zh')}
                  childSessions={children}
                  correlatedBrowserBindingKey={correlated}
                  artifactFiles={artifactIndex?.files ?? null}
                  onOpenBrowserSurface={openBrowserSurface}
                  onPreviewArtifact={previewArtifact}
                  onFocusChildSession={(taskId) => {
                    sessionStore.setActiveSession(taskId)
                    openExecution(taskId, 'record')
                  }}
                />
              )
            })()}
            {executionDetailTab === 'observatory' && isOwnerConsole(auth) && (
              // FE-EB-23：Observatory 深链进同一执行工作台 Tab（负责人/专家）；非 owner 不渲染、不入默认页签。
              <ExecutionObservatoryPanel
                zh={locale.startsWith('zh')}
                projectId={projectStore.activeProjectId}
                activeTaskId={sessionStore.activeSessionId}
                onOpenFullObservatory={(taskId) => {
                  const observe = String(taskId || sessionStore.activeSessionId || '').trim()
                  if (observe && typeof window !== 'undefined') {
                    try {
                      const url = new URL(window.location.href)
                      url.searchParams.set('view', 'observatory')
                      url.searchParams.set('observe_task', observe)
                      window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`)
                    } catch { /* ignore */ }
                  }
                  setObservatoryOpenSequence(value => value + 1)
                }}
              />
            )}
        </ExecutionInspector>
      )}
      {assistantOpen && !executionPanelOpen && activePage !== 'master' && (
        <aside className="shell-assistant" aria-label={t('shell.assistant')} data-testid="shell-assistant">
          <header className="shell-assistant-head">
            <img src="./brand/anan-avatar.png" alt="" width={30} height={26} />
            <div><strong>{t('shell.assistant')}</strong><small>{t('shell.assistantNote', { page: activePageLabel })}</small></div>
            <button type="button" onClick={() => setAssistantOpen(false)} aria-label={t('shell.assistantClose')}>×</button>
          </header>
          {masterControl}
        </aside>
      )}
      {activePage === 'settings' && (
        <PreferencesPage
          quietCompletion={quietCompletion}
          quietSaveFailed={quietSaveFailed}
          onQuietCompletionChange={changeQuietCompletion}
          theme={theme}
          onTheme={(next) => { setTheme(next); saveStoredTheme(next) }}
          devToolsShown={showDevTools}
          onToggleDevTools={() => setShowDevTools(shown => !shown)}
          notify={settingsData?.notify ?? null}
          onOpenChannels={() => openPage('config')}
          accountsSlot={isOwnerConsole(auth) ? accountsSection() : undefined}
          modelsSlot={isOwnerConsole(auth) ? (
            <SettingsModal
              embedded
              data={settingsData}
              error={settingsError}
              testResult={settingsTestResult}
              notifyResult={settingsNotifyResult}
              gateStatus={gateStatus}
              onGateStatus={() => clientRef.current?.gateStatus(getActiveProjectId())}
              connected={status === 'connected'}
              onNotifyTest={() => {
                setSettingsNotifyResult(null)
                clientRef.current?.notifyTest(getActiveProjectId())
              }}
              onClose={() => undefined}
              onRetry={() => {
                setSettingsError(null)
                clientRef.current?.settingsGet(getActiveProjectId())
              }}
              onSetKey={(apiKeyEnv, value) => clientRef.current?.settingsSetKey(getActiveProjectId(), apiKeyEnv, value)}
              onTestModel={(model) => {
                setSettingsTestResult(null)
                clientRef.current?.settingsTestModel(getActiveProjectId(), model)
              }}
              onSetRouting={(defaultModel, routing) => clientRef.current?.settingsSetRouting(getActiveProjectId(), defaultModel, routing)}
              onSetModelTiers={(tiers, tierParams, taskTiers, residency, optout, windowCap) =>
                clientRef.current?.settingsSetModelTiers(getActiveProjectId(), tiers, tierParams, taskTiers,
                                                         residency, optout, windowCap)}
              onSetFlag={(env, enabled) => clientRef.current?.settingsSetFlag(getActiveProjectId(), env, enabled)}
            />
          ) : undefined}
        />
      )}
      {activePage === 'config' && (
        <ConfigPage
          initialSection={configEntry}
          settings={settingsData}
          settingsError={settingsError}
          onOpenModels={openModelsDialog}
          capabilities={isOwnerConsole(auth) ? capabilityCenter : undefined}
          onCapabilitySection={() => requestCapabilityLists(getActiveProjectId())}
          projects={projectStore.projects}
          projectOverview={projectOverview}
          onAskProjectOverview={isOwnerConsole(auth) ? () => clientRef.current?.projectOrderOverview(getActiveProjectId()) : undefined}
          dataLocations={dataLocations}
          dataBackups={dataBackups}
          cleanupPreview={cleanupPreview}
          onAskCleanupPreview={isOwnerConsole(auth) ? () => clientRef.current?.dataCleanupPreview(getActiveProjectId()) : undefined}
          onAskDataLocations={isOwnerConsole(auth) ? () => {
            clientRef.current?.dataLocations(getActiveProjectId())
            clientRef.current?.dataBackups(getActiveProjectId())
          } : undefined}
          onForgetBackupTask={isOwnerConsole(auth) ? (name) => {
            clientRef.current?.forgetBackupTask(name)
            // 清完立刻重读一次，让那一行当场消失——而不是让人怀疑「点了到底有没有用」。
            window.setTimeout(() => clientRef.current?.dataBackups(getActiveProjectId()), 400)
          } : undefined}
          onOpenProject={(projectId) => { if (projectId !== getActiveProjectId()) { const switchSeq = beginProjectSwitch(projectId); clientRef.current?.switchProject(projectId, switchSeq) } openPage('dashboard') }}
          // 组织与角色 (S3-7): the one organisation board, for the owner; 偏好 is the way to 设置.
          orgBoard={isOwnerConsole(auth) ? orgBoard : undefined}
          onOpenPreferences={() => openPage('settings')}
          // 渠道连接's 企业微信 card (W3b): the owner's only; it asks for the status itself when the section shows it.
          customerServiceCard={isOwnerConsole(auth) ? (
            <CustomerServiceCard
              key={`cs-${getActiveProjectId()}`}
              state={customerService.projectId === getActiveProjectId()
                ? customerService
                : { ...EMPTY_CUSTOMER_SERVICE, projectId: getActiveProjectId() }}
              connected={status === 'connected'}
              onSend={(action, params) => {
                const projectId = getActiveProjectId()
                const client = clientRef.current
                const suiteId = String(params.suite_id ?? '')
                const budget = (params.budget ?? {}) as Record<string, unknown>
                const disposition = !client ? 'send-failed' : (
                  action === 'customer_service_prepare' ? client.customerServicePrepare(projectId, params)
                  : action === 'customer_service_detail' ? client.customerServiceDetail(projectId, suiteId)
                  : action === 'customer_service_test_start' ? client.customerServiceTestStart(projectId, suiteId, budget)
                  : action === 'customer_service_review' ? client.customerServiceReview(projectId, params)
                  : action === 'customer_service_admit' ? client.customerServiceAdmit(projectId, suiteId)
                  : action === 'customer_service_revoke' ? client.customerServiceRevoke(projectId, suiteId, String(params.reason_code ?? ''))
                  : action === 'customer_service_channel_select' ? client.customerServiceChannelSelect(projectId, suiteId, budget)
                  : action === 'customer_service_channel_pause' ? client.customerServiceChannelPause(projectId, String(params.account_id ?? ''))
                  : action === 'customer_service_channel_replace' ? client.customerServiceChannelReplace(projectId, suiteId, budget, String(params.expected_record_sha256 ?? ''))
                  : 'send-failed'
                )
                const sent = disposition === 'sent' || disposition === 'queued'
                setCustomerService(prev => customerServiceSent(prev, projectId, action, sent))
                return sent
              }}
            />
          ) : undefined}
          wecomCard={isOwnerConsole(auth) ? (
            <WecomChannelCard
              key={getActiveProjectId()}
              state={wecomStateFor(wecomChannel, getActiveProjectId())}
              connected={status === 'connected'}
              flows={flowChoices(workflows, getActiveProjectId())}
              onAsk={() => {
                const projectId = getActiveProjectId()
                clientRef.current?.wecomChannelStatus(projectId)
                if (workflows === null) clientRef.current?.workflowList(projectId)
              }}
              onSwitch={enabled => {
                const projectId = getActiveProjectId()
                const sent = clientRef.current?.wecomChannelSet(projectId, enabled) ?? 'send-failed'
                setWecomChannel(prev => wecomSent(prev, projectId, 'switch', sent))
              }}
              onSaveAccount={request => {
                const projectId = getActiveProjectId()
                const sent = clientRef.current?.wecomKfAccountSet(projectId, request) ?? 'send-failed'
                setWecomChannel(prev => wecomSent(prev, projectId, 'account', sent))
              }}
              onRemoveAccount={openKfid => {
                const projectId = getActiveProjectId()
                const sent = clientRef.current?.wecomKfAccountSet(projectId, { open_kfid: openKfid, remove: true }) ?? 'send-failed'
                setWecomChannel(prev => wecomSent(prev, projectId, 'account', sent))
              }}
            />
          ) : undefined}
        />
      )}

      {/* Settings modal (模型与密钥) */}
      {showSettings && (
        <SettingsModal
          focusProvider={modelsFocus}
          data={settingsData}
          error={settingsError}
          testResult={settingsTestResult}
          notifyResult={settingsNotifyResult}
          gateStatus={gateStatus}
          onGateStatus={() => clientRef.current?.gateStatus(getActiveProjectId())}
          connected={status === 'connected'}
          onNotifyTest={() => {
            setSettingsNotifyResult(null)
            clientRef.current?.notifyTest(getActiveProjectId())
          }}
          onClose={() => setShowSettings(false)}
          onRetry={() => {
            setSettingsError(null)
            clientRef.current?.settingsGet(getActiveProjectId())
          }}
          accountsSlot={accountsSection()}
          onSetKey={(apiKeyEnv, value) => clientRef.current?.settingsSetKey(getActiveProjectId(), apiKeyEnv, value)}
          onTestModel={(model) => {
            setSettingsTestResult(null)
            clientRef.current?.settingsTestModel(getActiveProjectId(), model)
          }}
          onSetRouting={(defaultModel, routing) => clientRef.current?.settingsSetRouting(getActiveProjectId(), defaultModel, routing)}
          onSetModelTiers={(tiers, tierParams, taskTiers, residency, optout, windowCap) =>
            clientRef.current?.settingsSetModelTiers(getActiveProjectId(), tiers, tierParams, taskTiers,
                                                     residency, optout, windowCap)}
          onSetFlag={(env, enabled) => clientRef.current?.settingsSetFlag(getActiveProjectId(), env, enabled)}
        />
      )}

      {/* 搭建台: the builder surface of WorkflowsPage, untouched. 业务工单 is the inbox page below (step 3). */}
      {activePage === 'workflows' && (
        <WorkflowsPage
          surface="builder"
          employeeTrial={{ projectId: projectStore.activeProjectId,
            ack: employeeTrialAck?.project_id === projectStore.activeProjectId ? employeeTrialAck : null,
            send: (action, payload) => {
              if (payload.project_id !== getActiveProjectId()) return 'send-failed'
              return clientRef.current?.employeeWorkflowTrial(action, payload) ?? 'send-failed'
            },
            onOpenOrder: orderId => {
              setOrderQueueRequest(current => ({ queue: 'all', query: '', open: orderId, flow: '', seq: (current?.seq ?? 0) + 1 }))
              openPage('orders')
            } }}
          workflows={workflows}
          orders={orders}
          ordersTruncated={ordersTruncated}
          onLoadOlderOrders={orderPaging.nextCursor ? loadOlderOrders : undefined}
          olderOrdersLoading={orderPaging.loading}
          drawerExtra={isOwnerConsole(auth) ? order => (
            <DistributionAuthorizationCard zh={locale.startsWith('zh')} orderId={order.order_id}
              reply={distributionReply && distributionReply.order_id === order.order_id ? distributionReply : null}
              onAsk={(verb, body) => {
                const sent = clientRef.current?.distributionAuthorization(getActiveProjectId(), verb, { ...body, order_id: order.order_id })
                return sent === 'sent' || sent === 'queued'
              }} />
          ) : undefined}
          orderMetricsTruncated={orderMetricsTruncated}
          orderScope={orderScope}
          onOrderScopeChange={(scope) => { setOrderScope(scope); clientRef.current?.orderList(getActiveProjectId(), scope) }}
          error={workflowError}
          invokeState={workflowInvoke}
          publishingWorkflowId={stillOut(tracked, publishingWorkflowId, 'workflow_publish')}
          rollingBackWorkflowId={rollingBackWorkflowId}
          rolloutBusyWorkflowId={rolloutBusyWorkflowId}
          rolloutRollbackBusyWorkflowId={rolloutRollbackBusyWorkflowId}
           creatingDraftWorkflowId={creatingDraftWorkflowId}
           recordingTestWorkflowId={recordingTestWorkflowId}
           simulatingTestWorkflowId={simulatingTestWorkflowId}
           onSimulateTest={(workflowId, expectedRevision, fields) => {
             if (!requireObservedWorkflowRevision(expectedRevision)) return
             setSimulatingTestWorkflowId(workflowId)
             clientRef.current?.workflowSimulateTest(getActiveProjectId(), workflowId, expectedRevision, fields)
           }}
           submittingTestingWorkflowId={submittingTestingWorkflowId}
          requestingReleaseWorkflowId={requestingReleaseWorkflowId}
          admissionRefusal={admissionRefusal}
          decidingReleaseWorkflowId={decidingReleaseWorkflowId}
          updatingWorkflowId={stillOut(tracked, updatingWorkflowId, 'workflow_update_nodes')}
          preflight={workflowPreflight}
          preflightingWorkflowId={preflightingWorkflowId}
          nodeTrials={workflowNodeTrials}
          nodeTrialBusy={workflowNodeTrialBusy && anyBusy(tracked, ['workflow_node_trial_start', 'workflow_node_trial_answer', 'workflow_node_trial_abandon'])}
          flowTrials={workflowFlowTrials}
          publishedFlowTrials={publishedFlowTrials.workflowId === builderFlow?.id ? publishedFlowTrials.rows : []}
          publishedFlowTrialError={publishedFlowTrials.workflowId === builderFlow?.id ? publishedFlowTrials.error : '正在读取已发布版本…'}
          flowTrialBusy={workflowFlowTrialBusy && (
            anyBusy(tracked, 'workflow_flow_trial_start') || anyBusy(tracked, 'workflow_flow_trial_continue')
            || anyBusy(tracked, 'workflow_flow_trial_stop')
          )}
          cancellingOrderId={busyIdOf(tracked, 'order_cancel')}
          budgetBusyOrderId={busyIdOf(tracked, ['run_budget_create', 'run_budget_raise', 'run_budget_close'])}
          onRefresh={() => { clientRef.current?.workflowList(getActiveProjectId()); clientRef.current?.workflowNodeTrialList(getActiveProjectId()); clientRef.current?.workflowFlowTrialList(getActiveProjectId(), builderFlowRef.current?.id ?? ''); clientRef.current?.solutionBlueprintList(getActiveProjectId()); clientRef.current?.runBudgetList(getActiveProjectId()); clientRef.current?.orderMetrics(getActiveProjectId()); refreshOrderList() }}
           onReviewOrder={(orderId, decision, note) => clientRef.current?.orderReview(getActiveProjectId(), orderId, decision, note)}
          onCancelOrder={isOwnerConsole(auth) ? (orderId, reason, cascadeChildren) => {
            // On the request tracker (其它写 15 s): 取消整单 waits for its receipt, a send that never left is said at once.
            const entry = requestTracker.track(keyOf('order_cancel', orderId), () => {
              const sent = clientRef.current?.orderCancel(getActiveProjectId(), orderId, reason, cascadeChildren) === 'sent'
              return sent ? 'sent' : 'send-failed'
            }, { kind: 'write' })
            return entry.state === 'busy'
          } : undefined}
           updatingHandlingOrderId={stillOut(tracked, updatingHandlingOrderId, 'order_handling_update')}
           onUpdateHandling={isOwnerConsole(auth) ? (orderId, patch) => {
             setUpdatingHandlingOrderId(orderId)
             requestTracker.track(keyOf('order_handling_update', orderId),
               () => clientRef.current?.orderHandlingUpdate(getActiveProjectId(), orderId, patch) ?? 'send-failed', { kind: 'write' })
           } : undefined}
           updatingFdeControlPlaneOrderId={updatingFdeControlPlaneOrderId}
           onUpdateFdeControlPlane={(orderId, patch) => {
             setUpdatingFdeControlPlaneOrderId(orderId)
             clientRef.current?.fdeControlPlaneUpdate(getActiveProjectId(), orderId, patch)
           }}
          onPublish={(workflowId, expectedRevision) => {
            if (!requireObservedWorkflowRevision(expectedRevision)) return
            setPublishingWorkflowId(workflowId)
            // 发布 on the request tracker (发布与试跑 30 s): the button says 「发出去了，等回执」 until the receipt, never for ever.
            requestTracker.track(keyOf('workflow_publish', workflowId),
              () => clientRef.current?.workflowPublish(getActiveProjectId(), workflowId, expectedRevision) ?? 'send-failed', { kind: 'publish' })
          }}
          onRollback={(workflowId, expectedRevision) => {
            if (!requireObservedWorkflowRevision(expectedRevision)) return
            if (!window.confirm(t('workflows.rollbackConfirm'))) return
            setRollingBackWorkflowId(workflowId)
            clientRef.current?.workflowRollback(getActiveProjectId(), workflowId, expectedRevision)
          }}
          onRolloutStart={(workflowId, expectedRevision) => {
            if (!requireObservedWorkflowRevision(expectedRevision)) return
            setRolloutBusyWorkflowId(workflowId)
            clientRef.current?.workflowRolloutStart(getActiveProjectId(), workflowId, expectedRevision)
          }}
          onRolloutObserve={(workflowId, expectedRevision, outcome) => {
            if (!requireObservedWorkflowRevision(expectedRevision)) return
            setRolloutBusyWorkflowId(workflowId)
            clientRef.current?.workflowRolloutObserve(getActiveProjectId(), workflowId, expectedRevision, outcome)
          }}
          onRolloutEvaluate={(workflowId) => clientRef.current?.workflowRolloutEvaluate(getActiveProjectId(), workflowId)}
          onRolloutFinalize={(workflowId, expectedRevision) => {
            if (!requireObservedWorkflowRevision(expectedRevision)) return
            setRolloutBusyWorkflowId(workflowId)
            clientRef.current?.workflowRolloutFinalize(getActiveProjectId(), workflowId, expectedRevision)
          }}
          onRolloutRollback={(workflowId, expectedRevision) => {
            if (!requireObservedWorkflowRevision(expectedRevision)) return
            if (!window.confirm('确认按灰度失败门槛回滚？')) return
            setRolloutRollbackBusyWorkflowId(workflowId)
            clientRef.current?.workflowRolloutRollback(getActiveProjectId(), workflowId, expectedRevision)
          }}
          onCreateDraft={(workflowId, draftId, name, summary, changeNote) => {
            setCreatingDraftWorkflowId(workflowId)
            clientRef.current?.workflowCreateDraft(getActiveProjectId(), workflowId, draftId, name, summary, changeNote)
          }}
          onRecordTest={(workflowId, expectedRevision, outcome, notes, failureReason) => {
            if (!requireObservedWorkflowRevision(expectedRevision)) return
            setRecordingTestWorkflowId(workflowId)
            clientRef.current?.workflowRecordTest(getActiveProjectId(), workflowId, expectedRevision, {
              outcome,
              notes,
              failure_reason: failureReason,
            })
          }}
          onSubmitTesting={(workflowId, expectedRevision, changeNote) => {
            if (!requireObservedWorkflowRevision(expectedRevision)) return
            setAdmissionRefusal(null)
            setSubmittingTestingWorkflowId(workflowId)
            clientRef.current?.workflowSubmitTesting(getActiveProjectId(), workflowId, expectedRevision, changeNote)
          }}
          onRequestRelease={(workflowId, expectedRevision) => {
            if (!requireObservedWorkflowRevision(expectedRevision)) return
            setAdmissionRefusal(null)
            setRequestingReleaseWorkflowId(workflowId)
            clientRef.current?.workflowRequestRelease(getActiveProjectId(), workflowId, expectedRevision)
          }}
          onDecideRelease={(workflowId, expectedRevision, decision) => {
            if (!requireObservedWorkflowRevision(expectedRevision)) return
            if (decision === 'rejected' && !window.confirm(t('workflows.rejectReleaseConfirm'))) return
            setDecidingReleaseWorkflowId(workflowId)
            clientRef.current?.workflowDecideRelease(getActiveProjectId(), workflowId, expectedRevision, decision)
          }}
          onReviewSkill={isOwnerConsole(auth) ? (workflowId, expectedRevision, skillId, note) => {
            if (!requireObservedWorkflowRevision(expectedRevision)) return
            clientRef.current?.workflowSkillReview(getActiveProjectId(), workflowId, expectedRevision, skillId, note)
          } : undefined}
          nodesSaved={nodesSaved}
          nodeDraftState={nodeDraftState}
          onBuilderFlow={reportBuilderFlow}
          orderQueueRequest={orderQueueRequest}
          onOpenLibrary={() => openPage('components')}
          employeeDesign={isOwnerConsole(auth) ? { controller: employeeAssets,
            project_id: employeeAssetProjectId(projectsHydratedRef.current, currentProject.pendingProjectId,
              projectStore.activeProjectId, projectStore.projects),
            workflows_read_project_id: workflowsReadProjectId, onOpenLibrary: () => openPage('components'),
          } : undefined}
          blockers={blockers}
          onShowBuilder={() => openPage('workflows')}
          onOpenAssistant={() => { setExecutionPanelOpen(false); setAssistantOpen(true) }}
          onViewComponent={isOwnerConsole(auth) ? (componentId) => {
            setComponentDetail({ id: componentId, detail: null, loading: true, error: null })
            clientRef.current?.componentDetail(getActiveProjectId(), componentId)
          } : undefined}
          // 4.7-A16: 编辑 on a step's skill -- 组件库's own 编辑 (the workbench on its 契约 page), with the step remembered.
          onEditComponent={isOwnerConsole(auth) ? (componentId, from) => {
            setStepReturn(from)
            setComponentWork({ id: componentId, tab: 'contract' })
            setComponentDetail({ id: componentId, detail: null, loading: true, error: null })
            setComponentVersionsState({ id: componentId, versions: null, loading: true, error: null, busy: '' })
            clientRef.current?.componentDetail(getActiveProjectId(), componentId)
            clientRef.current?.componentVersions(getActiveProjectId(), componentId)
            openPage('components')
          } : undefined}
          stepReturnRequest={stepReturnRequest}
          onStepReturnHandled={(seq) => setStepReturnRequest(current => (current?.seq === seq ? null : current))}
          improvementSlot={isOwnerConsole(auth) ? (wf) => improvementStarter(`flow:${wf.id}`, flowImprovementTargets(wf, connectorRegistrations, componentCatalog?.internal?.skills ?? [])) : undefined}
          openFlowRequest={openFlowRequest}
          onOpenFlowHandled={(seq) => setOpenFlowRequest(current => (current?.seq === seq ? null : current))}
          defaultModel={settingsData?.default_model ?? ''}
          modelProviders={settingsData?.providers}
          workflowVersions={flowVersionRecords}
          onLoadVersions={isOwnerConsole(auth) ? (workflowId) => clientRef.current?.workflowVersions(getActiveProjectId(), workflowId) : undefined}
          projectNameOf={(id) => projectStore.projects.find(project => project.id === id)?.name || id}
          editLock={editLock}
          compositionRequest={compositionRequest}
          onCompositionHandled={(seq) => setCompositionRequest(current => current?.seq === seq ? null : current)}
          broughtComponent={broughtComponent}
          // Every trial not admitted -- a discarded one too: a step naming it would save a skill nothing installs.
          trialSkills={trialsNotAdmitted(skillTrials).map(trial => trial.slug)}
          proposalCandidates={proposalCandidates}
          onProposalApplied={(workflowId, candidates) => setProposalCandidates(prev => ({ ...prev, [workflowId]: candidates }))}
          candidateActions={isOwnerConsole(auth) ? (workflowId, candidate) => {
            const fromCatalog = candidate.source === 'catalog'
            const trial = trialForListing(skillTrials, candidate.id, '', fromCatalog ? 'catalog' : 'skillhub')
            if (trial?.status === 'admitted') return <small className="cl-line">{t('market.trialStatus.admitted')}</small>
            const library = componentCatalog?.library
            // The page's library status when 组件库 has loaded (it is current); otherwise what the proposal saw.
            if (fromCatalog && !trial && (library ? !library.ready : candidate.can_try === false)) {
              // No library named on this machine (or not the scanned one): an import would only be refused.
              const reason = library && !library.ready ? library.reason : 'library_not_configured'
              return <small className="cl-line" data-testid="candidate-no-library">{t(`components.library.${reason}` as Parameters<typeof t>[0], { library: library?.library ?? '' })}</small>
            }
            const busyKey = fromCatalog ? `catalog:${candidate.id}` : candidate.id
            const downloading = marketBusy[busyKey] === 'downloading'
            return (
              <button type="button" className="capability-refresh-btn" data-testid="candidate-use" disabled={status !== 'connected' || downloading}
                onClick={() => {
                  if (trial) { openBuilderWith({ kind: 'skill', id: trial.slug, name: trial.name, workflowId, trial: { slug: trial.slug, version: trial.version, source: trial.source } }); return }
                  candidateUseAfterDownloadRef.current[busyKey] = { workflowId, name: candidate.name }
                  setMarketBusy(prev => ({ ...prev, [busyKey]: 'downloading' }))
                  if (fromCatalog) clientRef.current?.skillTrialImportCatalog(getActiveProjectId(), candidate.id)
                  else clientRef.current?.skillTrialDownload(getActiveProjectId(), candidate.id)
                }}>
                {downloading ? t(fromCatalog ? 'market.importing' : 'market.downloading') : t('market.use')}
              </button>
            )
          } : undefined}
          broughtExtra={broughtComponent?.trial ? (
            // 调用测试: the skill's test is recorded right here on the shelf; admission opens its trial card.
            <TrialShelf
              trial={skillTrials.find(item => item.slug === broughtComponent.trial?.slug && item.version === broughtComponent.trial?.version && (item.source || 'skillhub') === (broughtComponent.trial?.source || 'skillhub')) ?? null}
              busy={broughtComponent.trial ? marketBusy[trialKey(broughtComponent.trial)] : undefined}
              connected={status === 'connected'}
              onMark={trialHandlers.onMark}
              onDiscard={trialHandlers.onDiscard}
              onRestore={trialHandlers.onRestore}
              onOpenAdmission={openTrialCard}
            />
          ) : undefined}
          onDismissBrought={() => setBroughtComponent(null)}
          internalSkills={componentCatalog?.internal?.skills}
          nodeProposal={isOwnerConsole(auth) ? pendingNodeProposal : null}
          onNodeProposalDone={() => setPendingNodeProposal(null)}
          onBuilderFocus={setBuilderFocus}
          onCreateBlank={isOwnerConsole(auth) ? (draftId, team, name) => {
            setCreatingBlank(true)
            clientRef.current?.workflowCreateBlank(getActiveProjectId(), draftId, team, name)
          } : undefined}
          creatingBlank={creatingBlank}
          teams={(savedOrgsList ?? []).map(org => ({ name: org.name, label: org.organization_name || org.name }))}
          onUpdateNodes={(workflowId, nodes, expectedVersion, newVersion, changeNote, expectedRevision) => {
            setUpdatingWorkflowId(workflowId)
            // 保存步骤 on the request tracker (保存草稿 10 s); the builder's button keeps its own label and place.
            requestTracker.track(keyOf('workflow_update_nodes', workflowId),
              () => clientRef.current?.workflowUpdateNodes(getActiveProjectId(), workflowId, nodes as unknown as Record<string, unknown>[], expectedVersion, newVersion, changeNote, expectedRevision ?? '') ?? 'send-failed',
              { kind: 'draft' })
          }}
          employees={orgInfoData?.employees ?? []}
          employeesOrgId={orgInfoData?.organization_id ?? ''}
          contacts={staffContacts}
          onOpenPeople={isOwnerConsole(auth) ? () => {
            // 去绑定: the open project's 人员与职责 tab, where a member's Feishu or email is bound.
            setPeopleTabRequest(current => ({ tab: 'collab', collab: 'people', seq: (current?.seq ?? 0) + 1 }))
            openPage('dashboard')
          } : undefined}
          onPreflight={(workflowId, fields, nodeId) => {
            setPreflightingWorkflowId(workflowId)
            setWorkflowPreflight(null)
            clientRef.current?.workflowPreflight(getActiveProjectId(), workflowId, fields, nodeId)
          }}
          refusedSave={refusedSave}
          nodeTrialExtras={isOwnerConsole(auth) ? {
            pinning: nodeTrialPinning,
            onPlan: (workflowId, nodeId) => clientRef.current?.workflowNodeTrialList(getActiveProjectId(), workflowId, nodeId),
            artifact: trialArtifact,
            onOpenArtifact: (specId, path) => {
              trialArtifactWantedRef.current = { specId, path }
              setTrialArtifact({ preview: null, loading: true, error: null })
              clientRef.current?.workflowNodeTrialArtifact(getActiveProjectId(), specId, path)
            },
            onCloseArtifact: () => {
              trialArtifactWantedRef.current = null
              setTrialArtifact({ preview: null, loading: false, error: null })
            },
            // The trial's answer and abandon go through the request tracker (其它写 15 s): a send that never left is said
            // by it (没发出去（没连上）) and the busy flag is taken back at once.
            onAnswer: (workflowId, specId, reply) => {
              setWorkflowNodeTrialBusy(true)
              const entry = requestTracker.track(keyOf('workflow_node_trial_answer', specId),
                () => clientRef.current?.workflowNodeTrialAnswer(getActiveProjectId(), workflowId, specId, reply) ?? 'send-failed', { kind: 'write' })
              if (entry.state !== 'busy') setWorkflowNodeTrialBusy(false)
            },
            onAbandon: specId => {
              setWorkflowNodeTrialBusy(true)
              const entry = requestTracker.track(keyOf('workflow_node_trial_abandon', specId),
                () => clientRef.current?.workflowNodeTrialAbandon(getActiveProjectId(), specId) ?? 'send-failed', { kind: 'write' })
              if (entry.state !== 'busy') setWorkflowNodeTrialBusy(false)
            },
          } : undefined}
          onNodeTrial={isOwnerConsole(auth) ? (workflowId, workflowVersion, nodeId, pins, imageLimit) => {
            setWorkflowNodeTrialBusy(true)
            // 真实试跑 on the request tracker (发布与试跑 30 s): the receipt names the step (node_id) it accepted.
            const entry = requestTracker.track(keyOf('workflow_node_trial_start', nodeId), () => clientRef.current?.workflowNodeTrialStart(
              getActiveProjectId(), workflowId, workflowVersion, nodeId, newRequestId(), 12, pins, imageLimit,
            ) ?? 'send-failed', { kind: 'publish' })
            if (entry.state !== 'busy') setWorkflowNodeTrialBusy(false)
          } : undefined}
          onFlowTrial={isOwnerConsole(auth) ? (workflowId, _workflowVersion, action, limits, target) => {
            if (status !== 'connected' || currentProject.pendingProjectId || workflowId !== builderFlowRef.current?.id) return
            const active = matchingFlowTrial(workflowFlowTrials, workflowId, target)
            if ((action === 'continue' || action === 'stop') && !active) {
              showToast('未找到匹配的原试跑记录，请刷新后再操作。', 'error')
              clientRef.current?.workflowFlowTrialList(getActiveProjectId(), workflowId)
              return
            }
            if (action === 'continue' && active) {
              setWorkflowFlowTrialBusy(true)
              const entry = requestTracker.track(keyOf('workflow_flow_trial_continue', active.flow_id), () => clientRef.current?.workflowFlowTrialContinue(
                getActiveProjectId(), workflowId, active.flow_id,
              ) ?? 'send-failed', { kind: 'publish' })
              if (entry.state !== 'busy') setWorkflowFlowTrialBusy(false)
              return
            }
            if (action === 'stop' && active) {
              setWorkflowFlowTrialBusy(true)
              const entry = requestTracker.track(keyOf('workflow_flow_trial_stop', active.flow_id), () => clientRef.current?.workflowFlowTrialStop(
                getActiveProjectId(), workflowId, active.flow_id,
              ) ?? 'send-failed', { kind: 'write' })
              if (entry.state !== 'busy') setWorkflowFlowTrialBusy(false)
              return
            }
            const key = flowTrialTargetKey(workflowId, target)
            const selected = publishedFlowTrials.workflowId === workflowId && !publishedFlowTrials.error ? publishedFlowTrials.rows : []
            if (!key || !selectableFlowTrialTarget(selected, workflowId, target)
              || (target.source === 'saved_draft' && target.workflowVersion !== builderFlowRef.current?.version)) {
              showToast('试跑版本已变化，请重新选择要测试的版本。', 'error')
              return
            }
            const requestId = action === 'new_round'
              ? newRequestId()
              : (flowTrialRequestIds.current[key] || newRequestId())
            flowTrialRequestIds.current[key] = requestId
            setWorkflowFlowTrialBusy(true)
            const entry = requestTracker.track(keyOf('workflow_flow_trial_start', workflowId), () => clientRef.current?.workflowFlowTrialStart(
              getActiveProjectId(), workflowId, target.workflowVersion, requestId, limits.perNode, limits.total, action === 'new_round',
              target.source === 'published_employee' ? { packageId: target.packageId!, fingerprint: target.fingerprint! } : undefined,
            ) ?? 'send-failed', { kind: 'publish' })
            if (entry.state !== 'busy') setWorkflowFlowTrialBusy(false)
          } : undefined}
          onOpenTask={(taskId) => openExecution(taskId)}
          onAcceptanceGet={(orderId) => clientRef.current?.acceptanceGet(getActiveProjectId(), orderId)}
          onOrderTimeline={(orderId) => {
            setOrderTimeline(null); setOrderTimelineError(null); setOrderModelPlan(null)
            clientRef.current?.orderTimeline(getActiveProjectId(), orderId)
            clientRef.current?.orderModelPlan(getActiveProjectId(), orderId)
          }}
          onCustomerPreview={isOwnerConsole(auth) ? requestCustomerPreview : undefined}
          customerPreviewSlot={isOwnerConsole(auth) && customerPreview && <CustomerPreviewPanel
            view={customerPreview.view} loading={customerPreview.loading} error={customerPreview.error}
            onClose={clearCustomerPreview} onRetry={() => requestCustomerPreview(customerPreview.request.orderId)}
          />}
          budgets={runBudgets ?? undefined}
          orderMetrics={orderMetrics}
          orderUsage={orderUsage}
          onResolveBudget={isOwnerConsole(auth) ? (reservationId, happened) => clientRef.current?.runBudgetResolve(getActiveProjectId(), reservationId, happened) : undefined}
          pendingReservations={pendingReservations}
          releaseEvidence={releaseEvidence}
          onReleaseEvidence={(workflowId) => { setReleaseEvidence(null); clientRef.current?.workflowReleaseEvidence(getActiveProjectId(), workflowId) }}
          blueprints={blueprints}
          onSaveBlueprint={(orderId, name) => clientRef.current?.solutionBlueprintSave(getActiveProjectId(), orderId, name)}
          onApplyBlueprint={(blueprintId, orderId) => clientRef.current?.solutionBlueprintApply(getActiveProjectId(), blueprintId, orderId)}
          orderTimeline={orderTimeline}
          orderModelPlan={orderModelPlan}
          orderTimelineError={orderTimelineError}
          acceptanceBusyOrderId={stillOut(tracked, acceptanceBusyOrderId, ['acceptance_evidence', 'acceptance_submit', 'acceptance_decide', 'acceptance_rework'])}
          // The acceptance writes on the request tracker (其它写 15 s): the panel is busy only while a receipt is out.
          onAcceptanceEvidence={(orderId, criterion, evidence) => {
            setAcceptanceBusyOrderId(orderId)
            requestTracker.track(keyOf('acceptance_evidence', orderId),
              () => clientRef.current?.acceptanceEvidence(getActiveProjectId(), orderId, criterion, evidence) ?? 'send-failed', { kind: 'write' })
          }}
          onAcceptanceSubmit={(orderId) => {
            setAcceptanceBusyOrderId(orderId)
            requestTracker.track(keyOf('acceptance_submit', orderId),
              () => clientRef.current?.acceptanceSubmit(getActiveProjectId(), orderId) ?? 'send-failed', { kind: 'write' })
          }}
          onAcceptanceDecide={(orderId, decision, note, exceptions, options) => {
            setAcceptanceBusyOrderId(orderId)
            requestTracker.track(keyOf('acceptance_decide', orderId),
              () => clientRef.current?.acceptanceDecide(getActiveProjectId(), orderId, decision, note, exceptions, options) ?? 'send-failed', { kind: 'write' })
          }}
          onAcceptanceRework={(orderId, targetNode, requestId) => {
            setAcceptanceBusyOrderId(orderId)
            requestTracker.track(keyOf('acceptance_rework', orderId), () => clientRef.current?.acceptanceRework(
              getActiveProjectId(),
              orderId,
              targetNode,
              requestId || newRequestId(),
            ) ?? 'send-failed', { kind: 'write' })
          }}
          onAcceptanceRoundExport={(orderId, roundNo) => {
            clientRef.current?.acceptanceRoundExport(getActiveProjectId(), orderId, roundNo)
          }}
          onPortfolioExport={(orderId) => {
            clientRef.current?.portfolioExport(getActiveProjectId(), orderId)
          }}
          onAuthorizeRedistribution={isOwnerConsole(auth) ? (orderId) => {
            if (!window.confirm(t('orders.acceptance.authorizeRedistributionConfirm'))) return
            if (clientRef.current?.workflowRedistributionAuthorize(getActiveProjectId(), orderId, '') !== 'sent') showToast(t('app.notSent'), 'error')
          } : undefined}
          onOpenArtifact={(path) => {
            setArtifactPreviewLoading(true)
            setArtifactPreviewError(null)
            setArtifactPreview(null)
            clientRef.current?.artifactRead(getActiveProjectId(), path)
          }}
          artifactsSlot={artifactsPanel}
          onInvoke={(workflowId, fields, budgetLimits) => {
            setWorkflowInvoke({ workflowId, status: 'sending' })
            clientRef.current?.workflowInvoke(getActiveProjectId(), workflowId, fields, budgetLimits)
          }}
          // The allowance writes on the request tracker (其它写 15 s): a grant, raise or close that never left is said
          // at once; while one is out the order's budget buttons wait (budgetBusyOrderId), never for ever.
          onCreateBudget={isOwnerConsole(auth) ? (orderId, limits) => requestTracker.track(keyOf('run_budget_create', orderId),
            () => clientRef.current?.runBudgetCreate(getActiveProjectId(), orderId, limits) ?? 'send-failed', { kind: 'write' }).state === 'busy' : undefined}
          onRaiseBudget={isOwnerConsole(auth) ? (orderId, limits) => requestTracker.track(keyOf('run_budget_raise', orderId),
            () => clientRef.current?.runBudgetRaise(getActiveProjectId(), orderId, limits) ?? 'send-failed', { kind: 'write' }).state === 'busy' : undefined}
          onCloseBudget={isOwnerConsole(auth) ? (orderId, reason) => requestTracker.track(keyOf('run_budget_close', orderId),
            () => clientRef.current?.runBudgetClose(getActiveProjectId(), orderId, reason) ?? 'send-failed', { kind: 'write' }).state === 'busy' : undefined}
        />
      )}

      {/* 业务工单 = 收件箱 (frontend redesign step 3, first page): the server's attention verdict groups the rows; every
          write goes out on the request tracker or the answer path; every reply came through the current-project gate. */}
      {activePage === 'orders' && (
        <OrdersInboxPage
          workflows={workflows}
          orders={orders}
          reworkInspectionSlot={auth?.account?.role === 'owner' ? order => (
            <AIEmployeeReworkInspectionHost binding={reworkInspectionBinding({
              hydrated: projectsHydratedRef.current, pendingProjectId: currentProject.pendingProjectId,
              activeProjectId: projectStore.activeProjectId, acceptedOrderProjectId: ordersReadProjectId,
              generation: currentProject.generation, account: auth?.account, order,
            })} connected={status === 'connected'} transport={clientRef.current} />
          ) : undefined}
          ordersTruncated={ordersTruncated}
          onLoadOlderOrders={orderPaging.nextCursor ? loadOlderOrders : undefined}
          olderOrdersLoading={orderPaging.loading}
          error={workflowError}
          connected={status === 'connected'}
          onRefresh={() => { clientRef.current?.workflowList(getActiveProjectId()); clientRef.current?.solutionBlueprintList(getActiveProjectId()); clientRef.current?.runBudgetList(getActiveProjectId()); clientRef.current?.orderMetrics(getActiveProjectId()); refreshOrderList(); askBlockers(getActiveProjectId()) }}
          projects={projectStore.projects}
          activeProjectId={projectStore.activeProjectId}
          onSwitchProject={(id) => {
            // The same two steps the project selector takes (lib/currentProject: one reset, one gate).
            const switchSeq = beginProjectSwitch(id)
            clientRef.current?.switchProject(id, switchSeq)
          }}
          onOpenProject={() => openPage('dashboard')}
          orderScope={orderScope}
          onOrderScopeChange={(scope) => { setOrderScope(scope); clientRef.current?.orderList(getActiveProjectId(), scope) }}
          orderQueueRequest={orderQueueRequest}
          invokeState={workflowInvoke}
          onInvoke={(workflowId, fields, budgetLimits) => {
            setWorkflowInvoke({ workflowId, status: 'sending' })
            clientRef.current?.workflowInvoke(getActiveProjectId(), workflowId, fields, budgetLimits)
          }}
          employees={orgInfoData?.employees ?? []}
          blockers={blockers}
          budgets={runBudgets}
          pendingReservations={pendingReservations}
          onResolveBudget={isOwnerConsole(auth) ? (reservationId, happened) => clientRef.current?.runBudgetResolve(getActiveProjectId(), reservationId, happened) : undefined}
          onCreateBudget={isOwnerConsole(auth) ? (orderId, limits) => requestTracker.track(keyOf('run_budget_create', orderId),
            () => clientRef.current?.runBudgetCreate(getActiveProjectId(), orderId, limits) ?? 'send-failed', { kind: 'write' }).state === 'busy' : undefined}
          onRaiseBudget={isOwnerConsole(auth) ? (orderId, limits) => requestTracker.track(keyOf('run_budget_raise', orderId),
            () => clientRef.current?.runBudgetRaise(getActiveProjectId(), orderId, limits) ?? 'send-failed', { kind: 'write' }).state === 'busy' : undefined}
          onCloseBudget={isOwnerConsole(auth) ? (orderId, reason) => requestTracker.track(keyOf('run_budget_close', orderId),
            () => clientRef.current?.runBudgetClose(getActiveProjectId(), orderId, reason) ?? 'send-failed', { kind: 'write' }).state === 'busy' : undefined}
          budgetBusyOrderId={busyIdOf(tracked, ['run_budget_create', 'run_budget_raise', 'run_budget_close'])}
          orderMetrics={orderMetrics}
          orderUsage={orderUsage}
          orderMetricsTruncated={orderMetricsTruncated}
          tracked={tracked}
          // 我来接 / 打回 on the request tracker (其它写 15 s): the button says 「发出去了，等回执」 until the receipt, never for ever.
          onReviewOrder={(orderId, decision, note) => {
            requestTracker.track(keyOf('order_review', orderId), () => {
              if (status !== 'connected' || !clientRef.current) return 'send-failed'
              clientRef.current.orderReview(getActiveProjectId(), orderId, decision, note)
              return 'sent'
            }, { kind: 'write' })
          }}
          // 看进度 / 打开执行区 (S3-7): the run's record in 执行区's side window, over the inbox.
          onOpenTask={(taskId) => openExecution(taskId)}
          onAcceptanceGet={(orderId) => clientRef.current?.acceptanceGet(getActiveProjectId(), orderId)}
          acceptanceBusyOrderId={stillOut(tracked, acceptanceBusyOrderId, ['acceptance_evidence', 'acceptance_submit', 'acceptance_decide', 'acceptance_rework'])}
          onAcceptanceEvidence={(orderId, criterion, evidence) => {
            setAcceptanceBusyOrderId(orderId)
            requestTracker.track(keyOf('acceptance_evidence', orderId),
              () => clientRef.current?.acceptanceEvidence(getActiveProjectId(), orderId, criterion, evidence) ?? 'send-failed', { kind: 'write' })
          }}
          onAcceptanceSubmit={(orderId) => {
            setAcceptanceBusyOrderId(orderId)
            requestTracker.track(keyOf('acceptance_submit', orderId),
              () => clientRef.current?.acceptanceSubmit(getActiveProjectId(), orderId) ?? 'send-failed', { kind: 'write' })
          }}
          onAcceptanceDecide={(orderId, decision, note, exceptions, options) => {
            setAcceptanceBusyOrderId(orderId)
            requestTracker.track(keyOf('acceptance_decide', orderId),
              () => clientRef.current?.acceptanceDecide(getActiveProjectId(), orderId, decision, note, exceptions, options) ?? 'send-failed', { kind: 'write' })
          }}
          onAcceptanceRework={(orderId, targetNode, requestId) => {
            setAcceptanceBusyOrderId(orderId)
            requestTracker.track(keyOf('acceptance_rework', orderId), () => clientRef.current?.acceptanceRework(
              getActiveProjectId(), orderId, targetNode, requestId || newRequestId(),
            ) ?? 'send-failed', { kind: 'write' })
          }}
          onAcceptanceRoundExport={(orderId, roundNo) => { clientRef.current?.acceptanceRoundExport(getActiveProjectId(), orderId, roundNo) }}
          onPortfolioExport={(orderId) => { clientRef.current?.portfolioExport(getActiveProjectId(), orderId) }}
          onAuthorizeRedistribution={isOwnerConsole(auth) ? (orderId) => {
            if (!window.confirm(t('orders.acceptance.authorizeRedistributionConfirm'))) return
            if (clientRef.current?.workflowRedistributionAuthorize(getActiveProjectId(), orderId, '') !== 'sent') showToast(t('app.notSent'), 'error')
          } : undefined}
          onOpenArtifact={(path) => {
            setArtifactPreviewLoading(true)
            setArtifactPreviewError(null)
            setArtifactPreview(null)
            clientRef.current?.artifactRead(getActiveProjectId(), path)
          }}
          onDecideBlocker={(blocker, optionId) => decideBlocker(blocker, optionId, 'orders-inbox')}
          // A19 暂缓提醒一天 / 恢复提醒: on 总控's card until the owner cut 总控 to the conversation; the order
          // is handled here now. The ack (blocker_snooze / blocker_unsnooze above) says it in words and re-reads the pauses.
          onHoldBlocker={(blocker, untilAt) => clientRef.current?.blockerSnooze(getActiveProjectId(), blocker.checkpoint_id, untilAt)}
          onReleaseBlocker={(blocker) => clientRef.current?.blockerUnsnooze(getActiveProjectId(), blocker.checkpoint_id)}
          answering={answering}
          decisionErrors={blockerDecisionErrors}
          gateDetail={gateDetail}
          gateBusy={gateWaiting}
          gateError={gateError}
          onOpenGate={(blocker) => {
            setGateDetail(null)
            clientRef.current?.gateDetail(getActiveProjectId(), blocker.checkpoint_id)
          }}
          onDecideGate={decideGate}
          onCloseGate={() => setGateDetail(null)}
          artifactIndex={artifactIndex}
          artifactsLoading={artifactLoading}
          onRefreshArtifacts={() => {
            setArtifactLoading(true)
            setArtifactError(null)
            clientRef.current?.artifactsList(getActiveProjectId())
          }}
          orderTimeline={orderTimeline}
          orderTimelineError={orderTimelineError}
          onOrderTimeline={(orderId) => {
            setOrderTimeline(null); setOrderTimelineError(null); setOrderModelPlan(null)
            clientRef.current?.orderTimeline(getActiveProjectId(), orderId)
            clientRef.current?.orderModelPlan(getActiveProjectId(), orderId)
          }}
          orderModelPlan={orderModelPlan}
          onCancelOrder={isOwnerConsole(auth) ? (orderId, reason, cascadeChildren) => {
            const entry = requestTracker.track(keyOf('order_cancel', orderId), () => {
              const sent = clientRef.current?.orderCancel(getActiveProjectId(), orderId, reason, cascadeChildren) === 'sent'
              return sent ? 'sent' : 'send-failed'
            }, { kind: 'write' })
            return entry.state === 'busy'
          } : undefined}
          onUpdateHandling={isOwnerConsole(auth) ? (orderId, patch) => {
            setUpdatingHandlingOrderId(orderId)
            requestTracker.track(keyOf('order_handling_update', orderId),
              () => clientRef.current?.orderHandlingUpdate(getActiveProjectId(), orderId, patch) ?? 'send-failed', { kind: 'write' })
          } : undefined}
          updatingHandlingOrderId={stillOut(tracked, updatingHandlingOrderId, 'order_handling_update')}
          onOpenFlow={(workflowId) => { nodeDraftState.setBuilderWorkflowId(workflowId); openPage('workflows') }}
          onOpenAssistant={() => { setExecutionPanelOpen(false); setAssistantOpen(true) }}
          onCustomerPreview={isOwnerConsole(auth) ? requestCustomerPreview : undefined}
          customerPreviewSlot={isOwnerConsole(auth) && customerPreview && <CustomerPreviewPanel
            view={customerPreview.view} loading={customerPreview.loading} error={customerPreview.error}
            onClose={clearCustomerPreview} onRetry={() => requestCustomerPreview(customerPreview.request.orderId)}
          />}
          blueprints={blueprints}
          onSaveBlueprint={(orderId, name) => clientRef.current?.solutionBlueprintSave(getActiveProjectId(), orderId, name)}
          onApplyBlueprint={(blueprintId, orderId) => clientRef.current?.solutionBlueprintApply(getActiveProjectId(), blueprintId, orderId)}
          onUpdateFdeControlPlane={(orderId, patch) => {
            setUpdatingFdeControlPlaneOrderId(orderId)
            clientRef.current?.fdeControlPlaneUpdate(getActiveProjectId(), orderId, patch)
          }}
          updatingFdeControlPlaneOrderId={updatingFdeControlPlaneOrderId}
          distributionSlot={isOwnerConsole(auth) ? order => (
            <DistributionAuthorizationCard zh={locale.startsWith('zh')} orderId={order.order_id}
              reply={distributionReply && distributionReply.order_id === order.order_id ? distributionReply : null}
              onAsk={(verb, body) => {
                const sent = clientRef.current?.distributionAuthorization(getActiveProjectId(), verb, { ...body, order_id: order.order_id })
                return sent === 'sent' || sent === 'queued'
              }} />
          ) : undefined}
          artifactsSlot={artifactsPanel}
        />
      )}

      {/* 总控 with its project and conversation column (policy): the column right of the rail and
          left of the conversation, collapsible; a drawer at narrow widths, opened from the title line. Nothing is
          added above the conversation. */}
      {activePage === 'master' && (
        <div className="master-layout" data-testid="master-layout"
          data-threads={masterColumn ? (threadsCollapsed ? 'collapsed' : 'open') : undefined}
          data-drawer={masterColumn && threadsDrawer ? 'open' : undefined}>
          {masterColumn}
          {masterColumn && threadsDrawer && (
            <button type="button" className="master-threads-scrim" aria-label={t('master.threads.hide')} onClick={() => setThreadsDrawer(false)} />
          )}
          {masterControl}
        </div>
      )}

      {/* Dashboard Page (数据中台) */}
      {activePage === 'dashboard' && (
        <DashboardPage
          adoptions={isOwnerConsole(auth) ? projectAdoptions : undefined}
          onAdoptWorkflow={isOwnerConsole(auth) ? (workflowId) => clientRef.current?.projectWorkflowAdopt(getActiveProjectId(), workflowId) : undefined}
          onSetAdoptionStatus={isOwnerConsole(auth) ? (workflowId, adoptionStatus) => clientRef.current?.projectWorkflowAdoptionStatus(getActiveProjectId(), workflowId, adoptionStatus) : undefined}
          onDeriveWorkflow={isOwnerConsole(auth) ? (workflowId) => {
            if (clientRef.current?.workflowDeriveForProject(getActiveProjectId(), workflowId) !== 'sent') showToast(t('app.notSent'), 'error')
          } : undefined}
          projects={projectStore.projects}
          // 打开组织 → 配置 · 组织与角色 (S3-7), where the 组织 page went; the board itself is also this level's other board.
          onOpenOrg={isOwnerConsole(auth) ? () => openConfigSection('org') : undefined}
          orgSlot={isOwnerConsole(auth) ? orgBoard : undefined}
          projectOverview={projectOverview}
          onAskProjectOverview={isOwnerConsole(auth) ? () => clientRef.current?.projectOrderOverview(getActiveProjectId()) : undefined}
          manageSlot={projectSelector('manage')}
          createSlot={projectSelector('create')}
          showAllSeq={projectsHomeSeq}
          onEnterProject={(id) => {
            // The same two steps the project selector takes.
            const switchSeq = beginProjectSwitch(id)
            clientRef.current?.switchProject(id, switchSeq)
          }}
          connected={status === 'connected'}
          sessions={sessionStore.sessions}
          recordedCost={recordedCost}
          recordedCostObservation={applyProjectCostObservation(recordedCostObservation, undefined, projectStore.activeProjectId)}
          agents={swarmAgents}
          workflows={workflows}
          workOrders={orders}
          budgets={runBudgets}
          dutyLog={dutyLog}
          dutyLogError={dutyLogError}
          onRefreshDutyLog={() => clientRef.current?.dutyLogList(getActiveProjectId())}
          projectContext={{
            project_id: projectStore.activeProjectId,
            customer_id: projectStore.projects.find(p => p.id === projectStore.activeProjectId)?.customer_id ?? '',
            lifecycle_status: projectStore.projects.find(p => p.id === projectStore.activeProjectId)?.lifecycle_status ?? '',
            orders: {
              total: orders?.length ?? 0,
              // Not ended, by the queue function 工单去向 and 业务工单 use: an order ends when it is closed or rejected.
              open: openOrderCount(orders),
            },
            artifacts: artifactIndex?.files.length ?? 0,
          }}
          onNavigate={(page, target) => {
            // A number that means exactly one thing opens that thing: landing on a list and asking the person to
            // find it again is the step this card exists to remove. Since S3-7 running work is 执行区's side window
            // (记录, or 文件 for the deliverables); (owner) a card is its order in 业务工单.
            if (page === 'workbench') {
              if (target) openSessionFromApproval(target)
              else openExecution()
            } else if (page === 'workflows') {
              if (target) nodeDraftState.setBuilderWorkflowId(target)
              setActivePage('workflows'); clientRef.current?.workflowList(getActiveProjectId()); refreshOrderList()
            }
            else if (page === 'orders') {
              const asked = ordersTarget(target, ORDER_QUEUES)
              if (asked) setOrderQueueRequest(current => ({ queue: asked.queue as OrderQueue, state: asked.state, query: asked.query, open: asked.open, flow: asked.flow, seq: (current?.seq ?? 0) + 1 }))
              openPage('orders')
            }
            else if (page === 'artifacts') {
              openExecution(target || undefined, 'files')
            }
            else {
              // 待审批 (retired): 业务工单 on 等你处理; a card named with it has its order opened there once the server
              // says which it is (checkpoint_locate, the same road as a Feishu link) -- 总控 lists no cards any more.
              openInboxGroup('mine')
              if (target) clientRef.current?.checkpointLocate(getActiveProjectId(), target)
            }
          }}
          // 对应画布节点 / 配置交接 / 编辑节点: the builder, that flow, that step -- by the builder's own step-return
          // request (the same road 组件库's 回到步骤 takes), so the builder itself is unchanged.
          onOpenNode={(workflowId, nodeId) => {
            stepReturnSeqRef.current += 1
            setStepReturnRequest({ workflowId, nodeId, seq: stepReturnSeqRef.current })
            openPage('workflows')
          }}
          employees={orgInfoData?.employees ?? []}
          profile={projectProfile}
          profileState={projectProfileState}
          salesRecordsSlot={isOwnerConsole(auth) ? <CRMProjectRecords bus={crmAcks} projectId={getActiveProjectId()} identity={auth?.account?.login ?? 'local-owner'}
            connected={status === 'connected'} allowed={isOwnerConsole(auth)} send={message => {
              if (clientRef.current?.send(message, { queue: false }) !== 'sent') throw new Error('crm_disconnected')
            }} /> : undefined}
          connections={projectConnections}
          connectionsState={projectConnectionsState}
          onAskPanorama={() => { if (!isOwnerConsole(auth)) return; const pid = getActiveProjectId(); clientRef.current?.projectProfileGet(pid); clientRef.current?.projectConnectionsGet(pid) }}
          // The two writes of this page, on the request tracker: busy / no receipt / not sent, never re-sent on their own.
          onSaveProfile={isOwnerConsole(auth) ? (profile) => {
            requestTracker.track('project_profile_set', () => clientRef.current?.projectProfileSet(getActiveProjectId(), profile) ?? 'send-failed')
          } : undefined}
          onSaveConnection={isOwnerConsole(auth) ? (connection) => {
            requestTracker.track(keyOf('project_connections_set', connection.id), () => clientRef.current?.projectConnectionsSet(getActiveProjectId(), connection) ?? 'send-failed')
          } : undefined}
          tracked={tracked}
          onCustomerPreview={isOwnerConsole(auth) ? requestCustomerPreview : undefined}
          customerPreviewSlot={isOwnerConsole(auth) && customerPreview && <CustomerPreviewPanel
            view={customerPreview.view} loading={customerPreview.loading} error={customerPreview.error}
            onClose={clearCustomerPreview} onRetry={() => requestCustomerPreview(customerPreview.request.orderId)}
          />}
          onComposeWorkflow={isOwnerConsole(auth) ? (callerId, targetId) => {
            compositionSeqRef.current += 1
            nodeDraftState.setBuilderWorkflowId(callerId)
            setCompositionRequest({ callerId, targetId, seq: compositionSeqRef.current })
            setActivePage('workflows')
            clientRef.current?.workflowList(getActiveProjectId())
            clientRef.current?.solutionBlueprintList(getActiveProjectId())
            clientRef.current?.runBudgetList(getActiveProjectId())
            clientRef.current?.orderMetrics(getActiveProjectId())
            refreshOrderList()
          } : undefined}
          onOpenSession={(taskId, opts) => {
            openSessionFromApproval(taskId, !!opts?.revealPendingDecision)
          }}
          artifactsSlot={artifactsPanel}
          filesSlot={filesPanel}
          memorySlot={isOwnerConsole(auth) ? customerMemoryPanel : undefined}
          onOpenFiles={isOwnerConsole(auth) ? () => { askDataIndex(); askCustomerMemory() } : askDataIndex}
          usageByProject={usageByProject}
          onAskUsage={(projectId) => clientRef.current?.dataUsage(projectId)}
          projectFolderSlot={isOwnerConsole(auth) ? <>{projectFolderPanel}{recyclePanel}</> : undefined}
          peopleSlot={isOwnerConsole(auth) ? (
            <PeopleContacts
              employees={orgInfoData?.employees ?? []}
              contacts={staffContacts}
              zh={locale.startsWith('zh')}
              connected={status === 'connected'}
              bind={contactBind}
              onBind={(person, channel, identity) => {
                contactBindForRef.current = person.employee_id
                setContactBind({ busyEmployeeId: person.employee_id, error: null })
                if (clientRef.current?.contactBind(getActiveProjectId(), bindRequest(person, channel, identity)) !== 'sent') {
                  contactBindForRef.current = ''
                  setContactBind({ busyEmployeeId: '', error: null })
                  showToast(t('app.notSent'), 'error')
                }
              }}
            />
          ) : undefined}
          tabRequest={peopleTabRequest}
          onOpenStorage={isOwnerConsole(auth) ? () => {
            clientRef.current?.dataLocations(getActiveProjectId())
            dispatchRecycle({ kind: 'load' })
            clientRef.current?.dataRecycleList(getActiveProjectId())
          } : undefined}
        />
      )}

      {/* A step's skill opened from the node editor: the same read-only detail, over the builder; closing it returns
          to the step as it was. */}
      {activePage === 'workflows' && componentDetail && (
        <ComponentDetailPanel view={componentDetail} onClose={() => setComponentDetail(null)} t={t} />
      )}
      {activePage === 'components' && (
        <ComponentLibraryPage
          catalog={componentCatalog}
          employeeContent={isOwnerConsole(auth) ? <AIEmployeeAssetHost controller={employeeAssets} project_id={employeeAssetProjectId(
            projectsHydratedRef.current, currentProject.pendingProjectId, projectStore.activeProjectId, projectStore.projects,
          )} workflows={workflows} workflow_error={workflowError} workflows_read_project_id={workflowsReadProjectId}
            open_builder_workflow_id={nodeDraftState.editingWorkflowId ?? nodeDraftState.builderWorkflowId} /> : null}
          handoffContent={isOwnerConsole(auth) ? (
            <EmployeeHandoffHost
              controller={employeeAssets}
              projectId={getActiveProjectId()}
              key={`handoffs-${getActiveProjectId()}`}
              state={employeeHandoffs.projectId === getActiveProjectId()
                ? employeeHandoffs
                : { ...EMPTY_EMPLOYEE_HANDOFFS, projectId: getActiveProjectId() }}
              connected={status === 'connected'}
              onAsk={(handoffStatus, offset, taskId) => {
                const projectId = getActiveProjectId()
                const disposition = clientRef.current?.employeeHandoffList(
                  projectId, handoffStatus ?? '', offset ?? 0, taskId ?? '') ?? 'send-failed'
                const sent = disposition === 'sent' || disposition === 'queued'
                setEmployeeHandoffs(prev => employeeHandoffsSent(prev, projectId, 'employee_handoff_list', sent))
              }}
              onRework={(handoffId, instruction) => {
                const projectId = getActiveProjectId()
                const disposition = clientRef.current?.employeeHandoffRework(projectId, handoffId, instruction) ?? 'send-failed'
                const sent = disposition === 'sent' || disposition === 'queued'
                setEmployeeHandoffs(prev => employeeHandoffsSent(prev, projectId, 'employee_handoff_rework', sent))
                return sent
              }}
              onContinue={(handoffId, instruction, instanceId) => {
                const projectId = getActiveProjectId()
                const disposition = clientRef.current?.employeeHandoffContinue(projectId, handoffId, instruction, instanceId) ?? 'send-failed'
                const sent = disposition === 'sent' || disposition === 'queued'
                setEmployeeHandoffs(prev => employeeHandoffsSent(prev, projectId, 'employee_handoff_continue', sent))
                return sent
              }}
              onDecide={(handoffId, decision, text) => {
                const projectId = getActiveProjectId()
                const disposition = clientRef.current?.employeeHandoffDecide(projectId, handoffId, decision, text) ?? 'send-failed'
                const sent = disposition === 'sent' || disposition === 'queued'
                setEmployeeHandoffs(prev => employeeHandoffsSent(prev, projectId, 'employee_handoff_decide', sent))
                return sent
              }}
              onCheck={(handoffId) => {
                const projectId = getActiveProjectId()
                const disposition = clientRef.current?.employeeHandoffCheck(projectId, handoffId) ?? 'send-failed'
                const sent = disposition === 'sent' || disposition === 'queued'
                setEmployeeHandoffs(prev => employeeHandoffsSent(prev, projectId, 'employee_handoff_check', sent))
                return sent
              }}
              onAskNode={(nodeId) => {
                const projectId = getActiveProjectId()
                const disposition = clientRef.current?.employeeHandoffNodeList(projectId, nodeId) ?? 'send-failed'
                const sent = disposition === 'sent' || disposition === 'queued'
                setEmployeeHandoffs(prev => employeeHandoffsSent(prev, projectId, 'employee_handoff_node_list', sent))
              }}
            />
          ) : undefined}
          workflows={workflows}
          // S3-5 组件库一列: the draft the builder holds (加进草稿 goes there), the way to a flow on the builder, 让安安起草一个
          // through the ordinary conversation, and the request tracker's words for a 查看 / 编辑 that got no receipt.
          tracked={tracked}
          currentDraftId={nodeDraftState.editingWorkflowId ?? nodeDraftState.builderWorkflowId}
          onOpenFlowOnBuilder={isOwnerConsole(auth) ? (workflowId) => { setOpenFlowRequest(current => ({ workflowId, seq: (current?.seq ?? 0) + 1 })); openPage('workflows') } : undefined}
          onDraftWithAssistant={isOwnerConsole(auth) ? (request) => {
            // The same road 让安安起草这个组件 takes (master_send); 安安 answers on 总控, which opens once the request is out
            // -- on a conversation of its own that the request makes (总控对话栏, policy: 总控 is entered on a
            // new conversation, so the reply is in view there rather than in an earlier one).
            const entry = requestTracker.track('master_send', () => (startMasterConversation(request) ? 'sent' : 'send-failed'))
            if (entry.state === 'busy') showPage('master')
          } : undefined}
          improvementActions={isOwnerConsole(auth) ? improvementActions : null}
          improvementStarter={isOwnerConsole(auth) ? (target) => improvementStarter(`skill:${target.id}`, [target]) : null}
          returnToStep={stepReturn}
          onReturnToStep={stepReturn ? () => {
            // Back to the builder, that flow, that step selected and open: the draft is App's, so nothing typed is lost.
            stepReturnSeqRef.current += 1
            setStepReturnRequest({ workflowId: stepReturn.workflowId, nodeId: stepReturn.nodeId, seq: stepReturnSeqRef.current })
            setStepReturn(null)
            setComponentDetail(null); setComponentWork(null)
            openPage('workflows')
          } : undefined}
          onUseComponent={isOwnerConsole(auth) ? (component, workflowId) => openBuilderWith({ ...component, workflowId }) : undefined}
          onViewComponent={isOwnerConsole(auth) ? (componentId) => {
            setComponentWork(null)
            setComponentDetail({ id: componentId, detail: null, loading: true, error: null })
            // A list read on the tracker (5 s): the panel says 「没收到回执」 instead of reading for ever.
            requestTracker.track(keyOf('component_detail', componentId), () => {
              clientRef.current?.componentDetail(getActiveProjectId(), componentId)
              return status === 'connected' ? 'sent' : 'send-failed'
            }, { kind: 'list' })
          } : undefined}
          componentDetail={componentDetail}
          onCloseComponentDetail={() => { setComponentDetail(null); setComponentWork(null) }}
          onWorkComponent={isOwnerConsole(auth) ? (componentId, tab) => {
            setComponentWork({ id: componentId, tab })
            setComponentDetail({ id: componentId, detail: null, loading: true, error: null })
            setComponentVersionsState({ id: componentId, versions: null, loading: true, error: null, busy: '' })
            requestTracker.track(keyOf('component_detail', componentId), () => {
              clientRef.current?.componentDetail(getActiveProjectId(), componentId)
              return status === 'connected' ? 'sent' : 'send-failed'
            }, { kind: 'list' })
            requestTracker.track(keyOf('component_versions', componentId), () => clientRef.current?.componentVersions(getActiveProjectId(), componentId) ?? 'send-failed', { kind: 'list' })
          } : undefined}
          componentWorkbench={componentWork && componentDetail?.id === componentWork.id && componentVersionsState.id === componentWork.id ? {
            initialTab: componentWork.tab,
            versions: componentVersionsState.versions,
            loading: componentVersionsState.loading,
            error: componentVersionsState.error,
            // The busy flag narrowed by the tracker (S2: every write three-state): a write that timed out or was cut
            // gives its buttons back with the tracker's words instead of waiting for a receipt that will not come.
            // 'edit' holds only while component_edit on this skill is out; a candidate's id only while one of its
            // chain writes is.
            busy: componentVersionsState.busy === 'edit'
              ? (stateOf(tracked, keyOf('component_edit', componentWork.id)) === 'busy' ? 'edit' : '')
              : (stillOut(tracked, componentVersionsState.busy, [
                'component_candidate_test', 'component_candidate_review', 'component_candidate_revise',
                'component_candidate_evaluation_spec', 'component_candidate_evaluation_sessions', 'component_candidate_evaluation_record',
                'skill_improvement_stage', 'skill_improvement_activate', 'skill_improvement_rollback',
              ]) ?? ''),
            connected: status === 'connected',
            // Every write of the chain on the tracker, keyed by what its receipt names (component_edit the skill, the
            // rest the candidate): a request that never left gives the button back at once and the tracker says why
            // (没发出去); no receipt in time says 没收到回执; nothing is re-sent on its own.
            onEdit: (fields, fingerprint) => {
              setComponentVersionsState(current => ({ ...current, busy: 'edit' }))
              if (requestTracker.track(keyOf('component_edit', componentWork.id), () => clientRef.current?.componentEdit(getActiveProjectId(), componentWork.id, fields, fingerprint) ?? 'send-failed').state !== 'busy') setComponentVersionsState(current => ({ ...current, busy: '' }))
            },
            onTest: (candidateId, outcome, note) => {
              setComponentVersionsState(current => ({ ...current, busy: candidateId }))
              if (requestTracker.track(keyOf('component_candidate_test', candidateId), () => clientRef.current?.componentCandidateTest(getActiveProjectId(), componentWork.id, candidateId, outcome, note) ?? 'send-failed').state !== 'busy') setComponentVersionsState(current => ({ ...current, busy: '' }))
            },
            onReview: (candidateId, decision, note, fingerprint) => {
              setComponentVersionsState(current => ({ ...current, busy: candidateId }))
              if (requestTracker.track(keyOf('component_candidate_review', candidateId), () => clientRef.current?.componentCandidateReview(getActiveProjectId(), componentWork.id, candidateId, decision, note, fingerprint) ?? 'send-failed').state !== 'busy') setComponentVersionsState(current => ({ ...current, busy: '' }))
            },
            // Staging, activation and rollback are the skill-improvement calls 技能改进 already uses, with its confirms
            // and on the same tracker keys (one request out per candidate, whichever page pressed it).
            onStage: (candidateId) => {
              setComponentVersionsState(current => ({ ...current, busy: candidateId }))
              if (requestTracker.track(keyOf('skill_improvement_stage', candidateId), () => clientRef.current?.skillImprovementStage(getActiveProjectId(), candidateId) ?? 'send-failed').state !== 'busy') setComponentVersionsState(current => ({ ...current, busy: '' }))
            },
            onActivate: (candidate: ComponentCandidate) => {
              const fingerprint = candidate.activation?.deployment_fingerprint ?? ''
              if (!fingerprint || !window.confirm(t('components.activateConfirm', { skill: candidate.skill_id, version: candidate.version }))) return
              setComponentVersionsState(current => ({ ...current, busy: candidate.candidate_id }))
              if (requestTracker.track(keyOf('skill_improvement_activate', candidate.candidate_id), () => clientRef.current?.skillImprovementActivate(getActiveProjectId(), candidate.candidate_id, fingerprint) ?? 'send-failed').state !== 'busy') setComponentVersionsState(current => ({ ...current, busy: '' }))
            },
            onRollback: (candidate: ComponentCandidate) => {
              const fingerprint = candidate.activation?.deployment_fingerprint ?? ''
              if (!fingerprint || !window.confirm(t('components.rollbackConfirm', { skill: candidate.skill_id }))) return
              setComponentVersionsState(current => ({ ...current, busy: candidate.candidate_id }))
              if (requestTracker.track(keyOf('skill_improvement_rollback', candidate.candidate_id), () => clientRef.current?.skillImprovementRollback(getActiveProjectId(), candidate.candidate_id, fingerprint) ?? 'send-failed').state !== 'busy') setComponentVersionsState(current => ({ ...current, busy: '' }))
            },
            // 4.7-A03: 修订, the frozen dual-run standard, the two pre-bound groups and the recorded comparison.
            onRevise: (candidate: ComponentCandidate, fields, reason) => {
              setComponentVersionsState(current => ({ ...current, busy: candidate.candidate_id }))
              if (requestTracker.track(keyOf('component_candidate_revise', candidate.candidate_id), () => clientRef.current?.componentCandidateRevise(getActiveProjectId(), componentWork.id, candidate.candidate_id, fields, reason, candidate.candidate_fingerprint) ?? 'send-failed').state !== 'busy') setComponentVersionsState(current => ({ ...current, busy: '' }))
            },
            onEvaluationSpec: (candidate: ComponentCandidate, spec) => {
              setComponentVersionsState(current => ({ ...current, busy: candidate.candidate_id }))
              if (requestTracker.track(keyOf('component_candidate_evaluation_spec', candidate.candidate_id), () => clientRef.current?.componentCandidateEvaluationSpec(getActiveProjectId(), componentWork.id, candidate.candidate_id, candidate.candidate_fingerprint, spec) ?? 'send-failed').state !== 'busy') setComponentVersionsState(current => ({ ...current, busy: '' }))
            },
            onEvaluationSessions: (candidate: ComponentCandidate, variant, sessionIds) => {
              setComponentVersionsState(current => ({ ...current, busy: candidate.candidate_id }))
              if (requestTracker.track(keyOf('component_candidate_evaluation_sessions', candidate.candidate_id), () => clientRef.current?.componentCandidateEvaluationSessions(getActiveProjectId(), componentWork.id, candidate.candidate_id, candidate.candidate_fingerprint, variant, sessionIds) ?? 'send-failed').state !== 'busy') setComponentVersionsState(current => ({ ...current, busy: '' }))
            },
            onEvaluationRecord: (candidate: ComponentCandidate, baselineSessionIds, candidateSessionIds, note) => {
              setComponentVersionsState(current => ({ ...current, busy: candidate.candidate_id }))
              if (requestTracker.track(keyOf('component_candidate_evaluation_record', candidate.candidate_id), () => clientRef.current?.componentCandidateEvaluationRecord(getActiveProjectId(), componentWork.id, candidate.candidate_id, candidate.candidate_fingerprint, baselineSessionIds, candidateSessionIds, note) ?? 'send-failed').state !== 'busy') setComponentVersionsState(current => ({ ...current, busy: '' }))
            },
            refusal: componentVersionsState.refusal ?? null,
          } : null}
          onTryCatalogSkill={isOwnerConsole(auth) ? (skill) => {
            catalogOpenAfterImportRef.current.add(skill.id)
            setMarketBusy(prev => ({ ...prev, [`catalog:${skill.id}`]: 'downloading' }))
            // On the tracker: not sent or no receipt gives the button back (the tracker says why); nothing is re-sent.
            const entry = requestTracker.track(keyOf('skill_trial_import', skill.id), () => {
              clientRef.current?.skillTrialImportCatalog(getActiveProjectId(), skill.id)
              return status === 'connected' ? 'sent' : 'send-failed'
            })
            if (entry.state !== 'busy') { catalogOpenAfterImportRef.current.delete(skill.id); setMarketBusy(prev => { const next = { ...prev }; delete next[`catalog:${skill.id}`]; return next }) }
          } : undefined}
          onOpenTrial={isOwnerConsole(auth) ? openTrialCard : undefined}
          skillImprovements={isOwnerConsole(auth) ? skillImprovements : undefined}
          improvementProposals={isOwnerConsole(auth) ? improvementProposals : undefined}
          distributionInbox={isOwnerConsole(auth) ? distributionInbox : undefined}
          onExportDistribution={isOwnerConsole(auth) ? workflowId => {
            if (!window.confirm(t('components.distribution.exportConfirm', { workflow: workflowId }))) return
            // On the tracker: 没发出去 / 没收到回执 are its words (toasted by its onChange); nothing is re-sent on its own.
            requestTracker.track(keyOf('workflow_distribution_export', workflowId), () => clientRef.current?.workflowDistributionExport(getActiveProjectId(), workflowId) ?? 'send-failed')
          } : undefined}
          onReceiveDistribution={isOwnerConsole(auth) ? (path, decision, note) => {
            if (decision === 'accept' && !window.confirm(t('components.distribution.acceptConfirm'))) return
            requestTracker.track('workflow_distribution_receive', () => clientRef.current?.workflowDistributionReceive(getActiveProjectId(), path, decision, note) ?? 'send-failed')
          } : undefined}
          onRefreshDistributionInbox={isOwnerConsole(auth) ? () => {
            setDistributionInbox(null)
            clientRef.current?.workflowDistributionInbox(getActiveProjectId())
          } : undefined}
          skillImprovementBusy={skillImprovementBusy}
          skillImprovementError={skillImprovementError}
          onRefreshSkillImprovements={isOwnerConsole(auth) ? () => {
            setSkillImprovements(null); setImprovementProposals(null); setSkillImprovementError(null)
            clientRef.current?.skillImprovementList(getActiveProjectId())
            clientRef.current?.improvementProposalList(getActiveProjectId())
          } : undefined}
          onStageSkillImprovement={isOwnerConsole(auth) ? candidate => {
            setSkillImprovementBusy(candidate.candidate_id)
            // The three deployment writes on the tracker: a request that did not go out gives its button back at once.
            if (requestTracker.track(keyOf('skill_improvement_stage', candidate.candidate_id), () => clientRef.current?.skillImprovementStage(getActiveProjectId(), candidate.candidate_id) ?? 'send-failed').state !== 'busy') setSkillImprovementBusy('')
          } : undefined}
          onActivateSkillImprovement={isOwnerConsole(auth) ? candidate => {
            const fingerprint = candidate.activation?.deployment_fingerprint ?? ''
            if (!fingerprint || !window.confirm(t('components.activateConfirm', { skill: candidate.skill_id, version: candidate.version }))) return
            setSkillImprovementBusy(candidate.candidate_id)
            if (requestTracker.track(keyOf('skill_improvement_activate', candidate.candidate_id), () => clientRef.current?.skillImprovementActivate(getActiveProjectId(), candidate.candidate_id, fingerprint) ?? 'send-failed').state !== 'busy') setSkillImprovementBusy('')
          } : undefined}
          onRollbackSkillImprovement={isOwnerConsole(auth) ? candidate => {
            const fingerprint = candidate.activation?.deployment_fingerprint ?? ''
            if (!fingerprint || !window.confirm(t('components.rollbackConfirm', { skill: candidate.skill_id }))) return
            setSkillImprovementBusy(candidate.candidate_id)
            if (requestTracker.track(keyOf('skill_improvement_rollback', candidate.candidate_id), () => clientRef.current?.skillImprovementRollback(getActiveProjectId(), candidate.candidate_id, fingerprint) ?? 'send-failed').state !== 'busy') setSkillImprovementBusy('')
          } : undefined}
          market={isOwnerConsole(auth) ? {
            connected: status === 'connected',
            categories: marketCategories,
            browse: marketBrowse,
            trials: skillTrials,
            busy: marketBusy,
            trialError: skillTrialError,
            fileView: trialFileView,
            focus: trialFocus,
            onFocusHandled: () => setTrialFocus(null),
            mismatchAsk: trialMismatchAsk,
            onMismatchAskHandled: () => setTrialMismatchAsk(null),
            workflows,
            onBrowse: (query) => {
              setMarketBrowse(prev => ({ ...prev, loading: true, error: null }))
              clientRef.current?.skillMarketBrowse(getActiveProjectId(), { ...query, limit: MARKET_TOP_LIMIT })
            },
            onDownload: (slug) => {
              setMarketBusy(prev => ({ ...prev, [slug]: 'downloading' }))
              clientRef.current?.skillTrialDownload(getActiveProjectId(), slug)
            },
            ...trialHandlers,
            onViewFile: (trial, path) => clientRef.current?.skillTrialFile(getActiveProjectId(), trial.slug, trial.version, path, trial.source),
            onRefreshTrials: () => clientRef.current?.skillTrialList(getActiveProjectId()),
            // 调用测试: the trial skill goes to the chosen workflow's builder shelf, where it is tested.
            onUse: (trial, workflowId) => openBuilderWith({ kind: 'skill', id: trial.slug, name: trial.name, workflowId, trial: { slug: trial.slug, version: trial.version, source: trial.source } }),
          } : null}
          connected={status === 'connected'}
          loading={componentCatalogLoading}
          error={componentCatalogError}
          onRefresh={() => {
            setComponentCatalogLoading(true); setComponentCatalogError(null); clientRef.current?.componentCatalogList(getActiveProjectId()); clientRef.current?.workflowList(getActiveProjectId())
            if (isOwnerConsole(auth)) {
              setSkillImprovements(null); setImprovementProposals(null); setSkillImprovementError(null)
              clientRef.current?.skillImprovementList(getActiveProjectId()); clientRef.current?.improvementProposalList(getActiveProjectId())
            }
          }}
        />
      )}

      <DevToolsOverlay
        open={showDevTools}
        store={eventTimeline}
        snapshot={snapshot}
        wsUrlInput={wsUrlInput}
        onWsUrlInputChange={setWsUrlInput}
        onApplyWsUrl={applyWsUrl}
        onClose={() => setShowDevTools(false)}
      />
      {toastMessage && <div className={toastType === 'error' ? 'toast-error' : 'toast-success'}>{toastMessage}</div>}
      {isOwnerConsole(auth) && <ExecutionObservatoryHost
        store={observatoryTrace} historyReader={observatoryHistory} projectId={projectStore.activeProjectId}
        connection={status} pendingProject={Boolean(currentProject.pendingProjectId)} openSequence={observatoryOpenSequence}
        tasks={sessionStore.sessions.filter(session => session.projectId === projectStore.activeProjectId).map(session => ({ id: session.taskId, title: session.title, status: session.status }))}
        orders={(ordersReadProjectId === projectStore.activeProjectId ? orders ?? [] : []).map(order => ({
          id: order.order_id,
          title: [order.customer_name, order.brand_name].filter(Boolean).join(' · ') || order.order_id,
          status: order.status || '未提供',
          ...(order.launched_task_id ? { taskId: order.launched_task_id } : {}),
          ...(order.customer_name ? { customerName: order.customer_name } : {}),
          ...(order.brand_name ? { brandName: order.brand_name } : {}),
          ...(typeof order.handling?.amount_quoted === 'number' ? { amount: order.handling.amount_quoted } : {}),
          ...(order.handling?.currency ? { currency: order.handling.currency } : {}),
          ...(order.handling?.due_at ? { dueAt: order.handling.due_at } : {}),
          ...(order.handling?.assignee_employee_id ? { assigneeEmployeeId: order.handling.assignee_employee_id } : {}),
          ...(order.acceptance?.status ? { acceptanceStatus: order.acceptance.status } : {}),
          ...(order.fde_control_plane?.phase ? { fdePhase: order.fde_control_plane.phase } : {}),
        }))}
        org={orgInfoData}
        agents={swarmAgents}
        projectName={projectStore.projects.find(project => project.id === projectStore.activeProjectId)?.name || projectStore.activeProjectId}
        onRequestData={() => { clientRef.current?.orgInfo(); clientRef.current?.listAgents() }}
        onOpenTask={id => openExecution(id)}
      />}
      {/* ── Global Execution Panel (accessible from any page) ── */}
      <MaybeExecutionPanel
        taskId={executionPanelTaskId}
        projectId={projectStore.activeProjectId}
        sessions={sessionStore.sessions}
        agents={swarmAgents}
        getExternalTeamActivity={externalTeamActivityStore.getForTask}
        onLoadOlder={handleLoadOlderTeamActivity}
        onSelectInvocation={handleSelectTeamInvocation}
        runtimeOnly={executionPanelRuntimeOnly}
        onOpenRuntimeActivity={() => setExecutionPanelRuntimeOnly(true)}
        onClose={() => {
          setExecutionPanelTaskId(null)
          setExecutionPanelRuntimeOnly(false)
        }}
      />
    </div>
  )
}
