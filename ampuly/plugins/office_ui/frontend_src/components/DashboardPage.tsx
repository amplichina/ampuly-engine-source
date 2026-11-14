import { reportProject } from '../lib/reportFocus'
// 项目中心 = 一列项目 + 四个页签 + 业务全景 (frontend redesign S3-2B, policy; the prototype's owner-business-ui.js,
// FRONTEND-IA 「项目」 row). Two levels: every project in one list (名字 · 类型 · 客户 · 规划管理者 and the four numbers,
// each a way into the inbox), and one project with four tabs -- 业务总览 (the boundary line, 现在需要关注, the flow lines
// with their stage tiles), 业务流 (the flow cards), 协作与接入 (步骤责任 / 软件与连接 / 人员与职责), 资料与数据 (the files,
// the storage, the recorded amounts, the folded 系统记录).
//
// Every number is the server's (`standing` / `flows` on project_order_overview, S0-A): nothing is counted here any
// more, and where the server sent none the page says so instead of drawing a zero. Every word on an order comes from
// the server's verdict (`attention`) through lib/projectPanorama; every write (保存业务说明, 保存交接规划) goes through
// the request tracker in App, and its button says the receipt's states; every reply reaches this page through the
// current-project gate in App. The pause cards left this page (they are in 总控 and the inbox); only a count link
// leads to them. The builder is untouched: 对应画布节点 links land on the step by the builder's own step-return.

import { useEffect, useMemo, useState, type DragEvent, type ReactNode } from 'react'
import { useI18n } from '../i18n'
import type { Answering } from '../lib/answerPath'
import { formatBytes, type UsageEntry } from '../lib/dataBrowser'
import { dueState, moneyText, moneyTotals } from '../lib/orderHandling'
import { attentionOf, type InboxOrderLike } from '../lib/ordersInbox'
import { lifecycleLabel, projectRows } from '../lib/projectList'
import { flowStandingOf, standingOf, type ProjectOverview } from '../lib/projectOverview'
import {
  BOUNDARY_PLACEHOLDERS, CONNECTION_MODES, STAGE_MODE_WORDS, STANDING_KEYS, STANDING_WORDS, attentionTop, customerPreviewOrders, employeePreviewRows,
  endcapLanes, orderLine, placedOrders, profileFrom, refusalText, responsibilityRows, stagesOf, stageView, standingTarget, standingText,
  type ConnectionLane, type FlowStage, type PanoramaNode, type ProjectProfile, type Standing, type StandingKey,
} from '../lib/projectPanorama'
import { COLLAB_TABS, COLLAB_TAB_LABELS, PROJECT_TABS, PROJECT_TAB_LABELS, blockHidden, projectListHidden, type CollabTab, type ProjectLevel, type ProjectTab } from '../lib/projectTabs'
import { projectWorkflowComposition } from '../lib/projectWorkflowComposition'
import { keyOf, type RequestState, type Tracked } from '../lib/requestTracker'
import { classifyPageError, pageErrorMessageKey } from '../lib/pageError'
import type { ProgressEntry, Project, RoleWorkItemRow, Session, WorkItemProgressEntry } from '../types/kanban'
import type { AgentInfo, OrgEmployee } from '../types/visual'
import type { RunBudget, WorkflowSummary, WorkOrderSummary } from './WorkflowsPage'
import type { ProjectCostObservation } from '../lib/projectCostObservation'

const TERMINAL_STATUSES = new Set(['done', 'delivered', 'failed', 'cancelled'])
const BLOCKED_STATUSES = new Set(['awaiting_human', 'awaiting_manager_review', 'awaiting_review'])

export interface DashboardBlockerOption {
  id: string
  label: string
  kind: 'approve' | 'deny'
}

export type BlockerKind = 'gate' | 'permission' | 'question' | 'delivery' | 'staffing' | 'suspended' | 'reorg' | 'failure' | 'wait' | 'other'

const BLOCKER_KINDS: BlockerKind[] = ['gate', 'permission', 'question', 'delivery', 'staffing', 'suspended', 'reorg', 'failure', 'wait', 'other']

/** The kind the server classified this pause as, or `other` for anything unknown to this build. */
export function blockerKind(blocker: { kind?: string; gate?: unknown }): BlockerKind {
  const kind = String(blocker.kind ?? '')
  // A permission request is never a business gate, even when the server attached gate details to it (its card
  // spec names the step too): it is answered with its own options, not with 放行 / 打回.
  if (blocker.gate && kind !== 'permission') return 'gate'
  return (BLOCKER_KINDS as string[]).includes(kind) ? (kind as BlockerKind) : 'other'
}

export interface DashboardBlocker {
  checkpoint_id: string
  checkpoint_type: string
  kind?: string
  task_id: string
  session_id: string
  /** The runtime task that raised the pause (in company mode, a work item's task rather than the order's). */
  waiting_task_id?: string
  subject: string
  detail: string
  options: DashboardBlockerOption[]
  created_at: string
  feishu?: { sent_at: string; link: string; decision: string }
  gate?: { node_id: string; node_name: string; version: number; assignee: string }
  /** A permission request as the server worded it (it knows the order folder; the page does not): shown as is. */
  permission?: { summary: string; outside: boolean }
  /** A19: the reminder is held until this local time. The pause is still pending. */
  snoozed_until?: string
  snooze_note?: string
  /** The project the pause belongs to, when the server says: a card of another project is refused before sending. */
  project_id?: string
  /** The order behind the pause, when the server names it (never the task id: that is not a thing the owner reads). */
  order_id?: string
  order_title?: string
  /** The order's own step, by lib/orderDrawer's keys, when the server attaches it: worded by STEP_TEXT. */
  order_step?: string
  /** The card as the server worded it (S1-A): title, one line, the one primary answer, the rest under 更多, what the
   *  answer does, and which order it is. When present it wins over the console's own wording (lib/ownerCard.ts). */
  owner_card?: Record<string, unknown>
}

export interface DutyLogEntry {
  at: string
  kind: string
  order_id: string
  text: string
  action?: string
  result?: string
  failure_reason?: string
  sent: { channel: string; ok: boolean; detail?: string }[]
  muted?: boolean
}

export interface DutyLogState {
  configured: boolean
  entries: DutyLogEntry[]
}

/** One flow a project adopted (project_workflow_adoptions). */
export interface WorkflowAdoption {
  workflow_id: string
  workflow_version: string
  status: 'active' | 'paused' | 'withdrawn'
  adopted_by?: string
  adopted_at?: string
}

/** What a flow card says about its adoption, and which of the three actions it offers. Pure, so it is tested alone. */
export function adoptionState(flow: { status: string; version: string }, adoption: WorkflowAdoption | undefined): {
  kind: 'not_published' | 'not_adopted' | 'active' | 'paused' | 'withdrawn'
  upgradeTo: string
} {
  if (!adoption || adoption.status === 'withdrawn') {
    if (flow.status !== 'published') return { kind: 'not_published', upgradeTo: '' }
    return { kind: adoption ? 'withdrawn' : 'not_adopted', upgradeTo: '' }
  }
  const newer = flow.status === 'published' && flow.version && flow.version !== adoption.workflow_version ? flow.version : ''
  return { kind: adoption.status, upgradeTo: newer }
}

/** 本项目专用的业务流 (4.7-A01): a flow derived for this project is the project's already, so there is nothing to adopt.
 *  Its card offers the next step for where it stands: finish it in the builder while it is a draft (or while it
 *  cannot take orders), and place an order once it is being tested (a trial order) or published. Adopting stays for
 *  global flows. The server agrees: an order on a project's own flow is checked for its owner, not for an adoption. */
export function ownFlowStep(flow: { status: string; invokable?: boolean }): {
  kind: 'own_draft' | 'own_testing' | 'own_published' | 'own_blocked' | 'own_retired'
  action: 'build' | 'order' | 'none'
} {
  if (flow.status === 'retired') return { kind: 'own_retired', action: 'none' }
  if (flow.status === 'testing' || flow.status === 'published') {
    if (flow.invokable === false) return { kind: 'own_blocked', action: 'build' }
    return { kind: flow.status === 'testing' ? 'own_testing' : 'own_published', action: 'order' }
  }
  return { kind: 'own_draft', action: 'build' }
}

/** What the page knows about one of the S3-2A reads (the profile, the connections). */
export interface PanoramaReadState {
  /** The reply came (ok or refused). */
  loaded: boolean
  /** The server said the store is behind (ampuly db-version --apply). */
  needsMigration: boolean
  /** The server's own sentence for that (`why_zh`); '' when it sent none. */
  why?: string
  /** A refusal, in the server's words; '' for none. */
  error: string
}

export const EMPTY_READ: PanoramaReadState = Object.freeze({ loaded: false, needsMigration: false, why: '', error: '' })

/** What a dialog's save button says: the tracker's state for its key, the receipt's code ('' = accepted) and the
 *  sentence to show ('' when there is nothing to say) -- busy / no receipt / not sent in the tracker's words, a refusal
 *  in the server's (lib/projectPanorama refusalText). */
export interface SaveState { state: RequestState; code: string; words: string }
export const IDLE_SAVE: SaveState = Object.freeze({ state: 'idle', code: '', words: '' })

interface DashboardPageProps {
  connected?: boolean
  sessions: Session[]
  /** The project's whole recorded spend. Preferred over summing the rendered sessions, which omits the runtime
   *  auxiliary tasks that hold real cost. totalUsd null means the record could not be read. */
  recordedCost?: { totalUsd: number | null; calls: number }
  /** Exact-project historical subtotal only, never an invoice or currency proof. */
  recordedCostObservation?: ProjectCostObservation | null
  agents: AgentInfo[]
  dutyLog?: DutyLogState
  dutyLogError?: string | null
  onRefreshDutyLog?: () => void
  onOpenSession: (taskId: string, opts?: { revealPendingDecision?: boolean }) => void
  /** Kept for the callers that still hand them in; the pause cards themselves are in 总控 and the inbox now. */
  answering?: Answering
  decisionErrors?: Record<string, string>
  artifactsSlot?: ReactNode
  /** 资料: the project's own files, by order and by folder. */
  filesSlot?: ReactNode
  /** 客户记忆 (4.7-A09): what the project remembers about its customers. Owner console only. */
  memorySlot?: ReactNode
  onOpenFiles?: () => void
  /** How much space each project takes, as far as anybody has measured (D8's usage map). */
  usageByProject?: Record<string, UsageEntry>
  onAskUsage?: (projectId: string) => void
  /** 项目文件夹: where this project keeps its files, and how the owner changes it. Owner console only. */
  projectFolderSlot?: ReactNode
  onOpenStorage?: () => void
  /** The project's flows and orders (order rows carry the server's `attention` and `current_nodes`). */
  workflows?: WorkflowSummary[] | null
  workOrders?: WorkOrderSummary[] | null
  budgets?: RunBudget[] | null
  /** Which delivery space this is, and what is inside it right now. */
  projectContext?: {
    project_id: string
    customer_id?: string
    lifecycle_status?: string
    orders: { total: number; open: number }
    artifacts: number
  }
  /** Every project of this console, for 全部项目; entering one is the project switch the selector does. */
  projects?: Project[]
  onEnterProject?: (projectId: string) => void
  /** The way to the organisation (roles, roster, structure). Absent on a console that may not see it. */
  onOpenOrg?: () => void
  /** 协作人员与组织, shown on the all-projects level as a sub-board (the same element the 组织 page shows). */
  orgSlot?: ReactNode
  /** Where every project's orders stand, as the server counted them; null = not read or refused. */
  projectOverview?: ProjectOverview | null
  /** Ask for it: the click that goes to the all-projects level, and the project level as it opens. */
  onAskProjectOverview?: () => void
  /** 数据与存储: the project's administration (backup precheck, record, archive, export, backup, customer binding). */
  manageSlot?: ReactNode
  /** 全部项目: the new-project control (projects are made here, not in the top bar). */
  createSlot?: ReactNode
  /** Raised by the top bar's project chip: show the all-projects level (where projects are chosen). 0 = never. */
  showAllSeq?: number
  /** Jump from a count or a row to the page that owns it; `target` is what lib/orderSpread ordersTarget reads. */
  onNavigate?: (page: 'workbench' | 'approvals' | 'workflows' | 'orders' | 'artifacts', target?: string) => void
  /** Stage a call in the caller's builder draft; this does not save the workflow. */
  onComposeWorkflow?: (callerId: string, targetId: string) => void
  /** 项目采用业务流 (4.7 Q01): what this project adopted; null while loading, absent where it is not offered. */
  adoptions?: WorkflowAdoption[] | null
  onAdoptWorkflow?: (workflowId: string) => void
  onSetAdoptionStatus?: (workflowId: string, status: 'active' | 'paused' | 'withdrawn') => void
  onDeriveWorkflow?: (workflowId: string) => void
  /** 人员与职责 · 联系方式 (4.7-A07): each member's bound channels, and the owner's bind form. Absent = not shown. */
  peopleSlot?: ReactNode
  /** Asked from outside the page (the builder's 去绑定): open this project's tab (and board). A new seq asks again. */
  tabRequest?: { tab: ProjectTab; collab?: CollabTab; seq: number } | null
  /** The company's roster, for names: an employee id is never shown as is. */
  employees?: OrgEmployee[]
  /** 本项目的业务边界 (project_profile_get, S3-2A); null until read. */
  profile?: ProjectProfile | null
  profileState?: PanoramaReadState
  /** 保存业务说明 (project_profile_set): on the request tracker in App under the key `project_profile_set`. */
  onSaveProfile?: (profile: { start: string; scope: string; end: string; manager_employee_id: string }) => void
  /** 软件与连接 (project_connections_get, S3-2A); null until read. */
  /** Project-scoped sales facts; manual data only, never a flow controller. */
  salesRecordsSlot?: ReactNode
  connections?: ConnectionLane[] | null
  connectionsState?: PanoramaReadState
  /** 保存交接规划 for one lane (project_connections_set): tracked under `project_connections_set:<lane>`. */
  onSaveConnection?: (connection: { id: string; tool: string; purpose: string; mode: string; stage: string }) => void
  /** Ask for the profile and the connections of the project on screen (once per project, as the page opens). */
  onAskPanorama?: () => void
  /** The request tracker's entries (lib/requestTracker useTracked): a button says its own write's state. */
  tracked?: Tracked
  /** 对应画布节点 / 配置交接 / 编辑节点: the builder, that flow, that step selected -- by the builder's own step-return. */
  onOpenNode?: (workflowId: string, nodeId: string) => void
  /** 客户视角: the read-only customer view of one order (customer_view_preview). */
  onCustomerPreview?: (orderId: string) => void
  customerPreviewSlot?: ReactNode
}

// Internal work-item ids leak into log titles as "Report:3B6E…::V7"-style suffixes; strip them so the stream stays
// human-readable.
const ID_JUNK = /(?:Report|Review):{0,2}\s*[0-9A-Fa-f][0-9A-Fa-f ‐-]{7,}(?:::V\d+)?/g

function cleanSummary(text: string | undefined): string {
  if (!text) return ''
  return text.replace(ID_JUNK, '').replace(/\s{2,}/g, ' ').trim()
}

interface LogRow {
  key: string
  timestamp: number
  sessionTitle: string
  roleName?: string
  type: string
  summary: string
  detail?: string
}

function isPrimary(session: Session): boolean {
  if (session.mode === 'child') return false
  return !session.parentSessionId
}

function isActiveStatus(status: string): boolean {
  return !TERMINAL_STATUSES.has(status)
}

function startOfToday(): number {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  return d.getTime()
}

function collectWorkItemRows(session: Session): RoleWorkItemRow[] {
  const byId = new Map<string, RoleWorkItemRow>()
  for (const summary of Object.values(session.roleWorkItems ?? {})) {
    for (const row of summary.workItems) byId.set(row.workItemId, row)
  }
  return Array.from(byId.values())
}

/** Existing session fields are historical labels, not invoice/currency proof.
 * Missing/bad values stay unknown. A recorded zero never proves a free call. */
function sessionCostRecord(value: unknown, field: 'USD' | 'CNY', zh: boolean): {
  text: string; state: 'unknown' | 'legacy_recorded' | 'legacy_zero_ambiguous'
} {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    return { text: zh ? '未知' : 'Unknown', state: 'unknown' }
  }
  const decimals = field === 'USD' ? 3 : 2
  const symbol = field === 'USD' ? '$' : '¥'
  const minimum = field === 'USD' ? 0.001 : 0.01
  const amount = value > 0 && value < minimum
    ? `< ${symbol}${minimum.toFixed(decimals)} ${field}`
    : `${symbol}${value.toFixed(decimals)} ${field}`
  return value === 0
    ? { text: `${amount} · ${zh ? '零值待核' : 'zero unverified'}`, state: 'legacy_zero_ambiguous' }
    : { text: amount, state: 'legacy_recorded' }
}

function formatClock(ts: number, todayStart: number): string {
  if (!ts) return '—'
  const d = new Date(ts)
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  if (ts >= todayStart) return hm
  return `${d.getMonth() + 1}/${d.getDate()} ${hm}`
}

function formatTokens(total: number): string {
  if (total >= 1_000_000) return `${(total / 1_000_000).toFixed(1)}M`
  if (total >= 1_000) return `${(total / 1_000).toFixed(1)}k`
  return String(total)
}

type PanoramaRow = WorkOrderSummary & InboxOrderLike

/** A dialog frame: the builder's scrim and box, so every dialog of the console looks alike. */
function Dialog({ title, sub, testId, zh, onClose, children }: { title: string; sub?: string; testId: string; zh: boolean; onClose: () => void; children: ReactNode }) {
  return (
    <>
      <div className="wf-action-scrim" onClick={onClose} aria-hidden="true" />
      <section className="wf-action-dialog pc-dialog" role="dialog" aria-modal="true" aria-label={title} data-testid={testId}>
        <header className="wf-action-head">
          <div><strong>{title}</strong>{sub && <small>{sub}</small>}</div>
          <button type="button" onClick={onClose} aria-label={zh ? '关闭' : 'Close'}>×</button>
        </header>
        <div className="wf-action-body pc-dialog-body">{children}</div>
      </section>
    </>
  )
}

/** The four numbers, each a way into the inbox on the list it counted. Red only for 出错或逾期 above zero; 0 is not
 *  coloured; a project the server sent no standing for says so instead of drawing zeros. */
function Numbers({ standing, zh, onOpen, testId }: { standing: Standing | null; zh: boolean; onOpen?: (key: StandingKey) => void; testId: string }) {
  if (!standing) return <span className="pc-numbers pc-numbers--unknown" data-testid={testId}><em>{zh ? '还没读到工单去向' : 'order standing not read yet'}</em></span>
  return (
    <span className="pc-numbers" data-testid={testId} data-truncated={standing.truncated ? 'true' : undefined} data-partial={standing.partial ? 'true' : undefined}>
      {STANDING_KEYS.map(key => {
        const hot = key === 'abnormal_or_overdue' && !standing.partial && standing[key] > 0
        return (
          <button key={key} type="button" data-standing={key} className={hot ? 'is-hot' : ''} disabled={!onOpen}
            title={zh ? '到业务工单看这些单' : 'Open these orders in the inbox'}
            onClick={() => onOpen?.(key)}>
            <strong>{standingText(standing, key, zh)}</strong> {zh ? `单${STANDING_WORDS[key][0]}` : STANDING_WORDS[key][1]}
          </button>
        )
      })}
    </span>
  )
}

export function DashboardPage({
  projects, onEnterProject, onOpenOrg, orgSlot, projectOverview = null, onAskProjectOverview, manageSlot, createSlot, showAllSeq = 0, recordedCostObservation, connected = true,
  projectContext, onNavigate, onComposeWorkflow, adoptions, onAdoptWorkflow, onSetAdoptionStatus, onDeriveWorkflow, sessions, agents, dutyLog, dutyLogError, onRefreshDutyLog,
  onOpenSession, artifactsSlot, filesSlot, memorySlot, onOpenFiles, usageByProject = {}, onAskUsage, projectFolderSlot, onOpenStorage, workflows, workOrders, peopleSlot, tabRequest = null,
  employees = [], profile = null, profileState = EMPTY_READ, onSaveProfile, connections = null, connectionsState = EMPTY_READ, onSaveConnection, onAskPanorama, tracked = {},
  onOpenNode, onCustomerPreview, customerPreviewSlot, salesRecordsSlot,
}: DashboardPageProps) {
  const { t, translateMaybe, locale } = useI18n()
  const zh = locale.startsWith('zh')
  const w = (cn: string, en: string) => (zh ? cn : en)
  const todayStart = startOfToday()
  const projectId = projectContext?.project_id ?? ''
  const [compositionSource, setCompositionSource] = useState<string | null>(null)

  const composeInto = (callerId: string, targetId: string) => {
    if (!connected || !onComposeWorkflow || !targetId || callerId === targetId) return
    onComposeWorkflow(callerId, targetId)
    setCompositionSource(null)
  }

  const dropWorkflow = (event: DragEvent<HTMLElement>, callerId: string) => {
    event.preventDefault()
    const targetId = event.dataTransfer.getData('application/x-superones-workflow') || compositionSource || ''
    composeInto(callerId, targetId)
  }

  // ── names ────────────────────────────────────────────────────────────────
  // An employee id is never shown as is: the roster's name, or a plain word when the roster does not know the id.
  const nameOf = (employeeId: string): string => employees.find(person => person.employee_id === employeeId)?.name || w('同事', 'a colleague')
  const flowOf = (id: string) => (workflows ?? []).find(flow => flow.id === id) ?? null
  const flowName = (id: string) => flowOf(id)?.name?.trim() || w('业务流', 'flow')
  const rows = useMemo(() => (workOrders ?? []) as PanoramaRow[], [workOrders])
  // A flow derived for another project is that project's own: not listed here.
  const reportFocus = typeof window === 'undefined' ? '' : reportProject(window.location.search)
  const ownFlows = useMemo(() => (workflows ?? []).filter(flow => reportFocus && reportFocus === projectId ? flow.project_id === projectId : !flow.project_id || flow.project_id === projectId), [workflows, projectId, reportFocus])
  const workflowComposition = useMemo(
    () => workflows === undefined || workflows === null ? workflows : projectWorkflowComposition(ownFlows, rows, undefined),
    [workflows, ownFlows, rows],
  )
  // Which flows carry orders here (the server's per-flow standing, else the ids on the rows): the flows tab says
  // when an order sits on a flow the project no longer lists.
  const spread = useMemo(() => {
    const ids = new Set<string>(Object.keys(projectOverview?.[projectId]?.flows ?? {}))
    for (const order of rows) if ((order.workflow_id ?? '').trim()) ids.add(order.workflow_id.trim())
    return [...ids].sort().map(flowId => ({ flowId, standing: flowStandingOf(projectOverview, projectId, flowId), orders: rows.filter(order => order.workflow_id === flowId).length }))
  }, [projectOverview, projectId, rows])

  const stats = useMemo(() => {
    let imageCostTotal: number | null = null
    let imagesTotal: number | null = null
    let imageCostUnknown = false
    let imageCountUnknown = false
    let imageCostOverflow = false
    let imageCountOverflow = false
    let imageCostZeroAmbiguous = false
    let imageCountZeroAmbiguous = false
    let tokensTotal = 0
    for (const session of sessions) {
      // Session recordedCostUsd stays on the session row. The project headline
      // reads recordedCostObservation only, so this loop does not add it up.
      // Independent historical observations. A missing count never hides an
      // amount, and neither field can turn an absent/bad record into zero.
      const imageAmount = session.imageCostCny
      if (typeof imageAmount === 'number' && Number.isFinite(imageAmount) && imageAmount >= 0) {
        imageCostZeroAmbiguous ||= imageAmount === 0
        if (!imageCostOverflow) {
          const next: number = imageCostTotal === null ? imageAmount : imageCostTotal + imageAmount
          if (Number.isFinite(next)) imageCostTotal = next
          else { imageCostTotal = null; imageCostOverflow = true; imageCostUnknown = true }
        }
      } else imageCostUnknown = true
      const imageCount = session.imageCount
      if (typeof imageCount === 'number' && Number.isSafeInteger(imageCount) && imageCount >= 0) {
        imageCountZeroAmbiguous ||= imageCount === 0
        if (!imageCountOverflow) {
          const next: number = imagesTotal === null ? imageCount : imagesTotal + imageCount
          if (Number.isSafeInteger(next)) imagesTotal = next
          else { imagesTotal = null; imageCountOverflow = true; imageCountUnknown = true }
        }
      } else imageCountUnknown = true
      tokensTotal += session.totalTokens ?? 0
    }
    return {
      imageCostTotal,
      imagesTotal,
      imageCostUnknown,
      imageCountUnknown,
      imageCostZeroAmbiguous,
      imageCountZeroAmbiguous,
      tokensTotal,
    }
  }, [sessions])

  const orders = useMemo(
    () => sessions.filter(isPrimary).slice().sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0)),
    [sessions],
  )

  const logRows = useMemo<LogRow[]>(() => {
    const found: LogRow[] = []
    for (const session of sessions) {
      const title = session.title || session.taskId
      for (const entry of (session.workItemLog ?? []) as WorkItemProgressEntry[]) {
        found.push({
          key: `${session.taskId}-wi-${entry.timestamp}-${entry.type}-${found.length}`,
          timestamp: entry.timestamp,
          sessionTitle: title,
          roleName: entry.roleName,
          type: entry.type,
          summary: cleanSummary(entry.workItemProjectionTitle) || cleanSummary(entry.detail),
          detail: entry.detail,
        })
      }
      // Child/task sessions carry the fine-grained runtime log; primary company sessions already surface theirs
      // through workItemLog above.
      if (!session.workItemLog?.length) {
        for (const entry of (session.progressLog ?? []) as ProgressEntry[]) {
          found.push({
            key: `${session.taskId}-p-${entry.timestamp}-${entry.seq ?? 0}-${found.length}`,
            timestamp: entry.timestamp,
            sessionTitle: title,
            roleName: session.workItemRoleName,
            type: entry.type,
            summary: cleanSummary(entry.summary),
            detail: entry.detail,
          })
        }
      }
    }
    found.sort((a, b) => b.timestamp - a.timestamp)
    return found.slice(0, 60)
  }, [sessions])

  const staffRows = useMemo(() => {
    const workload = new Map<string, number>()
    for (const session of sessions) {
      if (!isActiveStatus(session.status)) continue
      for (const id of session.assigneeIds) workload.set(id, (workload.get(id) ?? 0) + 1)
    }
    return agents.map(agent => ({ agent, busyCount: workload.get(agent.agent_id) ?? 0 }))
  }, [agents, sessions])

  type TKey = Parameters<typeof t>[0]

  const statusLabel = (status: string): string => {
    const key = `dashboard.status.${status}` as TKey
    const label = t(key)
    return label === key ? status : label
  }

  const modeLabel = (session: Session): string => {
    const mode = session.isCompanyRuntime || session.execMode === 'company' || session.execMode === 'org'
      ? (session.execMode === 'org' ? 'org' : 'company')
      : 'task'
    return t(`dashboard.mode.${mode}` as TKey)
  }

  const statusClass = (session: Session): string => {
    if ((session.pendingPermissionCount ?? 0) > 0 || BLOCKED_STATUSES.has(session.status)) return 'warn'
    if (session.status === 'failed' || session.status === 'cancelled') return 'bad'
    if (session.status === 'done' || session.status === 'delivered') return 'good'
    return 'run'
  }

  // ── levels, tabs, what is open ───────────────────────────────────────────
  // Two levels, as the prototype has them: every project, or one project with its four tabs. The console always has
  // a project open (every request carries one), so the page opens inside it; 全部项目 is one step back.
  const [level, setLevel] = useState<ProjectLevel>('project')
  // The all-projects level has two boards: the projects, and the company's people and organisation.
  const [allBoard, setAllBoard] = useState<'projects' | 'org'>('projects')
  // The top bar's project chip was clicked: go to where projects are chosen, and read how each one stands.
  useEffect(() => {
    if (showAllSeq <= 0) return
    setLevel('all'); setAllBoard('projects'); onAskProjectOverview?.()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- a click, not a subscription to the callback
  }, [showAllSeq])
  // A request already here when the page mounts (the builder's 去绑定, a test) is honoured at once; later ones by the effect.
  const [tab, setTab] = useState<ProjectTab>(() => (tabRequest && tabRequest.seq > 0 ? tabRequest.tab : 'overview'))
  const [collab, setCollab] = useState<CollabTab>(() => (tabRequest && tabRequest.seq > 0 && tabRequest.collab ? tabRequest.collab : 'steps'))
  // Asked from outside (the builder's 去绑定 → 协作与接入 · 人员与职责): inside the open project, on that tab.
  useEffect(() => {
    if (!tabRequest || tabRequest.seq <= 0) return
    setLevel('project'); setTab(tabRequest.tab)
    if (tabRequest.collab) setCollab(tabRequest.collab)
  }, [tabRequest])
  // The project level opens on the server's standing and the project's own boundary and lanes: asked once per
  // project as the page opens on it (and again on the click to 全部项目, which reads every project).
  useEffect(() => {
    if (!projectId) return
    onAskProjectOverview?.()
    onAskPanorama?.()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per project, not a subscription to the callbacks
  }, [projectId])
  // 资料与数据 reads a project's files and storage; nothing is read until the tab is actually opened -- and it is
  // read again when the project changes, or the page would keep showing the last project's files under this name.
  useEffect(() => {
    if (level === 'project' && tab === 'data') onOpenFiles?.()
    if (level === 'project' && tab === 'data') onOpenStorage?.()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- opening the tab, not a subscription to the callback
  }, [level, tab, projectContext?.project_id])
  const off = (block: Parameters<typeof blockHidden>[0]) => blockHidden(block, level, tab)

  // 业务全景: only the FIRST flow line expanded by default (owner's ruling), the rest folded to one row with counts.
  const [foldedFlows, setFoldedFlows] = useState<Record<string, boolean>>({})
  const flowExpanded = (flowId: string, index: number) => foldedFlows[flowId] === undefined ? index === 0 : !foldedFlows[flowId]
  // The stage whose 阶段交接 is open, per flow.
  const [openStage, setOpenStage] = useState<Record<string, string>>({})
  const [boundaryOpen, setBoundaryOpen] = useState(false)
  // 步骤责任: which flow, and whether every step or only the human gates.
  const [stepsFlow, setStepsFlow] = useState('')
  const [allSteps, setAllSteps] = useState(false)
  // The one dialog open: the boundary editor, one lane's 配置交接, or the role preview.
  const [dialog, setDialog] = useState<{ kind: 'boundary' } | { kind: 'lane'; id: string } | { kind: 'preview' } | null>(null)
  const [previewPerson, setPreviewPerson] = useState('')
  // The tracker key of the save the open dialog sent: set as its form submits, cleared as it closes. A receipt counts
  // for a dialog only after it submitted, so a settled entry left by an earlier dialog (the tracker keeps it) is
  // neither a refusal to show again nor an acceptance to close on.
  const [attempted, setAttempted] = useState('')
  const closeDialog = () => { setDialog(null); setAttempted('') }
  useEffect(() => { setDialog(null); setAttempted(''); setFoldedFlows({}); setOpenStage({}); setStepsFlow(''); setPreviewPerson('') }, [projectId])
  // A save whose receipt came back accepted closes its dialog; a refusal, a timeout or a cut leaves it open with the
  // words under the button (the server's sentence for a refusal), and nothing is sent again on its own.
  const attemptedEntry = attempted ? tracked[attempted] : undefined
  useEffect(() => {
    if (attempted && attemptedEntry?.state === 'settled' && attemptedEntry.code === '') closeDialog()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the receipt's arrival, not the dialog's identity
  }, [attempted, attemptedEntry?.state, attemptedEntry?.code])

  const project = projects?.find(item => item.id === projectId)
  const currentProfile = profile ?? profileFrom(project?.profile) ?? null
  const lanes = connections ?? []
  const standing = standingOf(projectOverview, projectId)
  const attention = attentionTop(rows, 3)
  const projectName = project && project.name && project.name !== project.id ? project.name : projectId
  const managerName = currentProfile?.manager_name || (currentProfile?.manager_employee_id ? nameOf(currentProfile.manager_employee_id) : '')
  const customerWord = projectContext?.customer_id ? `${w('客户', 'customer')} ${projectContext.customer_id}` : w('未绑定客户', 'no customer bound')
  const firstStagedFlow = ownFlows.find(flow => stagesOf(flow).length > 0) ?? null
  const stageName = (id: string) => (firstStagedFlow ? stagesOf(firstStagedFlow).find(stage => stage.id === id)?.name : '') || w('待关联', 'not linked yet')

  // ── the boundary and the lane editors ────────────────────────────────────
  // The server's own sentence when it sent one (`why_zh` names the exact command and where the backup goes).
  const migrationNote = (
    <p className="pc-migration" data-testid="panorama-needs-migration" role="status">
      {w('要先运行 ampuly db-version --apply，这一项才能读写。', 'Run ampuly db-version --apply first; until then this cannot be read or written.')}
      {(profileState.why || connectionsState.why) && <><br />{profileState.why || connectionsState.why}</>}
    </p>
  )
  // What a dialog's save button says: busy / no receipt / not sent in the tracker's words; a refusal in the server's
  // sentence (the tracker leaves a settled entry unworded on purpose -- the page says it); nothing for a receipt that
  // is not this dialog's (see `attempted`).
  const saveState = (key: string): SaveState => {
    const entry = tracked[key]
    if (!entry || entry.state === 'idle') return IDLE_SAVE
    if (entry.state === 'settled') return attempted === key ? { state: 'settled', code: entry.code, words: refusalText(entry.code, zh) } : IDLE_SAVE
    return { state: entry.state, code: entry.code, words: entry.words }
  }

  const boundaryDialog = dialog?.kind === 'boundary' && (
    <BoundaryDialog zh={zh} profile={currentProfile ?? { type: '', start: '', scope: '', end: '', manager_employee_id: '', manager_name: '', updated_at: '' }}
      employees={employees.filter(person => person.kind !== 'ai')} connected={connected} save={saveState('project_profile_set')}
      onClose={closeDialog} onSave={onSaveProfile && (profile => { setAttempted('project_profile_set'); onSaveProfile(profile) })} />
  )
  const laneDialog = dialog?.kind === 'lane' && (() => {
    const lane = lanes.find(item => item.id === dialog.id)
    if (!lane) return null
    const key = keyOf('project_connections_set', lane.id)
    return (
      <LaneDialog zh={zh} lane={lane} stages={firstStagedFlow ? stagesOf(firstStagedFlow) : []} connected={connected}
        save={saveState(key)} onClose={closeDialog} onSave={onSaveConnection && (connection => { setAttempted(key); onSaveConnection(connection) })} />
    )
  })()
  const previewDialog = dialog?.kind === 'preview' && (
    <Dialog title={w('员工与客户视角 · 只读预览', 'An employee’s or the customer’s view · read only')} testId="role-preview" zh={zh} onClose={() => setDialog(null)}>
      <p className="pc-notice">{w('仅检验信息范围，不模拟登录或授权。正式系统必须由后端按身份隔离。', 'Checks what each person would see; it signs nobody in and grants nothing. The server isolates by identity.')}</p>
      <div className="pc-person-picker" data-testid="role-preview-people">
        {employees.filter(person => person.kind !== 'ai').map(person => (
          <button key={person.employee_id} type="button" className={`shell-btn${previewPerson === person.employee_id ? ' shell-btn--primary' : ''}`} data-person={person.employee_id}
            onClick={() => setPreviewPerson(person.employee_id)}>{person.name} · {w('员工', 'employee')}</button>
        ))}
        {projectContext?.customer_id && (
          <button type="button" className={`shell-btn${previewPerson === 'customer' ? ' shell-btn--primary' : ''}`} data-person="customer" onClick={() => setPreviewPerson('customer')}>
            {rows.find(order => order.customer_id === projectContext.customer_id)?.customer_name?.trim() || w('客户', 'the customer')} · {w('客户', 'customer')}
          </button>
        )}
        {employees.filter(person => person.kind !== 'ai').length === 0 && !projectContext?.customer_id && <small>{w('本项目还没有成员。', 'This project has no members yet.')}</small>}
      </div>
      {!previewPerson && <p className="pc-empty">{w('选择一位项目成员，预览他需要看见的材料与任务。这里不会切换登录身份。', 'Pick a member to preview what they need to see. Nobody is signed in as anyone else.')}</p>}
      {previewPerson && previewPerson !== 'customer' && (() => {
        const items = employeePreviewRows(rows, previewPerson, flowName, zh)
        return (
          <section data-testid="role-preview-employee">
            <h3>{w('分给我的步骤', 'Steps assigned to me')} · {nameOf(previewPerson)}</h3>
            {items.length === 0 ? <p className="pc-empty">{w('当前没有属于该成员的任务或交付。', 'Nothing is assigned to this member right now.')}</p> : (
              <ul className="pc-person-tasks">
                {items.map(item => (
                  <li key={item.orderId} data-order={item.orderId}>
                    <strong>{item.title}</strong>
                    <span>{item.status}{item.node ? ` · ${item.node}` : ''}{item.standard ? ` · ${item.standard}` : ''}</span>
                    <small>{w('先阅读固定版本材料，再提交结果或修改意见。', 'Read the fixed version of the materials first, then hand in the result or the feedback.')}</small>
                  </li>
                ))}
              </ul>
            )}
            <small className="pc-muted">{w('只读视角预览，不在此代替本人审批，也不发送消息。', 'A read-only preview: nothing is decided for them here and no message is sent.')}</small>
          </section>
        )
      })()}
      {previewPerson === 'customer' && (() => {
        const items = customerPreviewOrders(rows, projectContext?.customer_id ?? '')
        return (
          <section data-testid="role-preview-customer">
            <h3>{w('我的交付', 'My deliveries')}</h3>
            {items.length === 0 ? <p className="pc-empty">{w('当前没有属于该客户的工单。', 'This customer has no orders right now.')}</p> : (
              <ul className="pc-person-tasks">
                {items.map(order => {
                  const line = orderLine(order, flowName(order.workflow_id), nameOf, zh)
                  return (
                    <li key={order.order_id} data-order={order.order_id}>
                      <strong>{line.title}</strong>
                      <span>{line.step}{line.word ? ` · ${line.word}` : ''}</span>
                      {onCustomerPreview && <button type="button" className="shell-btn" data-testid="role-preview-customer-open" disabled={!connected} onClick={() => onCustomerPreview(order.order_id)}>{w('看客户视角', 'See as the customer')}</button>}
                    </li>
                  )
                })}
              </ul>
            )}
            {customerPreviewSlot}
            <small className="pc-muted">{w('仅显示指定客户材料。确认或要求修改，应关联本轮交付版本。', 'Only that customer’s materials. Accepting or asking for changes refers to this round’s version.')}</small>
          </section>
        )
      })()}
    </Dialog>
  )

  // ── one flow line ────────────────────────────────────────────────────────
  const renderFlowLine = (flow: WorkflowSummary, index: number) => {
    const stages = stagesOf(flow)
    const nodes = (flow.nodes ?? []) as PanoramaNode[]
    const placed = placedOrders(rows, flow.id, stages)
    const unplaced = placed.filter(item => !item.stage)
    const expanded = flowExpanded(flow.id, index)
    const flowStanding = flowStandingOf(projectOverview, projectId, flow.id)
    const opened = openStage[flow.id] ? stages.find(stage => stage.id === openStage[flow.id]) ?? null : null
    const orderButton = (order: PanoramaRow, className = '') => {
      const line = orderLine(order, flowName(order.workflow_id), nameOf, zh)
      return (
        <button key={order.order_id} type="button" className={`pc-order${line.hot ? ' is-hot' : ''}${className ? ` ${className}` : ''}`} data-order={order.order_id} disabled={!onNavigate}
          onClick={() => onNavigate?.('orders', `order:${order.order_id}`)}>
          <strong>{line.title}</strong>
          <small>{[line.word || line.step, line.who ? `${w('轮到', 'turn:')} ${line.who}` : '', line.due].filter(Boolean).join(' · ')}</small>
        </button>
      )
    }
    return (
      <article key={flow.id} className={`pc-flowline${expanded ? ' is-expanded' : ''}`} data-testid="panorama-flow" data-flow={flow.id} data-expanded={expanded ? 'true' : 'false'}>
        <header className="pc-flowline-head">
          <button type="button" className="pc-flowline-toggle" aria-expanded={expanded} data-testid="panorama-flow-toggle"
            onClick={() => setFoldedFlows(current => ({ ...current, [flow.id]: expanded }))}>
            <h3>{flow.name?.trim() || w('业务流', 'Flow')}</h3>
            <small>{flow.version ? `v${flow.version}` : w('草稿', 'draft')}{flow.project_id ? ` · ${w('本项目专用', 'this project’s own')}` : ''}</small>
          </button>
          <Numbers standing={flowStanding} zh={zh} onOpen={onNavigate ? key => onNavigate('orders', standingTarget(key, flow.id)) : undefined} testId="panorama-flow-numbers" />
        </header>
        {expanded && (
          <>
            <div className="pc-pan-scroll">
              <div className="pc-pan-rail">
                <aside className="pc-endcap" data-testid="panorama-endcap-in">
                  <h4>{w('业务从哪里来', 'Where the work comes from')}</h4>
                  <p>{currentProfile?.start || BOUNDARY_PLACEHOLDERS.start[zh ? 0 : 1]}</p>
                  {endcapLanes(lanes, 'in').map(lane => (
                    <button key={lane.id} type="button" data-lane={lane.id} onClick={() => setDialog({ kind: 'lane', id: lane.id })}>
                      <span>{lane.tool || lane.name}</span><small>{lane.status}</small>
                    </button>
                  ))}
                  <small>{w('项目接入规划 · 本流绑定待核验', 'Planned connections · not verified for this flow')}</small>
                </aside>
                <div className="pc-stage-strip" aria-label={w(`${flow.name}的业务阶段`, `${flow.name}'s stages`)}>
                  {stages.length === 0 ? (
                    <div className="pc-unmapped" data-testid="panorama-unmapped">
                      <h4>{w('这条业务流还没有阶段映射', 'This flow has no stage map yet')}</h4>
                      <p>{w('保留它独立的流程与工单，不套用其他业务的步骤。', 'It keeps its own steps and orders; another business’s stages are not laid over it.')}</p>
                      <div className="pc-stage-orders">
                        {placed.slice(0, 2).map(item => orderButton(item.order))}
                        {placed.length === 0 && <small>{w('暂无在途工单', 'no orders in flight')}</small>}
                        {placed.length > 2 && <button type="button" className="pc-link" disabled={!onNavigate} onClick={() => onNavigate?.('orders', standingTarget('in_flight', flow.id))}>{w(`另 ${placed.length - 2} 单 →`, `${placed.length - 2} more →`)}</button>}
                      </div>
                    </div>
                  ) : stages.map(stage => {
                    const view = stageView(stage, nodes, nameOf, zh)
                    const here = placed.filter(item => item.stage === stage.id)
                    const active = opened?.id === stage.id
                    return (
                      <section key={stage.id} className={`pc-stage-tile pc-stage-tile--${view.mode}${active ? ' is-active' : ''}`} data-testid="panorama-stage" data-stage={stage.id}>
                        <button type="button" className="pc-stage-open" aria-expanded={active}
                          onClick={() => setOpenStage(current => ({ ...current, [flow.id]: active ? '' : stage.id }))}>
                          <strong>{stage.name}</strong>
                          <span>{STAGE_MODE_WORDS[view.mode][zh ? 0 : 1]}{stage.parallel ? w(' · 含并行', ' · parallel inside') : ''}</span>
                          <small>{view.names.join('、') || w('按节点配置执行', 'as the steps are configured')}</small>
                        </button>
                        <div className="pc-stage-orders" data-testid="panorama-stage-orders">
                          {here.slice(0, 2).map(item => orderButton(item.order))}
                          {here.length === 0 && <small>{w('暂无已定位的在途工单', 'no order stands here')}</small>}
                          {here.length > 2 && <button type="button" className="pc-link" data-testid="panorama-stage-more" disabled={!onNavigate} onClick={() => onNavigate?.('orders', standingTarget('in_flight', flow.id))}>{w(`另 ${here.length - 2} 单 →`, `${here.length - 2} more →`)}</button>}
                        </div>
                      </section>
                    )
                  })}
                </div>
                <aside className="pc-endcap" data-testid="panorama-endcap-out">
                  <h4>{w('交付到哪里', 'Where it is delivered')}</h4>
                  <p>{currentProfile?.end || BOUNDARY_PLACEHOLDERS.end[zh ? 0 : 1]}</p>
                  {endcapLanes(lanes, 'out').map(lane => (
                    <button key={lane.id} type="button" data-lane={lane.id} onClick={() => setDialog({ kind: 'lane', id: lane.id })}>
                      <span>{lane.tool || lane.name}</span><small>{lane.status}</small>
                    </button>
                  ))}
                  <small>{w('项目接入规划 · 本流绑定待核验', 'Planned connections · not verified for this flow')}</small>
                </aside>
              </div>
            </div>
            <footer className="pc-flowline-foot">
              <div>
                <button type="button" className="pc-link" disabled={!onNavigate} onClick={() => onNavigate?.('orders', `flow:${flow.id}`)}>{w('本流工单', 'This flow’s orders')}</button>
                <button type="button" className="pc-link" disabled={!onNavigate} onClick={() => onNavigate?.('workflows', flow.id)}>{w('在搭建台编辑', 'Edit in Builder')}</button>
                <button type="button" className="pc-link" data-testid="panorama-responsibility" onClick={() => { setStepsFlow(flow.id); setTab('collab'); setCollab('steps') }}>{w('谁负责哪一步', 'Who does which step')}</button>
              </div>
              {unplaced.length > 0 && stages.length > 0 && (
                <button type="button" className="pc-link" data-testid="panorama-unplaced" disabled={!onNavigate} onClick={() => onNavigate?.('orders', standingTarget('in_flight', flow.id))}>
                  {w(`${unplaced.length} 单待定位 →`, `${unplaced.length} not placed →`)}
                </button>
              )}
            </footer>
            {opened && (() => {
              const view = stageView(opened, nodes, nameOf, zh)
              const here = placed.filter(item => item.stage === opened.id)
              const laneOf = (id: string) => lanes.find(lane => lane.id === id)
              return (
                <div className="pc-expanded" data-testid="panorama-stage-body" data-stage={opened.id}>
                  <section className="pc-handoff">
                    <header>
                      <div><small>{w('阶段交接', 'Handover')} · {opened.parallel ? w('内部含并行与依赖', 'parallel steps and dependencies inside') : w('依照业务流依赖推进', 'in the flow’s order')}</small><h4>{opened.name}</h4></div>
                      {view.nodes[0] && onOpenNode && <button type="button" className="shell-btn" onClick={() => onOpenNode(flow.id, view.nodes[0].id)}>{w('在画布中查看', 'See on the canvas')}</button>}
                    </header>
                    <dl>
                      <div><dt>{w('收到什么', 'What comes in')}</dt><dd>{opened.input || w('待补', 'to be written')}</dd></div>
                      <div><dt>{w('这一段做什么', 'What this part does')}</dt><dd>{opened.work || w('待补', 'to be written')}<small>{w('执行能力：', 'Executed by: ')}{[...new Set(view.nodes.map(node => node.responsibility || (node.skills ?? []).join('、') || node.role || '').filter(Boolean))].join('、') || w('待配置', 'to configure')}</small></dd></div>
                      <div><dt>{w('谁来确认', 'Who confirms')}</dt><dd>
                        {view.gates.length === 0 ? w('本阶段无已声明的人工关口；异常由项目管理者协调。', 'No human gate is declared in this part; the project manager handles exceptions.')
                          : view.gates.map(node => (
                            <p key={node.id}>{node.name || node.id}：{stageView({ ...opened, nodes: [node.id] }, [node], nameOf, zh).names[0]}
                              {onOpenNode && <button type="button" className="pc-link" data-testid="panorama-configure-gate" onClick={() => onOpenNode(flow.id, node.id)}>{w('配置交接', 'Configure')}</button>}
                            </p>
                          ))}
                      </dd></div>
                      <div><dt>{w('交出什么', 'What goes out')}</dt><dd>{opened.output || w('待补', 'to be written')}
                        <details><summary>{w('查看节点交付约定', 'The steps’ deliverables')}</summary>
                          {view.nodes.map(node => <p key={node.id}><b>{node.name || node.id}</b><br />{(node.deliverables ?? []).join('、') || w('待补交付约定', 'deliverables to be written')}</p>)}
                        </details>
                      </dd></div>
                      <div><dt>{w('交给下一步', 'Handed to')}</dt><dd>{opened.next || w('待补', 'to be written')}<small>{w('要求修改：', 'Changes asked for: ')}{opened.back || w('按流程配置', 'as the flow says')}。{w('具体回转由节点配置决定。', 'Where it goes back is the step’s own setting.')}</small></dd></div>
                    </dl>
                    <div className="pc-node-links" data-testid="panorama-node-links">
                      <small>{w('对应画布节点', 'Steps on the canvas')}</small>
                      {view.nodes.length === 0 ? <span>{w('当前草稿已无对应节点，请重新映射阶段。', 'The draft has no such step any more; map the stage again.')}</span>
                        : view.nodes.map(node => (
                          <button key={node.id} type="button" className="pc-link" data-node={node.id} disabled={!onOpenNode} onClick={() => onOpenNode?.(flow.id, node.id)}>{node.name || node.id}</button>
                        ))}
                    </div>
                  </section>
                  <aside className="pc-stage-side">
                    <section>
                      <h4>{w('用什么协作', 'Working through')}</h4>
                      {/* The stage's own lane list when the stage map names one; else the lanes the owner hung on this stage. */}
                      {(opened.systems.length ? opened.systems.map(laneOf).filter((lane): lane is ConnectionLane => Boolean(lane)) : lanes.filter(lane => lane.stage === opened.id)).map(lane => (
                        <button key={lane.id} type="button" className="pc-system-mini" data-lane={lane.id} onClick={() => setDialog({ kind: 'lane', id: lane.id })}>
                          <strong>{lane.name}</strong><span>{lane.tool || w('待选择', 'to choose')}</span><small>{lane.status} · {lane.mode}</small>
                        </button>
                      ))}
                      <small className="pc-muted">{w('这里只保存接入规划，不创建连接或发送消息。', 'Only the plan is kept here; nothing is connected and nothing is sent.')}</small>
                    </section>
                    <section>
                      <header><h4>{w('这一段的工单', 'Orders in this part')} <b>{here.length}</b></h4>
                        <button type="button" className="pc-link" disabled={!onNavigate} onClick={() => onNavigate?.('orders', standingTarget('in_flight', flow.id))}>{w('全部查看', 'See all')}</button></header>
                      <div className="pc-tasks">
                        {here.length === 0 ? <p className="pc-empty">{w('这个阶段暂无已定位的在途工单。', 'No order stands in this part right now.')}</p> : here.map(item => orderButton(item.order))}
                      </div>
                    </section>
                  </aside>
                </div>
              )
            })()}
          </>
        )}
      </article>
    )
  }

  // ── 步骤责任 ─────────────────────────────────────────────────────────────
  const stepsFlowId = ownFlows.some(flow => flow.id === stepsFlow) ? stepsFlow : (ownFlows[0]?.id ?? '')
  const stepsFlowObj = flowOf(stepsFlowId)
  const stepRows = stepsFlowObj ? responsibilityRows((stepsFlowObj.nodes ?? []) as PanoramaNode[], nameOf, zh, !allSteps) : []

  return (
    <div className="dashboard-page pc-page">
      {level === 'project' && (
        <button type="button" className="dashboard-back" data-testid="dashboard-all-projects" onClick={() => { setLevel('all'); onAskProjectOverview?.() }}>
          {w('← 全部项目', '← All projects')}
        </button>
      )}
      <div className="dashboard-header pc-title">
        <div className="pc-title-text">
          {level === 'project' && projectContext ? (
            <>
              <small data-testid="dashboard-project-line">{[currentProfile?.type, customerWord].filter(Boolean).join(' · ')}</small>
              <h2 className="dashboard-title">{projectName}</h2>
              <Numbers standing={standing} zh={zh} onOpen={onNavigate ? key => onNavigate('orders', standingTarget(key)) : undefined} testId="dashboard-project-numbers" />
            </>
          ) : (
            <>
              <small>{t('dashboard.title')}</small>
              <h2 className="dashboard-title">{w('让每项业务都有清晰的起点和交付。', 'Every business with a clear start and a clear delivery.')}</h2>
              <p className="dashboard-projects-note">{w('选一个项目，查看它如何运转、由谁配合，以及哪里需要你。', 'Pick a project to see how it runs, who takes part, and where it needs you.')}</p>
            </>
          )}
        </div>
        <div className="dashboard-header-right">
          {level === 'all' && createSlot && <div className="dashboard-create-project" data-testid="dashboard-create-project">{createSlot}</div>}
          {level === 'all' && onOpenOrg && !orgSlot && (
            // The company's one roster: people, roles, structure. Company-wide, so its way in is on the overview of
            // every project, not inside one of them.
            <button type="button" className="shell-btn" data-testid="dashboard-people-entry" onClick={onOpenOrg}>{w('协作人员与组织 →', 'People & organisation →')}</button>
          )}
          {level === 'project' && (
            // The project's own two actions, where the prototype has them: its settings (资料与数据, where its
            // administration lives) and a new order (the inbox's own ＋ 新建工单 -- no second order form is made here).
            <>
              <button type="button" className="shell-btn" data-testid="dashboard-project-settings" onClick={() => setTab('data')}>{w('项目设置', 'Project settings')}</button>
              <button type="button" className="shell-btn shell-btn--primary" data-testid="dashboard-new-order" disabled={!onNavigate} onClick={() => onNavigate?.('orders')}>{w('＋ 新建工单', '+ New order')}</button>
            </>
          )}
        </div>
      </div>

      {level === 'project' && (
        <nav className="dashboard-sections dashboard-tabs" role="tablist" aria-label={w('项目页签', 'Project tabs')}>
          {PROJECT_TABS.map(id => (
            <button key={id} type="button" role="tab" data-project-tab={id} aria-selected={tab === id} className={tab === id ? 'is-active' : ''}
              onClick={() => setTab(id)}>{PROJECT_TAB_LABELS[id][zh ? 0 : 1]}</button>
          ))}
        </nav>
      )}

      {/* ── 业务总览 (a): the boundary in one line, 展开 for the three answers and the manager. ── */}
      {projectContext && (
        <details className="pc-scope" data-testid="panorama-boundary" hidden={off('boundary')} open={boundaryOpen} onToggle={event => setBoundaryOpen((event.target as HTMLDetailsElement).open)}>
          <summary>
            <span>{w('本项目的业务边界', 'This project’s business boundary')}</span>
            <strong>{profileState.needsMigration ? w('（要先升级数据库）', '(database behind)') : currentProfile?.scope || BOUNDARY_PLACEHOLDERS.scope[zh ? 0 : 1]}</strong>
            <small>{w('展开说明', 'Details')}</small>
          </summary>
          <div>
            {profileState.needsMigration ? migrationNote : (
              <>
                {profileState.error && <p className="pc-notice" role="alert">{refusalText(profileState.error, zh)}</p>}
                <p><b>{w('从哪里接手：', 'Starts when: ')}</b>{currentProfile?.start || BOUNDARY_PLACEHOLDERS.start[zh ? 0 : 1]}</p>
                <p><b>{w('到哪里结束：', 'Ends when: ')}</b>{currentProfile?.end || BOUNDARY_PLACEHOLDERS.end[zh ? 0 : 1]}</p>
                <p><b>{w('规划管理者：', 'Planning manager: ')}</b>{managerName || w('待配置', 'to configure')} · {w('不授予权限', 'no permission granted')}</p>
                {onSaveProfile && <button type="button" className="shell-btn" data-testid="panorama-boundary-edit" onClick={() => setDialog({ kind: 'boundary' })}>{w('编辑业务范围', 'Edit the boundary')}</button>}
              </>
            )}
          </div>
        </details>
      )}

      {/* ── 业务总览 (b): 现在需要关注 -- the three orders that most need a look, abnormal first, from the server's verdict. ── */}
      {projectContext && (
        <section className="pc-attention" data-testid="panorama-attention" aria-label={w('项目当前重点', 'What needs a look now')} hidden={off('attention')}>
          <header>
            <h3>{attention.total > 0 ? w('现在需要关注', 'Needs a look now') : w('目前没有待你处理或异常的工单', 'Nothing waits on you and nothing is in trouble')}</h3>
            <small>{attention.total > 0 ? w(`${attention.total} 单 · 异常优先`, `${attention.total} orders · trouble first`) : w('仍可在下方查看在途业务', 'The business in flight is below')}</small>
          </header>
          {attention.items.map(order => {
            const line = orderLine(order, flowName(order.workflow_id), nameOf, zh)
            return (
              <button key={order.order_id} type="button" className={`pc-attention-row${line.hot ? ' is-hot' : ''}`} data-order={order.order_id} disabled={!onNavigate}
                onClick={() => onNavigate?.('orders', `order:${order.order_id}`)}>
                <strong>{line.title}</strong>
                <span>{[line.word || line.step, line.who ? `${w('轮到', 'turn:')} ${line.who}` : ''].filter(Boolean).join(' · ')}</span>
                <small>{line.due ? `${line.due}　` : ''}{w('查看 →', 'Open →')}</small>
              </button>
            )
          })}
          {attention.total > 3 && <small>{w('上方项目计数可查看全部待办与异常；此处仅显示最需关注的 3 单。', 'The numbers above open everything waiting or in trouble; only the three that need a look most are shown here.')}</small>}
          {rows.some(order => attentionOf(order).partial && !attentionOf(order).ended) && <small data-testid="panorama-partial">{w('有的单服务端没能判定（暂停或预算没读到），这里可能少算。', 'Some orders could not be judged by the server (pauses or budgets unread); this list may be short.')}</small>}
        </section>
      )}

      {/* ── 业务总览 (c): 业务全景 -- one line per flow, the first expanded, the rest folded to a row with counts. ── */}
      {projectContext && (
        <section className="pc-panorama" data-testid="panorama" hidden={off('panorama')}>
          <div className="pc-pan-heading"><h3>{w('业务全景', 'The business at a glance')}</h3><small>{w('看运行在这里，改逻辑去搭建台', 'See it run here; change how it works in Builder')}</small></div>
          {workflows === null ? <div className="dashboard-empty">{w('正在读取业务流…', 'Loading flows…')}</div>
            : ownFlows.length === 0 ? (
              <section className="pc-empty pc-first">
                <h4>{w('先把业务说清楚，不必先接通所有平台。', 'Say what the business is first; the platforms can wait.')}</h4>
                <ol>
                  <li>{w('描述业务，形成流程草稿', 'Describe the business and get a draft flow')}</li>
                  <li>{w('使用样例检查输入、产出与人工关口', 'Check the inputs, outputs and human gates with a sample')}</li>
                  <li>{w('按需配置人员与原有软件', 'Configure people and your existing software as needed')}</li>
                  <li>{w('完成正式验证后接单', 'Take orders once it is verified')}</li>
                </ol>
                <button type="button" className="shell-btn shell-btn--primary" disabled={!onNavigate} onClick={() => onNavigate?.('workflows')}>{w('创建业务流草稿', 'Draft a flow')}</button>
              </section>
            ) : ownFlows.map(renderFlowLine)}
          {ownFlows.length > 0 && <p className="pc-journey-note">{w('全景是业务阅读分组，不是执行顺序；物料分支仍按真实依赖并行。人员来自当前草稿，工单按各自锁定版本运行；点击阶段展开交接详情。', 'The stages are how the business reads, not the run order; branches still run in parallel by their real dependencies. People come from the current draft; each order runs on the version it locked. Click a stage for its handover.')}</p>}
        </section>
      )}

      {/* ── 业务流: today's flow cards (采用 / 升级 / 暂停 / 恢复 / 撤下 / 派生 / 到搭建台). ── */}
      {workflowComposition !== undefined && (
        <section className="dashboard-card dashboard-workflow-map" id="dash-flows" data-testid="project-workflow-composition" hidden={off('composition')}>
          <div className="dashboard-workflow-map__head">
            <div>
              <h3 className="dashboard-card-title">{w('业务流组合', 'Workflow composition')}</h3>
              <p>{w('设计与运行分开。采用新版本只影响之后创建的工单。把一个业务流拖到另一个卡片上，只会打开组合草稿。', 'Design and running are apart: adopting a new version affects only orders placed after it. Drop one flow on another to stage a composition draft.')}</p>
            </div>
            {workflowComposition && <span>{workflowComposition.length}</span>}
          </div>
          {workflowComposition === null ? (
            <div className="dashboard-empty">{w('正在读取业务流…', 'Loading workflows…')}</div>
          ) : workflowComposition.length === 0 ? (
            <div className="dashboard-empty">{w('这个项目还没有业务流。请先到搭建台创建或接入。', 'This project has no workflows yet. Create or connect one in Builder.')}</div>
          ) : (
            <div className="dashboard-workflow-map__grid">
              {workflowComposition.map(workflow => (
                <article
                  key={workflow.id}
                  className={`dashboard-process-card${compositionSource === workflow.id ? ' is-composition-source' : ''}`}
                  data-workflow-id={workflow.id}
                  draggable={Boolean(onComposeWorkflow && connected)}
                  onDragStart={event => {
                    event.dataTransfer.setData('application/x-superones-workflow', workflow.id)
                    event.dataTransfer.effectAllowed = 'link'
                    setCompositionSource(workflow.id)
                  }}
                  onDragEnd={() => setCompositionSource(null)}
                  onDragOver={event => {
                    if (compositionSource && compositionSource !== workflow.id) event.preventDefault()
                  }}
                  onDrop={event => dropWorkflow(event, workflow.id)}
                >
                  <div className="dashboard-process-card__top">
                    <strong>{workflow.name}</strong>
                    <span>v{workflow.version}</span>
                  </div>
                  <div className="dashboard-process-card__facts">
                    <span title={workflow.status}>{t(`workflows.status.${workflow.status}` as Parameters<typeof t>[0]) === `workflows.status.${workflow.status}` ? workflow.status : t(`workflows.status.${workflow.status}` as Parameters<typeof t>[0])}</span>
                    {/* Where this flow's orders stand (the server's numbers), and the way to them: the inbox on exactly this flow. */}
                    <button type="button" className="dashboard-process-card__orders" data-testid="flow-orders" disabled={!onNavigate || workflow.orderCount === 0}
                      onClick={() => onNavigate?.('orders', `flow:${workflow.id}`)}>
                      {(() => {
                        const own = flowStandingOf(projectOverview, projectId, workflow.id)
                        // No rows here and the server's four numbers (if it sent any) all zero and not a floor: no orders.
                        if (workflow.orderCount === 0 && (!own || (!own.truncated && STANDING_KEYS.every(key => own[key] === 0)))) return w('还没有工单', 'no orders yet')
                        if (!own) return w(`共 ${workflow.orderCount} 单`, `${workflow.orderCount} orders`)
                        return <>{w('在途', 'in flight')} <b>{standingText(own, 'in_flight', zh)}</b>{own.waiting_on_me > 0 && <em> · {w('等你处理', 'waiting on you')} <b>{standingText(own, 'waiting_on_me', zh)}</b></em>} · {w('已交付', 'delivered')} {standingText(own, 'delivered', zh)}</>
                      })()}
                    </button>
                  </div>
                  {(workflow.calls.length > 0 || workflow.missingCalls.length > 0) && (
                    <div className="dashboard-process-card__calls">
                      <span>{w('调用', 'Calls')}</span>
                      {workflow.calls.map(target => <b key={target}>→ {flowName(target)}</b>)}
                      {workflow.missingCalls.map(target => <b key={target} className="is-missing">→ {target} · {w('未接入', 'missing')}</b>)}
                    </div>
                  )}
                  {adoptions !== undefined && (() => {
                    // 项目采用业务流: which version this project runs and whether it takes new orders on it.
                    const adoption = (adoptions ?? []).find(item => item.workflow_id === workflow.id)
                    // This project's own flow has nothing to adopt: say where it stands and offer the next step. (One
                    // adopted before 4.7-A01 keeps its adoption row below: that adoption still decides its orders.)
                    if (workflow.project && !adoption) {
                      const own = ownFlowStep({ status: workflow.status, invokable: (workflows ?? []).find(item => item.id === workflow.id)?.invokable })
                      return (
                        <div className="dashboard-process-card__adoption" data-testid="flow-own-step" data-state={own.kind}>
                          <span className="dashboard-adoption-chip is-own">
                            {own.kind === 'own_draft' ? w('本项目专用 · 草稿：改好、试过、提交测试后就能下单', 'This project’s own · draft: finish, try and submit it for testing to take orders')
                              : own.kind === 'own_testing' ? w('本项目专用 · 测试中：可以试跑下单', 'This project’s own · in testing: trial orders can be placed')
                                : own.kind === 'own_published' ? w('本项目专用 · 已发布：直接下单，不用采用', 'This project’s own · published: place orders, nothing to adopt')
                                  : own.kind === 'own_blocked' ? w('本项目专用 · 现在还不能下单：先到搭建台处理', 'This project’s own · cannot take orders yet: fix it in Builder')
                                    : w('本项目专用 · 已停用', 'This project’s own · retired')}
                          </span>
                          {own.action === 'build' && (
                            <button type="button" data-testid="flow-own-build" disabled={!onNavigate} onClick={() => onNavigate?.('workflows', workflow.id)}>
                              {w('去搭建台完善', 'Finish in Builder')}
                            </button>
                          )}
                          {/* 业务工单 is where orders are placed (＋ 新建工单) and where this flow's orders are listed. */}
                          {own.action === 'order' && (
                            <button type="button" data-testid="flow-own-order" disabled={!onNavigate}
                              title={w('到「业务工单」：点「＋ 新建工单」，选这条业务流，填好开单字段和预算上限', 'Opens Orders: press + New order, pick this flow, fill in its fields and allowance')}
                              onClick={() => onNavigate?.('orders', `flow:${workflow.id}`)}>
                              {own.kind === 'own_testing' ? w('发起订单（试跑）', 'Place an order (trial)') : w('发起订单', 'Place an order')}
                            </button>
                          )}
                        </div>
                      )
                    }
                    const state = adoptionState(workflow, adoption)
                    return (
                      <div className="dashboard-process-card__adoption" data-testid="flow-adoption" data-state={state.kind}>
                        <span className={`dashboard-adoption-chip is-${state.kind}`}>
                          {state.kind === 'active' ? w(`本项目采用 v${adoption?.workflow_version} · 接单中`, `Adopted v${adoption?.workflow_version} · taking orders`)
                            : state.kind === 'paused' ? w(`本项目采用 v${adoption?.workflow_version} · 已暂停接单`, `Adopted v${adoption?.workflow_version} · paused`)
                              : state.kind === 'withdrawn' ? w('已从本项目撤下', 'Withdrawn from this project')
                                : state.kind === 'not_adopted' ? w('本项目未采用（沿用全局入口）', 'Not adopted (the global path)')
                                  : w('发布后才能采用', 'Adoptable once published')}
                        </span>
                        {connected && (state.kind === 'not_adopted' || state.kind === 'withdrawn') && onAdoptWorkflow && (
                          <button type="button" data-testid="flow-adopt" onClick={() => onAdoptWorkflow(workflow.id)}>{w(`采用 v${workflow.version}`, `Adopt v${workflow.version}`)}</button>
                        )}
                        {connected && state.upgradeTo && onAdoptWorkflow && (
                          <button type="button" data-testid="flow-upgrade" onClick={() => onAdoptWorkflow(workflow.id)}>{w(`升级到 v${state.upgradeTo}`, `Upgrade to v${state.upgradeTo}`)}</button>
                        )}
                        {connected && state.kind === 'active' && onSetAdoptionStatus && (
                          <button type="button" data-testid="flow-pause" onClick={() => onSetAdoptionStatus(workflow.id, 'paused')}>{w('暂停接单', 'Pause orders')}</button>
                        )}
                        {connected && state.kind === 'paused' && onSetAdoptionStatus && (
                          <button type="button" data-testid="flow-resume" onClick={() => onSetAdoptionStatus(workflow.id, 'active')}>{w('恢复接单', 'Resume orders')}</button>
                        )}
                        {connected && (state.kind === 'active' || state.kind === 'paused') && onSetAdoptionStatus && (
                          <button type="button" data-testid="flow-withdraw" onClick={() => onSetAdoptionStatus(workflow.id, 'withdrawn')}>{w('撤下', 'Withdraw')}</button>
                        )}
                      </div>
                    )
                  })()}
                  <div className="dashboard-process-card__actions">
                    <button type="button" className="dashboard-process-card__open" onClick={() => onNavigate?.('workflows', workflow.id)} disabled={!onNavigate}>
                      {w('到搭建台', 'Open in Builder')}
                    </button>
                    {/* 派生为本项目草稿 (policy): a global flow stays the template other projects adopt; this
                        project's own changes go into a draft of its own, cut from the version shown here. */}
                    {onDeriveWorkflow && !workflow.project && workflow.status !== 'retired' && (
                      <button type="button" className="dashboard-process-card__derive" data-testid="flow-derive" disabled={!connected}
                        title={w('从这个版本复制出一个只属于本项目的草稿；原流程和其他项目不受影响', 'Copy this version into a draft that belongs to this project only; the original and other projects are untouched')}
                        onClick={() => onDeriveWorkflow(workflow.id)}>
                        {w('派生为本项目草稿', 'Derive for this project')}
                      </button>
                    )}
                    {workflow.project && <span className="dashboard-adoption-chip is-derived" data-testid="flow-derived">{w(`项目 · ${workflow.project}`, `Project · ${workflow.project}`)}</span>}
                    {onComposeWorkflow && (
                      <button
                        type="button"
                        className="dashboard-process-card__compose"
                        data-testid={`workflow-compose-${workflow.id}`}
                        disabled={!connected}
                        onClick={() => {
                          if (!compositionSource) setCompositionSource(workflow.id)
                          else if (compositionSource === workflow.id) setCompositionSource(null)
                          else composeInto(workflow.id, compositionSource)
                        }}
                      >
                        {compositionSource === workflow.id
                          ? w('取消组件选择', 'Cancel component')
                          : compositionSource
                            ? w('组合到这里', 'Compose here')
                            : w('选为组件', 'Use as component')}
                      </button>
                    )}
                  </div>
                </article>
              ))}
            </div>
          )}
          {/* Orders on a flow the project no longer lists (retired, removed) still exist: say so here, or a project
              with five orders shows a flows tab that accounts for none of them. */}
          {(() => {
            const listed = new Set((workflows ?? []).map(item => item.id))
            const orphans = spread.filter(row => !listed.has(row.flowId))
            if (workflowComposition === null || orphans.length === 0) return null
            return (
              <p className="dashboard-flow-orphans" data-testid="dashboard-flow-orphans">
                {w('另有工单属于这个项目已不再列出的业务流：', 'Orders on flows this project no longer lists: ')}
                {orphans.map(row => (
                  <button key={row.flowId} type="button" disabled={!onNavigate} onClick={() => onNavigate?.('orders', `flow:${row.flowId}`)}>
                    {row.flowId} {row.standing ? w(`在途 ${standingText(row.standing, 'in_flight', zh)} · 已交付 ${standingText(row.standing, 'delivered', zh)}`, `${standingText(row.standing, 'in_flight', zh)} in flight · ${standingText(row.standing, 'delivered', zh)} delivered`) : w(`${row.orders} 张`, `${row.orders}`)}
                  </button>
                ))}
              </p>
            )
          })()}
        </section>
      )}

      {/* ── 协作与接入: 步骤责任 / 软件与连接 / 人员与职责. ── */}
      {projectContext && (
        <section className="pc-collab" data-testid="panorama-collab" hidden={off('collab')}>
          <div className="pc-section-head">
            <div>
              <h3>{w('员工与原有软件，一起完成业务。', 'People and your existing software, finishing the work together.')}</h3>
              <p>{w('角色按项目授权；软件按业务交接配置。填写配置不代表连接可用。', 'Roles are granted per project; software is configured per handover. A filled-in plan is not a working connection.')}</p>
            </div>
            <nav className="pc-tabs" aria-label={w('协作与接入', 'People & connections')}>
              {COLLAB_TABS.map(id => (
                <button key={id} type="button" data-collab-tab={id} aria-selected={collab === id} className={collab === id ? 'is-active' : ''} onClick={() => setCollab(id)}>{COLLAB_TAB_LABELS[id][zh ? 0 : 1]}</button>
              ))}
            </nav>
          </div>
          {collab === 'steps' && (
            <section className="pc-responsibility" data-testid="panorama-responsibility-table">
              <header>
                <div>
                  <h4>{w('谁负责哪一步', 'Who does which step')}</h4>
                  <p>{w('执行与确认分别列出；修改配置仍回到原节点，不在这里造第二份规则。', 'Execution and confirmation are listed apart; changes are made on the step itself, never as a second rule here.')}</p>
                </div>
                {ownFlows.length > 1 && (
                  <select aria-label={w('选择责任表业务流', 'Flow')} value={stepsFlowId} onChange={event => setStepsFlow(event.target.value)}>
                    {ownFlows.map(flow => <option key={flow.id} value={flow.id}>{flow.name?.trim() || flow.id}</option>)}
                  </select>
                )}
              </header>
              <div className="pc-responsibility-tools">
                <button type="button" className="pc-link" data-testid="panorama-all-steps" onClick={() => setAllSteps(value => !value)}>{allSteps ? w('只看人工关口', 'Human gates only') : w('查看全部步骤', 'Every step')}</button>
                <small>{w('当前草稿 · 不改变已派发任务', 'The current draft · tasks already handed out are unchanged')}</small>
                <button type="button" className="pc-link" data-testid="panorama-role-preview" onClick={() => setDialog({ kind: 'preview' })}>{w('员工 / 客户视角', 'As an employee / the customer')}</button>
              </div>
              {stepRows.length === 0 ? (
                <div className="pc-empty">{stepsFlowObj && (stepsFlowObj.nodes ?? []).length > 0 ? w('本流程没有已声明的人工关口。', 'This flow declares no human gate.') : w('尚无可读取的节点定义；请先在搭建台配置这条业务流。', 'No steps to read yet; configure the flow in Builder first.')}</div>
              ) : (
                <div className="pc-responsibility-scroll">
                  <table>
                    <thead><tr><th>{w('步骤 / 执行', 'Step / executed by')}</th><th>{w('由谁确认', 'Confirmed by')}</th><th>{w('渠道 / 期限', 'Channel / time limit')}</th><th>{w('交付与意见返回', 'Deliverables and feedback')}</th><th></th></tr></thead>
                    <tbody>
                      {stepRows.map(row => (
                        <tr key={row.node.id} data-node={row.node.id} data-gate={row.gate ? 'true' : 'false'}>
                          <td><strong>{row.name}</strong><small>{row.execution}</small></td>
                          <td>{row.confirmer}<small>{row.mode === 'customer' ? w('客户确认', 'the customer confirms') : row.mode === 'human' ? w('人工确认', 'a person confirms') : ''}</small></td>
                          <td>{row.channel}<small>{row.deadline}</small></td>
                          <td>{row.deliverables}<small>{w('意见返回：', 'Feedback goes to: ')}{row.returnTo}</small></td>
                          <td>{onOpenNode && stepsFlowObj && <button type="button" className="pc-link" data-testid="panorama-node-edit" onClick={() => onOpenNode(stepsFlowObj.id, row.node.id)}>{row.gate ? w('配置交接', 'Configure') : w('编辑节点', 'Edit step')}</button>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          )}
          {collab === 'systems' && (
            <div className="pc-systems" data-testid="panorama-connections">
              {salesRecordsSlot && <div data-testid="project-sales-records">{salesRecordsSlot}</div>}
              {connectionsState.needsMigration ? migrationNote : connections === null && !connectionsState.loaded ? <div className="dashboard-empty">{w('正在读取接入规划…', 'Loading the connection plan…')}</div> : (
                <>
                  {connectionsState.error && <p className="pc-notice" role="alert">{refusalText(connectionsState.error, zh)}</p>}
                  {lanes.map(lane => (
                    <article key={lane.id} data-lane={lane.id}>
                      <header><span className="pc-system-icon" aria-hidden="true">↔</span><div><h4>{lane.name}</h4><small data-testid="panorama-lane-status">{lane.status}</small></div></header>
                      <h5>{lane.tool || w('待选择原有业务软件', 'Software to choose')}</h5>
                      <p>{lane.purpose}</p>
                      <dl><dt>{w('交互方式', 'How')}</dt><dd>{lane.mode}</dd><dt>{w('关联阶段', 'Stage')}</dt><dd>{lane.stage ? stageName(lane.stage) : w('待关联', 'not linked yet')}</dd></dl>
                      {/* What the server saw behind the word, in its own sentences (the status is judged from real facts only). */}
                      {(lane.evidence ?? []).length > 0 && <ul className="pc-evidence" data-testid="panorama-lane-evidence">{(lane.evidence ?? []).map((line, index) => <li key={index}>{line}</li>)}</ul>}
                      {onSaveConnection && <button type="button" className="shell-btn" data-testid="panorama-lane-edit" onClick={() => setDialog({ kind: 'lane', id: lane.id })}>{w('配置交接', 'Configure')}</button>}
                    </article>
                  ))}
                  <p className="pc-muted">{w('保存的是接入规划，不是连接：不填密钥、不建连接、不发消息。', 'What is saved is a plan, not a connection: no credential, no connection, no message.')}</p>
                </>
              )}
            </div>
          )}
        </section>
      )}
      {peopleSlot && (
        <div className="dashboard-tab-slot" data-testid="dashboard-people-contacts" hidden={off('staff') || collab !== 'people' || undefined}>{peopleSlot}</div>
      )}
      <section className="dashboard-card dashboard-staff" id="dash-people" hidden={off('staff') || collab !== 'people' || undefined}>
        <h3 className="dashboard-card-title">{t('dashboard.staff.title')}</h3>
        {staffRows.length === 0 ? (
          <div className="dashboard-empty">{t('dashboard.staff.empty')}</div>
        ) : (
          <ul className="dashboard-staff-list">
            {staffRows.map(({ agent, busyCount }) => (
              <li key={agent.agent_id} className="dashboard-staff-row">
                <span className={`dashboard-dot dashboard-dot--${busyCount > 0 || agent.runtime_status === 'tool_active' || agent.runtime_status === 'reflecting' ? 'busy' : 'idle'}`} />
                <span className="dashboard-staff-name" title={agent.description}>{translateMaybe('role.name', agent.name)}</span>
                <span className="dashboard-staff-meta">
                  {agent.current_tool ? agent.current_tool : busyCount > 0 ? t('dashboard.staff.busy', { count: busyCount }) : t('dashboard.staff.idle')}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section className="dashboard-card" data-testid="dashboard-org-entry" hidden={off('org') || collab !== 'people' || undefined}>
        <h3 className="dashboard-card-title">{w('组织与角色', 'Organisation and roles')}</h3>
        <p className="dashboard-projects-note">{w(
          '上面是这个项目里正在干活的成员。角色、名册、汇报结构与委派策略在「组织」里管理；业务流的步骤按角色指派，执行时解析成具体的人。系统目前没有按项目的成员与权限矩阵，这里不编。',
          'Above: who is working in this project. Roles, the roster, reporting structure and delegation live in Organisation; a flow’s steps name roles, resolved to people at run time. There is no per-project permission matrix yet, and none is invented here.')}</p>
        {onOpenOrg && <button type="button" className="shell-btn" onClick={onOpenOrg}>{w('打开组织 →', 'Open organisation →')}</button>}
      </section>

      {/* ── 全部项目: one row per project, and the company's organisation as the level's other board. ── */}
      {level === 'all' && orgSlot && (
        <nav className="dashboard-sections dashboard-tabs" aria-label={w('项目中心的板块', 'Boards of the project centre')} data-testid="dashboard-all-boards">
          {([['projects', w('全部项目', 'All projects')], ['org', w('协作人员与组织', 'People & organisation')]] as const).map(([id, label]) => (
            <button key={id} type="button" data-all-board={id} aria-selected={allBoard === id} className={allBoard === id ? 'is-active' : ''} onClick={() => setAllBoard(id)}>{label}</button>
          ))}
        </nav>
      )}
      {level === 'all' && orgSlot && allBoard === 'org' && (
        // One organisation for the whole company -- roles, roster, structure, runtime teams -- so it lives on the
        // level that is about every project. A project's own 人员与职责 board points here.
        <section className="dashboard-org-board" data-testid="dashboard-org-board">{orgSlot}</section>
      )}
      {projects && projects.length > 0 && (
        <section className="dashboard-card dashboard-projects" id="dash-projects" data-testid="dashboard-projects" hidden={projectListHidden(level) || (allBoard === 'org' && Boolean(orgSlot)) || undefined}>
          <h3 className="dashboard-card-title">{w(`全部项目 · ${projects.length} 个`, `All projects · ${projects.length}`)}</h3>
          {projects.length > 1 && projectOverview && Object.values(projectOverview).some(entry => entry.standing?.partial) && (
            <p className="dashboard-projects-note" data-testid="dashboard-partial-note">{w('有的项目服务端没能读到暂停或预算，它的「等你处理」和「出错或逾期」写成「未知」，不是 0。', 'For some projects the server could not read the pauses or budgets: their 等你处理 and 出错或逾期 say unknown, not 0.')}</p>
          )}
          <ul className="dashboard-project-list">
            {projectRows(projects, projectId).map(row => {
              const rowProfile = profileFrom(projects.find(item => item.id === row.id)?.profile) ?? (row.id === projectId ? currentProfile : null)
              const rowManager = rowProfile?.manager_name || (rowProfile?.manager_employee_id ? nameOf(rowProfile.manager_employee_id) : '')
              return (
                <li key={row.id} data-project={row.id} className={row.current ? 'is-current' : ''}>
                  <div>
                    <strong>{row.name}</strong>
                    <small>{[rowProfile?.type, row.customer ? w(`客户 ${row.customer}`, `customer ${row.customer}`) : w('未绑定客户', 'no customer bound'), w(`规划管理者 ${rowManager || '待配置'}`, `planning manager ${rowManager || 'to configure'}`)].filter(Boolean).join(' · ')}</small>
                  </div>
                  {/* Another project's number switches the console there first (the same road as 切换到这个项目), then opens
                      the inbox on the list it counted. */}
                  <Numbers standing={standingOf(projectOverview, row.id)} zh={zh} testId="dashboard-project-standing"
                    onOpen={onNavigate && (row.current || (row.enterable && onEnterProject && connected)) ? key => { if (!row.current) onEnterProject?.(row.id); onNavigate('orders', standingTarget(key)) } : undefined} />
                  {(() => {
                    // How much this project takes. Measuring walks the whole project, so it is asked for per row and
                    // never on the way in.
                    const measured = usageByProject[row.id]
                    if (measured?.usage) {
                      return (
                        <span className="dashboard-project-usage" data-testid="dashboard-project-usage">
                          {w('占用 ', 'uses ')}{measured.usage.complete ? '' : w('至少 ', 'at least ')}
                          <strong>{formatBytes(measured.usage.total_bytes)}</strong>
                        </span>
                      )
                    }
                    if (!onAskUsage) return null
                    return (
                      <button type="button" className="shell-btn" data-testid="dashboard-measure-project" disabled={!connected || measured?.computing}
                        onClick={() => onAskUsage(row.id)}>{measured?.computing ? w('统计中…', 'Measuring…') : w('统计占用', 'Measure')}</button>
                    )
                  })()}
                  <span className="shell-tag">{lifecycleLabel(row.lifecycle, zh)}</span>
                  {row.current
                    ? <><span className="shell-tag shell-tag--ok">{w('当前项目', 'You are here')}</span>
                      <button type="button" className="shell-btn" data-testid="dashboard-enter-current" onClick={() => { setTab('overview'); setLevel('project') }}>{w('进入 →', 'Enter →')}</button></>
                    // Said as what it does: the whole console moves to that project (B5, : 「进入」 read as a
                    // look inside, and nobody found where projects are switched).
                    : <button type="button" className="shell-btn" data-testid="dashboard-switch-project" disabled={!connected || !row.enterable || !onEnterProject} onClick={() => { onEnterProject?.(row.id); setTab('overview'); setLevel('project') }}>{w('切换到这个项目', 'Switch to this project')}</button>}
                  {/* Administration (record, archive, export, backup, delete, customer binding) lives inside the project;
                      this goes straight to that tab. */}
                  <button type="button" className="shell-btn" data-testid="dashboard-manage-project" disabled={!connected || (!row.current && (!row.enterable || !onEnterProject))}
                    onClick={() => { if (!row.current) onEnterProject?.(row.id); setTab('data'); setLevel('project') }}>{w('管理', 'Manage')}</button>
                </li>
              )
            })}
          </ul>
        </section>
      )}

      {/* ── 资料与数据: the files, the storage and administration, the recorded amounts, the folded 系统记录. ── */}
      <div className="dashboard-grid">
        <div className="dashboard-side">
          <p className="dashboard-projects-note" hidden={off('artifacts')}>{w('交付物按工单归属，版本随验收保留。', 'Deliverables belong to their order; each accepted round keeps its version.')}</p>
          <div className="dashboard-tab-slot" hidden={off('artifacts')}>{filesSlot ?? artifactsSlot}</div>
          {memorySlot && (
            <div className="dashboard-tab-slot" data-testid="dashboard-customer-memory" hidden={off('artifacts')}>{memorySlot}</div>
          )}
          {projectFolderSlot && (
            <div className="dashboard-tab-slot" data-testid="dashboard-project-folder" hidden={off('storage')}>
              {projectFolderSlot}
            </div>
          )}
          <section className="dashboard-card" data-testid="dashboard-storage" hidden={off('storage')}>
            <h3 className="dashboard-card-title">{w('数据与存储', 'Data & storage')}</h3>
            <ul className="dashboard-facts">
              <li><span>{w('项目', 'Project')}</span><strong>{projectName || '—'}</strong></li>
              <li><span>{w('生命周期', 'Lifecycle')}</span><strong>{lifecycleLabel(projectContext?.lifecycle_status ?? 'active', zh)}</strong></li>
              <li><span>{w('客户绑定', 'Customer')}</span><strong>{projectContext?.customer_id || w('未绑定', 'none')}</strong></li>
              <li><span>{w('交付物文件', 'Deliverable files')}</span><strong>{projectContext?.artifacts ?? 0}</strong></li>
              <li data-testid="dashboard-recorded-cost">
                <span>{w('旧记录小计（历史字段标记 USD）', 'Historical subtotal (legacy USD label)')}</span>
                <strong>{recordedCostObservation?.project_id === projectId && recordedCostObservation.state === 'legacy_recorded'
                  ? `$${recordedCostObservation.recorded_subtotal_text} USD` : '—'}</strong>
              </li>
              {recordedCostObservation?.project_id === projectId && recordedCostObservation.state === 'legacy_recorded'
                ? recordedCostObservation.runs?.map(run => (
                  <li key={run.run_index} data-testid="dashboard-run-cost" data-run-index={run.run_index}>
                    <span>{w('运行', 'Run')} {run.run_index}</span>
                    <strong>${run.recorded_subtotal_text} USD</strong>
                  </li>
                )) : null}
              <li data-testid="dashboard-actual-cost-unknown">
                <span>{w('实际总额', 'Actual total')}</span><strong>{w('未知', 'Unknown')}</strong>
              </li>
              <li><span>{w('费用依据', 'Cost basis')}</span><strong>{w('币种、定价、完整性与实账待核', 'Currency, pricing, coverage and invoice unverified')}</strong></li>
              {(() => {
                const amount = sessionCostRecord(stats.imageCostTotal, 'CNY', zh)
                const partial = w('部分记录，另有未知', 'Partial records; others unknown')
                const zero = w('零值待核', 'zero unverified')
                const mixedZero = w('含零记录，零值待核', 'Includes zero records; zero unverified')
                const countState = stats.imagesTotal === null ? 'unknown'
                  : stats.imageCountZeroAmbiguous ? 'legacy_zero_ambiguous' : 'legacy_recorded'
                const amountState = stats.imageCostTotal === null ? 'unknown'
                  : stats.imageCostZeroAmbiguous ? 'legacy_zero_ambiguous' : 'legacy_recorded'
                return (
                  <li data-testid="dashboard-image-cost-separate" title={w('CNY 仅为历史字段标签；币种、定价、完整性与实账待核。不换汇、不与 USD 合并。', 'CNY is only a historical field label; currency, pricing, coverage and invoice unverified. No conversion or combined USD total.')}>
                    <span>{w('图片旧记录（历史字段标记 CNY，独立）', 'Image records (legacy CNY label, separate)')}</span>
                    <strong>
                      <span data-testid="dashboard-image-record-count" data-cost-state={countState} data-partial={stats.imagesTotal !== null && stats.imageCountUnknown ? 'true' : undefined}>
                        {w('计数记录', 'Count records')}: {stats.imagesTotal === null ? w('未知', 'Unknown') : stats.imagesTotal}
                        {stats.imagesTotal === 0 ? ` · ${zero}` : stats.imagesTotal !== null && stats.imageCountZeroAmbiguous ? ` · ${mixedZero}` : ''}
                        {stats.imagesTotal !== null && stats.imageCountUnknown ? ` · ${partial}` : ''}
                      </span><br />
                      <span data-testid="dashboard-image-record-amount" data-cost-state={amountState} data-partial={stats.imageCostTotal !== null && stats.imageCostUnknown ? 'true' : undefined}>
                        {w('金额小计', 'Amount subtotal')}: {amount.text}
                        {stats.imageCostTotal !== null && stats.imageCostTotal > 0 && stats.imageCostZeroAmbiguous ? ` · ${mixedZero}` : ''}
                        {stats.imageCostTotal !== null && stats.imageCostUnknown ? ` · ${partial}` : ''}
                      </span>
                    </strong>
                  </li>
                )
              })()}
              <li><span>{t('dashboard.total.tokens')}</span><strong>{formatTokens(stats.tokensTotal)}</strong></li>
              {(() => {
                const measured = usageByProject[projectId]
                if (!measured?.usage) return null
                const attachments = measured.usage.buckets?.attachments?.bytes ?? 0
                return (
                  <>
                    <li data-testid="dashboard-storage-usage"><span>{w('占用空间', 'Space used')}</span>
                      <strong>{measured.usage.complete ? '' : w('至少 ', 'at least ')}{formatBytes(measured.usage.total_bytes)}</strong></li>
                    <li><span>{w('其中附件', 'of which attachments')}</span><strong>{formatBytes(attachments)}</strong></li>
                  </>
                )
              })()}
            </ul>
            {manageSlot && <div className="dashboard-manage" data-testid="dashboard-manage">{manageSlot}</div>}
            <p className="dashboard-projects-note">{w(
              '交付物按工单存放在项目工作区的 orders/<工单号>/ 下（见上面的资料）。备份预检、归档、导出与生命周期记录就在上面这一排。按项目的独立数据库与目录配置，服务端尚未提供——这里只显示真实存在的东西。',
              'Deliverables live under orders/<order id>/ in the project workspace (the files above). Backup precheck, archive, export and the lifecycle record are the row above. Per-project databases and directory configuration have no server support yet; only what exists is shown.')}</p>
          </section>
          {/* What was recorded on this project's orders: quoted, received, still to come -- per currency, never added
              across currencies -- and how many open orders are past the date someone set. Nothing is drawn until an
              order records an amount or a date: the engine does not know money it was not told. */}
          {(() => {
            const totals = moneyTotals(workOrders)
            const lateCount = rows.filter(order => {
              const kind = dueState(order.handling?.due_at, new Date(), zh, attentionOf(order).ended).kind
              return kind === 'overdue' || kind === 'today'
            }).length
            const dated = rows.filter(order => order.handling?.due_at).length
            if (totals.length === 0 && dated === 0) return null
            return (
              <section className="dashboard-card dashboard-money" data-testid="dashboard-money" hidden={off('money')}>
                <h3 className="dashboard-card-title">{w('已记录的金额与期限', 'Recorded amounts and dates')}</h3>
                <div className="dashboard-facts">
                  {totals.map(row => (
                    <div key={row.currency} data-currency={row.currency}>
                      <span>{w(`报价 / 已收款（${row.orders} 张）`, `Quoted / received (${row.orders})`)}</span>
                      <strong>{moneyText(row.quoted, row.currency)} / {moneyText(row.received, row.currency)}</strong>
                      <small>{w('待收', 'to come')} {moneyText(Math.max(0, row.quoted - row.received), row.currency)}</small>
                    </div>
                  ))}
                  {dated > 0 && (
                    <div data-late={lateCount > 0 || undefined}>
                      <span>{w(`到期或逾期（设了期限的 ${dated} 张）`, `Due or overdue (of ${dated} dated)`)}</span>
                      <strong>{lateCount}</strong>
                    </div>
                  )}
                </div>
                <p className="dashboard-projects-note">{w('金额与期限是在工单「经办」里记下来的事实；没记的单不算在内。不是财务系统：不算利润、不对账。', 'Amounts and dates are what was recorded in an order’s Handling card; orders without them are not counted. Not an accounting system.')}</p>
              </section>
            )
          })()}
          {/* 系统记录, folded: the duty patrol, the run log and the run sessions -- the machine's own records of this
              project, kept under 资料与数据 rather than under 配置 → 服务与连接 because every one of them is read per
              project (the requests carry the project id) and says nothing about the machine's services. */}
          <details className="dashboard-card pc-records" data-testid="dashboard-records" hidden={off('records')}>
            <summary className="dashboard-card-title">{w('系统记录', 'System records')}<span className="dashboard-card-count">{w('值班巡检 · 运行日志 · 运行会话', 'duty patrol · run log · run sessions')}</span></summary>
            <section className="dashboard-duty">
              <h4 className="dashboard-card-title">
                {t('dashboard.duty.title')}
                <span className="dashboard-duty-channel">{dutyLog?.configured ? t('dashboard.duty.channelOn') : t('dashboard.duty.channelOff')}</span>
              </h4>
              {dutyLogError ? (
                <div className={`dashboard-duty-error dashboard-duty-error--${classifyPageError(dutyLogError)}`} role="alert" data-error-kind={classifyPageError(dutyLogError)}>
                  <span><strong>{t(pageErrorMessageKey(dutyLogError) as Parameters<typeof t>[0])}</strong><br />{dutyLogError}</span>
                  <button className="dashboard-inline-btn" onClick={onRefreshDutyLog} disabled={!connected}>{t('dashboard.duty.retry')}</button>
                </div>
              ) : !dutyLog || dutyLog.entries.length === 0 ? (
                <div className="dashboard-empty">{t('dashboard.duty.empty')}</div>
              ) : (
                <ul className="dashboard-log-list">
                  {[...dutyLog.entries].reverse().map((entry, index) => {
                    const kindKey = `dashboard.duty.kind.${entry.kind}` as TKey
                    const kindLabel = t(kindKey) === kindKey ? entry.kind : t(kindKey)
                    const delivered = entry.sent.length > 0 && entry.sent.every(s => s.ok)
                    const failed = entry.sent.length > 0 && !delivered
                    const detailParts = [entry.action, entry.result, entry.failure_reason].filter(Boolean)
                    const named = rows.find(order => order.order_id === entry.order_id)
                    return (
                      <li key={`${entry.at}-${index}`} className="dashboard-log-row" title={entry.text}>
                        <span className="dashboard-log-time">{formatClock(Date.parse(entry.at) || 0, todayStart)}</span>
                        <span className="dashboard-log-body">
                          <span className="dashboard-log-source">
                            {kindLabel}
                            {named ? ` · ${orderLine(named, flowName(named.workflow_id), nameOf, zh).title}` : ''}
                            {' · '}
                            {failed ? t('dashboard.duty.sendFailed') : delivered ? t('dashboard.duty.sent') : entry.muted ? t('dashboard.duty.muted') : t('dashboard.duty.notSent')}
                          </span>
                          <span className="dashboard-log-summary">{entry.text}</span>
                          {detailParts.length > 0 && (
                            <span className="dashboard-log-detail" aria-label={t('dashboard.duty.detail')}>{detailParts.join(' · ')}</span>
                          )}
                        </span>
                      </li>
                    )
                  })}
                </ul>
              )}
            </section>
            <section className="dashboard-logs" id="dash-logs">
              <h4 className="dashboard-card-title">{t('dashboard.logs.title')}</h4>
              {logRows.length === 0 ? (
                <div className="dashboard-empty">{t('dashboard.logs.empty')}</div>
              ) : (
                <ul className="dashboard-log-list">
                  {logRows.map(row => (
                    <li key={row.key} className="dashboard-log-row" title={row.detail || row.summary}>
                      <span className="dashboard-log-time">{formatClock(row.timestamp, todayStart)}</span>
                      <span className="dashboard-log-body">
                        <span className="dashboard-log-source">
                          {row.sessionTitle.length > 14 ? `${row.sessionTitle.slice(0, 14)}…` : row.sessionTitle}
                          {row.roleName ? ` · ${translateMaybe('role.name', row.roleName)}` : ''}
                        </span>
                        <span className="dashboard-log-summary">
                          {row.summary || (() => {
                            const key = `dashboard.log.type.${row.type}` as TKey
                            const label = t(key)
                            return label === key ? row.type : label
                          })()}
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
            <section className="dashboard-orders" id="dash-orders">
              <h4 className="dashboard-card-title">{t('dashboard.orders.title')}</h4>
              {orders.length === 0 ? (
                <div className="dashboard-empty">{t('dashboard.orders.empty')}</div>
              ) : (
                <div className="dashboard-table-wrap">
                  <table className="dashboard-table">
                    <thead>
                      <tr>
                        <th>{t('dashboard.orders.col.title')}</th>
                        <th>{t('dashboard.orders.col.mode')}</th>
                        <th>{t('dashboard.orders.col.status')}</th>
                        <th>{t('dashboard.orders.col.items')}</th>
                        <th>{t('dashboard.orders.col.cost')}</th>
                        <th>{t('dashboard.orders.col.updated')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {orders.map(order => {
                        const items = collectWorkItemRows(order)
                        const doneCount = items.filter(row => row.phase === 'approved').length
                        const failedCount = items.filter(row => row.phase === 'failed').length
                        const usdRecord = sessionCostRecord(order.recordedCostUsd, 'USD', zh)
                        const imageRecord = sessionCostRecord(order.imageCostCny, 'CNY', zh)
                        return (
                          <tr key={order.taskId} onClick={() => onOpenSession(order.taskId)}>
                            <td className="dashboard-order-title" title={order.title}>{order.title || w('未命名会话', 'untitled session')}</td>
                            <td><span className="dashboard-mode-chip">{modeLabel(order)}</span></td>
                            <td>
                              <span className={`dashboard-status dashboard-status--${statusClass(order)}`}>
                                {statusLabel(order.status)}
                                {(order.pendingPermissionCount ?? 0) > 0 && <span className="dashboard-status-pending"> · {t('dashboard.pendingApproval')}</span>}
                              </span>
                            </td>
                            <td>
                              {items.length > 0 ? `${doneCount}/${items.length}` : '—'}
                              {failedCount > 0 && <span className="dashboard-status dashboard-status--bad" style={{ marginLeft: 6 }}>· {failedCount} {t('dashboard.failedItems')}</span>}
                            </td>
                            <td data-testid="dashboard-session-cost" title={w('历史字段记录；币种、定价、完整性与实账待核。不换汇、不合并。', 'Historical field records; currency, pricing, coverage and invoice unverified. No conversion or combined total.')}>
                              <span data-testid="dashboard-session-recorded-cost" data-cost-state={usdRecord.state}>{w('旧记录（历史字段标记 USD）', 'Historical record (legacy USD label)')}: {usdRecord.text}</span><br />
                              <span data-testid="dashboard-session-image-cost" data-cost-state={imageRecord.state}>{w('图片费用记录（CNY，独立）', 'Image records (CNY, separate)')}: {imageRecord.text}</span><br />
                              <small data-testid="dashboard-session-actual-cost">{w('实际总额：未知', 'Actual total: Unknown')}</small>
                            </td>
                            <td>{formatClock(order.updatedAt, todayStart)}</td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </details>
        </div>
      </div>
      {boundaryDialog}
      {laneDialog}
      {previewDialog}
    </div>
  )
}

// ── the two editors ─────────────────────────────────────────────────────────

/** 明确这项业务的范围: the three answers and the planning manager. Saving changes no flow, no permission and no order in
 *  flight; the button says the receipt's states from the tracker (busy / no receipt / not sent), and a refusal is the
 *  server's sentence under the button, where the person is. Exported for its test. */
export function BoundaryDialog({ zh, profile, employees, connected, save, onClose, onSave }: {
  zh: boolean; profile: ProjectProfile; employees: OrgEmployee[]; connected: boolean; save: SaveState
  onClose: () => void; onSave?: (profile: { start: string; scope: string; end: string; manager_employee_id: string }) => void
}) {
  const w = (cn: string, en: string) => (zh ? cn : en)
  // The four fields the server records (business_store PROJECT_PROFILE_FIELDS); a 类型 is not one of them.
  const [draft, setDraft] = useState({ start: profile.start, scope: profile.scope, end: profile.end, manager_employee_id: profile.manager_employee_id })
  const [problem, setProblem] = useState('')
  const complete = draft.start.trim() && draft.scope.trim() && draft.end.trim()
  const busy = save.state === 'busy'
  return (
    <Dialog title={w('明确这项业务的范围', 'Define this business’s boundary')} testId="boundary-dialog" zh={zh} onClose={onClose}>
      <p className="pc-notice">{w('仅保存项目业务说明，不更改流程、权限或在途工单。', 'Only the project’s business note is saved; no flow, permission or order in flight changes.')}</p>
      <form className="pc-form" onSubmit={event => { event.preventDefault(); if (!complete) { setProblem(w('请补齐起点、范围和终点', 'Fill in the start, the scope and the end')); return } setProblem(''); onSave?.({ ...draft, start: draft.start.trim(), scope: draft.scope.trim(), end: draft.end.trim() }) }}>
        <label>{w('从哪里接手', 'Where it starts')}<textarea maxLength={800} value={draft.start} onChange={event => setDraft({ ...draft, start: event.target.value })} /></label>
        <label>{w('安普里负责什么、不负责什么', 'What Ampuly handles, and what it does not')}<textarea maxLength={800} value={draft.scope} onChange={event => setDraft({ ...draft, scope: event.target.value })} /></label>
        <label>{w('什么结果算交付完成', 'What counts as delivered')}<textarea maxLength={800} value={draft.end} onChange={event => setDraft({ ...draft, end: event.target.value })} /></label>
        <label>{w('项目管理者', 'Project manager')}
          <select value={draft.manager_employee_id} onChange={event => setDraft({ ...draft, manager_employee_id: event.target.value })}>
            <option value="">{w('待配置', 'to configure')}</option>
            {employees.map(person => <option key={person.employee_id} value={person.employee_id}>{person.name}</option>)}
          </select>
          <small>{w('规划责任归属，不新增或扩大项目权限。', 'Planning responsibility only; it grants or widens no permission.')}</small>
        </label>
        {problem && <p className="pc-notice" role="alert">{problem}</p>}
        <button type="submit" className="shell-btn shell-btn--primary" data-testid="boundary-save" disabled={!connected || !onSave || busy}>{busy ? save.words : w('保存业务说明', 'Save the note')}</button>
        {!busy && save.words && <p className="pc-receipt" role={save.state === 'settled' ? 'alert' : 'status'} data-testid="boundary-save-words">{save.words}</p>}
      </form>
    </Dialog>
  )
}

/** <lane> · 业务交接: the plan for one lane. Saving a plan is not connecting. The button says the receipt's states as
 *  the boundary's does, a refusal in the server's sentence under it. Exported for its test. */
export function LaneDialog({ zh, lane, stages, connected, save, onClose, onSave }: {
  zh: boolean; lane: ConnectionLane; stages: FlowStage[]; connected: boolean; save: SaveState
  onClose: () => void; onSave?: (connection: { id: string; tool: string; purpose: string; mode: string; stage: string }) => void
}) {
  const w = (cn: string, en: string) => (zh ? cn : en)
  const [draft, setDraft] = useState({ tool: lane.tool, purpose: lane.purpose, mode: lane.mode, stage: lane.stage })
  const [problem, setProblem] = useState('')
  const busy = save.state === 'busy'
  return (
    <Dialog title={`${lane.name} · ${w('业务交接', 'handover')}`} testId="lane-dialog" zh={zh} onClose={onClose}>
      <p className="pc-notice">{w('只配置接入方案，不填写密钥，不创建真实连接。保存后状态为“待验证”。', 'Only the plan is configured: no credential, no real connection. Once saved the lane reads “to verify”.')}</p>
      <form className="pc-form" onSubmit={event => { event.preventDefault(); if (!draft.tool.trim() || !draft.purpose.trim()) { setProblem(w('请填写软件与交接内容', 'Name the software and what is handed over')); return } setProblem(''); onSave?.({ id: lane.id, tool: draft.tool.trim(), purpose: draft.purpose.trim(), mode: draft.mode, stage: draft.stage }) }}>
        <label>{w('使用的软件或平台', 'Software or platform')}<input maxLength={120} value={draft.tool} onChange={event => setDraft({ ...draft, tool: event.target.value })} /></label>
        <label>{w('交进去什么，拿回来什么', 'What goes in, what comes back')}<textarea maxLength={800} value={draft.purpose} onChange={event => setDraft({ ...draft, purpose: event.target.value })} /></label>
        <label>{w('交互方式', 'How')}
          <select value={draft.mode} onChange={event => setDraft({ ...draft, mode: event.target.value })}>
            {CONNECTION_MODES.map(mode => <option key={mode} value={mode}>{mode}</option>)}
          </select>
        </label>
        <label>{w('主要关联阶段', 'Main stage')}
          <select value={draft.stage} onChange={event => setDraft({ ...draft, stage: event.target.value })}>
            <option value="">{w('待关联', 'not linked yet')}</option>
            {stages.map(stage => <option key={stage.id} value={stage.id}>{stage.name}</option>)}
          </select>
        </label>
        <p className="pc-muted">{w('登录、授权、读写范围与真实连通测试留待引擎接入，本页不模拟成功。', 'Sign-in, authorisation, read/write scope and a real connection test come with the engine’s connector; this page does not pretend they passed.')}</p>
        {problem && <p className="pc-notice" role="alert">{problem}</p>}
        <button type="submit" className="shell-btn shell-btn--primary" data-testid="lane-save" disabled={!connected || !onSave || busy}>{busy ? save.words : w('保存交接规划', 'Save the plan')}</button>
        {!busy && save.words && <p className="pc-receipt" role={save.state === 'settled' ? 'alert' : 'status'} data-testid="lane-save-words">{save.words}</p>}
      </form>
    </Dialog>
  )
}
