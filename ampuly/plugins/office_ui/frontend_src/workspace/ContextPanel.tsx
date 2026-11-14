import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { NativeApprovalLevel, RoleWorkItemSummary, Session, TaskPreferredAgent } from '../types/kanban'
import type { ChatMessage, CheckpointReplyMetadata, InteractionReplyReceipt, OutgoingAttachmentPayload } from '../types/chat'
import type { AgentInfo, OrgInfoPayload, SavedOrgSummary } from '../types/visual'
import type { CommsStatePayload, CommsMessagePayload } from '../lib/wsClient'
import { CommsPanel } from './CommsPanel'
import { ProjectCockpit } from './ProjectCockpit'
import { PRIORITY_META, type TaskPriority } from '../types/kanban'
import { TaskHeaderBar } from '../chat/TaskHeaderBar'
import { MarkdownBody, MessageList } from '../chat/MessageList'
import { MessageComposer } from '../chat/MessageComposer'
import { AgentWorkPanel } from '../chat/AgentWorkPanel'
import { WorkItemProgressCard } from '../chat/WorkItemProgressCard'
import { ExternalTeamSummaryCard } from '../chat/ExternalTeamSummaryCard'
import type { ExternalTeamActivityRecord } from '../stores/ExternalTeamActivityStore'
import { IconTimeline, IconHandoff, IconTool } from '../chat/SvgIcons'
import { isSessionWorking } from '../lib/sessionRuntime'
import { getWorkItemRoleLabel } from '../lib/workItemIdentity'
import { getLinkedRuntimeTaskId } from '../lib/workItemRuntimeIds'
import { executionAgentLabel } from '../lib/externalAgents'
import { TaskDetailView } from './TaskDetailView'
import {
  getConversationPeerSessions,
  getConversationHeaderSession,
  getConversationSessionView,
  getConversationMessageCount,
  getWorkItemRoleSessions,
  mergeConversationProgressLog,
  projectSessionConversation,
} from '../lib/workItemSessions'
import { useI18n } from '../i18n'

type TFunction = ReturnType<typeof useI18n>['t']

type ActiveView =
  | { kind: 'session'; taskId: string }
  | { kind: 'task-detail'; taskId: string }
  | { kind: 'activity' }
  | { kind: 'child-detail' }

interface ContextPanelProps {
  panelState: 'collapsed' | 'open' | 'maximized'
  width: number
  onResizeMouseDown: (e: React.MouseEvent) => void
  isResizing: boolean

  activeView: ActiveView
  activeSession: Session | null
  activeTask?: import('../types/kanban').KanbanTask | null
  linkedTaskSession?: Session | null
  linkedTaskSessionMessages?: ChatMessage[]
  childDetailSession: Session | null

  messages: ChatMessage[]
  childDetailMessages: ChatMessage[]
  allSessions: Session[]
  openSessions: Session[]
  multiViewSessions: Session[]
  multiSessionMessages: Record<string, ChatMessage[]>
  agents: AgentInfo[]
  childSessions: Session[]
  execMode?: string
  taskPreferredAgent: TaskPreferredAgent
  nativeApprovalDefault: NativeApprovalLevel
  savedOrgsList?: SavedOrgSummary[] | null
  activeSavedOrg?: string | null
  onSavedOrgsList?: () => void
  onSavedOrgLoad?: (name: string) => void
  canShowAgentsTab?: boolean

  channelId: string
  channelName: string
  unreadCounts?: Record<string, number>
  multiSessionView?: boolean

  panelTab: 'chat' | 'agents' | 'info' | 'comms' | 'team'
  onPanelTabChange: (tab: 'chat' | 'agents' | 'info' | 'comms' | 'team') => void

  commsState?: CommsStatePayload | null
  commsMessage?: CommsMessagePayload | null
  onCommsRefresh?: () => void
  onCommsReadMessage?: (path: string) => void
  orgInfoData?: OrgInfoPayload | null
  canShowTeamTab?: boolean
  onTeamStopRun?: () => void

  onTitleChange: (taskId: string, title: string) => void
  onSessionConfigChange?: (taskId: string, execMode: string, companyProfile?: string, orgId?: string) => void
  onSessionTaskAgentChange?: (taskId: string, preferredAgent: TaskPreferredAgent) => void
  onSessionNativeApprovalLevelChange?: (taskId: string, level: NativeApprovalLevel) => void
  onSetNativeApprovalDefault?: (level: NativeApprovalLevel) => void
  /**
   * The active session belongs to no order (policy, R1 option B): it stays readable, and its composer is
   * replaced by one line and 去总控 (no "continue in a new chat" either -- a session starts only with an order).
   */
  activeSessionWithoutOrder?: boolean
  onGoToMaster?: () => void
  onStop?: () => void
  onComplete?: () => void
  onResume?: () => void
  onResumeTask?: (taskId: string) => void
  onStopTask?: (taskId: string) => void
  onCompleteTask?: (taskId: string) => void
  onLocateOnBoard?: (taskId: string) => void
  onBackToParent?: () => void
  onCloseTaskDetail?: () => void
  onOpenChildDetail?: (taskId: string) => void
  onOpenExecutionPanel?: (taskId: string) => void
  getExternalTeamActivity?: (taskId: string, invocationId?: string) => ExternalTeamActivityRecord | undefined
  onSelectSessionTab?: (taskId: string) => void
  onCloseSessionTab?: (taskId: string) => void
  onToggleMultiSessionView?: () => void
  onCollapse: () => void
  onExpand: () => void
  onMaximize: () => void

  onComposerSend: (content: string, attachments?: OutgoingAttachmentPayload[]) => void | boolean
  onInteractionReply: (content: string, taskId?: string, metadata?: CheckpointReplyMetadata) => Promise<InteractionReplyReceipt>
  onWorkItemClick: (executionTurnId: string) => void
  onWorkItemOpenSession?: (executionTurnId: string) => void
  onMarkRead: () => void
  onLoadSessionHistory?: (
    taskId: string,
    oldestMessage?: ChatMessage,
    detailLevel?: 'summary' | 'full',
  ) => Promise<void> | void
  isSessionHistoryLoading?: (taskId: string) => boolean
}

function relativeTime(ts: number, t: TFunction): string {
  const diff = Date.now() - ts
  if (diff < 60_000) return t('session.justNow')
  if (diff < 3_600_000) return t('session.minutes', { count: Math.floor(diff / 60_000) })
  if (diff < 86_400_000) return t('session.hours', { count: Math.floor(diff / 3_600_000) })
  return t('session.days', { count: Math.floor(diff / 86_400_000) })
}

export function latestSessionPreviewMessage(messages: ChatMessage[]): ChatMessage | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].content.trim()) return messages[index]
  }
  return null
}

export function sessionPreviewText(content: string, maxLength = 360): string {
  const normalized = content.replace(/\s+/g, ' ').trim()
  if (maxLength <= 0) return ''
  if (normalized.length <= maxLength) return normalized
  return `${normalized.slice(0, Math.max(0, maxLength - 1)).trimEnd()}…`
}

function sessionRuntimeLabel(session: Session, activeChildCount: number, t: TFunction): string | null {
  if (activeChildCount > 1) return t('ctx.agentsWorking', { count: activeChildCount })
  const displayTool = session.displayTool || session.currentTool
  if (displayTool) return t('ctx.runningTool', { tool: displayTool })
  if (session.agentStatus === 'reflecting') return t('agent.status.reflecting')
  if (session.status === 'running') return t('kanban.status.running')
  return null
}

function activeRoleWorkItemCount(roleWorkItems?: Record<string, RoleWorkItemSummary>): number | undefined {
  if (!roleWorkItems || Object.keys(roleWorkItems).length === 0) return undefined
  return Object.values(roleWorkItems).filter(summary => summary.aggregatedStatus === 'active').length
}

function distinctWorkingRoleCount(sessions: Session[]): number {
  const roles = new Set(
    sessions
      .filter(isSessionWorking)
      .map(s => String(s.workItemRoleId ?? s.assigneeIds[0] ?? s.taskId).trim())
      .filter(Boolean),
  )
  return roles.size
}

function activeAgentCountFor(
  roleWorkItems: Record<string, RoleWorkItemSummary> | undefined,
  sessions: Session[],
): number | undefined {
  const roleCount = activeRoleWorkItemCount(roleWorkItems)
  if (roleCount !== undefined) return roleCount || undefined
  return distinctWorkingRoleCount(sessions) || undefined
}

function sessionModeLabel(session: Session): string {
  const execMode = composerExecModeForSession(session)
  if (execMode === 'org' || execMode === 'custom') return `company/${session.orgId ?? 'org'}`
  if (execMode === 'company') return `company/${session.companyProfile ?? 'corporate'}`
  return execMode
}

function normalizePanelExecMode(value?: string): 'task' | 'company' | 'org' {
  const normalized = String(value ?? '').trim().toLowerCase()
  if (normalized === 'company') return 'company'
  if (normalized === 'org' || normalized === 'custom') return 'org'
  return 'task'
}

function hasCompanyRuntimeIdentity(session: Session): boolean {
  return !!(
    session.isCompanyRuntime
    || session.parentSessionId
    || session.workItemProjectionId
    || session.roleWorkItems
    || session.executorRoleWorkItems
  )
}

function isCompanyRuntimeSession(
  session: Session | null | undefined,
  relatedSessionCount = 0,
): boolean {
  if (!session) return false
  const mode = String(session.execMode ?? '').trim().toLowerCase()
  return relatedSessionCount > 0
    || hasCompanyRuntimeIdentity(session)
    || mode === 'company'
    || mode === 'org'
    || mode === 'custom'
}

export function sessionHasMoreForDetail(
  session: Session,
  detailLevel: 'summary' | 'full',
): boolean | undefined {
  const scoped = detailLevel === 'full' ? session.fullHasMore : session.summaryHasMore
  if (scoped !== undefined) return scoped
  // Old snapshots only expose the unscoped value. Once either scoped cursor
  // has been observed, the generic field may describe the other policy.
  if (session.summaryHasMore === undefined && session.fullHasMore === undefined) {
    return session.hasMore
  }
  return undefined
}

export function conversationHasOlderHistory(
  sessions: Session[],
  displayedMessageCount: number,
  detailLevel: 'summary' | 'full',
  allowMessageCountFallback = true,
): boolean {
  const pagination = sessions.map(session => sessionHasMoreForDetail(session, detailLevel))
  if (pagination.some(hasMore => hasMore === true)) return true
  if (sessions.length !== 1 || pagination[0] === false) return false
  return allowMessageCountFallback && sessions[0].messageCount > displayedMessageCount
}

function hasCustomRuntimeIdentity(session: Session): boolean {
  const rawMode = String(session.execMode ?? '').trim().toLowerCase()
  const normalizedMode = normalizePanelExecMode(session.execMode)
  const profile = String(session.companyProfile ?? '').trim().toLowerCase()
  if (normalizedMode === 'company') return false
  if (normalizedMode === 'org') return true
  if (rawMode) return false
  return profile === 'custom' || !!session.orgId
}

function isSessionConfigLocked(session: Session | undefined, visibleMessageCount = 0): boolean {
  if (!session) return visibleMessageCount > 0
  const messageCount = Math.max(session.messageCount ?? 0, visibleMessageCount)
  if (messageCount > 0) return true

  const status = String(session.status ?? '').trim().toLowerCase()
  if (status && status !== 'pending') return true

  if (
    session.parentSessionId
    || session.workItemProjectionId
    || session.workItemRoleId
    || session.workItemTurnType
    || session.pendingRuntimeCheckpointId
  ) {
    return true
  }

  if (session.runtimeControlState && session.runtimeControlState !== 'idle') return true
  if ((session.progressLog?.length ?? 0) > 0 || (session.workItemLog?.length ?? 0) > 0) return true
  if (session.roleWorkItems && Object.keys(session.roleWorkItems).length > 0) return true
  if (session.executorRoleWorkItems && Object.keys(session.executorRoleWorkItems).length > 0) return true
  return false
}

export function composerExecModeForSession(session: Session, fallbackExecMode?: string): string {
  const rawMode = String(session.execMode ?? '').trim().toLowerCase()
  const hasExplicitMode = rawMode.length > 0
  const normalized = normalizePanelExecMode(session.execMode)
  if (hasCustomRuntimeIdentity(session)) {
    return 'org'
  }
  if (!hasExplicitMode && normalized === 'task' && hasCompanyRuntimeIdentity(session)) {
    return 'company'
  }
  return session.execMode ?? fallbackExecMode ?? 'task'
}

function composerTaskAgentForSession(
  session: Session,
  locked: boolean,
  fallbackAgent: TaskPreferredAgent,
): TaskPreferredAgent {
  if (locked) {
    if (session.selectedExecutionAgent && session.selectedExecutionAgent !== 'native') {
      return session.selectedExecutionAgent
    }
    return session.preferredAgent ?? session.selectedExecutionAgent ?? fallbackAgent
  }
  return session.preferredAgent ?? session.selectedExecutionAgent ?? fallbackAgent
}

function sessionDetailLevel(
  session: Session | null | undefined,
  options?: { childDetail?: boolean },
): 'summary' | 'full' {
  const childDetail = !!options?.childDetail
  if (!session) return 'summary'
  if (childDetail) return 'full'
  return session.execMode === 'company' || session.execMode === 'org' || session.execMode === 'custom' ? 'summary' : 'full'
}

const HUMAN_REVIEW_STATUSES = new Set([
  'awaiting_human',
  'awaiting_manager_review',
  'awaiting_review',
  'awaiting_peer',
])

function canShowContinue(session: Session): boolean {
  const status = String(session.status ?? '').trim()
  const fallback = session.runtimeControlState === 'suspended'
    || (!HUMAN_REVIEW_STATUSES.has(status) && status !== 'running' && status !== 'done')
  return Boolean(session.canResume ?? fallback)
}

function InfoTabView({
  task,
  agents,
  roleLabel,
}: {
  task: Session
  agents: AgentInfo[]
  roleLabel: string | null
}) {
  const { t, translateMaybe } = useI18n()
  const [showDev, setShowDev] = useState(false)
  const priorityKey = task.priority as TaskPriority | undefined
  const priorityMeta = priorityKey && (task.priority as string) in PRIORITY_META
    ? PRIORITY_META[priorityKey]
    : null
  const assigneeNames = task.assigneeIds
    .map(id => agents.find(a => a.agent_id === id)?.name ?? id)
    .filter(Boolean)
  const createdAt = new Date(task.createdAt)
  const employeeLabel = task.employeeAssignment?.name
    ? `${task.employeeAssignment.name}${task.employeeAssignment.category ? ` · ${task.employeeAssignment.category}` : ''}`
    : null
  // Field-level "fact" rendering helper — uniform spacing + treatment.
  const Fact = ({ label, value }: { label: string; value: React.ReactNode }) => (
    <div className="ctx-info-fact">
      <span className="ctx-info-fact-label">{label}</span>
      <span className="ctx-info-fact-value">{value}</span>
    </div>
  )
  return (
    <div className="ctx-info-v2">
      <section className="ctx-info-section">
        <h4 className="ctx-info-section-title">{t('ctx.infoStatus')}</h4>
        <div className="ctx-info-card">
          <div className="ctx-info-status-row">
            <span className={`ctx-info-status-pill status-${task.status}`}>
              <span className="ctx-info-status-dot" />
              {translateMaybe('kanban.status', task.status)}
            </span>
            {priorityMeta && (
              <span className="ctx-info-priority-pill" style={{ color: priorityMeta.color }}>
                <span aria-hidden="true">{priorityMeta.symbol}</span>
                {translateMaybe('td.priority', priorityMeta.label)}
              </span>
            )}
          </div>
          {task.tags && task.tags.length > 0 && (
            <div className="ctx-info-chips">
              {task.tags.map(tag => (
                <span key={tag} className="ctx-info-chip">{tag}</span>
              ))}
            </div>
          )}
        </div>
      </section>

      <section className="ctx-info-section">
        <h4 className="ctx-info-section-title">{t('ctx.peopleAgent')}</h4>
        <div className="ctx-info-card">
          {assigneeNames.length > 0 && (
            <Fact
              label={t('ctx.assignees')}
              value={
                <div className="ctx-info-chips">
                  {assigneeNames.map(name => (
                    <span key={name} className="ctx-info-chip">{name}</span>
                  ))}
                </div>
              }
            />
          )}
          {roleLabel && <Fact label={t('common.role')} value={roleLabel} />}
          {employeeLabel && <Fact label={t('common.employee')} value={employeeLabel} />}
          {task.selectedExecutionAgent && (
            <Fact label={t('ctx.executionAgent')} value={
              <span className="ctx-info-mono">{executionAgentLabel(task.selectedExecutionAgent)}</span>
            } />
          )}
        </div>
      </section>

      <section className="ctx-info-section">
        <h4 className="ctx-info-section-title">{t('ctx.timing')}</h4>
        <div className="ctx-info-card">
          <Fact
            label={t('ctx.created')}
            value={
              <span>
                {createdAt.toLocaleString(undefined, {
                  year: 'numeric', month: 'short', day: 'numeric',
                  hour: '2-digit', minute: '2-digit',
                })}
              </span>
            }
          />
        </div>
      </section>

      <section className="ctx-info-section ctx-info-dev-section">
        <button
          className={`ctx-info-dev-toggle${showDev ? ' expanded' : ''}`}
          type="button"
          onClick={() => setShowDev(v => !v)}
          aria-expanded={showDev}
        >
          <svg width="11" height="11" viewBox="0 0 16 16" aria-hidden="true">
            <path
              d={showDev ? 'M3 6l5 5 5-5' : 'M6 3l5 5-5 5'}
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          <span>{t('ctx.devDetails')}</span>
        </button>
        {showDev && (
          <div className="ctx-info-card ctx-info-dev-card">
            {task.workItemProjectionId && (
              <Fact label={t('common.projection')} value={
                <span className="ctx-info-mono ctx-info-mono-break">{task.workItemProjectionId}</span>
              } />
            )}
            {task.workflowContract && (
              <>
                <Fact label={t('ctx.workflowContract')} value={
                  <span className="ctx-info-mono ctx-info-mono-break">
                    {task.workflowContract.workflow_id ?? '—'}
                  </span>
                } />
                {task.workflowContract.workflow_version && (
                  <Fact label={t('ctx.workflowVersion')} value={
                    <span className="ctx-info-mono">{task.workflowContract.workflow_version}</span>
                  } />
                )}
                {task.workflowContract.manifest_sha256 && (
                  <Fact label={t('ctx.workflowManifestHash')} value={
                    <span className="ctx-info-mono ctx-info-mono-break">{task.workflowContract.manifest_sha256}</span>
                  } />
                )}
                {task.workflowContract.skill_versions && Object.keys(task.workflowContract.skill_versions).length > 0 && (
                  <Fact label={t('ctx.workflowSkills')} value={
                    <span className="ctx-info-mono ctx-info-mono-break">
                      {Object.entries(task.workflowContract.skill_versions).map(([skill, version]) => `${skill}@${version}`).join(' · ')}
                    </span>
                  } />
                )}
              </>
            )}
            <Fact label={t('ctx.taskId')} value={
              <span className="ctx-info-mono ctx-info-mono-break">{task.taskId}</span>
            } />
            <Fact label={t('ctx.channel')} value={
              <span className="ctx-info-mono ctx-info-mono-break">{task.channelId}</span>
            } />
            {task.sessionId && (
              <Fact label={t('ctx.session')} value={
                <span className="ctx-info-mono ctx-info-mono-break">{task.sessionId}</span>
              } />
            )}
          </div>
        )}
      </section>
    </div>
  )
}

export function ContextPanel({
  panelState,
  width,
  onResizeMouseDown,
  isResizing,
  activeView,
  activeSession,
  activeTask,
  linkedTaskSession: linkedTaskSessionProp,
  linkedTaskSessionMessages,
  childDetailSession,
  messages,
  childDetailMessages,
  allSessions,
  openSessions,
  multiViewSessions,
  multiSessionMessages,
  agents,
  childSessions,
  execMode,
  taskPreferredAgent,
  nativeApprovalDefault,
  savedOrgsList,
  activeSavedOrg,
  onSavedOrgsList,
  onSavedOrgLoad,
  canShowAgentsTab = false,
  channelId,
  channelName,
  unreadCounts,
  multiSessionView = false,
  panelTab,
  onPanelTabChange,
  commsState,
  commsMessage,
  onCommsRefresh,
  onCommsReadMessage,
  orgInfoData,
  canShowTeamTab = false,
  onTeamStopRun,
  onTitleChange,
  onSessionConfigChange,
  onSessionTaskAgentChange,
  onSessionNativeApprovalLevelChange,
  onSetNativeApprovalDefault,
  activeSessionWithoutOrder = false,
  onGoToMaster,
  onStop,
  onComplete,
  onResume,
  onResumeTask,
  onStopTask,
  onLocateOnBoard,
  onBackToParent,
  onCloseTaskDetail,
  onOpenChildDetail,
  onOpenExecutionPanel,
  getExternalTeamActivity,
  onSelectSessionTab,
  onCloseSessionTab,
  onToggleMultiSessionView,
  onCollapse,
  onExpand,
  onMaximize,
  onComposerSend,
  onInteractionReply,
  onWorkItemClick,
  onWorkItemOpenSession,
  onMarkRead,
  onLoadSessionHistory,
  isSessionHistoryLoading,
}: ContextPanelProps) {
  const { t, translateMaybe } = useI18n()
  const isActivity = activeView.kind === 'activity'
  const isChildDetail = activeView.kind === 'child-detail'
  const isTaskDetail = activeView.kind === 'task-detail'

  const isCompanyRuntime = isCompanyRuntimeSession(activeSession, childSessions.length)
  const showTabs = activeView.kind === 'session' && activeSession
  const canSend = !!activeSession
  const showSessionStrip = !isChildDetail && openSessions.length > 0
  const canShowMultiSessionView = openSessions.length > 1
  const showMultiSessionGrid = !!(multiSessionView && activeView.kind === 'session' && activeSession && canShowMultiSessionView && panelTab === 'chat')
  const childDetailScrollRef = useRef<HTMLDivElement | null>(null)
  const activeConversation = useMemo(() => {
    const conversationPeers = getConversationPeerSessions(activeSession, allSessions)
    return projectSessionConversation(activeSession, [...conversationPeers, ...childSessions])
  }, [activeSession, childSessions, allSessions])
  const activeDisplaySession = activeConversation.displaySession ?? activeSession
  const activeDetailMode = sessionDetailLevel(activeDisplaySession)
  const activeConversationSession = useMemo(() => {
    return getConversationSessionView(activeSession, activeConversation.runtimeSession, activeConversation.timelineSessions)
  }, [activeSession, activeConversation.runtimeSession, activeConversation.timelineSessions])
  const activeHeaderSession = useMemo(() => {
    return getConversationHeaderSession(activeSession, activeConversation.runtimeSession, activeConversation.timelineSessions)
  }, [activeSession, activeConversation.runtimeSession, activeConversation.timelineSessions])
  const activeConversationProgress = useMemo(() => {
    return mergeConversationProgressLog(activeConversation.timelineSessions)
  }, [activeConversation.timelineSessions])
  const activeWorkItemLog = useMemo(() => (
    activeConversationSession?.workItemLog ?? activeSession?.workItemLog ?? []
  ), [activeConversationSession?.workItemLog, activeSession?.workItemLog])
  const activeWorkItemRoleSessions = useMemo(() => (
    getWorkItemRoleSessions(activeConversationSession ?? activeSession, allSessions)
  ), [activeConversationSession, activeSession, allSessions])
  const activeRoleWorkItems = useMemo(() => (
    activeConversationSession?.roleWorkItems ?? activeSession?.roleWorkItems
  ), [activeConversationSession?.roleWorkItems, activeSession?.roleWorkItems])
  const activeExecutorRoleWorkItems = useMemo(() => (
    activeConversationSession?.executorRoleWorkItems ?? activeSession?.executorRoleWorkItems
  ), [
    activeConversationSession?.executorRoleWorkItems,
    activeSession?.executorRoleWorkItems,
  ])
  const hasRoleWorkItems = !!(
    (activeRoleWorkItems && Object.keys(activeRoleWorkItems).length > 0)
    || (activeExecutorRoleWorkItems && Object.keys(activeExecutorRoleWorkItems).length > 0)
  )
  const visibleAgentSessions = activeWorkItemRoleSessions.length > 0
    ? activeWorkItemRoleSessions
    : childSessions
  const activeConversationMessageCount = useMemo(() => {
    return getConversationMessageCount(activeConversation.timelineSessions)
  }, [activeConversation.timelineSessions])
  const activeConversationLoading = useMemo(() => (
    activeConversation.timelineSessions.some((session) => isSessionHistoryLoading?.(session.taskId) ?? false)
  ), [activeConversation.timelineSessions, isSessionHistoryLoading])
  const resolveConversationHistoryTarget = useCallback((oldestMessage?: ChatMessage) => {
    if (oldestMessage) {
      const matched = activeConversation.timelineSessions.find(
        (session) => session.channelId === oldestMessage.channelId,
      )
      if (matched && sessionHasMoreForDetail(matched, activeDetailMode) !== false) return matched
    }
    const knownTarget = activeConversation.timelineSessions.find(
      session => sessionHasMoreForDetail(session, activeDetailMode) === true,
    )
    if (knownTarget) return knownTarget
    return activeDisplaySession ?? activeSession
  }, [activeConversation.timelineSessions, activeDetailMode, activeDisplaySession, activeSession])

  // Child detail: find the agent for this session
  const childDetailAgent = useMemo(() => {
    if (!childDetailSession) return undefined
    const id = childDetailSession.assigneeIds[0]
    return id ? agents.find(a => a.agent_id === id) : undefined
  }, [childDetailSession, agents])
  // Use prop-provided linked session (computed + lazy-loaded by WorkspacePage)
  // with a local fallback for backwards compat
  const linkedTaskSession = linkedTaskSessionProp ?? (() => {
    const linkedRuntimeTaskId = getLinkedRuntimeTaskId(activeTask)
    if (!linkedRuntimeTaskId) return null
    return allSessions.find(session =>
      session.taskId === linkedRuntimeTaskId
      || session.runtimeTaskId === linkedRuntimeTaskId
      || session.executionTurnId === linkedRuntimeTaskId
    ) ?? null
  })()

  useEffect(() => {
    if (!isChildDetail || !childDetailSession) return
    childDetailScrollRef.current?.scrollTo({ top: 0, behavior: 'auto' })
  }, [isChildDetail, childDetailSession?.taskId])

  // Task for Info tab
  const taskForInfo = activeSession

  // Collapsed state: render a thin strip
  if (panelState === 'collapsed') {
    return (
      <div
        className="ctx-collapse-strip"
        onClick={() => onExpand()}
        title={t('ctx.openPanel')}
      >
        &#x25C0;
      </div>
    )
  }

  return (
    <>
      <div
        className={`ctx-resize-handle${isResizing ? ' active' : ''}`}
        onMouseDown={onResizeMouseDown}
      />
      <div
        className={`ctx-panel${panelState === 'maximized' ? ' maximized' : ''}`}
        style={panelState !== 'maximized' ? { width } : undefined}
      >
        {/* Task detail view */}
        {isTaskDetail && activeTask ? (
          <TaskDetailView
            task={activeTask}
            linkedSession={linkedTaskSession}
            linkedSessionMessages={linkedTaskSessionMessages}
            agents={agents}
            onBack={onCloseTaskDetail ?? onCollapse}
            onOpenLinkedSession={onSelectSessionTab}
            onOpenExecutionPanel={onOpenExecutionPanel}
          />
        ) : isChildDetail && childDetailSession ? (
          /* Child detail view */
          <div className="ctx-child-detail" ref={childDetailScrollRef}>
            <div className="ctx-child-topbar">
              <button className="ctx-back-btn" onClick={onBackToParent}>
                &#x2190; {t('ctx.backToParent')}
              </button>
              {(childDetailSession.canStop ?? childDetailSession.status === 'running') && childDetailSession.runtimeControlState !== 'suspending' && onStopTask && (
                <button
                  className="ctx-child-stop-btn"
                  onClick={() => onStopTask(childDetailSession.taskId)}
                  title={t('ctx.stopAgentTitle')}
                >
                  {t('pc.stop')}
                </button>
              )}
              {childDetailSession.runtimeControlState === 'suspending' && (
                <button className="ctx-child-stop-btn" disabled title={t('ctx.stoppingAgent')}>
                  {t('ctx.stopping')}
                </button>
              )}
              {/* Nor 恢复 on a step of a session no order launched (policy, R1 option B): it is read only too. */}
              {canShowContinue(childDetailSession) && onResumeTask && !activeSessionWithoutOrder && (
                <button
                  className="ctx-child-resume-btn"
                  onClick={() => onResumeTask(childDetailSession.taskId)}
                  title={t('ctx.continueAgentTitle')}
                >
                  {t('ctx.continue')}
                </button>
              )}
            </div>
            <div className="ctx-child-header">
              {childDetailAgent && (
                <div className="ctx-child-avatar">
                  {childDetailAgent.name.charAt(0).toUpperCase()}
                </div>
              )}
              <div className="ctx-child-meta">
                <span className="ctx-child-name">
                  {childDetailAgent?.name ?? childDetailSession.title}
                </span>
                {childDetailSession.workItemRoleName && (
                  <span className="ctx-child-work-item">
                    {childDetailSession.workItemRoleName}
                  </span>
                )}
                {!childDetailSession.workItemRoleName && childDetailSession.workItemProjectionId && (
                  <span className="ctx-child-work-item">
                    {childDetailSession.workItemProjectionId.replace(/_/g, ' ')}
                  </span>
                )}
              </div>
            </div>

            {childDetailSession.handoffContext && (
              <div className="exec-section ctx-child-markdown-section">
                <div className="exec-section-header">
                  <IconHandoff />
                  <span>{t('ctx.receivedFrom')}</span>
                </div>
                <div className="msg-content-agent-card ctx-child-markdown-card">
                  <MarkdownBody content={childDetailSession.handoffContext} />
                </div>
              </div>
            )}

            <div className="ctx-child-transcript">
              <div className="ctx-child-transcript-header">
                <IconTimeline />
                <span>{t('ctx.transcript')}</span>
              </div>
              <MessageList
                key={childDetailSession.channelId}
                messages={childDetailMessages}
                channelName={childDetailSession.title}
                viewKind="session"
                detailMode="full"
                agentStatus={childDetailSession.agentStatus}
                currentTool={childDetailSession.currentTool}
                toolElapsedMs={childDetailSession.toolElapsedMs}
                lastToolSummary={childDetailSession.lastToolSummary}
                progressLog={childDetailSession.progressLog}
                draftAssistantText={childDetailSession.draftAssistantText}
                draftUpdatedAt={childDetailSession.draftUpdatedAt}
                draftIteration={childDetailSession.draftIteration}
                draftTurnId={childDetailSession.draftTurnId}
                onMarkRead={onMarkRead}
                hasOlderHistory={
                  // A scoped backend cursor remains actionable during live
                  // work. Only the racy messageCount fallback is suppressed.
                  conversationHasOlderHistory(
                    [childDetailSession],
                    childDetailMessages.length,
                    'full',
                    !isSessionWorking(childDetailSession),
                  )
                }
                totalMessageCount={childDetailSession.messageCount}
                onLoadOlderHistory={(oldestMessage) => onLoadSessionHistory?.(childDetailSession.taskId, oldestMessage, 'full')}
                loadingOlderHistory={isSessionHistoryLoading?.(childDetailSession.taskId) ?? false}
                scrollPolicy="initial-bottom"
                scrollScope={childDetailSession.channelId}
                showRuntimeProgress
                renderUserMarkdown
              />
            </div>

            {childDetailSession.artifacts && childDetailSession.artifacts.length > 0 && (
              <div className="ctx-child-artifacts">
                <div className="ctx-child-section-header">
                  <IconTool />
                  <span>{t('ctx.artifacts')}</span>
                </div>
                <ul className="ctx-child-artifact-list">
                  {childDetailSession.artifacts.map((a, i) => (
                    <li key={i}>{a}</li>
                  ))}
                </ul>
              </div>
            )}

            {childDetailSession.handoffTo && (
              <div className="ctx-child-markdown-section ctx-child-passed-to">
                <div className="ctx-child-section-header">
                  <IconHandoff />
                  <span>{t('ctx.passedTo')}</span>
                </div>
                <div className="msg-content-agent-card ctx-child-markdown-card">
                  <MarkdownBody content={childDetailSession.handoffTo} />
                </div>
              </div>
            )}
          </div>
        ) : (
          <>
            {showSessionStrip && (
              <div className="ctx-session-strip">
                <div className="ctx-session-strip-scroll">
                  {openSessions.map((session) => {
                    const unreadCount = unreadCounts?.[session.channelId] ?? 0
                    const isActiveTab = activeSession?.taskId === session.taskId
                    return (
                      <div
                        key={session.taskId}
                        className={`ctx-session-chip${isActiveTab ? ' active' : ''}`}
                      >
                        <button
                          className="ctx-session-chip-main"
                          onClick={() => onSelectSessionTab?.(session.taskId)}
                          title={session.title}
                        >
                          <span className={`ctx-session-chip-status status-${session.status}`} />
                          <span className="ctx-session-chip-title">{session.title}</span>
                          {unreadCount > 0 && (
                            <span className="ctx-session-chip-unread">
                              {unreadCount > 99 ? '99+' : unreadCount}
                            </span>
                          )}
                        </button>
                        <button
                          className="ctx-session-chip-close"
                          onClick={() => onCloseSessionTab?.(session.taskId)}
                          title={t('ctx.closeTab')}
                        >
                          &#x00D7;
                        </button>
                      </div>
                    )
                  })}
                </div>
                {canShowMultiSessionView && (
                  <button
                    className={`ctx-session-view-btn${multiSessionView ? ' active' : ''}`}
                    onClick={onToggleMultiSessionView}
                  >
                    {multiSessionView ? t('ctx.singleView') : t('ctx.multiView')}
                  </button>
                )}
              </div>
            )}

            {/* Header with tabs or title */}
            <div className="ctx-header">
              {showMultiSessionGrid ? (
                <span className="ctx-header-title">
                  {isCompanyRuntime ? t('ctx.openRuntimeSessions') : t('ctx.openSessions')} · {t('ctx.ofCount', { a: multiViewSessions.length, b: openSessions.length })}
                </span>
              ) : showTabs ? (
                <div className="ctx-header-tabs" role="tablist" aria-label={t('ctx.sessionViewAria')}>
                  <button
                    role="tab"
                    aria-selected={panelTab === 'chat'}
                    className={`ctx-tab${panelTab === 'chat' ? ' active' : ''}`}
                    onClick={() => onPanelTabChange('chat')}
                  >
                    {t('ctx.tabChat')}
                  </button>
                  {canShowAgentsTab && (
                    <button
                      role="tab"
                      aria-selected={panelTab === 'agents'}
                      className={`ctx-tab${panelTab === 'agents' ? ' active' : ''}`}
                      onClick={() => onPanelTabChange('agents')}
                    >
                      {t('ctx.tabAgents')}
                    </button>
                  )}
                  <button
                    role="tab"
                    aria-selected={panelTab === 'info'}
                    className={`ctx-tab${panelTab === 'info' ? ' active' : ''}`}
                    onClick={() => onPanelTabChange('info')}
                  >
                    {t('ctx.tabInfo')}
                  </button>
                  {onCommsRefresh && (
                    <button
                      role="tab"
                      aria-selected={panelTab === 'comms'}
                      className={`ctx-tab${panelTab === 'comms' ? ' active' : ''}`}
                      onClick={() => onPanelTabChange('comms')}
                    >
                      {t('ctx.tabComms')}
                    </button>
                  )}
                  {canShowTeamTab && (
                    <button
                      role="tab"
                      aria-selected={panelTab === 'team'}
                      className={`ctx-tab${panelTab === 'team' ? ' active' : ''}`}
                      onClick={() => onPanelTabChange('team')}
                    >
                      {t('ctx.tabTeam')}
                    </button>
                  )}
                </div>
              ) : (
                <span className="ctx-header-title">{t('session.activity')}</span>
              )}

              <div className="ctx-header-actions">
                {panelState === 'open' && (
                  <button onClick={onMaximize} title={t('ctx.maximize')}>
                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><rect x="2" y="2" width="10" height="10" rx="1.5" stroke="currentColor" strokeWidth="1.3" /></svg>
                  </button>
                )}
                {panelState === 'maximized' && (
                  <button onClick={onExpand} title={t('ctx.restore')}>
                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><rect x="1" y="3" width="8" height="8" rx="1.5" stroke="currentColor" strokeWidth="1.3" /><path d="M5 3V1.5A.5.5 0 015.5 1H12.5a.5.5 0 01.5.5V8.5a.5.5 0 01-.5.5H11" stroke="currentColor" strokeWidth="1.3" /></svg>
                  </button>
                )}
                <button onClick={onCollapse} title={t('ctx.closePanel')}>
                  <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" /></svg>
                </button>
              </div>
            </div>

            {/* Body */}
            <div className="ctx-body">
              {/* No session selected */}
              {!activeSession && !isActivity && (
                <div className="ctx-empty">
                  <span className="ctx-empty-icon">&#x1F4CB;</span>
                  <span>{isCompanyRuntime ? t('ctx.selectWorkItem') : t('ctx.selectTask')}</span>
                </div>
              )}

              {showMultiSessionGrid && (
                <div className="ctx-multi-grid">
                  {multiViewSessions.map((session) => {
                    const sessionMessages = multiSessionMessages[session.taskId] ?? []
                    const previewMessage = latestSessionPreviewMessage(sessionMessages)
                    const activeChildCount = activeRoleWorkItemCount(session.roleWorkItems) ?? 0
                    const assigneeNames = session.assigneeIds
                      .map(id => agents.find(agent => agent.agent_id === id)?.name ?? id)
                      .filter(Boolean)
                    const runtimeLabel = sessionRuntimeLabel(session, activeChildCount, t)

                    return (
                      <section
                        key={session.taskId}
                        className={`ctx-multi-card${activeSession?.taskId === session.taskId ? ' focused' : ''}`}
                        data-session-preview-id={session.taskId}
                      >
                        <div className="ctx-multi-card-header">
                          <button
                            className="ctx-multi-card-main"
                            onClick={() => onSelectSessionTab?.(session.taskId)}
                            title={t('ctx.focusSession')}
                          >
                            <span className={`ctx-multi-card-status status-${session.status}`} />
                            <span className="ctx-multi-card-title">{session.title}</span>
                          </button>
                          <div className="ctx-multi-card-actions">
                            <button onClick={() => onSelectSessionTab?.(session.taskId)}>{t('ctx.focus')}</button>
                            <button onClick={() => onCloseSessionTab?.(session.taskId)} title={t('ctx.closeTab')}>
                              &#x00D7;
                            </button>
                          </div>
                        </div>
                        <div className="ctx-multi-card-meta">
                          <span>{sessionModeLabel(session)}</span>
                          <span>{translateMaybe('kanban.status', session.status)}</span>
                          {runtimeLabel && <span>{runtimeLabel}</span>}
                          {assigneeNames.length > 0 && <span>{assigneeNames.join(', ')}</span>}
                          <span>{relativeTime(session.updatedAt, t)}</span>
                        </div>
                        <div className="ctx-multi-card-body">
                          {previewMessage ? (
                            <>
                              <span className="ctx-multi-preview-sender">
                                {previewMessage.senderName || previewMessage.sender}
                              </span>
                              <p className="ctx-multi-preview-text">{sessionPreviewText(previewMessage.content)}</p>
                            </>
                          ) : (
                            <span className="ctx-multi-preview-empty">{t('ctx.noMessagesYet')}</span>
                          )}
                        </div>
                      </section>
                    )
                  })}
                </div>
              )}

              {/* Activity view */}
              {isActivity && (
                <>
                  <div style={{ padding: '8px 12px', fontSize: 12, color: 'var(--text-secondary)', borderBottom: '1px solid var(--border)' }}>
                    {t('ctx.recentActivity')}
                  </div>
                  <MessageList
                    key={channelId}
                    messages={messages}
                    channelName={t('session.activity')}
                    viewKind="activity"
                    detailMode="summary"
                    onInteractionReply={onInteractionReply}
                    onMarkRead={onMarkRead}
                    scrollScope={channelId}
                  />
                </>
              )}

              {/* Session view — Chat tab (the secretary channel left the console: policy, R1 option B) */}
              {activeSession && !isActivity && !showMultiSessionGrid && panelTab === 'chat' && (
                <div
                  className="ctx-session-chat"
                  data-session-render-key={`${activeSession.projectId}:${activeSession.taskId}:${activeConversationSession?.channelId ?? channelId}`}
                >
                  <TaskHeaderBar
                    session={activeHeaderSession ?? activeSession}
                    agents={agents}
                    onTitleChange={onTitleChange}
                    onViewOnBoard={onLocateOnBoard ? () => onLocateOnBoard(activeSession.taskId) : undefined}
                    onStop={onStop}
                    onComplete={(activeHeaderSession ?? activeSession).status !== 'done' && (activeHeaderSession ?? activeSession).status !== 'cancelled' ? onComplete : undefined}
                    // 恢复 only on a session an order launched (policy, R1 option B): one no order launched is
                    // read only, and resuming it would set it working again.
                    onResume={activeSessionWithoutOrder ? undefined : onResume}
                  />
                  {isCompanyRuntime && (
                    <div className="ctx-work-item-progress">
                      <WorkItemProgressCard
                        workItemLog={activeWorkItemLog}
                        roleWorkItems={activeRoleWorkItems}
                        executorRoleWorkItems={activeExecutorRoleWorkItems}
                        childSessions={activeWorkItemRoleSessions}
                        isCompanyRuntime={isCompanyRuntime}
                        onWorkItemClick={onWorkItemClick}
                        getExternalTeamActivity={getExternalTeamActivity}
                      />
                    </div>
                  )}
                  {!isCompanyRuntime && (
                    <ExternalTeamSummaryCard
                      session={activeConversationSession ?? activeDisplaySession ?? activeSession}
                      record={getExternalTeamActivity?.(
                        (activeConversationSession ?? activeDisplaySession ?? activeSession).executionTurnId || activeSession.taskId,
                        (activeConversationSession ?? activeDisplaySession ?? activeSession).externalTeamSummary?.externalInvocationId,
                      )}
                      onOpen={onOpenExecutionPanel}
                    />
                  )}
                  <MessageList
                    messages={messages}
                    channelName={channelName}
                    viewKind="session"
                    detailMode={activeDetailMode}
                    agentStatus={isCompanyRuntime ? undefined : (activeConversationSession?.agentStatus ?? activeDisplaySession?.agentStatus)}
                    currentTool={isCompanyRuntime ? undefined : (activeConversationSession?.currentTool ?? activeDisplaySession?.currentTool)}
                    toolElapsedMs={isCompanyRuntime ? undefined : (activeConversationSession?.toolElapsedMs ?? activeDisplaySession?.toolElapsedMs)}
                    lastToolSummary={isCompanyRuntime ? undefined : (activeConversationSession?.lastToolSummary ?? activeDisplaySession?.lastToolSummary)}
                    progressLog={isCompanyRuntime ? undefined : activeConversationProgress}
                    draftAssistantText={isCompanyRuntime ? undefined : (activeConversationSession?.draftAssistantText ?? activeDisplaySession?.draftAssistantText)}
                    draftUpdatedAt={isCompanyRuntime ? undefined : (activeConversationSession?.draftUpdatedAt ?? activeDisplaySession?.draftUpdatedAt)}
                    draftIteration={isCompanyRuntime ? undefined : (activeConversationSession?.draftIteration ?? activeDisplaySession?.draftIteration)}
                    draftTurnId={isCompanyRuntime ? undefined : (activeConversationSession?.draftTurnId ?? activeDisplaySession?.draftTurnId)}
                    isCompanyRuntime={isCompanyRuntime}
                    workItemLog={activeWorkItemLog}
                    roleWorkItems={activeRoleWorkItems}
                    executorRoleWorkItems={activeExecutorRoleWorkItems}
                    childSessions={activeWorkItemRoleSessions}
                    onInteractionReply={onInteractionReply}
                    onWorkItemClick={onWorkItemClick}
                    onWorkItemOpenSession={onWorkItemOpenSession}
                    onMarkRead={onMarkRead}
                    scrollScope={channelId}
                    hasOlderHistory={
                      // Keep known cursors available during live work;
                      // suppress only the count-based fallback.
                      conversationHasOlderHistory(
                        activeConversation.timelineSessions,
                        messages.length,
                        activeDetailMode,
                        !activeConversation.timelineSessions.some(isSessionWorking),
                      )
                    }
                    totalMessageCount={activeConversationMessageCount}
                    onLoadOlderHistory={(oldestMessage) => {
                      const targetSession = resolveConversationHistoryTarget(oldestMessage)
                      if (!targetSession) return
                      return onLoadSessionHistory?.(
                        targetSession.taskId,
                        oldestMessage,
                        activeDetailMode,
                      )
                    }}
                    loadingOlderHistory={activeConversationLoading}
                    showWorkItemRuntimeCard={false}
                    showRuntimeProgress={activeDetailMode === 'full'}
                  />
                  {activeSessionWithoutOrder ? (
                    // No order launched this session: it reads, it takes no new line (policy, R1 option B).
                    <div className="ctx-no-order-note" role="note" data-testid="ctx-no-order-note">
                      <span>{t('ctx.noOrderReadOnly')}</span>
                      {onGoToMaster && (
                        <button type="button" className="ctx-no-order-go" data-testid="ctx-no-order-go-master" onClick={onGoToMaster}>
                          {t('ctx.goToMaster')}
                        </button>
                      )}
                    </div>
                  ) : (
                  <MessageComposer
                    key={activeConversationSession?.channelId ?? channelId}
                    disabled={!canSend}
                    channelId={activeConversationSession?.channelId ?? channelId}
                    execMode={composerExecModeForSession(activeSession, execMode)}
                    companyProfile={activeSession.companyProfile}
                    taskPreferredAgent={composerTaskAgentForSession(
                      activeSession,
                      isSessionConfigLocked(
                        activeConversationSession ?? activeDisplaySession ?? activeSession,
                        Math.max(activeConversationMessageCount, messages.length),
                      ),
                      taskPreferredAgent,
                    )}
                    nativeApprovalLevel={activeSession.nativeApprovalLevel ?? nativeApprovalDefault}
                    nativeApprovalDefault={nativeApprovalDefault}
                    agentStatus={activeConversationSession?.agentStatus ?? activeDisplaySession?.agentStatus}
                    currentTool={activeConversationSession?.currentTool ?? activeDisplaySession?.currentTool}
                    displayTool={activeConversationSession?.displayTool ?? activeDisplaySession?.displayTool}
                    runtimeControlState={(activeConversationSession ?? activeDisplaySession)?.runtimeControlState}
                    canStop={(activeConversationSession ?? activeDisplaySession)?.canStop}
                    savedOrgs={savedOrgsList ?? null}
                    activeSavedOrg={activeSavedOrg ?? null}
                    selectedOrgId={activeSession.orgId ?? activeSavedOrg ?? null}
                    lockedMode={isSessionConfigLocked(
                      activeConversationSession ?? activeDisplaySession ?? activeSession,
                      Math.max(activeConversationMessageCount, messages.length),
                    )}
                    activeAgentCount={activeAgentCountFor(activeRoleWorkItems, visibleAgentSessions)}
                    contextTokens={activeConversationSession?.contextTokens ?? activeDisplaySession?.contextTokens}
                    contextWindow={activeConversationSession?.contextWindow ?? activeDisplaySession?.contextWindow}
                    contextRemainingPct={activeConversationSession?.contextRemainingPct ?? activeDisplaySession?.contextRemainingPct ?? 100}
                    onSend={onComposerSend}
                    onModeChange={(mode, profile, orgId) => onSessionConfigChange?.(activeSession.taskId, mode, profile, orgId)}
                    onTaskAgentChange={(preferredAgent) => onSessionTaskAgentChange?.(activeSession.taskId, preferredAgent)}
                    onNativeApprovalLevelChange={(level) => onSessionNativeApprovalLevelChange?.(activeSession.taskId, level)}
                    onSetNativeApprovalDefault={onSetNativeApprovalDefault}
                    onSavedOrgsRefresh={onSavedOrgsList}
                    onSavedOrgLoad={onSavedOrgLoad}
                    onStop={onStop}
                  />
                  )}
                </div>
              )}

              {/* Session view — Agents tab */}
              {activeSession && !showMultiSessionGrid && panelTab === 'agents' && canShowAgentsTab && (
                (visibleAgentSessions.length > 0 || hasRoleWorkItems) ? (
                  <AgentWorkPanel
                    sessions={visibleAgentSessions}
                    roleWorkItems={activeRoleWorkItems}
                    isCompanyRuntime={isCompanyRuntime}
                    agents={agents}
                    onOpenChildDetail={onOpenChildDetail}
                    onOpenExecutionPanel={onOpenExecutionPanel}
                  />
                ) : (
                  <div className="ctx-empty">
                    <span className="ctx-empty-icon">&#x1F9E0;</span>
                    <span>{t('ctx.noChildAgents')}</span>
                  </div>
                )
              )}

              {/* Session view — Info tab.
                  Reorganised into semantic cards (Overview / Identity /
                  Timing) instead of a flat key-value list. Internal-only
                  debug fields (projection id, channel id) are tucked into
                  a collapsed "Developer details" section so they don't
                  dominate the user-facing summary. */}
              {activeSession && !showMultiSessionGrid && panelTab === 'info' && taskForInfo && (
                <InfoTabView
                  task={taskForInfo}
                  agents={agents}
                  roleLabel={getWorkItemRoleLabel(taskForInfo)}
                />
              )}

              {/* Comms tab */}
              {panelTab === 'comms' && onCommsRefresh && onCommsReadMessage && (
                <div style={{ flex: 1, overflow: 'auto', minHeight: 0 }}>
                  <CommsPanel
                    state={commsState ?? null}
                    message={commsMessage ?? null}
                    onRefresh={onCommsRefresh}
                    onReadMessage={onCommsReadMessage}
                    embedded
                  />
                </div>
              )}

              {panelTab === 'team' && canShowTeamTab && (
                <div style={{ flex: 1, overflow: 'auto', minHeight: 0 }}>
                  <ProjectCockpit
                    orgInfoData={orgInfoData ?? null}
                    commsState={commsState ?? null}
                    onStopRun={onTeamStopRun}
                    embedded
                  />
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </>
  )
}
