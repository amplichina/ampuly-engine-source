import type {
  AgentRuntimePayload,
  OrgInfoPayload,
  OrgCreateMemberInput,
  OrgSavedCreatePayload,
  ReorgListPayload,
  SavedOrgSummary,
  SessionProgressPayload,
  SocketEnvelope,
  SocketStatus,
  VisualEvent,
  VisualSnapshot,
  WorkerNotificationPayload,
  WorkItemProgressPayload,
} from '../types/visual'
import type {
  InteractionReplyReceipt,
  InteractionReplyRequest,
  OutgoingAttachmentPayload,
  SessionSendMetadata,
} from '../types/chat'
import type { NativeApprovalLevel, TaskPreferredAgent, WorkflowContract } from '../types/kanban'
import type { ExternalTeamActivitySnapshotPayload, ExternalTeamEventV1 } from '../types/externalTeamActivity'
import { browserControlContext, browserPushScope } from '../workspace/browserAccountState'
import type { BrowserControlContext } from '../workspace/browserAccountState'
import { browserAccountInspectRequest } from '../workspace/browserAccountInspection'

import type { EmployeeWorkflowTrialAction } from './employeeWorkflowTrial'

export interface BrowserSessionChangedPush extends Record<string, unknown>, BrowserControlContext {
  project_id: string
  binding_key: string
  state: string
}

export interface BrowserFramePush extends Record<string, unknown>, BrowserControlContext {
  project_id: string
  binding_key: string
  data: string
  width?: number | null
  height?: number | null
}

interface SocketHandlers {
  onSnapshot?: (snapshot: VisualSnapshot) => void
  onAuth?: (payload: { required: boolean; bootstrap_needed: boolean; account: Record<string, unknown> | null }) => void
  onEvent?: (event: VisualEvent) => void
  onAck?: (payload: Record<string, unknown>) => void
  onStatus?: (status: SocketStatus, detail?: string) => void
  onChannelCreated?: (payload: { channel_id: string; name: string; channel_type: string; participants: string[] }) => void
  onBoardEvent?: (payload: Record<string, unknown>) => void
  onCrossOfficeCollab?: (payload: { agent_ids: string[]; task_id: string; action: string }) => void
  onCollabMessage?: (type: string, payload: Record<string, unknown>) => void
  onAgentRuntimeUpdate?: (payload: AgentRuntimePayload) => void
  onRuntimeStatusSync?: (payload: RuntimeStatusSyncPayload) => void
  onWorkerNotification?: (payload: WorkerNotificationPayload) => void
  onSessionCreated?: (payload: { project_id: string; task_id: string; channel_id: string; session_id?: string; parent_session_id?: string; origin_task_id?: string; title: string; status: string; created_at: number; assignee_ids?: string[]; exec_mode?: string; company_profile?: string; org_id?: string; organization_id?: string; preferred_agent?: TaskPreferredAgent; selected_execution_agent?: TaskPreferredAgent; native_approval_level?: NativeApprovalLevel; native_permission_scope_id?: string }) => void
  onSessionUpdated?: (payload: { project_id: string; task_id: string; exec_mode?: string; company_profile?: string; org_id?: string; organization_id?: string; preferred_agent?: TaskPreferredAgent; selected_execution_agent?: TaskPreferredAgent; native_approval_level?: NativeApprovalLevel; native_permission_scope_id?: string; workflow_contract?: WorkflowContract }) => void
  onNativePermissionDefaultUpdated?: (payload: { native_approval_default: NativeApprovalLevel }) => void
  onSessionMessage?: (payload: Record<string, unknown>) => void
  onBrowserFrame?: (payload: BrowserFramePush) => void
  onBrowserSessionChanged?: (payload: BrowserSessionChangedPush) => void
  onSessionTitleUpdated?: (payload: { project_id: string; task_id: string; title: string }) => void
  onSessionDeleted?: (payload: { project_id: string; task_id: string }) => void
  onSessionProgress?: (payload: SessionProgressPayload) => void
  onChildSessionCreated?: (payload: { project_id: string; session_id: string; parent_session_id: string; task_id: string; origin_task_id?: string; title: string; agent_id?: string; org_id?: string; organization_id?: string; selected_execution_agent?: TaskPreferredAgent }) => void
  onProjectSwitched?: (payload: { project_id: string; switch_seq?: string }) => void
  onProjectDeleted?: (payload: { project_id: string }) => void
  onOrgInfo?: (payload: OrgInfoPayload) => void
  onReorgList?: (payload: ReorgListPayload) => void
  onWorkItemProgress?: (payload: WorkItemProgressPayload) => void
  onExternalTeamActivityEvent?: (payload: ExternalTeamEventV1) => void
  onExternalTeamActivitySnapshot?: (payload: ExternalTeamActivitySnapshotPayload) => void
  onMarketListInstalled?: (payload: { packages: Array<Record<string, unknown>> }) => void
  onMarketBrowse?: (payload: { presets: Array<Record<string, unknown>> }) => void
  onMarketPreview?: (payload: Record<string, unknown>) => void
  onOrgConfigExport?: (payload: { yaml: string }) => void
  onOrgConfigImport?: (payload: { ok: boolean; dry_run?: boolean; preview?: { roles_added: number; roles_removed: number; employees_changed: number }; error?: string; validation_errors?: string[] }) => void
  onOrgSavedList?: (payload: { orgs: SavedOrgSummary[]; active_name?: string | null }) => void
  onOrgSavedSaveAs?: (payload: { ok: boolean; name: string; error?: string }) => void
  onOrgSavedCreate?: (payload: OrgSavedCreatePayload) => void
  onOrgSavedLoad?: (payload: { ok: boolean; name: string; error?: string }) => void
  onOrgSavedDelete?: (payload: { ok: boolean; name: string; error?: string }) => void
  onCommsState?: (payload: CommsStatePayload) => void
  onCommsMessage?: (payload: CommsMessagePayload) => void
}

import type { MasterArtifactReference } from './artifactReference'

/** 安安 · 引用: one thing a master-control message points at (master_references.py checks it exists). */
export type MasterReference =
  | { kind: 'workflow'; workflow_id: string; draft_revision?: string }
  | { kind: 'node'; workflow_id: string; node_id: string; draft_revision?: string }
  | { kind: 'component'; component_id: string; workflow_id?: string; node_id?: string }
  | MasterArtifactReference

/** 安安 · 附件: one file sent with a master-control message, in the order chat's upload shape. */
export interface MasterAttachment {
  filename: string
  mime_type: string
  /** base64, or a data: URL as FileReader.readAsDataURL gives it */
  data: string
}

export interface RuntimeStatusSyncPayload {
  project_id: string
  sessions: Array<{
    task_id: string
    status: string
    agent_status?: string
    current_tool?: string | null
  }>
}

export interface CommsMessageItem {
  message_id: string
  from: string
  to?: string
  subject: string
  sent_at: string
  blocking: boolean
  path: string
  bucket?: 'new' | 'seen' | 'sent'
}

/** @deprecated Use CommsMessageItem instead */
export type CommsRecentUnread = CommsMessageItem

export interface CommsRolePayload {
  role_id: string
  unread_count: number
  has_blocking: boolean
  seen_count: number
  outbox_count: number
  recent_unread: CommsMessageItem[]
  recent_seen?: CommsMessageItem[]
  recent_outbox?: CommsMessageItem[]
}

export interface CommsMeetingPayload {
  meeting_id: string
  topic: string
  status: string
  organizer: string
  participants: string[]
  entry_count: number
  opened_at: string
  closed_at?: string | null
  decision?: string | null
  transcript_path: string
}

export interface CommsFailurePayload {
  operation: string
  from_role: string
  to_role: string
  reason: string
  attempted_path?: string
  attempted_command?: string
  recorded_at?: string
  attempt_count?: number
  can_retry?: boolean
}

export interface CommsStatePayload {
  available: boolean
  reason?: string
  empty?: boolean
  project_id?: string
  task_id?: string
  session_id?: string
  workspace_root?: string
  output_root?: string
  comms_root?: string
  projection_status?: string
  recent_failures?: CommsFailurePayload[]
  roles?: CommsRolePayload[]
  meetings?: CommsMeetingPayload[]
}

export interface CommsMessagePayload {
  project_id: string
  path: string
  header: {
    from?: string
    to?: string
    sent_at?: string
    blocking?: boolean
    [key: string]: unknown
  }
  body: string
}

const RECONNECT_BASE_MS = 2000
const RECONNECT_MAX_MS = 30000
const RECONNECT_MAX_ATTEMPTS = 20
const PENDING_QUEUE_MAX = 100
const HEARTBEAT_INTERVAL_MS = 30_000
const HEARTBEAT_TIMEOUT_MS = 10_000
const PROJECT_SCOPED_MESSAGE_TYPES = new Set([
  'collab_sync',
  'kanban_create_task',
  'kanban_move_task',
  'run_task',
  'create_session',
  'session_send',
  'interaction_reply',
  'session_update_config',
  'session_delete',
  'session_detail',
  'external_team_activity_get',
  'session_stop',
  'session_resume',
  'session_complete',
  'session_update_title',
  'secretary_send',
  'project_index',
  'comms_state',
  'comms_read_message',
])

const SESSION_DETAIL_REQUEST_TIMEOUT_MS = 30_000
const INTERACTION_REPLY_TIMEOUT_MS = 30_000
export type SendDisposition = 'sent' | 'queued' | 'queue-full' | 'send-failed'

// 断线不自动重发 (owner's design,, S2-B): only a READ may wait in the queue for the next connection and go
// out again on its own. A write sent while the console is not connected is refused on the spot ('send-failed'), so a
// reconnect can never replay a decision, a save, a cancel or a grant into a state that has moved on. Until now 37
// methods each carried their own copy of that refusal and the rest queued; this is the one rule, and anything not
// listed here is a write. Opening a file manager or the machine's folder dialog is not a read either: replayed after
// a reconnect it would open a window nobody asked for just then.
const REPLAYABLE_READ_TYPES = new Set([
  'ping', 'list_agents', 'collab_sync', 'project_index', 'list_projects', 'switch_project', 'org_info', 'org_saved_list',
  'org_config_export', 'reorg_list', 'market_browse', 'market_preview', 'market_list_installed',
  'session_detail', 'external_team_activity_get', 'comms_state', 'comms_read_message',
  'process_spec_list', 'process_spec_get', 'component_catalog_list', 'component_detail', 'component_versions',
  'skill_market_categories', 'skill_market_browse', 'skill_trial_list', 'skill_trial_file', 'skill_improvement_list',
  'improvement_proposal_list', 'connector_candidate_list', 'connector_switch_list',
  'browser_session_list', 'browser_screenshot_read', 'browser_audit_trail',
  'harness_pending_list', 'harness_turn_actions', 'deployment_list', 'credential_reference_list',
  'order_metrics', 'run_budget_list', 'solution_blueprint_list', 'order_timeline', 'order_model_plan', 'channel_config_check',
  'master_attachment_page', 'master_thread_list', 'blockers_list', 'owner_attention_list', 'checkpoint_locate', 'gate_detail', 'gate_status', 'duty_log_list', 'account_list', 'contact_list',
  'my_tasks_list', 'customer_orders_view', 'project_order_overview', 'project_profile_get', 'project_connections_get', 'order_list', 'distribution_authorization_list', 'acceptance_get',
  'workflow_distribution_inbox', 'governance_policy_list', 'workflow_list', 'workflow_release_evidence',
  'project_workflow_adoptions', 'workflow_versions', 'workflow_preflight', 'workflow_node_trial_list',
  'workflow_node_trial_artifact', 'workflow_flow_trial_list', 'artifacts_list', 'artifact_read',
  'data_project_index', 'data_order_view', 'data_folder_list', 'data_preview', 'data_attachments', 'data_usage',
  'data_backups', 'data_cleanup_preview', 'plan_project_removal', 'data_locations', 'data_folder_legacy',
  'data_dir_browse', 'data_folder_plan', 'data_recycle_list', 'memory_overview', 'settings_get', 'wecom_channel_status',
  'project_lifecycle', 'restore_preflight',
])

/** Whether a message may wait in the queue for the next connection: reads may, writes never. A
 *  `browser_candidate_control` is a read only when it lists. */
export function queuesForReconnect(payload: Record<string, unknown>): boolean {
  const messageType = typeof payload.type === 'string' ? payload.type : ''
  if (messageType === 'browser_candidate_control') return payload.command === 'list'
  return REPLAYABLE_READ_TYPES.has(messageType)
}

export class VisualSocketClient {
  private ws: WebSocket | null = null
  private reconnectTimer: number | null = null
  private closedByUser = false
  private reconnectAttempt = 0
  private pendingQueue: string[] = []
  private lastAuthenticatedPrincipal = ''
  private heartbeatTimer: number | null = null
  private pongTimer: number | null = null
  private pendingSessionDetailRequests: Array<{
    projectId: string
    taskId: string
    detailLevel: 'summary' | 'full'
    viewGeneration?: number
    historyPage: boolean
    wireData: string
    queued: boolean
    settled: boolean
    timeout: ReturnType<typeof setTimeout> | null
    resolve: (payload: Record<string, unknown>) => void
  }> = []
  private pendingInteractionReplies = new Map<string, {
    wireData: string
    timeout: ReturnType<typeof setTimeout>
    resolve: (payload: InteractionReplyReceipt) => void
  }>()
  private ackObservers = new Set<(payload: Record<string, unknown>) => void>()

  constructor(
    private url: string,
    private handlers: SocketHandlers,
  ) {}

  updateUrl(url: string): void {
    this.url = url
  }

  /** Every receipt (`ack`), before the page's own handler sees it: for a layer that only needs to know a request
   *  was answered (lib/requestTracker). Returns the way to stop observing. */
  addAckObserver(observer: (payload: Record<string, unknown>) => void): () => void {
    this.ackObservers.add(observer)
    return () => { this.ackObservers.delete(observer) }
  }

  connect(): void {
    this.closedByUser = false
    this.handlers.onStatus?.('connecting')

    const socket = new WebSocket(this.url)
    this.ws = socket
    socket.onopen = () => {
      if (this.ws !== socket) return
      this.reconnectAttempt = 0
      this.handlers.onStatus?.('connected')
      // Transport-open precedes the server's auth frame. Replaying queued reads here races token sign-in.
      this.startHeartbeat()
    }
    socket.onmessage = (evt) => {
      if (this.ws !== socket) return
      // Any frame from the server proves the connection is alive. Only a pong
      // counted Previously,: a busy server answering slowly looked dead
      // after 10 s, the page dropped the connection, and every reconnect asked
      // the server for its whole state again -- making it busier still.
      this.handlePong()
      this.handleMessage(evt.data)
    }
    socket.onerror = () => {
      if (this.ws !== socket) return
      this.handlers.onStatus?.('error', 'WebSocket error')
    }
    socket.onclose = () => {
      if (this.ws !== socket) return
      this.stopHeartbeat()
      this.failPendingSessionDetailRequests('connection_closed')
      // 断线不自动重发 (owner's design): a decision whose receipt was cut off by the close is told so, in
      // words, and is NOT replayed on the next connection. The answer path (lib/answerPath) then looks at the list
      // before the next click sends anything. Until now the exact wire was re-queued and went out again on its own.
      this.failPendingInteractionReplies('connection_lost')
      this.handlers.onStatus?.('disconnected')
      this.ws = null
      if (!this.closedByUser) {
        this.scheduleReconnect()
      }
    }
  }

  disconnect(): void {
    this.closedByUser = true
    this.stopHeartbeat()
    this.failPendingSessionDetailRequests('disconnected')
    this.failPendingInteractionReplies('disconnected')
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    const socket = this.ws
    this.ws = null
    if (socket) {
      socket.onopen = null
      socket.onmessage = null
      socket.onerror = null
      socket.onclose = null
      socket.close()
    }
  }

  /** Trial commands are never queued, including reconciliation reads. A send exception is unknown. */
  employeeWorkflowTrial(action: EmployeeWorkflowTrialAction, payload: Record<string, unknown>): 'sent' | 'send-failed' {
    if (!['employee_workflow_trial_prepare', 'employee_workflow_trial_start', 'employee_workflow_trial_read'].includes(action))
      throw new Error('employee_trial_action_invalid')
    if (!this.ensureProjectScope({ ...payload, type: action })) return 'send-failed'
    const wire = JSON.stringify({ ...payload, type: action })
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return 'send-failed'
    this.ws.send(wire)
    return 'sent'
  }

  /** queue:false is for cancelable display reads that must not replay after reconnect. */
  send(payload: Record<string, unknown>, options?: { queue?: boolean }): SendDisposition {
    if (!this.ensureProjectScope(payload)) {
      return 'send-failed'
    }
    const data = JSON.stringify(payload)
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      // A write never waits for a reconnect (queuesForReconnect): refused now, worded by the caller, never replayed.
      if (options?.queue === false || !queuesForReconnect(payload)) return 'send-failed'
      if (this.pendingQueue.length < PENDING_QUEUE_MAX) {
        this.pendingQueue.push(data)
        return 'queued'
      }
      return 'queue-full'
    }
    try {
      this.ws.send(data)
      return 'sent'
    } catch {
      return 'send-failed'
    }
  }

  // ── Agent management ───────────────────────────────────────────────────

  listAgents(): void {
    this.send({ type: 'list_agents' })
  }

  // ── Execution mode ─────────────────────────────────────────────────────

  setExecutionMode(mode: string, profile?: string, preferredAgent?: TaskPreferredAgent, orgId?: string): void {
    this.send({ type: 'set_execution_mode', mode, profile: profile ?? 'corporate', preferred_agent: preferredAgent, org_id: orgId })
  }

  // ── Kanban integration ─────────────────────────────────────────────────

  kanbanCreateTask(opts: {
    board_id: string; column_id: string; title: string;
    description?: string; priority?: string;
    assignee_ids?: string[]; tags?: string[];
    task_id?: string;
    project_id: string;
  }): void {
    const pid = this.requireProjectId(opts.project_id, 'kanban_create_task')
    this.send({ type: 'kanban_create_task', ...opts, project_id: pid })
  }

  // The server reads task_id and column_id only (ws_handler._handle_kanban_move_task); a sort_order sent here was
  // dropped without a word, so this no longer offers one.
  kanbanMoveTask(projectId: string, taskId: string, columnId: string): void {
    const pid = this.requireProjectId(projectId, 'kanban_move_task')
    this.send({ type: 'kanban_move_task', task_id: taskId, column_id: columnId, project_id: pid })
  }

  // ── Collaboration protocol ─────────────────────────────────────────────

  collabSync(projectId: string, switchSeq?: string, viewGeneration?: number): void {
    const pid = this.requireProjectId(projectId, 'collab_sync')
    this.send({ type: 'collab_sync', project_id: pid, switch_seq: switchSeq, view_generation: viewGeneration })
  }

  projectIndex(projectId: string, switchSeq?: string, viewGeneration?: number): void {
    const pid = this.requireProjectId(projectId, 'project_index')
    this.send({ type: 'project_index', project_id: pid, switch_seq: switchSeq, view_generation: viewGeneration })
  }

  // ── Session protocol ───────────────────────────────────────────────────

  createSession(projectId: string, title?: string, execMode?: string, companyProfile?: string, preferredAgent?: TaskPreferredAgent, orgId?: string, nativeApprovalLevel?: NativeApprovalLevel): void {
    const pid = this.requireProjectId(projectId, 'create_session')
    this.send({
      type: 'create_session',
      project_id: pid,
      title: title ?? 'New Chat',
      exec_mode: execMode,
      company_profile: companyProfile,
      preferred_agent: preferredAgent,
      org_id: orgId,
      native_approval_level: nativeApprovalLevel,
    })
  }

  sessionSend(
    projectId: string,
    taskId: string,
    content: string,
    attachments?: OutgoingAttachmentPayload[],
    metadata?: SessionSendMetadata,
  ): SendDisposition {
    // A chat message expects no receipt (the reply is the message itself), so it is not tracked; but whether it left
    // the browser is answered here, so a message typed while not connected is never shown as sent (S2-B review).
    const pid = this.requireProjectId(projectId, 'session_send')
    return this.send({
      type: 'session_send',
      project_id: pid,
      task_id: taskId,
      content,
      attachments: attachments ?? [],
      metadata,
    })
  }

  interactionReply(
    projectId: string,
    request: InteractionReplyRequest,
  ): Promise<InteractionReplyReceipt> {
    const pid = this.requireProjectId(projectId, 'interaction_reply')
    const payload = {
      type: 'interaction_reply',
      project_id: pid,
      checkpoint_id: request.checkpointId,
      checkpoint_type: request.checkpointType,
      client_request_id: request.clientRequestId,
      requester_task_id: request.requesterTaskId,
      requester_session_id: request.requesterSessionId,
      decision: request.decision,
    }
    const wireData = JSON.stringify(payload)
    return new Promise((resolve) => {
      const existing = this.pendingInteractionReplies.get(request.clientRequestId)
      if (existing) {
        clearTimeout(existing.timeout)
        existing.resolve({
          ok: false,
          accepted: false,
          error: 'duplicate_request_in_flight',
          checkpoint_id: request.checkpointId,
          checkpoint_type: request.checkpointType,
          client_request_id: request.clientRequestId,
        })
      }
      // Not connected: refused now, in words (没连上), never queued to go out by itself after a reconnect -- a
      // decision is sent by a click, not by the connection coming back (owner's design).
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        this.pendingInteractionReplies.delete(request.clientRequestId)
        resolve({
          ok: false,
          accepted: false,
          error: 'socket_unavailable',
          checkpoint_id: request.checkpointId,
          checkpoint_type: request.checkpointType,
          client_request_id: request.clientRequestId,
        })
        return
      }
      const timeout = setTimeout(() => {
        const pending = this.pendingInteractionReplies.get(request.clientRequestId)
        if (!pending) return
        this.pendingInteractionReplies.delete(request.clientRequestId)
        const queuedIndex = this.pendingQueue.indexOf(pending.wireData)
        if (queuedIndex >= 0) this.pendingQueue.splice(queuedIndex, 1)
        pending.resolve({
          ok: false,
          accepted: false,
          error: 'request_timeout',
          checkpoint_id: request.checkpointId,
          checkpoint_type: request.checkpointType,
          client_request_id: request.clientRequestId,
        })
      }, INTERACTION_REPLY_TIMEOUT_MS)
      this.pendingInteractionReplies.set(request.clientRequestId, { wireData, timeout, resolve })
      const disposition = this.send(payload)
      if (disposition === 'queue-full' || disposition === 'send-failed') {
        this.failInteractionReply(
          request.clientRequestId,
          disposition === 'queue-full' ? 'send_queue_full' : 'send_failed',
        )
      }
    })
  }

  deleteSession(projectId: string, taskId: string): void {
    const pid = this.requireProjectId(projectId, 'session_delete')
    this.send({ type: 'session_delete', project_id: pid, task_id: taskId })
  }

  sessionUpdateTitle(projectId: string, taskId: string, title: string): void {
    const pid = this.requireProjectId(projectId, 'session_update_title')
    this.send({ type: 'session_update_title', project_id: pid, task_id: taskId, title })
  }

  sessionUpdateConfig(projectId: string, taskId: string, execMode: string, companyProfile?: string, preferredAgent?: TaskPreferredAgent, orgId?: string): void {
    const pid = this.requireProjectId(projectId, 'session_update_config')
    this.send({
      type: 'session_update_config',
      project_id: pid,
      task_id: taskId,
      exec_mode: execMode,
      company_profile: companyProfile,
      preferred_agent: preferredAgent,
      org_id: orgId,
    })
  }

  sessionUpdateNativePermission(projectId: string, taskId: string, level: NativeApprovalLevel): void {
    const pid = this.requireProjectId(projectId, 'session_update_config')
    this.send({
      type: 'session_update_config',
      project_id: pid,
      task_id: taskId,
      native_approval_level: level,
    })
  }

  setNativePermissionDefault(level: NativeApprovalLevel): void {
    this.send({
      type: 'native_permission_default_set',
      native_approval_level: level,
    })
  }

  sessionStop(projectId: string, taskId: string): SendDisposition {
    const pid = this.requireProjectId(projectId, 'session_stop')
    return this.send({ type: 'session_stop', project_id: pid, task_id: taskId })
  }

  sessionResume(
    projectId: string,
    taskId: string,
    runtimeSessionId?: string,
    checkpointId?: string,
    content?: string,
  ): SendDisposition {
    const pid = this.requireProjectId(projectId, 'session_resume')
    return this.send({
      type: 'session_resume',
      project_id: pid,
      task_id: taskId,
      runtime_session_id: runtimeSessionId,
      checkpoint_id: checkpointId,
      content,
    })
  }

  sessionComplete(projectId: string, taskId: string): void {
    const pid = this.requireProjectId(projectId, 'session_complete')
    this.send({ type: 'session_complete', project_id: pid, task_id: taskId })
  }

  sessionDetail(
    projectId: string,
    taskId: string,
    opts?: { limit?: number; beforeCreatedAt?: number; beforeMessageId?: string; detailLevel?: 'summary' | 'full'; include?: string[]; viewGeneration?: number },
  ): Promise<Record<string, unknown>> {
    const pid = this.requireProjectId(projectId, 'session_detail')
    const detailLevel = opts?.detailLevel ?? 'summary'
    return new Promise((resolve) => {
      const payload = {
        type: 'session_detail',
        project_id: pid,
        task_id: taskId,
        limit: opts?.limit,
        before_created_at: opts?.beforeCreatedAt,
        before_message_id: opts?.beforeMessageId,
        detail_level: detailLevel,
        include: opts?.include,
        view_generation: opts?.viewGeneration,
      }
      const wireData = JSON.stringify(payload)
      const request = {
        projectId: pid,
        taskId,
        detailLevel,
        viewGeneration: opts?.viewGeneration,
        historyPage: opts?.beforeCreatedAt !== undefined || !!opts?.beforeMessageId,
        wireData,
        queued: false,
        settled: false,
        timeout: null as ReturnType<typeof setTimeout> | null,
        resolve,
      }
      request.timeout = setTimeout(() => {
        const index = this.pendingSessionDetailRequests.indexOf(request)
        if (index >= 0) this.timeoutSessionDetailRequest(index)
      }, SESSION_DETAIL_REQUEST_TIMEOUT_MS)
      this.pendingSessionDetailRequests.push(request)
      const disposition = this.send(payload)
      request.queued = disposition === 'queued'
      if (disposition === 'queue-full' || disposition === 'send-failed') {
        const index = this.pendingSessionDetailRequests.indexOf(request)
        if (index >= 0) {
          this.failSessionDetailRequest(
            index,
            disposition === 'queue-full' ? 'send_queue_full' : 'send_failed',
          )
        }
      }
    })
  }

  externalTeamActivityGet(
    projectId: string,
    taskId: string,
    opts?: {
      externalInvocationId?: string
      limit?: number
      beforeCreatedAt?: string
      beforeEventId?: string
      viewGeneration?: number
    },
  ): void {
    const pid = this.requireProjectId(projectId, 'external_team_activity_get')
    this.send({
      type: 'external_team_activity_get',
      project_id: pid,
      task_id: taskId,
      external_invocation_id: opts?.externalInvocationId,
      limit: opts?.limit ?? 100,
      before_created_at: opts?.beforeCreatedAt,
      before_event_id: opts?.beforeEventId,
      view_generation: opts?.viewGeneration,
    })
  }

  secretarySend(projectId: string, content: string): SendDisposition {
    const pid = this.requireProjectId(projectId, 'secretary_send')
    return this.send({ type: 'secretary_send', project_id: pid, content })
  }

  accountSetCustomerScope(login: string, customerIds: string[]): void {
    this.send({ type: 'account_set_customer_scope', login, customer_ids: customerIds })
  }

  projectSetCustomer(projectId: string, customerId: string): void {
    this.send({ type: 'project_set_customer', project_id: this.requireProjectId(projectId, 'project_set_customer'), customer_id: customerId })
  }

  workflowReleaseEvidence(projectId: string, workflowId: string): void {
    this.send({ type: 'workflow_release_evidence', project_id: this.requireProjectId(projectId, 'workflow_release_evidence'), workflow_id: workflowId })
  }

  /** Every process description, each showing what it cannot yet answer. */
  processSpecList(projectId: string): void {
    this.send({ type: 'process_spec_list', project_id: this.requireProjectId(projectId, 'process_spec_list') })
  }

  processSpecGet(projectId: string, specId: string, version = ''): void {
    this.send({ type: 'process_spec_get', project_id: this.requireProjectId(projectId, 'process_spec_get'), spec_id: specId, version })
  }

  processSpecSave(projectId: string, spec: Record<string, unknown>): SendDisposition {
    return this.send({ type: 'process_spec_save', project_id: this.requireProjectId(projectId, 'process_spec_save'), spec })
  }

  /** generateSkills: draft the skills the description names and the template lacks, marked unreviewed. */
  processSpecCandidate(projectId: string, specId: string, version: string, templateId: string, draftId: string, generateSkills = false): SendDisposition {
    return this.send({ type: 'process_spec_candidate', project_id: this.requireProjectId(projectId, 'process_spec_candidate'),
      spec_id: specId, version, template_id: templateId, draft_id: draftId, generate_skills: generateSkills })
  }

  /** Offline only: it cannot reach a Provider, and says so in the result. */
  builderTrial(projectId: string, specId: string, version = '', requestId = ''): SendDisposition {
    return this.send({ type: 'builder_trial', project_id: this.requireProjectId(projectId, 'builder_trial'), spec_id: specId, version,
      ...(requestId ? { request_id: requestId } : {}) })
  }

  /** Owner-only. Continue an attempt parked at a human node with the person's decision (放行… / 打回…). */
  builderTrialResume(projectId: string, specId: string, version: string, attempt: number, decision: string, requestId = ''): SendDisposition {
    return this.send({ type: 'builder_trial_resume', project_id: this.requireProjectId(projectId, 'builder_trial_resume'),
      spec_id: specId, version, attempt, decision, ...(requestId ? { request_id: requestId } : {}) })
  }
  /** Owner-only. Stopping needs a reason; resuming does not. */
  connectorSwitchSet(projectId: string, connector: string, enabled: boolean, reason: string): void {
    this.send({
      type: 'connector_switch_set',
      project_id: this.requireProjectId(projectId, 'connector_switch_set'),
      connector, enabled, reason,
    })
  }

  componentCatalogList(projectId: string): void {
    this.send({ type: 'component_catalog_list', project_id: this.requireProjectId(projectId, 'component_catalog_list') })
  }

  /** 组件 · 查看: one internal-library skill, read only (no download, no network, no write). */
  componentDetail(projectId: string, componentId: string, kind: 'skill' = 'skill'): void {
    this.send({ type: 'component_detail', project_id: this.requireProjectId(projectId, 'component_detail'), kind, component_id: componentId })
  }

  // 在线技能市场（SkillHub，经控制台读取）与试用区。All owner-only on the console side.
  skillMarketCategories(projectId: string): void {
    this.send({ type: 'skill_market_categories', project_id: this.requireProjectId(projectId, 'skill_market_categories') })
  }

  skillMarketBrowse(projectId: string, query: { keyword: string; category: string; sort: 'stars' | 'downloads'; limit?: number }): void {
    this.send({ type: 'skill_market_browse', project_id: this.requireProjectId(projectId, 'skill_market_browse'), ...query })
  }

  skillTrialList(projectId: string): void {
    this.send({ type: 'skill_trial_list', project_id: this.requireProjectId(projectId, 'skill_trial_list') })
  }

  skillTrialDownload(projectId: string, slug: string): void {
    this.send({ type: 'skill_trial_download', project_id: this.requireProjectId(projectId, 'skill_trial_download'), slug })
  }

  skillTrialMark(projectId: string, slug: string, version: string, outcome: 'passed' | 'failed', note: string, source = 'skillhub'): void {
    this.send({ type: 'skill_trial_mark', project_id: this.requireProjectId(projectId, 'skill_trial_mark'), slug, version, outcome, note, source })
  }

  skillTrialAdmit(
    projectId: string, slug: string, version: string, note: string, licenseConfirmed: boolean, successCriteria: string[], source = 'skillhub',
    listingMismatchAcknowledged = false, listingMismatchKey = '',
  ): void {
    this.send({
      type: 'skill_trial_admit', project_id: this.requireProjectId(projectId, 'skill_trial_admit'), slug, version, note,
      license_confirmed: licenseConfirmed, success_criteria: successCriteria, source,
      // Only with the owner's yes to admitting a SkillHub package that differs from SkillHub's own file list, and with the
      // key of the differences the owner was shown: the server admits only when it is the trial's current one.
      ...(listingMismatchAcknowledged ? { listing_mismatch_acknowledged: true, listing_mismatch_key: listingMismatchKey } : {}),
    })
  }

  /** 弃用: the trial stops being offered; its files stay until it is tried again. */
  skillTrialDiscard(projectId: string, slug: string, version: string, source = 'skillhub'): void {
    this.send({ type: 'skill_trial_discard', project_id: this.requireProjectId(projectId, 'skill_trial_discard'), slug, version, source })
  }

  /** 重新试用: re-verify a discarded trial's files and put it back to be tested. */
  skillTrialRestore(projectId: string, slug: string, version: string, source = 'skillhub'): void {
    this.send({ type: 'skill_trial_restore', project_id: this.requireProjectId(projectId, 'skill_trial_restore'), slug, version, source })
  }

  /** 调用测试 on a local-catalog skill: copy it from the local resource library into the trial area, checked against the catalog. */
  skillTrialImportCatalog(projectId: string, skillId: string): void {
    this.send({ type: 'skill_trial_import', project_id: this.requireProjectId(projectId, 'skill_trial_import'), slug: skillId, source: 'catalog' })
  }

  skillTrialFile(projectId: string, slug: string, version: string, path: string, source = 'skillhub'): void {
    this.send({ type: 'skill_trial_file', project_id: this.requireProjectId(projectId, 'skill_trial_file'), slug, version, path, source })
  }

  skillImprovementList(projectId: string): void {
    this.send({ type: 'skill_improvement_list', project_id: this.requireProjectId(projectId, 'skill_improvement_list') })
  }

  improvementProposalList(projectId: string): void {
    this.send({ type: 'improvement_proposal_list', project_id: this.requireProjectId(projectId, 'improvement_proposal_list') })
  }

  // 4.7-A11 提一个改进: one sentence on a published flow, a library skill or a registered connector. The server
  // drafts and materializes an inert candidate and answers which editor it opens in.
  improvementProposalStart(projectId: string, targetKind: 'process' | 'skill' | 'connector', targetId: string, request: string): SendDisposition {
    return this.send({
      type: 'improvement_proposal_start',
      project_id: this.requireProjectId(projectId, 'improvement_proposal_start'),
      target_kind: targetKind, target_id: targetId, request,
    })
  }

  // The approval is judged on the last N finished real orders before and after; a worse key measure refuses it.
  improvementProposalApprove(projectId: string, proposalId: string, expectedFingerprint: string, sampleSize: number, note = ''): SendDisposition {
    return this.send({
      type: 'improvement_proposal_approve',
      project_id: this.requireProjectId(projectId, 'improvement_proposal_approve'),
      proposal_id: proposalId, expected_fingerprint: expectedFingerprint, sample_size: sampleSize, note,
    })
  }

  // A run-session dual run's standard: what should get better and by how much. What is measured -- the proposal's
  // own candidate -- is filled in by the server.
  improvementProposalBindEvaluation(
    projectId: string, proposalId: string, expectedFingerprint: string,
    standard: { hypothesis: string; metrics: Array<{ name: string; min_delta: number }>; min_samples: number },
  ): SendDisposition {
    return this.send({
      type: 'improvement_proposal_bind_evaluation',
      project_id: this.requireProjectId(projectId, 'improvement_proposal_bind_evaluation'),
      proposal_id: proposalId, expected_fingerprint: expectedFingerprint, standard,
    })
  }

  // 按真实订单评估收益: the server reads the last N finished real orders; the page sends no numbers.
  improvementProposalRecordEvaluation(projectId: string, proposalId: string, expectedFingerprint: string, sampleSize: number): SendDisposition {
    return this.send({
      type: 'improvement_proposal_record_evaluation',
      project_id: this.requireProjectId(projectId, 'improvement_proposal_record_evaluation'),
      proposal_id: proposalId, expected_fingerprint: expectedFingerprint, sample_size: sampleSize,
    })
  }

  improvementProposalBindHarnessSessions(
    projectId: string, proposalId: string, expectedFingerprint: string,
    variant: 'baseline' | 'candidate', sessionIds: string[],
  ): SendDisposition {
    return this.send({
      type: 'improvement_proposal_bind_harness_sessions',
      project_id: this.requireProjectId(projectId, 'improvement_proposal_bind_harness_sessions'),
      proposal_id: proposalId, expected_fingerprint: expectedFingerprint,
      variant, session_ids: sessionIds,
    })
  }

  improvementProposalRecordHarnessEvaluation(
    projectId: string, proposalId: string, expectedFingerprint: string,
    baselineSessionIds: string[], candidateSessionIds: string[], note: string,
  ): SendDisposition {
    return this.send({
      type: 'improvement_proposal_record_harness_evaluation',
      project_id: this.requireProjectId(projectId, 'improvement_proposal_record_harness_evaluation'),
      proposal_id: proposalId, expected_fingerprint: expectedFingerprint,
      baseline_session_ids: baselineSessionIds, candidate_session_ids: candidateSessionIds, note,
    })
  }

  // 组件 · 编辑 / 测试 / 发布 (4.7 A2-4): one internal skill's candidates, an owner's edit as a new draft candidate, its
  // verdict, and the owner's review. Staging, activation and rollback are the skill_improvement_* calls below.
  componentVersions(projectId: string, componentId: string): SendDisposition {
    return this.send({ type: 'component_versions', project_id: this.requireProjectId(projectId, 'component_versions'), component_id: componentId })
  }

  componentEdit(projectId: string, componentId: string, fields: { version: string; description: string; instructions: string; success_criteria: string[]; test_plan: string[] }, expectedFingerprint: string): SendDisposition {
    return this.send({
      type: 'component_edit', project_id: this.requireProjectId(projectId, 'component_edit'),
      component_id: componentId, fields, expected_fingerprint: expectedFingerprint,
    })
  }

  componentCandidateTest(projectId: string, componentId: string, candidateId: string, outcome: 'passed' | 'failed', note: string): SendDisposition {
    return this.send({
      type: 'component_candidate_test', project_id: this.requireProjectId(projectId, 'component_candidate_test'),
      component_id: componentId, candidate_id: candidateId, outcome, note,
    })
  }

  componentCandidateReview(projectId: string, componentId: string, candidateId: string, decision: 'approved' | 'rejected', note: string, expectedFingerprint: string): SendDisposition {
    return this.send({
      type: 'component_candidate_review', project_id: this.requireProjectId(projectId, 'component_candidate_review'),
      component_id: componentId, candidate_id: candidateId, decision, note, expected_fingerprint: expectedFingerprint,
    })
  }

  // 4.7-A03: a failed candidate revised into a new draft; the frozen dual-run standard; one pre-bound group of run
  // sessions; the comparison the system derives from them, recorded on the candidate.
  componentCandidateRevise(projectId: string, componentId: string, candidateId: string, fields: { version: string; description: string; instructions: string; success_criteria: string[]; test_plan: string[] }, reason: string, expectedFingerprint: string): SendDisposition {
    return this.send({
      type: 'component_candidate_revise', project_id: this.requireProjectId(projectId, 'component_candidate_revise'),
      component_id: componentId, candidate_id: candidateId, fields, reason, expected_fingerprint: expectedFingerprint,
    })
  }

  componentCandidateEvaluationSpec(projectId: string, componentId: string, candidateId: string, expectedFingerprint: string, spec: { hypothesis: string; metrics: Array<{ name: string; min_delta: number }>; min_samples: number }): SendDisposition {
    return this.send({
      type: 'component_candidate_evaluation_spec', project_id: this.requireProjectId(projectId, 'component_candidate_evaluation_spec'),
      component_id: componentId, candidate_id: candidateId, expected_fingerprint: expectedFingerprint,
      hypothesis: spec.hypothesis, metrics: spec.metrics, min_samples: spec.min_samples,
    })
  }

  componentCandidateEvaluationSessions(projectId: string, componentId: string, candidateId: string, expectedFingerprint: string, variant: 'baseline' | 'candidate', sessionIds: string[]): SendDisposition {
    return this.send({
      type: 'component_candidate_evaluation_sessions', project_id: this.requireProjectId(projectId, 'component_candidate_evaluation_sessions'),
      component_id: componentId, candidate_id: candidateId, expected_fingerprint: expectedFingerprint, variant, session_ids: sessionIds,
    })
  }

  componentCandidateEvaluationRecord(projectId: string, componentId: string, candidateId: string, expectedFingerprint: string, baselineSessionIds: string[], candidateSessionIds: string[], note: string): SendDisposition {
    return this.send({
      type: 'component_candidate_evaluation_record', project_id: this.requireProjectId(projectId, 'component_candidate_evaluation_record'),
      component_id: componentId, candidate_id: candidateId, expected_fingerprint: expectedFingerprint,
      baseline_session_ids: baselineSessionIds, candidate_session_ids: candidateSessionIds, note,
    })
  }

  skillImprovementStage(projectId: string, candidateId: string): SendDisposition {
    return this.send({
      type: 'skill_improvement_stage', project_id: this.requireProjectId(projectId, 'skill_improvement_stage'),
      candidate_id: candidateId,
    })
  }

  skillImprovementActivate(projectId: string, candidateId: string, expectedFingerprint: string): SendDisposition {
    return this.send({
      type: 'skill_improvement_activate', project_id: this.requireProjectId(projectId, 'skill_improvement_activate'),
      candidate_id: candidateId, expected_fingerprint: expectedFingerprint,
    })
  }

  skillImprovementRollback(projectId: string, candidateId: string, expectedFingerprint: string): SendDisposition {
    return this.send({
      type: 'skill_improvement_rollback', project_id: this.requireProjectId(projectId, 'skill_improvement_rollback'),
      candidate_id: candidateId, expected_fingerprint: expectedFingerprint,
    })
  }

  connectorCandidateList(projectId: string): void {
    this.send({ type: 'connector_candidate_list', project_id: this.requireProjectId(projectId, 'connector_candidate_list') })
  }

  browserSessionList(projectId: string): void {
    this.send({ type: 'browser_session_list', project_id: this.requireProjectId(projectId, 'browser_session_list') })
  }

  browserAccountInspect(projectId: string, taskId: string, bindingKey: string, requestId: string): SendDisposition {
    const request = browserAccountInspectRequest(projectId, taskId, bindingKey, requestId)
    if (!request) return 'send-failed'
    // A view request belongs to the current card; reconnect must not replay it for a later session.
    return this.send({ type: 'browser_account_inspect', ...request }, { queue: false })
  }

  browserSessionControl(projectId: string, bindingKey: string, command: string, note = '', ok?: boolean, context?: BrowserControlContext): SendDisposition {
    const project = this.requireProjectId(projectId, 'browser_session_control')
    const observed = browserControlContext(context)
    if (!observed) return 'send-failed'
    return this.send({
      type: 'browser_session_control',
      project_id: project,
      binding_key: bindingKey,
      ...observed,
      command,
      note,
      ...(ok === undefined ? {} : { ok }),
    }, { queue: false })
  }

  /** Owner desktop takeover / hand-back. Never a model tool; queue:false like browser control. */
  desktopSessionControl(projectId: string, taskId: string, command: 'takeover' | 'hand_back' | 'status', note = ''): SendDisposition {
    const project = this.requireProjectId(projectId, 'desktop_session_control')
    const task = String(taskId || '').trim()
    if (!task) return 'send-failed'
    return this.send({
      type: 'desktop_session_control',
      project_id: project,
      task_id: task,
      command,
      note,
    }, { queue: false })
  }

  effectLookBack(projectId: string, effectId: string): SendDisposition {
    // The owner asks whether an effect whose outcome is unknown actually happened. A write of the
    // record (never a second apply), so it is not queued for a later turn.
    return this.send({ type: 'effect_look_back', project_id: this.requireProjectId(projectId, 'effect_look_back'), effect_id: effectId })
  }

  browserViewStart(projectId: string, bindingKey: string, context?: BrowserControlContext): SendDisposition {
    const project = this.requireProjectId(projectId, 'browser_view_start')
    const observed = browserControlContext(context)
    if (!observed) return 'send-failed'
    // A subscription belongs to the generation the person opened; reconnect must not replay it.
    return this.send({ type: 'browser_view_start', project_id: project, binding_key: bindingKey, ...observed }, { queue: false })
  }

  browserViewStop(projectId: string, bindingKey: string, context?: BrowserControlContext): SendDisposition {
    const project = this.requireProjectId(projectId, 'browser_view_stop')
    const observed = browserControlContext(context)
    if (!observed) return 'send-failed'
    return this.send({ type: 'browser_view_stop', project_id: project, binding_key: bindingKey, ...observed }, { queue: false })
  }

  browserViewInput(projectId: string, bindingKey: string, event: Record<string, unknown>, context?: BrowserControlContext): SendDisposition {
    const project = this.requireProjectId(projectId, 'browser_view_input')
    const observed = browserControlContext(context)
    if (!observed) return 'send-failed'
    // The person operating the page; never queued for a later turn -- a keystroke that could not be sent is dropped.
    return this.send({ type: 'browser_view_input', project_id: project, binding_key: bindingKey, event, ...observed }, { queue: false })
  }

  browserViewPick(projectId: string, bindingKey: string, x: number, y: number, context?: BrowserControlContext): SendDisposition {
    const project = this.requireProjectId(projectId, 'browser_view_pick')
    const observed = browserControlContext(context)
    if (!observed) return 'send-failed'
    // Read-only: what element is under the point. Not queued -- a pick that could not be sent is dropped.
    return this.send({ type: 'browser_view_pick', project_id: project, binding_key: bindingKey, x, y, ...observed }, { queue: false })
  }

  browserScreenshotRead(projectId: string, bindingKey: string, name: string): void {
    // A read: the session's own masked screenshot, by bare file name, served from its own directory.
    this.send({ type: 'browser_screenshot_read', project_id: this.requireProjectId(projectId, 'browser_screenshot_read'), binding_key: bindingKey, name })
  }

  browserAuditTrail(projectId: string, bindingKey: string): void {
    // A read: one session's whole story (Gateway events, submits, delegation, state) for the owner.
    this.send({ type: 'browser_audit_trail', project_id: this.requireProjectId(projectId, 'browser_audit_trail'), binding_key: bindingKey })
  }

  connectorCandidateTest(projectId: string, candidateId: string): SendDisposition {
    return this.send({
      type: 'connector_candidate_test',
      project_id: this.requireProjectId(projectId, 'connector_candidate_test'),
      candidate_id: candidateId,
      write_authorized: true,
    })
  }

  connectorCandidateReview(projectId: string, candidateId: string, expectedFingerprint: string, approved: boolean, note: string): SendDisposition {
    return this.send({
      type: 'connector_candidate_review',
      project_id: this.requireProjectId(projectId, 'connector_candidate_review'),
      candidate_id: candidateId,
      expected_fingerprint: expectedFingerprint,
      approved,
      note,
    })
  }

  connectorCandidateRegister(projectId: string, candidateId: string, expectedFingerprint: string): SendDisposition {
    return this.send({
      type: 'connector_candidate_register',
      project_id: this.requireProjectId(projectId, 'connector_candidate_register'),
      candidate_id: candidateId,
      expected_fingerprint: expectedFingerprint,
    })
  }

  connectorCandidateRevoke(projectId: string, connector: string, expectedFingerprint: string, reason: string): SendDisposition {
    return this.send({
      type: 'connector_candidate_revoke',
      project_id: this.requireProjectId(projectId, 'connector_candidate_revoke'),
      connector,
      expected_fingerprint: expectedFingerprint,
      reason,
    })
  }

  planRollout(projectId: string, solutionId: string, version: string, targets: Array<{ customer_id: string; environment_id: string }>): void {
    this.send({
      type: 'deployment_rollout_plan',
      project_id: this.requireProjectId(projectId, 'deployment_rollout_plan'),
      solution_id: solutionId, version, targets,
    })
  }

  advanceRollout(projectId: string, rolloutId: string, customerId: string, environmentId: string): void {
    this.send({
      type: 'deployment_rollout_advance',
      project_id: this.requireProjectId(projectId, 'deployment_rollout_advance'),
      rollout_id: rolloutId, customer_id: customerId, environment_id: environmentId,
    })
  }

  /** A verdict somebody gives, not one inferred from time passing. */
  rolloutHealth(projectId: string, rolloutId: string, customerId: string, environmentId: string, passed: boolean): void {
    this.send({
      type: 'deployment_rollout_health',
      project_id: this.requireProjectId(projectId, 'deployment_rollout_health'),
      rollout_id: rolloutId, customer_id: customerId, environment_id: environmentId,
      checks: [{ name: 'owner_review', result: passed ? 'passed' : 'failed' }],
    })
  }

  promoteRollout(projectId: string, rolloutId: string): void {
    this.send({
      type: 'deployment_rollout_promote',
      project_id: this.requireProjectId(projectId, 'deployment_rollout_promote'),
      rollout_id: rolloutId,
    })
  }

  rollbackRollout(projectId: string, rolloutId: string, note: string): void {
    this.send({
      type: 'deployment_rollout_rollback',
      project_id: this.requireProjectId(projectId, 'deployment_rollout_rollback'),
      rollout_id: rolloutId, note,
    })
  }

  /** Which connectors somebody has switched off, and why. */
  connectorSwitchList(projectId: string): void {
    this.send({ type: 'connector_switch_list', project_id: this.requireProjectId(projectId, 'connector_switch_list') })
  }

  /** Turns whose outcome nobody recorded; their sessions refuse everything until a person settles them. */
  harnessPendingList(projectId: string): void {
    this.send({ type: 'harness_pending_list', project_id: this.requireProjectId(projectId, 'harness_pending_list') })
  }
  /** Owner-only. Asks a pending turn to stop wherever it runs; honoured at its next progress event. */
  harnessTurnInterrupt(projectId: string, sessionId: string, requestId = '', expectedTurnId = ''): SendDisposition {
    return this.send({ type: 'harness_turn_interrupt', project_id: this.requireProjectId(projectId, 'harness_turn_interrupt'), session_id: sessionId, request_id: requestId, expected_turn_id: expectedTurnId })
  }
  /** Owner-only. One more instruction for the turn that is running right now. Bound to that exact turn, so a
   *  sentence typed for one turn never lands in the next; accepted means delivered, never "already applied". */
  harnessTurnSteer(projectId: string, sessionId: string, expectedTurnId: string, text: string, requestId: string): SendDisposition {
    return this.send({
      type: 'harness_turn_steer',
      project_id: this.requireProjectId(projectId, 'harness_turn_steer'),
      session_id: sessionId, expected_turn_id: expectedTurnId, text, request_id: requestId,
    })
  }
  /** Owner-only. Closes a lost turn as failed, with who and why; never as a pass. */
  /** Owner-only read: what the provider said it did in one turn, and which of it finished. Settles nothing. */
  harnessTurnActions(projectId: string, sessionId: string, turnId: string): void {
    this.send({ type: 'harness_turn_actions', project_id: this.requireProjectId(projectId, 'harness_turn_actions'), session_id: sessionId, turn_id: turnId })
  }

  harnessTurnSettle(projectId: string, sessionId: string, reason: string, expectedTurnId = ''): SendDisposition {
    return this.send({
      type: 'harness_turn_settle',
      project_id: this.requireProjectId(projectId, 'harness_turn_settle'),
      session_id: sessionId, reason, expected_turn_id: expectedTurnId,
    })
  }
  /** What is running where, plus anything being moved right now. */
  deploymentList(projectId: string): void {
    this.send({ type: 'deployment_list', project_id: this.requireProjectId(projectId, 'deployment_list') })
  }

  credentialReferenceList(projectId: string): void {
    this.send({ type: 'credential_reference_list', project_id: this.requireProjectId(projectId, 'credential_reference_list') })
  }

  /** What each order cost, saved and promised -- or which of those is unknown. */
  orderMetrics(projectId: string): void {
    this.send({ type: 'order_metrics', project_id: this.requireProjectId(projectId, 'order_metrics') })
  }

  runBudgetList(projectId: string): void {
    this.send({ type: 'run_budget_list', project_id: this.requireProjectId(projectId, 'run_budget_list') })
  }

  /** Owner-only: give an order that already exists its allowance. The order's customer and environment are the
   *  server's to read from the order; only the order and the ceilings are named here. */
  runBudgetCreate(projectId: string, orderId: string, limits: Record<string, number>): SendDisposition {
    // A grant is never queued: replayed after a reconnect it could land on an order that has one by then.
    return this.send({ type: 'run_budget_create', project_id: this.requireProjectId(projectId, 'run_budget_create'), order_id: orderId, limits })
  }

  /** Owner-only: take an order's allowance away, with the reason. The way out when a ceiling was reached or set wrong;
   *  the order then runs as one without an allowance until a new one is granted. */
  runBudgetClose(projectId: string, orderId: string, reason: string): SendDisposition {
    return this.send({ type: 'run_budget_close', project_id: this.requireProjectId(projectId, 'run_budget_close'), order_id: orderId, reason })
  }

  /** Owner-only: raise the ceilings of an order's open allowance in place. The budget keeps its id, so nothing that
   *  spends from it has to be told and the order is never without a ceiling in between. Never queued: replayed after
   *  a reconnect it could raise an allowance that was closed and granted anew by then. */
  runBudgetRaise(projectId: string, orderId: string, limits: Record<string, number>): SendDisposition {
    return this.send({ type: 'run_budget_raise', project_id: this.requireProjectId(projectId, 'run_budget_raise'), order_id: orderId, limits })
  }

  runBudgetResolve(projectId: string, reservationId: string, happened: boolean, actual?: number): void {
    this.send({ type: 'run_budget_resolve', project_id: this.requireProjectId(projectId, 'run_budget_resolve'), reservation_id: reservationId, happened, actual })
  }

  solutionBlueprintList(projectId: string): void {
    this.send({ type: 'solution_blueprint_list', project_id: this.requireProjectId(projectId, 'solution_blueprint_list') })
  }

  solutionBlueprintSave(projectId: string, orderId: string, name: string, note = ''): void {
    this.send({ type: 'solution_blueprint_save', project_id: this.requireProjectId(projectId, 'solution_blueprint_save'), order_id: orderId, name, note })
  }

  solutionBlueprintApply(projectId: string, blueprintId: string, orderId: string): void {
    this.send({ type: 'solution_blueprint_apply', project_id: this.requireProjectId(projectId, 'solution_blueprint_apply'), blueprint_id: blueprintId, order_id: orderId })
  }

  orderTimeline(projectId: string, orderId: string): void {
    this.send({ type: 'order_timeline', project_id: this.requireProjectId(projectId, 'order_timeline'), order_id: orderId })
  }

  channelConfigCheck(projectId: string): void {
    this.send({ type: 'channel_config_check', project_id: this.requireProjectId(projectId, 'channel_config_check') })
  }

  capabilityReprobe(projectId: string): void {
    this.send({ type: 'capability_reprobe', project_id: this.requireProjectId(projectId, 'capability_reprobe') })
  }

  /** references: what the owner pointed at (a flow, one of its steps, a component), checked by the server before
   *  the message is stored; a refused one comes back as ack error 'reference_invalid' with `problems`.
   *  attachments: files in the order chat's shape (base64 `data`), at most 5 and 20 MB together; a file the store
   *  refuses is named back in `attachment_problems`, and a message left with nothing is refused. */
  masterSend(projectId: string, content: string, references: MasterReference[] = [], attachments: MasterAttachment[] = [], threadId = ''): SendDisposition {
    const pid = this.requireProjectId(projectId, 'master_send')
    // Stamped so the server can tell a retry or a reconnect replay from a
    // new message and answer it once (A10). The id is the client's own.
    // thread_id (总控对话栏, policy): the conversation it goes to; without it, the project's first one.
    return this.send({
      type: 'master_send', project_id: pid, content, client_message_id: crypto.randomUUID(),
      ...(references.length ? { references } : {}),
      ...(attachments.length ? { attachments } : {}),
      ...(threadId ? { thread_id: threadId } : {}),
    })
  }

  /** 总控对话栏 (policy, 第七节): a project's 总控 conversations, answered in an ack `master_thread_list`
   * carrying `threads`. Read-only; it may wait for the connection. */
  masterThreadList(projectId: string): SendDisposition {
    return this.send({ type: 'master_thread_list', project_id: this.requireProjectId(projectId, 'master_thread_list') })
  }

  /** A new 总控 conversation in a project, answered in an ack `master_thread_create` carrying `thread`. 总控 asks for
   *  it on the first send of a new conversation, never before, so no empty conversation piles up. */
  masterThreadCreate(projectId: string): SendDisposition {
    return this.send({ type: 'master_thread_create', project_id: this.requireProjectId(projectId, 'master_thread_create') })
  }

  /** Rename one 总控 conversation; answered in an ack `master_thread_rename` carrying `thread`. */
  masterThreadRename(projectId: string, threadId: string, title: string): SendDisposition {
    return this.send({ type: 'master_thread_rename', project_id: this.requireProjectId(projectId, 'master_thread_rename'), thread_id: threadId, title })
  }

  /** 归档 (archived) or 恢复 one 总控 conversation; nothing is deleted. Answered in an ack `master_thread_archive`. */
  masterThreadArchive(projectId: string, threadId: string, archived: boolean): SendDisposition {
    return this.send({ type: 'master_thread_archive', project_id: this.requireProjectId(projectId, 'master_thread_archive'), thread_id: threadId, archived })
  }

  /** 安安 · 看图 (4.7 A13): stop reading one file of a stored 总控 message; the file is not given to the model. Never
   *  queued: replayed after a reconnect it would cancel a reading the owner may have let run since. */
  masterAttachmentCancel(projectId: string, messageId: string, attachmentId: string): SendDisposition {
    return this.send({
      type: 'master_attachment_cancel', project_id: this.requireProjectId(projectId, 'master_attachment_cancel'),
      message_id: messageId, attachment_id: attachmentId,
    })
  }

  /** 来源定位 (4.7 A13): one page of a file sent to 总控 (a PDF's page, or another file's text), answered in an ack
   *  `master_attachment_page` carrying `preview`. Read-only. */
  masterAttachmentPage(projectId: string, attachmentId: string, page = 0): SendDisposition {
    return this.send({
      type: 'master_attachment_page', project_id: this.requireProjectId(projectId, 'master_attachment_page'),
      attachment_id: attachmentId, page,
    })
  }

  blockersList(projectId: string): SendDisposition {
    const pid = this.requireProjectId(projectId, 'blockers_list')
    return this.send({ type: 'blockers_list', project_id: pid })
  }

  /** Top5 item4: task-center read (server flag AMPULY_TASK_CENTER_API / force_enable_for_test). */
  employeeTaskCenterRead(projectId: string, extra: Record<string, unknown> = {}): SendDisposition {
    const pid = this.requireProjectId(projectId, 'employee_task_center_read')
    return this.send({ type: 'employee_task_center_read', project_id: pid, ...extra })
  }

  /** Top5 item5: pending-approval badge read (server flag AMPULY_PENDING_APPROVAL_BADGE). */
  pendingApprovalBadgeRead(projectId: string, extra: Record<string, unknown> = {}): SendDisposition {
    const pid = this.requireProjectId(projectId, 'pending_approval_badge_read')
    return this.send({ type: 'pending_approval_badge_read', project_id: pid, ...extra })
  }

  /** 总控 · 今天 (S3-3B): the owner's cross-project 等你处理 -- the three counts and the rows -- answered in an ack
   *  `owner_attention_list` carrying `counts` and `items` (S3-3A). Read-only; owner only. Asked with the view
   *  generation, so a reply from before a project switch is dropped on that alone (the list spans projects). */
  ownerAttentionList(projectId: string, viewGeneration?: number, limit?: number): SendDisposition {
    return this.send({ type: 'owner_attention_list', project_id: this.requireProjectId(projectId, 'owner_attention_list'), view_generation: viewGeneration, ...(limit ? { limit } : {}) })
  }

  /** A deep link's card (S3-3B): where one pause lives, answered in an ack `checkpoint_locate` carrying
   * `{checkpoint_id, project_id, order_id}` (S3-3A), so the console can open that order in 业务工单 (policy:
   * 总控 draws no rows any more). Read-only. */
  checkpointLocate(projectId: string, checkpointId: string): SendDisposition {
    return this.send({ type: 'checkpoint_locate', project_id: this.requireProjectId(projectId, 'checkpoint_locate'), checkpoint_id: checkpointId })
  }

  /** A19: hold one pause's reminder until a local wall-clock time. Decides nothing. */
  blockerSnooze(projectId: string, checkpointId: string, untilAt: string, note = ''): void {
    this.send({ type: 'blocker_snooze', project_id: this.requireProjectId(projectId, 'blocker_snooze'), checkpoint_id: checkpointId, until_at: untilAt, note })
  }

  blockerUnsnooze(projectId: string, checkpointId: string): void {
    this.send({ type: 'blocker_unsnooze', project_id: this.requireProjectId(projectId, 'blocker_unsnooze'), checkpoint_id: checkpointId })
  }

  workItemRetry(projectId: string, workItemId: string, anchorTaskId?: string, reason?: string): void {
    this.send({ type: 'work_item_retry', project_id: this.requireProjectId(projectId, 'work_item_retry'), work_item_id: workItemId, anchor_task_id: anchorTaskId ?? '', retry_reason: String(reason ?? 'manual_retry').trim() || 'manual_retry' })
  }

  gateDetail(projectId: string, checkpointId: string): void {
    this.send({ type: 'gate_detail', project_id: this.requireProjectId(projectId, 'gate_detail'), checkpoint_id: checkpointId })
  }

  gateDecide(projectId: string, decision: Record<string, unknown>): SendDisposition {
    return this.send({ type: 'gate_decide', project_id: this.requireProjectId(projectId, 'gate_decide'), ...decision })
  }

  gateStatus(projectId: string): void {
    this.send({ type: 'gate_status', project_id: this.requireProjectId(projectId, 'gate_status') })
  }

  dutyLogList(projectId: string): void {
    const pid = this.requireProjectId(projectId, 'duty_log_list')
    this.send({ type: 'duty_log_list', project_id: pid })
  }

  // ── Console accounts (子账号) ──────────────────────────────────────

  authLogin(projectId: string, login: string, password: string): void {
    this.send({ type: 'auth_login', project_id: this.requireProjectId(projectId, 'auth_login'), login, password })
  }

  authToken(projectId: string, token: string): void {
    this.send({ type: 'auth_token', project_id: this.requireProjectId(projectId, 'auth_token'), token })
  }

  authLogout(projectId: string, token: string): void {
    this.send({ type: 'auth_logout', project_id: this.requireProjectId(projectId, 'auth_logout'), token })
  }

  accountList(projectId: string): void {
    this.send({ type: 'account_list', project_id: this.requireProjectId(projectId, 'account_list') })
  }

  accountBootstrapOwner(projectId: string, login: string, password: string, displayName: string): void {
    this.send({ type: 'account_bootstrap_owner', project_id: this.requireProjectId(projectId, 'account_bootstrap_owner'), login, password, display_name: displayName })
  }

  accountCreate(projectId: string, login: string, password: string, employeeId: string, displayName: string): void {
    this.send({ type: 'account_create', project_id: this.requireProjectId(projectId, 'account_create'), login, password, employee_id: employeeId, display_name: displayName })
  }

  accountSetPassword(projectId: string, login: string, password: string): void {
    this.send({ type: 'account_set_password', project_id: this.requireProjectId(projectId, 'account_set_password'), login, password })
  }

  accountSetDisabled(projectId: string, login: string, disabled: boolean): void {
    this.send({ type: 'account_set_disabled', project_id: this.requireProjectId(projectId, 'account_set_disabled'), login, disabled })
  }

  employeeAdd(projectId: string, payload: { name: string; role_id: string; kind: string; supervisor_employee_id?: string; contact?: string }): void {
    this.send({ type: 'employee_add', project_id: this.requireProjectId(projectId, 'employee_add'), ...payload })
  }

  /** 人员与职责 · 联系方式 (4.7-A07): the contact book, members' bound channels included (lib/contactBook.ts). */
  contactList(projectId: string): void {
    this.send({ type: 'contact_list', project_id: this.requireProjectId(projectId, 'contact_list') })
  }

  /** Owner only (the server refuses anyone else): make a member's entry if needed and bind one channel identity. */
  contactBind(projectId: string, payload: { contact_id: string; customer_id: string; display_name: string; channel: string; identity: string }): SendDisposition {
    return this.send({ type: 'contact_bind', project_id: this.requireProjectId(projectId, 'contact_bind'), ...payload })
  }

  /** 配置 · 渠道连接 「企业微信（微信客服）」 (W3b), owner only (the server refuses anyone else): where the channel stands. */
  /** 4.8 handoffs: the server record is the only fact; decisions are explicit. */
  employeeHandoffList(projectId: string, handoffStatus = '', offset = 0, taskId = ''): SendDisposition {
    return this.send({ type: 'employee_handoff_list', project_id: this.requireProjectId(projectId, 'employee_handoff_list'), status: handoffStatus, offset, task_id: taskId })
  }
  employeeHandoffDecide(projectId: string, handoffId: string, decision: 'accept' | 'reject', text: string): SendDisposition {
    return this.send({ type: 'employee_handoff_decide', project_id: this.requireProjectId(projectId, 'employee_handoff_decide'), handoff_id: handoffId, decision, ...(decision === 'reject' ? { reason: text } : { note: text }) })
  }

  employeeHandoffRework(projectId: string, handoffId: string, instruction: string): SendDisposition {
    return this.send({ type: 'employee_handoff_rework', project_id: this.requireProjectId(projectId, 'employee_handoff_rework'), handoff_id: handoffId, instruction })
  }

  employeeHandoffContinue(projectId: string, handoffId: string, instruction: string, instanceId = ''): SendDisposition {
    const instance_id = instanceId.trim()
    return this.send({
      type: 'employee_handoff_continue',
      project_id: this.requireProjectId(projectId, 'employee_handoff_continue'),
      handoff_id: handoffId,
      instruction,
      ...(instance_id ? { instance_id } : {}),
    })
  }

  /** Owner only: re-read the origin check. Does not accept the handoff. */
  employeeHandoffCheck(projectId: string, handoffId: string): SendDisposition {
    return this.send({
      type: 'employee_handoff_check',
      project_id: this.requireProjectId(projectId, 'employee_handoff_check'),
      handoff_id: handoffId,
    })
  }

  /** Owner only: handoffs produced for one workflow node. */
  employeeHandoffNodeList(projectId: string, nodeId: string, handoffStatus = ''): SendDisposition {
    return this.send({
      type: 'employee_handoff_node_list',
      project_id: this.requireProjectId(projectId, 'employee_handoff_node_list'),
      node_id: nodeId,
      status: handoffStatus,
    })
  }

  /** 客服验收 (4.8): one owner-only suite/channel action; the server owns every result. */
  customerServicePrepare(projectId: string, params: Record<string, unknown>): SendDisposition {
    return this.send({ type: 'customer_service_prepare', action: 'customer_service_prepare', project_id: this.requireProjectId(projectId, 'customer_service_prepare'), params })
  }
  customerServiceDetail(projectId: string, suiteId: string): SendDisposition {
    return this.send({ type: 'customer_service_detail', action: 'customer_service_detail', project_id: this.requireProjectId(projectId, 'customer_service_detail'), params: { suite_id: suiteId } })
  }
  customerServiceTestStart(projectId: string, suiteId: string, budget: Record<string, unknown>): SendDisposition {
    return this.send({ type: 'customer_service_test_start', action: 'customer_service_test_start', project_id: this.requireProjectId(projectId, 'customer_service_test_start'), params: { suite_id: suiteId, budget } })
  }
  customerServiceReview(projectId: string, params: Record<string, unknown>): SendDisposition {
    return this.send({ type: 'customer_service_review', action: 'customer_service_review', project_id: this.requireProjectId(projectId, 'customer_service_review'), params })
  }
  customerServiceAdmit(projectId: string, suiteId: string): SendDisposition {
    return this.send({ type: 'customer_service_admit', action: 'customer_service_admit', project_id: this.requireProjectId(projectId, 'customer_service_admit'), params: { suite_id: suiteId } })
  }
  customerServiceRevoke(projectId: string, suiteId: string, reasonCode: string): SendDisposition {
    return this.send({ type: 'customer_service_revoke', action: 'customer_service_revoke', project_id: this.requireProjectId(projectId, 'customer_service_revoke'), params: { suite_id: suiteId, reason_code: reasonCode } })
  }
  customerServiceChannelSelect(projectId: string, suiteId: string, budget: Record<string, unknown>): SendDisposition {
    return this.send({ type: 'customer_service_channel_select', action: 'customer_service_channel_select', project_id: this.requireProjectId(projectId, 'customer_service_channel_select'), params: { suite_id: suiteId, budget } })
  }
  customerServiceChannelPause(projectId: string, accountId: string): SendDisposition {
    return this.send({ type: 'customer_service_channel_pause', action: 'customer_service_channel_pause', project_id: this.requireProjectId(projectId, 'customer_service_channel_pause'), params: { account_id: accountId } })
  }
  customerServiceChannelReplace(projectId: string, suiteId: string, budget: Record<string, unknown>, expectedRecordSha256: string): SendDisposition {
    return this.send({ type: 'customer_service_channel_replace', action: 'customer_service_channel_replace', project_id: this.requireProjectId(projectId, 'customer_service_channel_replace'), params: { suite_id: suiteId, budget, expected_record_sha256: expectedRecordSha256 } })
  }

  wecomChannelStatus(projectId: string): SendDisposition {
    return this.send({ type: 'wecom_channel_status', project_id: this.requireProjectId(projectId, 'wecom_channel_status') })
  }

  /** The owner's switch for the whole 企业微信 channel (default off). */
  wecomChannelSet(projectId: string, enabled: boolean, reason = ''): SendDisposition {
    return this.send({ type: 'wecom_channel_set', project_id: this.requireProjectId(projectId, 'wecom_channel_set'), enabled, reason })
  }

  /** Bind one 客服账号 to this project and one of its flows, or unbind it (`remove: true`). */
  wecomKfAccountSet(projectId: string, payload: Record<string, unknown>): SendDisposition {
    return this.send({ ...payload, type: 'wecom_kf_account_set', project_id: this.requireProjectId(projectId, 'wecom_kf_account_set') })
  }

  myTasksList(projectId: string): void {
    this.send({ type: 'my_tasks_list', project_id: this.requireProjectId(projectId, 'my_tasks_list') })
  }

  customerViewPreview(projectId: string, orderId: string, requestId: string): SendDisposition {
    return this.send({ type: 'customer_view_preview', project_id: this.requireProjectId(projectId, 'customer_view_preview'), order_id: orderId, request_id: requestId })
  }

  /** Where every visible project's orders stand (read only; the server refuses a customer-bound account). */
  projectOrderOverview(projectId: string): void {
    this.send({ type: 'project_order_overview', project_id: this.requireProjectId(projectId, 'project_order_overview') })
  }

  // ── 项目中心 · 业务边界与接入 (S3-2A / S3-2B) ─────────────────────────────

  /** 本项目的业务边界: type / start / scope / end / manager, as the project records it. Read only. */
  projectProfileGet(projectId: string): void {
    this.send({ type: 'project_profile_get', project_id: this.requireProjectId(projectId, 'project_profile_get') })
  }

  /** 保存业务说明: the project's own business note (the four fields flat, as the handler reads them). It changes no
   *  flow, no permission and no order in flight. */
  projectProfileSet(projectId: string, profile: { start: string; scope: string; end: string; manager_employee_id: string }): SendDisposition {
    return this.send({
      type: 'project_profile_set', project_id: this.requireProjectId(projectId, 'project_profile_set'),
      start: profile.start, scope: profile.scope, end: profile.end, manager_employee_id: profile.manager_employee_id,
    })
  }

  /** 软件与连接: the four lanes (业务来源 / 内部协作 / 客户确认 / 交付存储) as planned for this project. Read only. */
  projectConnectionsGet(projectId: string): void {
    this.send({ type: 'project_connections_get', project_id: this.requireProjectId(projectId, 'project_connections_get') })
  }

  /** 保存交接规划 for one lane (`lanes: [{lane, tool, purpose, mode, stage}]`, as the handler reads them). Saving a
   *  plan is not connecting: no credential, no connection, no message. */
  projectConnectionsSet(projectId: string, connection: { id: string; tool: string; purpose: string; mode: string; stage: string }): SendDisposition {
    return this.send({
      type: 'project_connections_set', project_id: this.requireProjectId(projectId, 'project_connections_set'),
      lanes: [{ lane: connection.id, tool: connection.tool, purpose: connection.purpose, mode: connection.mode, stage: connection.stage }],
    })
  }

  /** 4.7-A08: always asks for pages (`page: true`), so every reply carries `next_cursor`; `before` asks for the page
   *  older than that cursor (「查看更早的工单」). */
  orderList(projectId: string, scope?: { customer_id?: string; environment_id?: string }, before = ''): SendDisposition {
    const payload: Record<string, unknown> = {
      type: 'order_list',
      project_id: this.requireProjectId(projectId, 'order_list'),
      page: true,
    }
    const customerId = String(scope?.customer_id ?? '').trim()
    const environmentId = String(scope?.environment_id ?? '').trim()
    if (customerId) payload.customer_id = customerId
    if (environmentId) payload.environment_id = environmentId
    if (before) payload.before = before
    return this.send(payload)
  }

  /** 4.7-A08 分发授权单 (owner only): list / draft / approve / revoke one order's authorizations. Every reply carries
   *  the order's whole list again, so the card redraws from it. */
  distributionAuthorization(
    projectId: string,
    verb: 'list' | 'draft' | 'approve' | 'revoke',
    payload: { order_id: string } & Record<string, unknown>,
  ): SendDisposition {
    // One literal per action, so the product's list of callers can see each of the four routes being sent.
    const pid = this.requireProjectId(projectId, `distribution_authorization_${verb}`)
    switch (verb) {
      case 'list': return this.send({ type: 'distribution_authorization_list', project_id: pid, ...payload })
      case 'draft': return this.send({ type: 'distribution_authorization_draft', project_id: pid, ...payload })
      case 'approve': return this.send({ type: 'distribution_authorization_approve', project_id: pid, ...payload })
      case 'revoke': return this.send({ type: 'distribution_authorization_revoke', project_id: pid, ...payload })
    }
  }

  /** Owner-only. Suspends the run, asks its turns to stop, closes the order with the reason; effects are not rolled back. */
  /**
   * A cancel stops a run and closes an order. Typed while the console is offline it must not run on its own after a
   * reconnect, so it is never queued. `cascadeChildren` also cancels the orders this order's run started.
   */
  orderCancel(projectId: string, orderId: string, reason: string, cascadeChildren = false): SendDisposition {
    return this.send({
      type: 'order_cancel', project_id: this.requireProjectId(projectId, 'order_cancel'), order_id: orderId, reason,
      ...(cascadeChildren ? { cascade_children: true } : {}),
    })
  }
  orderReview(projectId: string, orderId: string, decision: 'approve' | 'reject', note: string): void {
    this.send({ type: 'order_review', project_id: this.requireProjectId(projectId, 'order_review'), order_id: orderId, decision, note })
  }

  /** Who handles an order, by when, for how much (owner). One signed event per change; '' clears a value. */
  orderHandlingUpdate(projectId: string, orderId: string, patch: Record<string, unknown>): SendDisposition {
    return this.send({ type: 'order_handling_update', project_id: this.requireProjectId(projectId, 'order_handling_update'), order_id: orderId, patch })
  }

  fdeControlPlaneUpdate(projectId: string, orderId: string, patch: Record<string, unknown>): void {
    const pid = this.requireProjectId(projectId, 'fde_control_plane_update')
    this.send({ type: 'fde_control_plane_update', project_id: pid, order_id: orderId, patch })
  }

  acceptanceGet(projectId: string, orderId: string): void {
    this.send({ type: 'acceptance_get', project_id: this.requireProjectId(projectId, 'acceptance_get'), order_id: orderId })
  }

  acceptanceEvidence(projectId: string, orderId: string, criterion: string, evidence: Record<string, unknown>): SendDisposition {
    return this.send({ type: 'acceptance_evidence', project_id: this.requireProjectId(projectId, 'acceptance_evidence'), order_id: orderId, criterion, evidence })
  }

  acceptanceSubmit(projectId: string, orderId: string): SendDisposition {
    return this.send({ type: 'acceptance_submit', project_id: this.requireProjectId(projectId, 'acceptance_submit'), order_id: orderId })
  }

  acceptanceDecide(projectId: string, orderId: string, decision: 'accepted' | 'rejected' | 'accepted_with_exceptions', note = '', exceptions: string[] = [], options: { closeOrder?: boolean } = {}): SendDisposition {
    // closeOrder: 确认收货并结案 -- the server records the owner's receipt as evidence where none was, submits, decides
    // and closes the order in one go (ws_handler._handle_acceptance_decide, close_order).
    return this.send({ type: 'acceptance_decide', project_id: this.requireProjectId(projectId, 'acceptance_decide'), order_id: orderId, decision, note, exceptions, ...(options.closeOrder ? { close_order: true } : {}) })
  }

  acceptanceRework(projectId: string, orderId: string, targetNode: string, requestId: string): SendDisposition {
    return this.send({ type: 'acceptance_rework', project_id: this.requireProjectId(projectId, 'acceptance_rework'), order_id: orderId, target_node: targetNode, request_id: requestId })
  }

  acceptanceRoundExport(projectId: string, orderId: string, roundNo: number): void {
    this.send({ type: 'acceptance_round_export', project_id: this.requireProjectId(projectId, 'acceptance_round_export'), order_id: orderId, round_no: roundNo })
  }

  portfolioExport(projectId: string, orderId: string): void {
    this.send({ type: 'portfolio_export', project_id: this.requireProjectId(projectId, 'portfolio_export'), order_id: orderId })
  }

  /** P5-08: record the customer's explicit redistribution grant on their accepted order. Only the order is named:
   *  the workflow, its exact version, fingerprint and manifest digest are bound by the server. */
  workflowRedistributionAuthorize(projectId: string, orderId: string, note: string): SendDisposition {
    // A local write: never queue and replay it after reconnecting into a later operator context.
    return this.send({ type: 'workflow_redistribution_authorize', project_id: this.requireProjectId(projectId, 'workflow_redistribution_authorize'), order_id: orderId, note })
  }

  /** P5-09: export one shareable published version as a signed distribution candidate. The key lives on the server. */
  workflowDistributionExport(projectId: string, workflowId: string): SendDisposition {
    // A local write: never queue and replay it after reconnecting into a later operator context.
    return this.send({ type: 'workflow_distribution_export', project_id: this.requireProjectId(projectId, 'workflow_distribution_export'), workflow_id: workflowId })
  }

  /** P5-09: verify a received candidate and record the owner's decision; an accepted one lands as a draft only. */
  workflowDistributionReceive(projectId: string, path: string, decision: 'accept' | 'reject', note: string): SendDisposition {
    // A local write: never queue and replay it after reconnecting into a later operator context.
    return this.send({ type: 'workflow_distribution_receive', project_id: this.requireProjectId(projectId, 'workflow_distribution_receive'), path, decision, note })
  }

  workflowDistributionInbox(projectId: string): void {
    this.send({ type: 'workflow_distribution_inbox', project_id: this.requireProjectId(projectId, 'workflow_distribution_inbox') })
  }

  /** The governance policy as the runtime reads it: which fields take effect, which are only stored, and who changed what. */
  governancePolicyList(projectId: string): void {
    this.send({ type: 'governance_policy_list', project_id: this.requireProjectId(projectId, 'governance_policy_list') })
  }

  /** Owner-only. One policy field, with the reason the server records beside the change. */
  governancePolicySet(projectId: string, field: string, value: boolean, reason: string): SendDisposition {
    // A policy write is never queued: replayed later it would be a decision nobody made at that moment.
    return this.send({ type: 'governance_policy_set', project_id: this.requireProjectId(projectId, 'governance_policy_set'), field, value, reason })
  }

  /** X07, owner-only: compile a sealed demonstration into a candidate, admit one, or list them. Approval references
   *  name approval records by id only; the server reads the facts itself. */
  browserCandidateControl(
    projectId: string,
    command: 'compile' | 'admit' | 'list',
    args: { demoId?: string; candidateId?: string; approvalRefs?: Record<string, string> } = {},
  ): SendDisposition {
    const message: Record<string, unknown> = {
      type: 'browser_candidate_control', project_id: this.requireProjectId(projectId, 'browser_candidate_control'), command,
    }
    if (command === 'compile') message.demo_id = args.demoId ?? ''
    if (command === 'admit') {
      message.candidate_id = args.candidateId ?? ''
      message.approval_refs = args.approvalRefs ?? {}
    }
    return this.send(message)
  }

  workflowList(projectId: string): void {
    const pid = this.requireProjectId(projectId, 'workflow_list')
    this.send({ type: 'workflow_list', project_id: pid, include_testing: true })
  }

  workflowPublish(projectId: string, workflowId: string, expectedRevision: string): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_publish')
    if (typeof expectedRevision !== 'string' || !expectedRevision.trim()) return 'send-failed'
    return this.send({ type: 'workflow_publish', project_id: pid, workflow_id: workflowId, expected_revision: expectedRevision })
  }

  workflowRollback(projectId: string, workflowId: string, expectedRevision: string): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_rollback')
    if (typeof expectedRevision !== 'string' || !expectedRevision.trim()) return 'send-failed'
    return this.send({ type: 'workflow_rollback', project_id: pid, workflow_id: workflowId, expected_revision: expectedRevision })
  }

  workflowRolloutStart(projectId: string, workflowId: string, expectedRevision: string, policy: { canary_percent?: number; min_observations?: number; max_failure_rate?: number } = {}): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_rollout_start')
    if (typeof expectedRevision !== 'string' || !expectedRevision.trim()) return 'send-failed'
    return this.send({ type: 'workflow_rollout_start', project_id: pid, workflow_id: workflowId, ...policy, expected_revision: expectedRevision })
  }

  workflowRolloutObserve(projectId: string, workflowId: string, expectedRevision: string, outcome: 'passed' | 'failed' | 'unknown', note = '', runId = ''): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_rollout_observe')
    if (typeof expectedRevision !== 'string' || !expectedRevision.trim()) return 'send-failed'
    return this.send({ type: 'workflow_rollout_observe', project_id: pid, workflow_id: workflowId, outcome, note, run_id: runId, expected_revision: expectedRevision })
  }

  workflowRolloutEvaluate(projectId: string, workflowId: string): void {
    const pid = this.requireProjectId(projectId, 'workflow_rollout_evaluate')
    this.send({ type: 'workflow_rollout_evaluate', project_id: pid, workflow_id: workflowId })
  }

  workflowRolloutFinalize(projectId: string, workflowId: string, expectedRevision: string): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_rollout_finalize')
    if (typeof expectedRevision !== 'string' || !expectedRevision.trim()) return 'send-failed'
    return this.send({ type: 'workflow_rollout_finalize', project_id: pid, workflow_id: workflowId, expected_revision: expectedRevision })
  }

  workflowRolloutRollback(projectId: string, workflowId: string, expectedRevision: string): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_rollout_rollback')
    if (typeof expectedRevision !== 'string' || !expectedRevision.trim()) return 'send-failed'
    return this.send({ type: 'workflow_rollout_rollback', project_id: pid, workflow_id: workflowId, expected_revision: expectedRevision })
  }

  workflowRecordTest(projectId: string, workflowId: string, expectedRevision: string, payload: {
    outcome: 'passed' | 'failed' | 'stopped'
    notes?: string
    failure_reason?: string
    duration_minutes?: number
    model_calls?: number
    cost_cny_estimate?: number
  }): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_record_test')
    if (typeof expectedRevision !== 'string' || !expectedRevision.trim()) return 'send-failed'
    return this.send({ type: 'workflow_record_test', project_id: pid, workflow_id: workflowId, ...payload, expected_revision: expectedRevision })
  }

  workflowSimulateTest(projectId: string, workflowId: string, expectedRevision: string, fields: Record<string, string>): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_simulate_test')
    if (typeof expectedRevision !== 'string' || !expectedRevision.trim()) return 'send-failed'
    return this.send({ type: 'workflow_simulate_test', project_id: pid, workflow_id: workflowId, fields, expected_revision: expectedRevision })
  }

  workflowSubmitTesting(projectId: string, workflowId: string, expectedRevision: string, changeNote = ''): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_submit_testing')
    if (typeof expectedRevision !== 'string' || !expectedRevision.trim()) return 'send-failed'
    return this.send({ type: 'workflow_submit_testing', project_id: pid, workflow_id: workflowId, change_note: changeNote, expected_revision: expectedRevision })
  }

  workflowCreateDraft(projectId: string, workflowId: string, draftId: string, name = '', summary = '', changeNote = ''): void {
    const pid = this.requireProjectId(projectId, 'workflow_create_draft')
    this.send({
      type: 'workflow_create_draft',
      project_id: pid,
      workflow_id: workflowId,
      draft_id: draftId,
      name,
      summary,
      change_note: changeNote,
    })
  }

  /** 项目采用业务流: what this project adopted, with the recent events. */
  projectWorkflowAdoptions(projectId: string): void {
    this.send({ type: 'project_workflow_adoptions', project_id: this.requireProjectId(projectId, 'project_workflow_adoptions') })
  }

  /** Adopt the published version of a flow in this project (again, with a newer one, to upgrade). */
  projectWorkflowAdopt(projectId: string, workflowId: string, note = ''): void {
    this.send({ type: 'project_workflow_adopt', project_id: this.requireProjectId(projectId, 'project_workflow_adopt'), workflow_id: workflowId, note })
  }

  /** 暂停接单 (paused) / 恢复 (active) / 撤下 (withdrawn) an adopted flow; the pinned version stays. */
  projectWorkflowAdoptionStatus(projectId: string, workflowId: string, status: 'active' | 'paused' | 'withdrawn', note = ''): void {
    this.send({
      type: 'project_workflow_adoption_status', project_id: this.requireProjectId(projectId, 'project_workflow_adoption_status'),
      workflow_id: workflowId, status, note,
    })
  }

  /** 在搭建台打开 a 安安 flow draft: the console compiles the spec stored on reply `messageId` again (no steps are sent
   *  from here), creates an empty draft `draftId` on the saved team `team` and answers with the steps for the canvas. */
  workflowCreateFromFlowDraft(projectId: string, messageId: string, draftId: string, team: string, sourceReference?: unknown, expectedMaterialized?: unknown): void {
    this.send({
      type: 'workflow_create_from_flow_draft',
      project_id: this.requireProjectId(projectId, 'workflow_create_from_flow_draft'),
      message_id: messageId, draft_id: draftId, team,
      ...(sourceReference ? { source_reference: sourceReference } : {}),
      ...(expectedMaterialized ? { expected_materialized: expectedMaterialized } : {}),
    })
  }

  /** 流程版本面板: this flow's published versions and which project runs which. Read-only; owner only. */
  workflowVersions(projectId: string, workflowId: string): SendDisposition {
    return this.send({ type: 'workflow_versions', project_id: this.requireProjectId(projectId, 'workflow_versions'), workflow_id: workflowId })
  }

  /** 派生为本项目草稿 (policy): a draft of a global flow that belongs to this project. The server picks the id
   * and hands back the one already derived for this project if there is one. Nothing runs or is adopted. */
  workflowDeriveForProject(projectId: string, workflowId: string): SendDisposition {
    return this.send({
      type: 'workflow_derive_for_project',
      project_id: this.requireProjectId(projectId, 'workflow_derive_for_project'),
      workflow_id: workflowId,
    })
  }

  /** ＋ 新建业务流: an empty draft (no steps, one intake field) run by the saved team `org`. Nothing runs. */
  workflowCreateBlank(projectId: string, draftId: string, org: string, name = '', summary = ''): void {
    this.send({
      type: 'workflow_create_blank',
      project_id: this.requireProjectId(projectId, 'workflow_create_blank'),
      draft_id: draftId,
      org,
      name,
      summary,
    })
  }

  workflowRequestRelease(projectId: string, workflowId: string, expectedRevision: string, note = ''): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_request_release')
    if (typeof expectedRevision !== 'string' || !expectedRevision.trim()) return 'send-failed'
    return this.send({ type: 'workflow_request_release', project_id: pid, workflow_id: workflowId, note, expected_revision: expectedRevision })
  }

  workflowDecideRelease(projectId: string, workflowId: string, expectedRevision: string, decision: 'approved' | 'rejected', note = ''): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_decide_release')
    if (typeof expectedRevision !== 'string' || !expectedRevision.trim()) return 'send-failed'
    return this.send({ type: 'workflow_decide_release', project_id: pid, workflow_id: workflowId, decision, note, expected_revision: expectedRevision })
  }

  /** Owner-only. A person stands behind a drafted skill: reviewed_by/at go into its file, status draft comes off. */
  workflowSkillReview(projectId: string, workflowId: string, expectedRevision: string, skillId: string, note = ''): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_skill_review')
    if (typeof expectedRevision !== 'string' || !expectedRevision.trim()) return 'send-failed'
    return this.send({ type: 'workflow_skill_review', project_id: pid,
      workflow_id: workflowId, skill_id: skillId, note, expected_revision: expectedRevision })
  }
  /** expectedRevision is the package's draft_revision the draft was made against; the server refuses a save
   *  whose draft was saved since, even at the same version (a layout-only save keeps the version). */
  workflowUpdateNodes(projectId: string, workflowId: string, nodes: Record<string, unknown>[], expectedVersion: string, newVersion: string, changeNote = '', expectedRevision = ''): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_update_nodes')
    return this.send({
      type: 'workflow_update_nodes',
      project_id: pid,
      workflow_id: workflowId,
      nodes,
      expected_version: expectedVersion,
      new_version: newVersion,
      change_note: changeNote,
      ...(expectedRevision ? { expected_revision: expectedRevision } : {}),
    })
  }

  workflowPreflight(projectId: string, workflowId: string, fields: Record<string, string>, nodeId = ''): void {
    const pid = this.requireProjectId(projectId, 'workflow_preflight')
    this.send({ type: 'workflow_preflight', project_id: pid, workflow_id: workflowId, fields, node_id: nodeId })
  }

  browserAuditExport(projectId: string, bindingKey: string): SendDisposition {
    // A local write: never queue and replay it after reconnecting into a later operator context.
    return this.send({ type: 'browser_audit_export', project_id: this.requireProjectId(projectId, 'browser_audit_export'), binding_key: bindingKey })
  }

  workflowNodeTrialList(projectId: string, workflowId = '', nodeId = ''): void {
    const pid = this.requireProjectId(projectId, 'workflow_node_trial_list')
    this.send({ type: 'workflow_node_trial_list', project_id: pid, workflow_id: workflowId, node_id: nodeId })
  }

  /** A person's answer to what a step asked inside a node trial. It continues a real Provider run: never queued
   *  for a later connection. */
  workflowNodeTrialAnswer(projectId: string, workflowId: string, specId: string, reply: string): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_node_trial_answer')
    return this.send({ type: 'workflow_node_trial_answer', project_id: pid, workflow_id: workflowId, spec_id: specId, reply })
  }

  /** Stop waiting for that answer: closes the trial's allowance. A write, so never queued either. */
  workflowNodeTrialAbandon(projectId: string, specId: string): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_node_trial_abandon')
    return this.send({ type: 'workflow_node_trial_abandon', project_id: pid, spec_id: specId })
  }

  /** One file a node trial left, through the deliverable preview. A read: safe to send whenever connected. */
  workflowNodeTrialArtifact(projectId: string, specId: string, path: string): void {
    const pid = this.requireProjectId(projectId, 'workflow_node_trial_artifact')
    this.send({ type: 'workflow_node_trial_artifact', project_id: pid, spec_id: specId, path })
  }

  workflowNodeTrialStart(projectId: string, workflowId: string, workflowVersion: string, nodeId: string, requestId: string, modelCallLimit = 12, pins: Array<{ node_id: string; spec_id: string }> = [], imageLimit?: number): SendDisposition {
    // A real Provider turn is a mutation. Never queue it for a later
    // connection: request_id makes a deliberate retry idempotent, not automatic.
    const pid = this.requireProjectId(projectId, 'workflow_node_trial_start')
    return this.send({
      type: 'workflow_node_trial_start', project_id: pid, workflow_id: workflowId,
      workflow_version: workflowVersion, node_id: nodeId, request_id: requestId,
      model_call_limit: modelCallLimit,
      // Left out when empty: a root step is tried on nothing, and says so by not naming anything.
      ...(pins.length > 0 ? { pins } : {}),
      // Only for a step that generates images: how many this trial may make. The server bounds it (0..6).
      ...(imageLimit !== undefined ? { image_limit: imageLimit } : {}),
    })
  }

  /** Whole saved draft or exact published employee pin tried once (every AI step in dependency order). Same product path as
   *  run_flow_trial / WS workflow_flow_trial_start. A real Provider run: never queued for later. */
  workflowFlowTrialStart(projectId: string, workflowId: string, workflowVersion: string, requestId: string, modelCallLimitPerNode = 12, totalModelCallLimit?: number, newRound = false, published?: { packageId: string; fingerprint: string }): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_flow_trial_start')
    return this.send({
      type: 'workflow_flow_trial_start', project_id: pid, workflow_id: workflowId,
      workflow_version: workflowVersion, request_id: requestId,
      model_call_limit_per_node: modelCallLimitPerNode,
      ...(totalModelCallLimit !== undefined ? { total_model_call_limit: totalModelCallLimit } : {}),
      ...(newRound ? { new_round: true } : {}),
      ...(published ? { employee_package_id: published.packageId, employee_package_fingerprint: published.fingerprint } : {}),
    })
  }

  workflowFlowTrialContinue(projectId: string, workflowId: string, flowId: string): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_flow_trial_continue')
    return this.send({
      type: 'workflow_flow_trial_continue', project_id: pid, workflow_id: workflowId, flow_id: flowId,
    })
  }

  workflowFlowTrialStop(projectId: string, workflowId: string, flowId: string): SendDisposition {
    const pid = this.requireProjectId(projectId, 'workflow_flow_trial_stop')
    return this.send({
      type: 'workflow_flow_trial_stop', project_id: pid, workflow_id: workflowId, flow_id: flowId,
    })
  }

  /** Owner-only list of whole-flow formal trials for a project (optionally one workflow). A read. */
  workflowFlowTrialList(projectId: string, workflowId = ''): void {
    const pid = this.requireProjectId(projectId, 'workflow_flow_trial_list')
    this.send({ type: 'workflow_flow_trial_list', project_id: pid, workflow_id: workflowId })
  }

  /** `budgetLimits`, when given, is the order's allowance: granted before the run starts, so the whole run spends
   *  from it. Left out entirely when empty -- an empty object would read as "a budget with no ceiling". */
  workflowInvoke(projectId: string, workflowId: string, fields: Record<string, string>, budgetLimits?: Record<string, number>): void {
    const pid = this.requireProjectId(projectId, 'workflow_invoke')
    const message: Record<string, unknown> = { type: 'workflow_invoke', project_id: pid, workflow_id: workflowId, fields }
    if (budgetLimits && Object.keys(budgetLimits).length > 0) message.budget_limits = budgetLimits
    this.send(message)
  }

  artifactsList(projectId: string): void {
    const pid = this.requireProjectId(projectId, 'artifacts_list')
    this.send({ type: 'artifacts_list', project_id: pid })
  }

  artifactRead(projectId: string, path: string): void {
    const pid = this.requireProjectId(projectId, 'artifact_read')
    this.send({ type: 'artifact_read', project_id: pid, path })
  }

  artifactReveal(projectId: string, path: string): void {
    const pid = this.requireProjectId(projectId, 'artifact_reveal')
    this.send({ type: 'artifact_reveal', project_id: pid, path })
  }

  // ── 资料 (the project's own files, read only) ──────────────────────

  dataProjectIndex(projectId: string): void {
    const pid = this.requireProjectId(projectId, 'data_project_index')
    this.send({ type: 'data_project_index', project_id: pid })
  }

  dataOrderView(projectId: string, orderId: string): void {
    const pid = this.requireProjectId(projectId, 'data_order_view')
    this.send({ type: 'data_order_view', project_id: pid, order_id: orderId })
  }

  dataFolderList(projectId: string, folder: string, offset = 0): void {
    const pid = this.requireProjectId(projectId, 'data_folder_list')
    this.send({ type: 'data_folder_list', project_id: pid, folder, offset })
  }

  dataPreview(projectId: string, path: string): void {
    const pid = this.requireProjectId(projectId, 'data_preview')
    this.send({ type: 'data_preview', project_id: pid, path })
  }

  dataAttachments(projectId: string, offset = 0): void {
    const pid = this.requireProjectId(projectId, 'data_attachments')
    this.send({ type: 'data_attachments', project_id: pid, offset })
  }

  dataUsage(projectId: string, refresh = false): void {
    const pid = this.requireProjectId(projectId, 'data_usage')
    this.send({ type: 'data_usage', project_id: pid, refresh })
  }

  /** 数据与文件 P3: the backups on this machine. Read only. */
  dataBackups(projectId: string, options: { offset?: number; limit?: number } = {}): void {
    const pid = this.requireProjectId(projectId, 'data_backups')
    this.send({ type: 'data_backups', project_id: pid, offset: options.offset ?? 0, limit: options.limit ?? 50 })
  }

  /** 数据与文件 P3: what an automatic cleanup would remove. Read only; there is no apply. */
  dataCleanupPreview(projectId: string): void {
    const pid = this.requireProjectId(projectId, 'data_cleanup_preview')
    this.send({ type: 'data_cleanup_preview', project_id: pid })
  }

  /** 数据与文件 P3: what deleting this project would take, and what it would leave. Read only. */
  planProjectRemoval(projectId: string): void {
    const pid = this.requireProjectId(projectId, 'plan_project_removal')
    this.send({ type: 'plan_project_removal', project_id: pid })
  }

  /** 清掉一个备份计划任务。只清产品自己装的那几个，服务端会再核一遍。 */
  forgetBackupTask(name: string): void {
    this.send({ type: 'forget_backup_task', name })
  }

  dataLocations(projectId: string): void {
    const pid = this.requireProjectId(projectId, 'data_locations')
    this.send({ type: 'data_locations', project_id: pid })
  }

  /** Open one of this project's folders in the file manager of the machine the console runs on. */
  dataReveal(projectId: string, folder = '', legacy = false): void {
    const pid = this.requireProjectId(projectId, 'data_reveal')
    this.send({ type: 'data_reveal', project_id: pid, folder, legacy })
  }

  /** The old folder's contents as they are right now, with a token the standing 一键导入 offer can use. */
  dataFolderLegacy(projectId: string): void {
    const pid = this.requireProjectId(projectId, 'data_folder_legacy')
    this.send({ type: 'data_folder_legacy', project_id: pid })
  }

  // ── 项目文件夹: choosing one, creating one, pointing a project at it ──

  dataDirBrowse(projectId: string, path = '', offset = 0): void {
    const pid = this.requireProjectId(projectId, 'data_dir_browse')
    this.send({ type: 'data_dir_browse', project_id: pid, path, offset })
  }

  /** Open the machine's own folder dialog and answer with what the owner picked. */
  dataDirPick(projectId: string, path = ''): void {
    const pid = this.requireProjectId(projectId, 'data_dir_pick')
    this.send({ type: 'data_dir_pick', project_id: pid, path })
  }

  dataDirCreate(projectId: string, path: string, name: string): void {
    const pid = this.requireProjectId(projectId, 'data_dir_create')
    this.send({ type: 'data_dir_create', project_id: pid, path, name })
  }

  dataFolderPlan(projectId: string, scope: 'project' | 'default', target: string): void {
    const pid = this.requireProjectId(projectId, 'data_folder_plan')
    this.send({ type: 'data_folder_plan', project_id: pid, scope, target })
  }

  /** Point the project (or the default root) at a folder. Moves nothing. */
  dataFolderSet(projectId: string, scope: 'project' | 'default', target: string, token: string): void {
    const pid = this.requireProjectId(projectId, 'data_folder_set')
    this.send({ type: 'data_folder_set', project_id: pid, scope, target, token })
  }

  /** The second question: move what the old folder still holds. ``source`` is echoed from a reply, never composed. */
  dataFolderImport(projectId: string, source: string, token: string): void {
    const pid = this.requireProjectId(projectId, 'data_folder_import')
    this.send({ type: 'data_folder_import', project_id: pid, source, token })
  }

  // ── 回收区: what a model "deleted" in this project, and the owner's two ways of dealing with it ──

  dataRecycleList(projectId: string): void {
    const pid = this.requireProjectId(projectId, 'data_recycle_list')
    this.send({ type: 'data_recycle_list', project_id: pid })
  }

  /** Put one recycled file back where it was. ``id`` is echoed from a listing, never composed. */
  dataRecycleRestore(projectId: string, id: string): void {
    const pid = this.requireProjectId(projectId, 'data_recycle_restore')
    this.send({ type: 'data_recycle_restore', project_id: pid, id })
  }

  /** Delete everything in the recycle area for good, with the token the listing handed out and the owner confirmed. */
  dataRecycleClear(projectId: string, token: string): void {
    const pid = this.requireProjectId(projectId, 'data_recycle_clear')
    this.send({ type: 'data_recycle_clear', project_id: pid, token })
  }

  // ── 客户记忆 (4.7-A09): what the project remembers, read and deleted one entry at a time by the owner ──

  memoryOverview(projectId: string): void {
    const pid = this.requireProjectId(projectId, 'memory_overview')
    this.send({ type: 'memory_overview', project_id: pid })
  }

  /** Delete one entry; its words go to the recycle area first. ``customer`` ('' = no customer) and ``entryId`` are
   *  echoed from an overview, never composed. */
  memoryDelete(projectId: string, customer: string, entryId: string): void {
    const pid = this.requireProjectId(projectId, 'memory_delete')
    this.send({ type: 'memory_delete', project_id: pid, customer, entry_id: entryId })
  }

  settingsGet(projectId: string): void {
    const pid = this.requireProjectId(projectId, 'settings_get')
    this.send({ type: 'settings_get', project_id: pid })
  }

  settingsSetKey(projectId: string, apiKeyEnv: string, value: string): void {
    const pid = this.requireProjectId(projectId, 'settings_set_key')
    this.send({ type: 'settings_set_key', project_id: pid, api_key_env: apiKeyEnv, value })
  }

  settingsSetFlag(projectId: string, env: string, enabled: boolean): void {
    const pid = this.requireProjectId(projectId, 'settings_set_flag')
    this.send({ type: 'settings_set_flag', project_id: pid, env, enabled })
  }

  settingsTestModel(projectId: string, model: string): void {
    const pid = this.requireProjectId(projectId, 'settings_test_model')
    this.send({ type: 'settings_test_model', project_id: pid, model })
  }

  notifyTest(projectId: string): void {
    const pid = this.requireProjectId(projectId, 'notify_test')
    this.send({ type: 'notify_test', project_id: pid })
  }

  settingsSetRouting(projectId: string, defaultModel: string, routing: Record<string, string>): void {
    const pid = this.requireProjectId(projectId, 'settings_set_routing')
    this.send({ type: 'settings_set_routing', project_id: pid, default_model: defaultModel, routing })
  }

  /** 模型分配: what the three tiers mean, and the rules around them. A separate action from the routing slots. */
  settingsSetModelTiers(projectId: string, tiers: Record<string, string>,
                        tierParams: Record<string, Record<string, unknown>>,
                        taskTiers: Record<string, string>, residency: string,
                        noTrainingOptout: Record<string, string>, workWindowCap: number, reason = ''): void {
    const pid = this.requireProjectId(projectId, 'settings_set_model_tiers')
    this.send({
      type: 'settings_set_model_tiers', project_id: pid, tiers, tier_params: tierParams,
      task_tiers: taskTiers, residency, no_training_optout: noTrainingOptout,
      work_window_cap: workWindowCap, reason,
    })
  }

  /** What settings one order was launched under -- read from that order's own event, not from today's config. */
  orderModelPlan(projectId: string, orderId: string): void {
    this.send({ type: 'order_model_plan', project_id: this.requireProjectId(projectId, 'order_model_plan'),
                order_id: orderId })
  }

  // ── Project management ──────────────────────────────────────────────

  listProjects(): void {
    this.send({ type: 'list_projects' })
  }

  projectLifecycle(projectId: string, limit = 100): void {
    this.send({ type: 'project_lifecycle', project_id: this.normalizeProjectId(projectId), limit })
  }

  createProject(projectId: string): void {
    this.send({ type: 'create_project', project_id: this.normalizeProjectId(projectId) })
  }

  deleteProject(projectId: string, confirmation = ''): void {
    const pid = this.normalizeProjectId(projectId)
    this.send({ type: 'delete_project', project_id: pid, confirmation })
  }

  archiveProject(projectId: string, reason?: string): void {
    this.send({ type: 'archive_project', project_id: this.normalizeProjectId(projectId), reason: reason ?? '' })
  }

  restoreProject(projectId: string, reason?: string): void {
    this.send({ type: 'restore_project', project_id: this.normalizeProjectId(projectId), reason: reason ?? '' })
  }

  exportProject(projectId: string, outputDir?: string): void {
    this.send({ type: 'export_project', project_id: this.normalizeProjectId(projectId), output_dir: outputDir })
  }

  backupProject(projectId: string): void {
    this.send({ type: 'backup_project', project_id: this.normalizeProjectId(projectId) })
  }

  /** Owner-only and read-only: what restoring this archive would bring back, what it would not, and why it would be refused. */
  restorePreflight(projectId: string, archivePath: string, targetProjectId = ''): void {
    this.send({
      type: 'restore_preflight', project_id: this.requireProjectId(projectId, 'restore_preflight'),
      archive_path: archivePath, target_project_id: targetProjectId,
    })
  }

  /**
   * `projectId` is the project to restore, not the one that is open. A restore writes: typed while the console is
   * offline it must not run on its own after a reconnect, so it is never queued.
   */
  restoreBackup(archivePath: string, projectId: string): SendDisposition {
    return this.send({ type: 'restore_backup', archive_path: archivePath, project_id: this.normalizeProjectId(projectId) })
  }

  destroyProject(projectId: string): void {
    const pid = this.normalizeProjectId(projectId)
    this.send({ type: 'destroy_project', project_id: pid, confirmation: `destroy:${pid}` })
  }

  switchProject(projectId: string, switchSeq?: string): void {
    this.send({ type: 'switch_project', project_id: this.normalizeProjectId(projectId), switch_seq: switchSeq })
  }

  // ── Org info ──────────────────────────────────────────────────────────

  orgInfo(): void {
    this.send({ type: 'org_info' })
  }

  reorgList(projectId: string): void {
    const pid = this.requireProjectId(projectId, 'reorg_list')
    this.send({ type: 'reorg_list', project_id: pid })
  }

  importEmployeeAsAgent(employeeId: string, officeId?: string): void {
    this.send({ type: 'import_employee_as_agent', employee_id: employeeId, office_id: officeId })
  }

  // ── AMPULY Market ─────────────────────────────────────────────────────────

  marketBrowse(): void {
    this.send({ type: 'market_browse' })
  }

  marketPreview(presetId: string): void {
    this.send({ type: 'market_preview', preset_id: presetId })
  }

  marketApplyPreset(presetId: string, strategy: string = 'namespace'): void {
    this.send({ type: 'market_apply_preset', preset_id: presetId, strategy })
  }

  marketListInstalled(): void {
    this.send({ type: 'market_list_installed' })
  }

  marketExport(data: { package_id: string; name: string; description: string; version: string }): void {
    this.send({ type: 'market_export', ...data })
  }

  marketInstall(path: string, strategy: string = 'namespace'): void {
    this.send({ type: 'market_install', path, strategy })
  }

  marketUninstall(packageId: string): void {
    this.send({ type: 'market_uninstall', package_id: packageId })
  }

  // ── Org Editing ───────────────────────────────────────────────────────

  addRole(roleId: string, name: string, responsibility: string, reportsTo: string = 'owner', icon?: string | null): void {
    this.send({ type: 'add_role', role_id: roleId, name, responsibility, reports_to: reportsTo, icon: icon || null })
  }

  bulkAddRoles(roles: Array<{ role_id: string; name: string; responsibility: string; reports_to: string; icon?: string | null }>): void {
    this.send({ type: 'bulk_add_roles', roles })
  }

  updateRole(roleId: string, updates: {
    name?: string
    responsibility?: string
    reports_to?: string
    can_spawn?: string[]
    icon?: string | null
    execution_strategy?: string
    preferred_external_agent?: string | null
    prompt_refs?: string[]
    tools?: string[]
  }): void {
    this.send({ type: 'update_role', role_id: roleId, ...updates })
  }

  bindExternalTeam(data: {
    boundary_role_id: string
    organization_id?: string
    scope?: 'role' | 'subtree'
    collapse_subtree?: boolean
    external_agent?: 'jiuwenswarm'
    provider_mode?: string
  }): void {
    this.send({
      type: 'bind_external_team',
      external_agent: 'jiuwenswarm',
      scope: 'subtree',
      collapse_subtree: true,
      provider_mode: 'team',
      ...data,
    })
  }

  unbindExternalTeam(data: {
    binding_id?: string
    boundary_role_id?: string
    organization_id?: string
  }): void {
    this.send({ type: 'unbind_external_team', ...data })
  }

  deleteRole(roleId: string): void {
    this.send({ type: 'delete_role', role_id: roleId })
  }

  updateRuntimePolicy(policy: Record<string, any>): void {
    this.send({ type: 'update_runtime_policy', policy })
  }

  updateOrgStrategy(data: { final_decider_role_id?: string | null }): void {
    this.send({ type: 'update_org_strategy', ...data })
  }

  resetArchitecture(): void {
    this.send({ type: 'reset_architecture' })
  }

  orgConfigExport(): void {
    this.send({ type: 'org_config_export' })
  }

  orgConfigImport(yaml: string, dryRun: boolean): void {
    this.send({ type: 'org_config_import', yaml, dry_run: dryRun })
  }

  orgSavedList(): void {
    this.send({ type: 'org_saved_list' })
  }

  orgSavedSaveAs(name: string, overwrite: boolean): void {
    this.send({ type: 'org_saved_save_as', name, overwrite })
  }

  orgSavedCreate(organizationName: string, members: OrgCreateMemberInput[]): void {
    this.send({ type: 'org_saved_create', organization_name: organizationName, members })
  }

  orgSavedLoad(name: string): void {
    this.send({ type: 'org_saved_load', name })
  }

  orgSavedDelete(name: string): void {
    this.send({ type: 'org_saved_delete', name })
  }

  commsState(projectId: string, opts?: { task_id?: string; session_id?: string }): void {
    const pid = this.requireProjectId(projectId, 'comms_state')
    this.send({ type: 'comms_state', project_id: pid, ...(opts || {}) })
  }

  commsReadMessage(
    projectId: string,
    path: string,
    scope: Pick<CommsStatePayload, 'task_id'> | null,
  ): void {
    const pid = this.requireProjectId(projectId, 'comms_read_message')
    this.send({
      type: 'comms_read_message',
      project_id: pid,
      task_id: String(scope?.task_id ?? ''),
      path,
    })
  }

  // ── Internal ───────────────────────────────────────────────────────────

  private normalizeProjectId(value: unknown): string {
    return typeof value === 'string' ? value.trim() : ''
  }

  private requireProjectId(projectId: unknown, action: string): string {
    const pid = this.normalizeProjectId(projectId)
    if (!pid) {
      throw new Error(`${action} requires non-empty project_id`)
    }
    return pid
  }

  private ensureProjectScope(payload: Record<string, unknown>): boolean {
    const messageType = typeof payload.type === 'string' ? payload.type : ''
    if (!PROJECT_SCOPED_MESSAGE_TYPES.has(messageType)) {
      return true
    }
    const pid = this.normalizeProjectId(payload.project_id ?? payload.projectId)
    if (!pid) {
      const error = `${messageType} requires non-empty project_id`
      console.error(`[wsClient] ${error}`, payload)
      this.handlers.onAck?.({ ok: false, error, action: messageType })
      return false
    }
    payload.project_id = pid
    return true
  }

  private handleMessage(raw: unknown): void {
    if (typeof raw !== 'string') {
      return
    }
    let parsed: SocketEnvelope | null = null
    try {
      parsed = JSON.parse(raw) as SocketEnvelope
    } catch {
      return
    }
    if (!parsed || typeof parsed !== 'object' || !('type' in parsed)) {
      return
    }
    try { switch (parsed.type) {
      case 'snapshot':
        this.handlers.onSnapshot?.(parsed.payload)
        break
      case 'auth': {
        const auth = parsed.payload
        if (!auth || typeof auth !== 'object') break
        const account = auth.account
        const login = typeof account?.login === 'string' ? account.login.trim() : ''
        const role = typeof account?.role === 'string' ? account.role : ''
        const principal = account && login && ['owner', 'employee', 'customer'].includes(role)
          ? `${role}:${login}` : (!account && auth.required === false ? 'local-owner' : '')
        if (principal && this.lastAuthenticatedPrincipal && principal !== this.lastAuthenticatedPrincipal) {
          // Reads queued for a different account are discarded; its pending display promises fail explicitly.
          this.failPendingSessionDetailRequests('principal_changed')
          this.pendingQueue = []
        }
        if (principal) this.lastAuthenticatedPrincipal = principal
        this.handlers.onAuth?.(auth)
        if (principal) this.flushPendingQueue()
        break
      }
      case 'event':
        this.handlers.onEvent?.(parsed.payload)
        break
      case 'ack': {
        const sourceSocket = this.ws
        const detailPayload = this.settleSessionDetailRequest(
          parsed.payload as unknown as Record<string, unknown>,
        )
        const ackPayload = this.settleInteractionReply(detailPayload)
        for (const observe of [...this.ackObservers]) {
          try { observe(ackPayload) } catch (e) { console.error('[wsClient] ack observer failed:', e) }
        }
        this.handlers.onAck?.(ackPayload as typeof parsed.payload)
        if (ackPayload.action === 'auth_logout' && ackPayload.ok === true
            && sourceSocket && this.ws === sourceSocket) {
          // A confirmed logout ends the old transport as well as its identity.
          // Its queued/read-in-flight replies must not enter the next account's
          // UI. Pending writes keep the existing unknown-outcome handling.
          this.disconnect()
          this.lastAuthenticatedPrincipal = ''
          this.pendingQueue = []
          this.handlers.onStatus?.('disconnected')
          this.pendingQueue = [] // Drop old-account reconciliation requested by that callback.
          this.connect() // The new socket must receive its own real auth frame.
        }
        break
      }
      case 'channel_created':
        this.handlers.onChannelCreated?.(parsed.payload)
        break
      case 'board_task_created':
      case 'board_task_moved':
        this.handlers.onBoardEvent?.(parsed.payload as unknown as Record<string, unknown>)
        break
      case 'board_task_status_changed':
      case 'execution_mode_resolved':
      case 'project_run_updated':
      case 'seat_digest_updated':
      case 'work_item_batch_updated':
      case 'session_runtime_control':
        this.handlers.onCollabMessage?.(parsed.type, parsed.payload as Record<string, unknown>)
        break
      case 'cross_office_collab':
        this.handlers.onCrossOfficeCollab?.(parsed.payload)
        break
      case 'chat_new_message':
      case 'chat_channel_created':
      case 'kanban_updated':
      case 'collab_sync_push':
      case 'project_index_push':
        this.handlers.onCollabMessage?.(parsed.type, parsed.payload as Record<string, unknown>)
        break
      case 'agent_runtime_update':
        this.handlers.onAgentRuntimeUpdate?.(parsed.payload)
        break
      case 'runtime_status_sync':
        this.handlers.onRuntimeStatusSync?.(parsed.payload)
        break
      case 'worker_notification':
        this.handlers.onWorkerNotification?.(parsed.payload as WorkerNotificationPayload)
        break
      case 'session_progress':
        this.handlers.onSessionProgress?.(parsed.payload)
        break
      case 'work_item_progress':
        this.handlers.onWorkItemProgress?.(parsed.payload as unknown as WorkItemProgressPayload)
        break
      case 'external_team_activity_event':
        this.handlers.onExternalTeamActivityEvent?.(parsed.payload as unknown as ExternalTeamEventV1)
        break
      case 'external_team_activity_snapshot':
        this.handlers.onExternalTeamActivitySnapshot?.(parsed.payload as unknown as ExternalTeamActivitySnapshotPayload)
        break
      case 'session_created':
        this.handlers.onSessionCreated?.(parsed.payload)
        break
      case 'session_updated':
        this.handlers.onSessionUpdated?.(parsed.payload)
        break
      case 'native_permission_default_updated':
        this.handlers.onNativePermissionDefaultUpdated?.(parsed.payload)
        break
      case 'browser_session_changed':
        if (browserPushScope(parsed.payload) && typeof parsed.payload.state === 'string') {
          this.handlers.onBrowserSessionChanged?.(parsed.payload as unknown as BrowserSessionChangedPush)
        }
        break
      case 'browser_frame':
        if (browserPushScope(parsed.payload) && typeof parsed.payload.data === 'string') {
          this.handlers.onBrowserFrame?.(parsed.payload as unknown as BrowserFramePush)
        }
        break
      case 'session_message':
        this.handlers.onSessionMessage?.(parsed.payload as Record<string, unknown>)
        break
      case 'session_title_updated':
        this.handlers.onSessionTitleUpdated?.(parsed.payload)
        break
      case 'session_deleted':
        this.handlers.onSessionDeleted?.(parsed.payload)
        break
      case 'child_session_created':
        this.handlers.onChildSessionCreated?.(parsed.payload)
        break
      case 'project_switched':
        this.handlers.onProjectSwitched?.(parsed.payload)
        break
      case 'project_deleted':
        this.handlers.onProjectDeleted?.(parsed.payload)
        break
      case 'org_info':
        this.handlers.onOrgInfo?.(parsed.payload)
        break
      case 'comms_state':
        this.handlers.onCommsState?.(parsed.payload as unknown as CommsStatePayload)
        break
      case 'comms_message':
        this.handlers.onCommsMessage?.(parsed.payload as unknown as CommsMessagePayload)
        break
      case 'comms_state_dirty':
        // Server pushed a "something changed" hint after a comms message
        // was sent. Re-issue the snapshot request so the panel updates
        // immediately instead of waiting for its polling tick.
        try {
          const projectId = typeof parsed.payload?.project_id === 'string' ? parsed.payload.project_id : ''
          if (projectId) this.commsState(projectId)
        } catch { /* ignore */ }
        break
      case 'reorg_list':
        this.handlers.onReorgList?.(parsed.payload)
        break
      case 'market_list_installed':
        this.handlers.onMarketListInstalled?.(parsed.payload as unknown as { packages: Array<Record<string, unknown>> })
        break
      case 'market_browse':
        this.handlers.onMarketBrowse?.(parsed.payload as unknown as { presets: Array<Record<string, unknown>> })
        break
      case 'market_preview':
        this.handlers.onMarketPreview?.(parsed.payload as Record<string, unknown>)
        break
      case 'org_config_export':
        this.handlers.onOrgConfigExport?.(parsed.payload as { yaml: string })
        break
      case 'org_config_import':
        this.handlers.onOrgConfigImport?.(parsed.payload as any)
        break
      case 'org_saved_list':
        this.handlers.onOrgSavedList?.(parsed.payload as { orgs: SavedOrgSummary[]; active_name?: string | null })
        break
      case 'org_saved_save_as':
        this.handlers.onOrgSavedSaveAs?.(parsed.payload as { ok: boolean; name: string; error?: string })
        break
      case 'org_saved_create':
        this.handlers.onOrgSavedCreate?.(parsed.payload as OrgSavedCreatePayload)
        break
      case 'org_saved_load':
        this.handlers.onOrgSavedLoad?.(parsed.payload as { ok: boolean; name: string; error?: string })
        break
      case 'org_saved_delete':
        this.handlers.onOrgSavedDelete?.(parsed.payload as { ok: boolean; name: string; error?: string })
        break
      case 'pong':
        this.handlePong()
        break
      default:
        break
    }
    } catch (e) { console.error('[wsClient] Error handling message:', parsed.type, e) }
  }

  private settleSessionDetailRequest(payload: Record<string, unknown>): Record<string, unknown> {
    const action = typeof payload.action === 'string' ? payload.action.trim() : ''
    const isSessionDetailAck = action === 'session_detail'
      || (!action && payload.error === 'store_not_ready')
    if (!isSessionDetailAck) return payload
    const projectId = this.normalizeProjectId(payload.project_id ?? payload.projectId)
    const taskId = typeof payload.task_id === 'string' ? payload.task_id : ''
    const detailLevel = payload.detail_level === 'full' ? 'full' : payload.detail_level === 'summary' ? 'summary' : ''
    const viewGeneration = typeof payload.view_generation === 'number' ? payload.view_generation : undefined
    const index = this.pendingSessionDetailRequests.findIndex(request => (
      (!projectId || request.projectId === projectId)
      && (!taskId || request.taskId === taskId)
      && (!detailLevel || request.detailLevel === detailLevel)
      && (viewGeneration === undefined || request.viewGeneration === viewGeneration)
    ))
    if (index < 0) return payload
    const [request] = this.pendingSessionDetailRequests.splice(index, 1)
    if (request.timeout !== null) clearTimeout(request.timeout)
    const normalizedPayload = {
      ...payload,
      action: 'session_detail',
      project_id: projectId || request.projectId,
      task_id: taskId || request.taskId,
      detail_level: detailLevel || request.detailLevel,
      client_history_page: request.historyPage,
    }
    if (!request.settled) {
      request.settled = true
      request.resolve(normalizedPayload)
    }
    return normalizedPayload
  }

  private settleInteractionReply(payload: Record<string, unknown>): Record<string, unknown> {
    if (String(payload.action ?? '').trim() !== 'interaction_reply') return payload
    const clientRequestId = String(payload.client_request_id ?? '').trim()
    if (!clientRequestId) return payload
    const pending = this.pendingInteractionReplies.get(clientRequestId)
    if (!pending) return payload
    this.pendingInteractionReplies.delete(clientRequestId)
    clearTimeout(pending.timeout)
    const queuedIndex = this.pendingQueue.indexOf(pending.wireData)
    if (queuedIndex >= 0) this.pendingQueue.splice(queuedIndex, 1)
    pending.resolve({
      ...payload,
      ok: payload.ok === true,
      accepted: payload.accepted === true,
      checkpoint_id: String(payload.checkpoint_id ?? ''),
      checkpoint_type: String(payload.checkpoint_type ?? ''),
      client_request_id: clientRequestId,
      error: typeof payload.error === 'string' ? payload.error : undefined,
      reason: typeof payload.reason === 'string' ? payload.reason : undefined,
      status: typeof payload.status === 'string' ? payload.status : undefined,
      deduplicated: payload.deduplicated === true,
    })
    return payload
  }

  private failInteractionReply(clientRequestId: string, error: string): void {
    const pending = this.pendingInteractionReplies.get(clientRequestId)
    if (!pending) return
    this.pendingInteractionReplies.delete(clientRequestId)
    clearTimeout(pending.timeout)
    const queuedIndex = this.pendingQueue.indexOf(pending.wireData)
    if (queuedIndex >= 0) this.pendingQueue.splice(queuedIndex, 1)
    let checkpointId = ''
    let checkpointType = ''
    try {
      const payload = JSON.parse(pending.wireData) as Record<string, unknown>
      checkpointId = String(payload.checkpoint_id ?? '')
      checkpointType = String(payload.checkpoint_type ?? '')
    } catch { /* already validated before serialization */ }
    pending.resolve({
      ok: false,
      accepted: false,
      error,
      checkpoint_id: checkpointId,
      checkpoint_type: checkpointType,
      client_request_id: clientRequestId,
    })
  }

  private failPendingInteractionReplies(error: string): void {
    for (const clientRequestId of [...this.pendingInteractionReplies.keys()]) {
      this.failInteractionReply(clientRequestId, error)
    }
  }

  private timeoutSessionDetailRequest(index: number): void {
    const request = this.pendingSessionDetailRequests[index]
    if (!request) return
    if (request.queued) {
      this.failSessionDetailRequest(index, 'request_timeout')
      return
    }

    if (request.timeout !== null) clearTimeout(request.timeout)
    request.timeout = null
    if (!request.settled) {
      request.settled = true
      request.resolve(this.sessionDetailFailurePayload(request, 'request_timeout'))
    }
    // Keep a settled tombstone in FIFO order until its ACK or connection
    // cleanup. Removing it would let a late ACK settle a newer request with
    // identical correlation fields; closing the shared socket would interrupt
    // unrelated runtime events.
  }

  private failSessionDetailRequest(index: number, error: string): void {
    const [request] = this.pendingSessionDetailRequests.splice(index, 1)
    if (!request) return
    if (request.timeout !== null) clearTimeout(request.timeout)
    if (request.queued) {
      const queuedIndex = this.pendingQueue.indexOf(request.wireData)
      if (queuedIndex >= 0) this.pendingQueue.splice(queuedIndex, 1)
    }
    if (!request.settled) request.resolve(this.sessionDetailFailurePayload(request, error))
  }

  private sessionDetailFailurePayload(
    request: {
      projectId: string
      taskId: string
      detailLevel: 'summary' | 'full'
      historyPage: boolean
    },
    error: string,
  ): Record<string, unknown> {
    return {
      ok: false,
      action: 'session_detail',
      error,
      project_id: request.projectId,
      task_id: request.taskId,
      detail_level: request.detailLevel,
      client_history_page: request.historyPage,
    }
  }

  private failPendingSessionDetailRequests(error: string): void {
    while (this.pendingSessionDetailRequests.length > 0) {
      this.failSessionDetailRequest(0, error)
    }
  }

  private flushPendingQueue(): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    const queued = this.pendingQueue.splice(0)
    for (const data of queued) {
      // Only reads reach the queue (send); a write that somehow did is dropped here rather than replayed.
      try { if (!queuesForReconnect(JSON.parse(data) as Record<string, unknown>)) continue } catch { continue }
      const detailRequestIndex = this.pendingSessionDetailRequests.findIndex(
        request => request.queued && request.wireData === data,
      )
      const interactionRequestId = [...this.pendingInteractionReplies.entries()]
        .find(([, request]) => request.wireData === data)?.[0]
      try {
        this.ws.send(data)
        if (detailRequestIndex >= 0) this.pendingSessionDetailRequests[detailRequestIndex].queued = false
      } catch {
        if (detailRequestIndex >= 0) this.failSessionDetailRequest(detailRequestIndex, 'send_failed')
        if (interactionRequestId) this.failInteractionReply(interactionRequestId, 'send_failed')
      }
    }
  }

  private startHeartbeat(): void {
    this.stopHeartbeat()
    this.heartbeatTimer = window.setInterval(() => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
      // One unanswered ping at a time: its own timer decides; a second would leak the first timer.
      if (this.pongTimer !== null) return
      this.ws.send(JSON.stringify({ type: 'ping' }))
      this.pongTimer = window.setTimeout(() => {
        this.pongTimer = null
        this.ws?.close()
      }, HEARTBEAT_TIMEOUT_MS)
    }, HEARTBEAT_INTERVAL_MS)
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer !== null) {
      window.clearInterval(this.heartbeatTimer)
      this.heartbeatTimer = null
    }
    if (this.pongTimer !== null) {
      window.clearTimeout(this.pongTimer)
      this.pongTimer = null
    }
  }

  private handlePong(): void {
    if (this.pongTimer !== null) {
      window.clearTimeout(this.pongTimer)
      this.pongTimer = null
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) return
    if (this.reconnectAttempt >= RECONNECT_MAX_ATTEMPTS) {
      this.handlers.onStatus?.('error', 'max reconnect attempts reached')
      return
    }
    const delay = Math.min(
      RECONNECT_BASE_MS * Math.pow(2, this.reconnectAttempt),
      RECONNECT_MAX_MS,
    )
    this.reconnectAttempt++
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, delay)
  }
}
