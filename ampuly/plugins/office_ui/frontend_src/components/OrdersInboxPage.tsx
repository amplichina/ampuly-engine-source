// 业务工单 = 收件箱 (frontend redesign step 3, first page; policy). One page, one question: which orders wait
// on me, and what do I do now. Left, the inbox: three groups (等你处理 / 进行中 / 已完成, 已取消 folded), each row four
// fixed slots (name · flow and step · due · one status word). Right, ONE order in three blocks: 现在 (one sentence
// where it stands and ONE primary button, its consequence under it), 交付物 (the order folder, one source), 记录 (who ·
// did what · when, the latest five). Under them GPT6's 「当前位置」 context, and 「更多」 with everything else the owner
// could do before, each still reachable. No 简明 / 详细 toggle: this is the one view.
//
// Every word comes from the server's projection (order_list's `attention`, S0-A; `current_nodes`, S0-B; the owner
// card, S1-A) read through lib/ordersInbox; the client-side queue rule is not used here. Every write goes through the
// callbacks App hands in -- each on the request tracker or the answer path -- and a button says the receipt's three
// states in the tracker's words. Every reply reaches this page through the current-project gate in App. Never 同意 /
// 通过: an approval is decided at the end of its materials (GatePanel, the acceptance panel), not on a row.
//
// The builder (WorkflowsPage, surface builder) is untouched: this page replaces only what its orders surface showed.

import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useI18n } from '../i18n'
import { filesInScope } from '../lib/artifactScope'
import { orderEventDetail, timelineEventName, timelineEventTime } from '../lib/orderEventNames'
import { feedbackNotes } from '../lib/orderDrawer'
import {
  INBOX_GROUPS, INBOX_STATES, INBOX_STATE_LABELS, GROUP_WORDS, attentionOf, buttonReceipt, expectedDelivery, firstListed, gateCardOf,
  inboxSections, intakeSummary, latestEntries, lessonAnswers, lessonCardOf, lineageOf, listedOrders, nowView, openCardOf, rowTitle, rowView, stateFromQueue, stepText,
  type InboxGroup, type InboxState, type NowView,
} from '../lib/ordersInbox'
import { holdUntil, ownerCardOf } from '../lib/ownerCard'
import { keyOf, type Tracked } from '../lib/requestTracker'
import type { Answering } from '../lib/answerPath'
import { classifyPageError, pageErrorMessageKey } from '../lib/pageError'
import { planSummary, type ModelPlan } from '../workspace/modelTiers'
import type { OrgEmployee } from '../types/visual'
import type { BudgetDraft } from './BudgetLimitInputs'
import type { DashboardBlocker } from './DashboardPage'
import type { ArtifactIndex } from './ArtifactsPanel'
import { GatePanel, type GateDecision, type GateDetail } from './GatePanel'
import { OrderAcceptancePanel } from './OrderAcceptancePanel'
import { OrderHandlingCard } from './OrderHandlingCard'
import { OrderLaunchForm } from './OrderLaunchForm'
import { OrderBlueprintBar, OrderBudgetPanel, OrderCancelForm, OrderFdeEditor, OrderRejectForm, OrderTimelinePanel } from './OrderMorePanels'
import { OwnerCard } from './OwnerCard'
import { WecomConversationBlock } from './WecomChannelCard'
import { conversationOf, orderSourceWord } from '../lib/wecomChannel'
import type { OrderMetrics, OrderTimeline, OrderUsage, RunBudget, SolutionBlueprint, WorkflowInvokeState, WorkflowSummary, WorkOrderSummary } from './WorkflowsPage'

/** The panels 「更多」 (and two of the 现在 buttons) open under the blocks, one at a time. */
type MorePanel = 'acceptance' | 'gate' | 'handling' | 'cancel' | 'reject' | 'budget' | 'blueprint' | 'fde' | 'timeline' | 'distribution'

export interface OrdersInboxPageProps {
  workflows: WorkflowSummary[] | null
  orders: WorkOrderSummary[] | null
  /** Owner supplied, read-only original rework inspection; no approval callback. */
  reworkInspectionSlot?: (order: WorkOrderSummary) => ReactNode
  /** The server listed only the latest part of the orders (order_list's `truncated`): the page says so. */
  ordersTruncated?: boolean
  onLoadOlderOrders?: () => void
  olderOrdersLoading?: boolean
  error?: string | null
  connected?: boolean
  onRefresh: () => void
  /** The projects this console knows and the one on screen: the filter row's project select switches between them. */
  projects?: ReadonlyArray<{ id: string; name: string }>
  activeProjectId?: string
  onSwitchProject?: (projectId: string) => void
  /** 返回项目 / the project link in 「当前位置」: 项目中心. */
  onOpenProject?: () => void
  orderScope?: { customer_id: string; environment_id: string }
  onOrderScopeChange?: (scope: { customer_id: string; environment_id: string }) => void
  /** Another page sent the person here: the group to stand in, a flow, a search, or one order to open (App keeps the
   *  old queue names on this request; they are mapped to the inbox's states). */
  orderQueueRequest?: { queue: string; state?: string; query?: string; open?: string; flow?: string; seq: number } | null
  /** ＋ 新建工单: the one launch form (OrderLaunchForm), in place. */
  invokeState: WorkflowInvokeState | null
  onInvoke: (workflowId: string, fields: Record<string, string>, budgetLimits?: Record<string, number>) => void
  employees?: OrgEmployee[]
  /** The pauses the console polls: the owner card in the 现在 block, the gate 看材料并处理 opens, the lesson card. */
  blockers?: DashboardBlocker[]
  budgets?: RunBudget[] | null
  pendingReservations?: Array<{ reservation_id: string; budget_id: string; kind: string; amount: number; note: string }>
  onResolveBudget?: (reservationId: string, happened: boolean) => void
  onCreateBudget?: (orderId: string, limits: Record<string, number>) => boolean
  onRaiseBudget?: (orderId: string, limits: Record<string, number>) => boolean
  onCloseBudget?: (orderId: string, reason: string) => boolean
  budgetBusyOrderId?: string | null
  orderMetrics?: OrderMetrics[]
  orderUsage?: OrderUsage[]
  orderMetricsTruncated?: boolean
  /** The request tracker's entries (lib/requestTracker useTracked): each button reads its own write's state. */
  tracked?: Tracked
  /** 我来接 / 打回: the intake review. On the tracker in App. */
  onReviewOrder: (orderId: string, decision: 'approve' | 'reject', note: string) => void
  /** 看进度 / 看出了什么问题 / 打开执行区: the order's run. */
  onOpenTask: (taskId: string) => void
  onAcceptanceGet: (orderId: string) => void
  acceptanceBusyOrderId?: string | null
  onAcceptanceEvidence: (orderId: string, criterion: string, evidence: Record<string, unknown>) => void
  onAcceptanceSubmit: (orderId: string) => void
  onAcceptanceDecide: (orderId: string, decision: 'accepted' | 'rejected' | 'accepted_with_exceptions', note: string, exceptions: string[], options?: { closeOrder?: boolean }) => void
  onAcceptanceRework: (orderId: string, targetNode: string, requestId?: string) => void
  onAcceptanceRoundExport?: (orderId: string, roundNo: number) => void
  onPortfolioExport?: (orderId: string) => void
  onAuthorizeRedistribution?: (orderId: string) => void
  onOpenArtifact?: (path: string) => void
  /** The one answer path (lib/answerPath) for the owner card and the lesson card. */
  onDecideBlocker?: (blocker: DashboardBlocker, optionId: string) => void
  /** 暂缓提醒一天 / 恢复提醒 (A19) on the pause the order waits on the owner for: under the 现在 card's 更多, or -- for a
   * gate, whose 现在 block opens the materials -- under the order's 更多. They were on 总控's card until the owner cut
   * 总控 to the conversation : the order is handled here now. Absent = not offered. */
  onHoldBlocker?: (blocker: DashboardBlocker, untilAt: string) => void
  onReleaseBlocker?: (blocker: DashboardBlocker) => void
  answering?: Answering
  decisionErrors?: Record<string, string>
  /** A business gate's materials view (GatePanel): asked for, drawn under the 现在 block, decided at its end. */
  gateDetail?: GateDetail | null
  gateBusy?: boolean
  gateError?: string | null
  onOpenGate?: (blocker: DashboardBlocker) => void
  onDecideGate?: (decision: GateDecision) => void
  onCloseGate?: () => void
  /** 交付物: the project's file index (artifacts_list), read for the order's own folder -- the one source. */
  artifactIndex?: ArtifactIndex | null
  artifactsLoading?: boolean
  onRefreshArtifacts?: () => void
  /** 记录: one order's timeline, asked for when the order is opened. */
  orderTimeline?: OrderTimeline | null
  orderTimelineError?: string | null
  onOrderTimeline?: (orderId: string) => void
  orderModelPlan?: ModelPlan
  onCancelOrder?: (orderId: string, reason: string, cascadeChildren: boolean) => boolean
  onUpdateHandling?: (orderId: string, patch: Record<string, unknown>) => void
  updatingHandlingOrderId?: string | null
  /** 到搭建台看流程: open this flow on the builder. */
  onOpenFlow?: (workflowId: string) => void
  onOpenAssistant?: () => void
  onCustomerPreview?: (orderId: string) => void
  customerPreviewSlot?: ReactNode
  blueprints?: SolutionBlueprint[]
  onSaveBlueprint?: (orderId: string, name: string) => void
  onApplyBlueprint?: (blueprintId: string, orderId: string) => void
  onUpdateFdeControlPlane?: (orderId: string, patch: Record<string, unknown>) => void
  updatingFdeControlPlaneOrderId?: string | null
  /** 分发授权单: the owner's card for one order (App fills it). */
  distributionSlot?: (order: WorkOrderSummary) => ReactNode
  /** The shared file preview drawer. */
  artifactsSlot?: ReactNode
  /** The order open at first (a test, or a link into one order). */
  initialOpenOrderId?: string
}

type OrderRow = WorkOrderSummary & Parameters<typeof attentionOf>[0]

export function OrdersInboxPage({
  workflows, orders, reworkInspectionSlot, ordersTruncated = false, onLoadOlderOrders, olderOrdersLoading = false, error, connected = true, onRefresh,
  projects = [], activeProjectId = '', onSwitchProject, onOpenProject, orderScope = { customer_id: '', environment_id: '' }, onOrderScopeChange,
  orderQueueRequest = null, invokeState, onInvoke, employees = [], blockers = [], budgets, pendingReservations, onResolveBudget, onCreateBudget, onRaiseBudget, onCloseBudget,
  budgetBusyOrderId = null, orderMetrics, orderUsage, orderMetricsTruncated = false, tracked = {}, onReviewOrder, onOpenTask, onAcceptanceGet, acceptanceBusyOrderId = null,
  onAcceptanceEvidence, onAcceptanceSubmit, onAcceptanceDecide, onAcceptanceRework, onAcceptanceRoundExport, onPortfolioExport, onAuthorizeRedistribution, onOpenArtifact,
  onDecideBlocker, onHoldBlocker, onReleaseBlocker, answering, decisionErrors, gateDetail = null, gateBusy = false, gateError = null, onOpenGate, onDecideGate, onCloseGate,
  artifactIndex = null, artifactsLoading = false, onRefreshArtifacts, orderTimeline = null, orderTimelineError = null, onOrderTimeline, orderModelPlan = null,
  onCancelOrder, onUpdateHandling, updatingHandlingOrderId = null, onOpenFlow, onOpenAssistant, onCustomerPreview, customerPreviewSlot,
  blueprints, onSaveBlueprint, onApplyBlueprint, onUpdateFdeControlPlane, updatingFdeControlPlaneOrderId = null, distributionSlot, artifactsSlot, initialOpenOrderId,
}: OrdersInboxPageProps) {
  const { t, locale } = useI18n()
  const zh = locale.startsWith('zh')
  // A request already here when the page mounts (sent from another page) is honoured at once; later ones by the effect.
  const [state, setState] = useState<InboxState>(() => (orderQueueRequest ? stateFromQueue(orderQueueRequest.queue, orderQueueRequest.state) : 'all'))
  const [flowFilter, setFlowFilter] = useState(() => orderQueueRequest?.flow ?? '')
  const [query, setQuery] = useState(() => orderQueueRequest?.query ?? '')
  const [scopeDraft, setScopeDraft] = useState(orderScope)
  const [selectedId, setSelectedId] = useState<string | null>(() => orderQueueRequest?.open || initialOpenOrderId || null)
  const [panel, setPanel] = useState<MorePanel | null>(null)
  const [logExpanded, setLogExpanded] = useState(false)
  const [newOrderOpen, setNewOrderOpen] = useState(false)
  const [newOrderFlowId, setNewOrderFlowId] = useState<string | null>(null)
  const [drafts, setDrafts] = useState<Record<string, Record<string, string>>>({})
  const [launchBudgets, setLaunchBudgets] = useState<Record<string, BudgetDraft>>({})
  const [refreshing, setRefreshing] = useState(false)
  const listRef = useRef<HTMLElement>(null)

  useEffect(() => { setScopeDraft(orderScope) }, [orderScope.customer_id, orderScope.environment_id])
  useEffect(() => { setRefreshing(false) }, [workflows, orders, error])

  // Sent here from another page: stand in that group, search for that, and open that order.
  const requestSeq = orderQueueRequest?.seq ?? 0
  useEffect(() => {
    if (!orderQueueRequest) return
    setState(stateFromQueue(orderQueueRequest.queue, orderQueueRequest.state))
    setQuery(orderQueueRequest.query ?? '')
    setFlowFilter(orderQueueRequest.flow ?? '')
    const one = orderQueueRequest.open ?? ''
    if (one) {
      setSelectedId(one)
      window.setTimeout(() => listRef.current?.querySelector(`[data-order-row="${CSS.escape(one)}"]`)?.scrollIntoView({ block: 'center' }), 80)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestSeq])

  // The new order form closes once its send came back placed; an error keeps it open with the reason.
  const lastInvoke = useRef(invokeState)
  useEffect(() => {
    const before = lastInvoke.current
    lastInvoke.current = invokeState
    if (before?.status === 'sending' && before.workflowId === newOrderFlowId && invokeState === null) setNewOrderFlowId(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [invokeState])

  const flowOf = (id: string) => (workflows ?? []).find(flow => flow.id === id) ?? null
  const flowName = (id: string) => flowOf(id)?.name?.trim() || id
  const nameOf = (id: string) => employees.find(person => person.employee_id === id)?.name || id
  const rows = (orders ?? []) as OrderRow[]
  const listed = useMemo(() => listedOrders(rows, { state, flow: flowFilter, query }), [rows, state, flowFilter, query])
  const sections = useMemo(() => inboxSections(listed), [listed])
  const current: OrderRow | null = (selectedId ? listed.find(order => order.order_id === selectedId) : null) ?? firstListed(sections)
  const currentId = current?.order_id ?? ''
  const flowsWithRows = useMemo(() => {
    const counts = new Map<string, number>()
    for (const order of rows) if (order.workflow_id) counts.set(order.workflow_id, (counts.get(order.workflow_id) ?? 0) + 1)
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)).map(([id]) => id)
  }, [rows])
  const partialRows = listed.some(order => attentionOf(order).partial && !attentionOf(order).ended)

  // Opening an order asks for its timeline (the 记录 block and 时间线全部 read it) and closes what the last one opened.
  const askTimeline = useRef(onOrderTimeline)
  askTimeline.current = onOrderTimeline
  useEffect(() => {
    if (!currentId) return
    setPanel(null)
    setLogExpanded(false)
    askTimeline.current?.(currentId)
  }, [currentId])

  const receiptOf = (action: string, id: string) => tracked[keyOf(action, id)]
  const openPanel = (next: MorePanel) => setPanel(prev => (prev === next ? null : next))
  const openAcceptance = (order: OrderRow) => { setPanel('acceptance'); onAcceptanceGet(order.order_id) }
  const openGate = (blocker: DashboardBlocker) => { setPanel('gate'); onOpenGate?.(blocker) }
  const openRun = (order: OrderRow) => { if (order.launched_task_id) onOpenTask(order.launched_task_id); else setPanel('timeline') }
  // 主工单 / 子工单: a linked order opens in place. One the filter row hides is brought back the way the old table's
  // 在工单表里操作 did -- the filters go back to 全部 -- so the link never lands on the first row instead.
  const openOrder = (orderId: string) => {
    if (!listed.some(order => order.order_id === orderId)) { setState('all'); setFlowFilter(''); setQuery('') }
    setSelectedId(orderId)
    window.setTimeout(() => listRef.current?.querySelector(`[data-order-row="${CSS.escape(orderId)}"]`)?.scrollIntoView({ block: 'center' }), 80)
  }

  const invokable = (workflows ?? []).filter(flow => flow.invokable)
  const newOrderFlow = newOrderFlowId ? invokable.find(flow => flow.id === newOrderFlowId) ?? null : null

  const renderRow = (order: OrderRow) => {
    const view = rowView(order, flowName(order.workflow_id), zh, nameOf)
    const active = order.order_id === currentId
    return (
      <button key={order.order_id} type="button" className={`inbox-row${active ? ' is-active' : ''}`} data-order-row={order.order_id} data-order-status={order.status}
        data-inbox-group={view.group} data-held={view.held ? 'true' : undefined} aria-current={active ? 'true' : undefined} onClick={() => setSelectedId(order.order_id)}>
        <span className="inbox-row__main">
          <strong className="inbox-row__title">{view.title}</strong>
          <small className="inbox-row__sub">{view.sub}</small>
        </span>
        <span className="inbox-row__side">
          <small className="inbox-row__time" data-tone={view.timeTone || undefined}>{view.time}</small>
          <i className="inbox-row__word" data-tone={view.wordTone || undefined}>{view.word}</i>
        </span>
      </button>
    )
  }

  const renderGroup = (group: InboxGroup) => {
    const items = sections[group]
    const words = GROUP_WORDS[group]
    const head = (
      <>
        <span className="inbox-group__name">{words.name[zh ? 0 : 1]}</span>
        <b className="inbox-group__count" data-count={items.length}>{items.length}</b>
        <small className="inbox-group__note">{words.holds[zh ? 0 : 1]} · {words.sortedBy[zh ? 0 : 1]}</small>
      </>
    )
    if (group === 'cancelled') {
      if (items.length === 0) return null
      return (
        <details key={group} className="inbox-group inbox-group--folded" data-inbox-group={group}>
          <summary className="inbox-group__head">{head}</summary>
          {items.map(renderRow)}
        </details>
      )
    }
    return (
      <section key={group} className="inbox-group" data-inbox-group={group} aria-label={words.name[zh ? 0 : 1]}>
        <h3 className="inbox-group__head">{head}</h3>
        {items.length === 0 ? <p className="inbox-group__empty">{t('inbox.groupEmpty')}</p> : items.map(renderRow)}
      </section>
    )
  }

  return (
    <div className="workflows-page orders-inbox" data-surface="orders">
      {!connected && <div className="workflows-offline-state" role="status">{t('workflows.offline')}</div>}
      <header className="inbox-head">
        <h2 className="workflows-title">{t('app.page.orders')}</h2>
        <div className="inbox-filters" data-testid="inbox-filters">
          <select className="inbox-filter" data-testid="inbox-state" aria-label={t('inbox.filter.state')} value={state} onChange={event => setState(event.target.value as InboxState)}>
            {INBOX_STATES.map(item => <option key={item} value={item}>{INBOX_STATE_LABELS[item][zh ? 0 : 1]}</option>)}
          </select>
          {flowsWithRows.length > 1 && (
            <select className="inbox-filter" data-testid="inbox-flow" aria-label={t('inbox.filter.flow')} value={flowFilter} onChange={event => setFlowFilter(event.target.value)}>
              <option value="">{t('inbox.filter.allFlows')}</option>
              {flowsWithRows.map(id => <option key={id} value={id}>{flowName(id)}</option>)}
            </select>
          )}
          {projects.length > 1 && onSwitchProject && (
            <select className="inbox-filter" data-testid="inbox-project" aria-label={t('inbox.filter.project')} value={activeProjectId} disabled={!connected}
              onChange={event => { if (event.target.value && event.target.value !== activeProjectId) onSwitchProject(event.target.value) }}>
              {projects.map(project => <option key={project.id} value={project.id}>{project.name || project.id}</option>)}
            </select>
          )}
          <input type="search" className="inbox-search" data-testid="inbox-search" value={query} onChange={event => setQuery(event.target.value)} placeholder={t('inbox.filter.search')} aria-label={t('inbox.filter.search')} />
          {onOrderScopeChange && (
            <details className="inbox-scope">
              <summary>{t('inbox.filter.more')}</summary>
              <form className="orders-scope-filter" onSubmit={event => { event.preventDefault(); onOrderScopeChange({ customer_id: scopeDraft.customer_id.trim(), environment_id: scopeDraft.environment_id.trim() }) }}>
                <input aria-label={t('orders.filter.customer')} placeholder={t('orders.filter.customer')} value={scopeDraft.customer_id} disabled={!connected} onChange={event => setScopeDraft(prev => ({ ...prev, customer_id: event.target.value }))} />
                <input aria-label={t('orders.filter.environment')} placeholder={t('orders.filter.environment')} value={scopeDraft.environment_id} disabled={!connected} onChange={event => setScopeDraft(prev => ({ ...prev, environment_id: event.target.value }))} />
                <button type="submit" className="workflows-cancel" disabled={!connected}>{t('orders.filter.apply')}</button>
                {(scopeDraft.customer_id || scopeDraft.environment_id) && (
                  <button type="button" className="workflows-cancel" disabled={!connected} onClick={() => { const empty = { customer_id: '', environment_id: '' }; setScopeDraft(empty); onOrderScopeChange(empty) }}>{t('orders.filter.clear')}</button>
                )}
              </form>
            </details>
          )}
        </div>
        <div className="inbox-head__actions">
          <button type="button" className="workflows-refresh" disabled={refreshing || !connected} aria-busy={refreshing} onClick={() => { if (refreshing) return; setRefreshing(true); onRefresh() }}>
            {refreshing ? t('workflows.loading') : t('workflows.refresh')}
          </button>
          <button type="button" className="shell-btn shell-btn--primary" data-testid="orders-new" aria-expanded={newOrderOpen} disabled={!connected} onClick={() => setNewOrderOpen(open => !open)}>
            {t('inbox.newOrder')}
          </button>
        </div>
      </header>

      {error && (
        <div className={`workflows-error workflows-error--page workflows-error--${classifyPageError(error)}`} role="alert" data-error-kind={classifyPageError(error)}>
          <span><strong>{t(pageErrorMessageKey(error) as Parameters<typeof t>[0])}</strong><br />{error}</span>
          <button type="button" className="workflows-cancel" onClick={onRefresh} disabled={!connected}>{t('workflows.retry')}</button>
        </div>
      )}

      {newOrderOpen && (
        <div className="orders-new-list" data-testid="orders-new-list">
          <p>{t('inbox.newOrderHint')}</p>
          {invokable.map(flow => (
            <button key={flow.id} type="button" data-flow={flow.id} onClick={() => { setNewOrderOpen(false); setNewOrderFlowId(flow.id) }}>
              <strong>{flow.name || flow.id}</strong><small>v{flow.version}</small>
            </button>
          ))}
          {invokable.length === 0 && <em>{workflows === null ? t('inbox.flowsLoading') : t('inbox.noInvokableFlow')}</em>}
        </div>
      )}
      {newOrderFlow && (
        <section className="orders-new-form" data-testid="orders-new-form" data-flow={newOrderFlow.id} aria-label={t('inbox.newOrderTitle')}>
          <header><strong>{t('inbox.newOrderTitle')} · {newOrderFlow.name || newOrderFlow.id}</strong><small>v{newOrderFlow.version}</small></header>
          <OrderLaunchForm
            fields={newOrderFlow.intake_fields}
            values={drafts[newOrderFlow.id] ?? {}}
            onField={(fieldId, value) => setDrafts(prev => ({ ...prev, [newOrderFlow.id]: { ...(prev[newOrderFlow.id] ?? {}), [fieldId]: value } }))}
            budget={launchBudgets[newOrderFlow.id] ?? {}}
            onBudget={next => setLaunchBudgets(prev => ({ ...prev, [newOrderFlow.id]: next }))}
            sending={invokeState?.workflowId === newOrderFlow.id && invokeState.status === 'sending'}
            error={invokeState?.workflowId === newOrderFlow.id && invokeState.status === 'error' ? invokeState.error ?? '' : ''}
            connected={connected}
            onCancel={() => setNewOrderFlowId(null)}
            onSubmit={limits => onInvoke(newOrderFlow.id, drafts[newOrderFlow.id] ?? {}, limits)}
          />
        </section>
      )}

      {ordersTruncated && orders !== null && orders.length > 0 && (
        <p className="orders-truncated" role="status" data-testid="orders-truncated">{t('inbox.listTruncated', { n: orders.length })}</p>
      )}
      {partialRows && <p className="orders-truncated" role="status" data-testid="inbox-partial">{t('inbox.partial')}</p>}

      <div className="inbox-body">
        <aside className="inbox-list" data-testid="inbox-list" ref={listRef} aria-label={t('inbox.listLabel')}>
          {orders === null ? (
            <p className="inbox-group__empty">{t('workflows.loading')}</p>
          ) : orders.length === 0 ? (
            <p className="inbox-group__empty" data-testid="inbox-empty">{t('inbox.empty')}</p>
          ) : listed.length === 0 ? (
            <p className="inbox-group__empty" role="status" data-testid="orders-none-shown">{t('inbox.noneShown', { n: orders.length })}</p>
          ) : INBOX_GROUPS.map(renderGroup)}
          {ordersTruncated && onLoadOlderOrders && orders !== null && orders.length > 0 && (
            <p className="orders-truncated" data-testid="orders-load-older">
              <button type="button" className="shell-btn" disabled={olderOrdersLoading || !connected} onClick={onLoadOlderOrders}>
                {olderOrdersLoading ? t('inbox.loadingOlder') : t('inbox.loadOlder')}
              </button>
            </p>
          )}
        </aside>

        <section className="inbox-detail" data-testid="inbox-detail" data-order={currentId || undefined}>
          {!current ? (
            <p className="inbox-detail__empty">{orders === null ? t('workflows.loading') : t('inbox.pickOne')}</p>
          ) : (
            <OrderDetail
              order={current} zh={zh} t={t} connected={connected} panel={panel} openPanel={openPanel} logExpanded={logExpanded} onExpandLog={() => setLogExpanded(true)}
              flow={flowOf(current.workflow_id)} flowName={flowName(current.workflow_id)} flowOf={flowOf} nameOf={nameOf} orders={rows} onOpenOrder={openOrder}
              projectName={projects.find(project => project.id === activeProjectId)?.name || activeProjectId} onOpenProject={onOpenProject}
              blockers={blockers} tracked={tracked} receiptOf={receiptOf} reworkInspectionSlot={reworkInspectionSlot}
              onReviewOrder={onReviewOrder} onOpenTask={onOpenTask} openRun={openRun} openAcceptance={openAcceptance} openGate={openGate}
              onAcceptanceGet={onAcceptanceGet} acceptanceBusy={acceptanceBusyOrderId === current.order_id}
              onAcceptanceEvidence={onAcceptanceEvidence} onAcceptanceSubmit={onAcceptanceSubmit} onAcceptanceDecide={onAcceptanceDecide} onAcceptanceRework={onAcceptanceRework}
              onAcceptanceRoundExport={onAcceptanceRoundExport} onPortfolioExport={onPortfolioExport} onAuthorizeRedistribution={onAuthorizeRedistribution} onOpenArtifact={onOpenArtifact}
              onDecideBlocker={onDecideBlocker} onHoldBlocker={onHoldBlocker} onReleaseBlocker={onReleaseBlocker} answering={answering} decisionErrors={decisionErrors}
              gateDetail={gateDetail} gateBusy={gateBusy} gateError={gateError} onOpenGate={onOpenGate} onDecideGate={onDecideGate} onCloseGate={() => { onCloseGate?.(); setPanel(null) }}
              artifactIndex={artifactIndex} artifactsLoading={artifactsLoading} onRefreshArtifacts={onRefreshArtifacts}
              orderTimeline={orderTimeline} orderTimelineError={orderTimelineError} orderModelPlan={orderModelPlan}
              budgets={budgets} pendingReservations={pendingReservations} onResolveBudget={onResolveBudget} onCreateBudget={onCreateBudget} onRaiseBudget={onRaiseBudget} onCloseBudget={onCloseBudget}
              budgetBusyOrderId={budgetBusyOrderId} orderMetrics={orderMetrics} orderUsage={orderUsage} orderMetricsTruncated={orderMetricsTruncated}
              onCancelOrder={onCancelOrder} onUpdateHandling={onUpdateHandling} handlingBusy={updatingHandlingOrderId === current.order_id} employees={employees}
              onOpenFlow={onOpenFlow} onOpenAssistant={onOpenAssistant} onCustomerPreview={onCustomerPreview}
              blueprints={blueprints} onSaveBlueprint={onSaveBlueprint} onApplyBlueprint={onApplyBlueprint}
              onUpdateFdeControlPlane={onUpdateFdeControlPlane} fdeBusy={updatingFdeControlPlaneOrderId === current.order_id} distributionSlot={distributionSlot}
            />
          )}
        </section>
      </div>

      {customerPreviewSlot}
      {artifactsSlot && <div className="workflows-artifacts" aria-label={t('artifacts.title')}>{artifactsSlot}</div>}
    </div>
  )
}

// ── one order ─────────────────────────────────────────────────────────────

type Translate = ReturnType<typeof useI18n>['t']

interface OrderDetailProps {
  order: OrderRow
  reworkInspectionSlot?: OrdersInboxPageProps['reworkInspectionSlot']
  zh: boolean
  t: Translate
  connected: boolean
  panel: MorePanel | null
  openPanel: (panel: MorePanel) => void
  logExpanded: boolean
  onExpandLog: () => void
  flow: WorkflowSummary | null
  flowName: string
  /** The flows the page knows, for naming a linked order and the step that sent it out. */
  flowOf: (workflowId: string) => WorkflowSummary | null
  nameOf: (employeeId: string) => string
  /** Every row the server listed (not the filtered ones): 主工单 / 子工单 are found among them. */
  orders: OrderRow[]
  /** Open a linked order in place. */
  onOpenOrder: (orderId: string) => void
  projectName: string
  onOpenProject?: () => void
  blockers: DashboardBlocker[]
  tracked: Tracked
  receiptOf: (action: string, id: string) => Tracked[string] | undefined
  onReviewOrder: OrdersInboxPageProps['onReviewOrder']
  onOpenTask: OrdersInboxPageProps['onOpenTask']
  openRun: (order: OrderRow) => void
  openAcceptance: (order: OrderRow) => void
  openGate: (blocker: DashboardBlocker) => void
  onAcceptanceGet: OrdersInboxPageProps['onAcceptanceGet']
  acceptanceBusy: boolean
  onAcceptanceEvidence: OrdersInboxPageProps['onAcceptanceEvidence']
  onAcceptanceSubmit: OrdersInboxPageProps['onAcceptanceSubmit']
  onAcceptanceDecide: OrdersInboxPageProps['onAcceptanceDecide']
  onAcceptanceRework: OrdersInboxPageProps['onAcceptanceRework']
  onAcceptanceRoundExport?: OrdersInboxPageProps['onAcceptanceRoundExport']
  onPortfolioExport?: OrdersInboxPageProps['onPortfolioExport']
  onAuthorizeRedistribution?: OrdersInboxPageProps['onAuthorizeRedistribution']
  onOpenArtifact?: OrdersInboxPageProps['onOpenArtifact']
  onDecideBlocker?: OrdersInboxPageProps['onDecideBlocker']
  onHoldBlocker?: OrdersInboxPageProps['onHoldBlocker']
  onReleaseBlocker?: OrdersInboxPageProps['onReleaseBlocker']
  answering?: Answering
  decisionErrors?: Record<string, string>
  gateDetail: GateDetail | null
  gateBusy: boolean
  gateError: string | null
  onOpenGate?: OrdersInboxPageProps['onOpenGate']
  onDecideGate?: OrdersInboxPageProps['onDecideGate']
  onCloseGate: () => void
  artifactIndex: ArtifactIndex | null
  artifactsLoading: boolean
  onRefreshArtifacts?: () => void
  orderTimeline: OrderTimeline | null
  orderTimelineError: string | null
  orderModelPlan: ModelPlan
  budgets?: RunBudget[] | null
  pendingReservations?: OrdersInboxPageProps['pendingReservations']
  onResolveBudget?: OrdersInboxPageProps['onResolveBudget']
  onCreateBudget?: OrdersInboxPageProps['onCreateBudget']
  onRaiseBudget?: OrdersInboxPageProps['onRaiseBudget']
  onCloseBudget?: OrdersInboxPageProps['onCloseBudget']
  budgetBusyOrderId: string | null
  orderMetrics?: OrderMetrics[]
  orderUsage?: OrderUsage[]
  orderMetricsTruncated: boolean
  onCancelOrder?: OrdersInboxPageProps['onCancelOrder']
  onUpdateHandling?: OrdersInboxPageProps['onUpdateHandling']
  handlingBusy: boolean
  employees: OrgEmployee[]
  onOpenFlow?: OrdersInboxPageProps['onOpenFlow']
  onOpenAssistant?: () => void
  onCustomerPreview?: OrdersInboxPageProps['onCustomerPreview']
  blueprints?: SolutionBlueprint[]
  onSaveBlueprint?: OrdersInboxPageProps['onSaveBlueprint']
  onApplyBlueprint?: OrdersInboxPageProps['onApplyBlueprint']
  onUpdateFdeControlPlane?: OrdersInboxPageProps['onUpdateFdeControlPlane']
  fdeBusy: boolean
  distributionSlot?: OrdersInboxPageProps['distributionSlot']
}

function OrderDetail(props: OrderDetailProps) {
  const { order, zh, t, connected, panel, openPanel, flow, flowName, nameOf, projectName, blockers, receiptOf } = props
  const attention = attentionOf(order)
  const step = stepText(order, zh)
  const gate = gateCardOf(order, blockers)
  const card = openCardOf(order, blockers)
  const lesson = lessonCardOf(order, blockers)
  const employee = attention.waiting_employee_id ? nameOf(attention.waiting_employee_id) : ''
  const now: NowView = nowView(order, { step, employee, hasCard: !!card }, zh)
  const timeline = props.orderTimeline && props.orderTimeline.order_id === order.order_id ? props.orderTimeline : null
  const title = [order.customer_name, order.brand_name].map(value => String(value ?? '').trim()).filter(Boolean).join(' · ') || flowName
  const files = filesInScope(props.artifactIndex?.files ?? [], 'order', order.order_id)
  const notes = feedbackNotes(order)
  const cancellable = !!props.onCancelOrder && ['launched', 'producing', 'delivering'].includes(order.status)
  const ended = attention.ended
  const showGate = panel === 'gate' && gate
  // The lesson card's two answers: the server's words (owner_card.more, S1-A) on the coordinator's ids.
  const lessonCard = lesson ? ownerCardOf(lesson, zh) : null
  const lessonAnswer = lessonAnswers(lessonCard, zh)
  const lessonPending = !!lesson && props.answering?.[lesson.checkpoint_id]?.state === 'pending'
  // 主工单 / 子工单, by name (never a number): the old drawer's lineage, now under 「当前位置」.
  const lineage = lineageOf(order, props.orders)
  const nameOfOrder = (row: OrderRow) => rowTitle(row, props.flowOf(row.workflow_id)?.name?.trim() || '', zh)
  const parentStep = lineage.parent?.row && lineage.parent.nodeId
    ? (props.flowOf(lineage.parent.row.workflow_id)?.nodes ?? []).find(node => node.id === lineage.parent?.nodeId)?.name?.trim() || ''
    : ''

  // The one primary button of the 现在 block, and the write behind it on the tracker.
  const primary = (() => {
    switch (now.kind) {
      case 'intake': {
        const said = buttonReceipt(receiptOf('order_review', order.order_id), now.button)
        return { ...said, onPress: () => props.onReviewOrder(order.order_id, 'approve', ''), tone: now.buttonTone }
      }
      case 'review':
        return { label: now.button, disabled: !gate || !props.onOpenGate, note: !gate ? t('inbox.now.gateNotRead') : '', onPress: () => { if (gate) props.openGate(gate) }, tone: now.buttonTone }
      case 'acceptance':
      case 'customer':
        return { label: now.button, disabled: !order.acceptance, note: '', onPress: () => props.openAcceptance(order), tone: now.buttonTone }
      case 'close': {
        const said = buttonReceipt(receiptOf('acceptance_decide', order.order_id), now.button)
        const status = order.acceptance?.status ?? ''
        return {
          ...said, tone: now.buttonTone, disabled: said.disabled || props.acceptanceBusy || !order.acceptance,
          onPress: () => {
            const passedAlready = status === 'accepted' || status === 'accepted_with_exceptions'
            if (!window.confirm(t(passedAlready ? 'orders.acceptance.closeAcceptedConfirm' : 'orders.acceptance.receiveAndCloseConfirm'))) return
            props.onAcceptanceDecide(order.order_id, status === 'accepted_with_exceptions' ? 'accepted_with_exceptions' : 'accepted', '', [], { closeOrder: true })
          },
        }
      }
      case 'failed':
      case 'running':
        return { label: now.button, disabled: false, note: '', onPress: () => props.openRun(order), tone: now.buttonTone }
      default:
        return null
    }
  })()

  const moreItems: Array<{ id: string; label: string; onPress: () => void; active?: boolean }> = []
  // Any order that ran keeps its run readable, ended or not (the old 打开): the ended-order rule retires primary
  // buttons, not a read entry under 更多.
  if (order.launched_task_id) moreItems.push({ id: 'run', label: t('inbox.more.openRun'), onPress: () => props.onOpenTask(order.launched_task_id) })
  if (now.kind === 'employee' && gate && props.onOpenGate) moreItems.push({ id: 'owner-review', label: t('inbox.more.ownerReview'), onPress: () => props.openGate(gate), active: panel === 'gate' })
  // A19 on a gate the owner reviews: its 现在 block opens the materials (no owner card there), so 暂缓提醒一天 /
  // 恢复提醒 sit here; holding decides nothing (policy: moved from 总控's card).
  const gateToHold = now.kind === 'review' && gate?.checkpoint_id ? gate : null
  if (gateToHold && gateToHold.snoozed_until && props.onReleaseBlocker) moreItems.push({ id: 'release', label: t('approvals.release'), onPress: () => props.onReleaseBlocker?.(gateToHold) })
  if (gateToHold && !gateToHold.snoozed_until && props.onHoldBlocker) moreItems.push({ id: 'hold', label: t('approvals.hold'), onPress: () => props.onHoldBlocker?.(gateToHold, holdUntil()) })
  if (order.acceptance) moreItems.push({ id: 'acceptance', label: t('inbox.more.acceptance'), onPress: () => props.openAcceptance(order), active: panel === 'acceptance' })
  // Two entries open the same 经办 card; pressing either while it is open leaves it open (its own 收起 closes it).
  moreItems.push({ id: 'handling', label: t('inbox.more.handling'), onPress: () => { if (panel !== 'handling') openPanel('handling') }, active: panel === 'handling' })
  moreItems.push({ id: 'money', label: t('inbox.more.money'), onPress: () => { if (panel !== 'handling') openPanel('handling') }, active: panel === 'handling' })
  if (order.status === 'pending_review') moreItems.push({ id: 'reject', label: t('inbox.more.reject'), onPress: () => openPanel('reject'), active: panel === 'reject' })
  if (flow && flow.status !== 'retired' && props.onOpenFlow) moreItems.push({ id: 'flow', label: t('inbox.more.flow'), onPress: () => props.onOpenFlow?.(order.workflow_id) })
  if (props.onOpenAssistant) moreItems.push({ id: 'assistant', label: t('inbox.more.assistant'), onPress: () => props.onOpenAssistant?.() })
  if (props.onCustomerPreview) moreItems.push({ id: 'customer-preview', label: t('customerPreview.title'), onPress: () => props.onCustomerPreview?.(order.order_id) })
  if (cancellable) moreItems.push({ id: 'cancel', label: t('inbox.more.cancel'), onPress: () => openPanel('cancel'), active: panel === 'cancel' })
  moreItems.push({ id: 'budget', label: t('inbox.more.budget'), onPress: () => openPanel('budget'), active: panel === 'budget' })
  if (order.acceptance?.artifact_snapshot?.verified) moreItems.push({ id: 'export', label: t('orders.acceptance.exportRound'), onPress: () => props.openAcceptance(order), active: panel === 'acceptance' })
  if (order.fde_control_plane && (props.onSaveBlueprint || props.onApplyBlueprint)) moreItems.push({ id: 'blueprint', label: t('inbox.more.blueprint'), onPress: () => openPanel('blueprint'), active: panel === 'blueprint' })
  if (order.fde_control_plane && props.onUpdateFdeControlPlane) moreItems.push({ id: 'fde', label: t('orders.fde.edit'), onPress: () => openPanel('fde'), active: panel === 'fde' })
  moreItems.push({ id: 'timeline', label: t('inbox.more.timeline'), onPress: () => openPanel('timeline'), active: panel === 'timeline' })
  if (props.distributionSlot) moreItems.push({ id: 'distribution', label: t('inbox.more.distribution'), onPress: () => openPanel('distribution'), active: panel === 'distribution' })

  const intake = intakeSummary(order, flow?.intake_fields)
  const expected = expectedDelivery(order, flow?.nodes)
  const log = latestEntries(timeline?.entries ?? [], props.logExpanded ? Number.MAX_SAFE_INTEGER : 5)
  const version = order.acceptance?.workflow_version || flow?.version || ''
  // How the order came in (企业微信客服 / 安安起草 / 需求表; a console order says nothing), and -- for one from 企业微信客服
  // -- its conversation with the customer, read only, as the owner's timeline carries it (W3b).
  const sourceWord = orderSourceWord(order.source, zh)
  const conversation = conversationOf(timeline, order.order_id)

  return (
    <article className="inbox-order" data-testid="inbox-order" data-order-id={order.order_id} data-now={now.kind} data-tone={now.tone || undefined}>
      <header className="inbox-order__head">
        <small className="inbox-order__id">{order.order_id}{order.customer_name ? ` · ${order.customer_name}` : ''}</small>
        <h1 className="inbox-order__title">{title}</h1>
        <p className="inbox-order__meta">
          {projectName && <span>{projectName}</span>}{projectName && flowName ? ' / ' : ''}{flowName && <span>{flowName}</span>}
          {sourceWord && <span data-testid="order-source" data-source={order.source}>{zh ? ` · 来源：${sourceWord}` : ` · From: ${sourceWord}`}</span>}
          {attention.snoozed && <span className="inbox-word" data-tone="">{t('inbox.held')}</span>}
        </p>
      </header>

      <section className="inbox-now" data-testid="order-now" data-now={now.kind} data-tone={now.tone || undefined}>
        <small className="inbox-now__label">{t('inbox.now')}</small>
        <h2 className="inbox-now__title">{now.title}</h2>
        {now.line && <p className="inbox-now__line">{now.line}</p>}
        {now.kind === 'card' && card && (
          <OwnerCard blocker={card} layout="compact" className="inbox-now__card" connected={connected} attributes={{ 'data-testid': 'order-now-card' }}
            pending={props.answering?.[card.checkpoint_id]?.state === 'pending'} refusal={props.decisionErrors?.[card.checkpoint_id]}
            onDecide={(blocker, optionId) => props.onDecideBlocker?.(blocker, optionId)}
            onReview={props.onOpenGate ? blocker => props.openGate(blocker) : undefined}
            onOpen={blocker => props.onOpenTask(blocker.task_id || order.launched_task_id)}
            onHold={props.onHoldBlocker && card.checkpoint_id ? blocker => props.onHoldBlocker?.(blocker, holdUntil()) : undefined}
            onRelease={props.onReleaseBlocker && card.checkpoint_id ? props.onReleaseBlocker : undefined} />
        )}
        {now.kind === 'card' && !card && (
          <p className="inbox-now__line" data-testid="order-now-card-pending">{t('inbox.now.gateNotRead')}</p>
        )}
        {now.kind === 'no_receipt' && (
          <OrderBudgetPanel order={order} connected={connected} budgets={props.budgets} pendingReservations={props.pendingReservations} onResolveBudget={props.onResolveBudget}
            budgetBusyOrderId={props.budgetBusyOrderId} />
        )}
        {primary && (
          <div className="inbox-now__foot">
            <button type="button" className={`shell-btn${primary.tone === 'primary' ? ' shell-btn--primary' : ''} inbox-now__primary`} data-testid="order-now-primary" data-action={now.kind}
              disabled={!connected || primary.disabled} onClick={primary.onPress}>
              {primary.label}
            </button>
            {now.consequence && <small className="inbox-consequence" data-testid="order-now-consequence">{now.consequence}</small>}
            {primary.note && <span className="inbox-receipt-note" role="status" data-testid="order-now-receipt">{primary.note}</span>}
          </div>
        )}
        {now.kind === 'ended' && attention.ended_as === 'closed' && (
          <details className="inbox-lesson" data-testid="order-lesson" data-open-card={lesson ? 'true' : 'false'}>
            <summary>{t('inbox.lesson.title')}</summary>
            {lesson ? (
              <>
                <p>{lessonCard?.line || t('inbox.lesson.line')}</p>
                <div className="inbox-lesson__actions">
                  {([['learn', lessonAnswer.learn], ['skip', lessonAnswer.skip]] as const).map(([which, action]) => (
                    <button key={action.id} type="button" className={which === 'skip' ? 'workflows-cancel' : 'workflows-invoke'} data-testid={`order-lesson-${which}`} data-option={action.id}
                      disabled={!connected || lessonPending} onClick={() => props.onDecideBlocker?.(lesson, action.id)}>
                      {lessonPending ? t('ownerCard.pending') : action.label}
                    </button>
                  ))}
                </div>
                <small className="inbox-consequence">{lessonCard?.consequence || t('inbox.lesson.consequence')}</small>
                {props.decisionErrors?.[lesson.checkpoint_id] && <p className="approval-center-refusal workflows-error" role="alert">{props.decisionErrors[lesson.checkpoint_id]}</p>}
              </>
            ) : (
              <p>{t('inbox.lesson.answered')}</p>
            )}
          </details>
        )}
        {showGate && (
          gate && props.gateDetail && props.gateDetail.checkpoint_id === gate.checkpoint_id && props.onDecideGate
            ? <GatePanel detail={props.gateDetail} busy={props.gateBusy} connected={connected} error={props.gateError} onDecide={props.onDecideGate} onClose={props.onCloseGate} />
            : <p className="inbox-group__empty" role="status" data-testid="order-gate-loading">{t('inbox.now.gateLoading')}</p>
        )}
        {panel === 'acceptance' && order.acceptance && (
          <>
            {notes.length > 0 && (
              <div className="inbox-notes" data-testid="order-feedback-notes">
                {notes.map(note => (
                  <blockquote key={note.kind} className="order-drawer-quote" data-note={note.kind}>
                    <small>{note.kind === 'intake' ? t('inbox.note.intake') : note.kind === 'decision' ? t('inbox.note.decision', { round: note.round ?? 1 }) : t('inbox.note.rework', { round: note.round ?? 1 })}</small>
                    <p>{note.text}</p>
                  </blockquote>
                ))}
              </div>
            )}
            <OrderAcceptancePanel order={order} connected={connected} busy={props.acceptanceBusy} receipt={receiptOf('acceptance_decide', order.order_id)}
              onAcceptanceEvidence={props.onAcceptanceEvidence} onAcceptanceSubmit={props.onAcceptanceSubmit} onAcceptanceDecide={props.onAcceptanceDecide} onAcceptanceRework={props.onAcceptanceRework}
              onAcceptanceRoundExport={props.onAcceptanceRoundExport} onPortfolioExport={props.onPortfolioExport} onAuthorizeRedistribution={props.onAuthorizeRedistribution} onOpenArtifact={props.onOpenArtifact} />
          </>
        )}
      </section>

      <section className="inbox-context" data-testid="order-context" aria-label={t('inbox.context')}>
        <header>
          {props.onOpenProject
            ? <button type="button" className="inbox-link" onClick={props.onOpenProject}>{projectName || t('app.page.projects')} ↗</button>
            : <span>{projectName}</span>}
          <small>{version ? t('inbox.context.version', { v: version }) : t('inbox.context.noVersion')} · {t('inbox.context.round', { n: order.acceptance?.round_no ?? 1 })}</small>
        </header>
        <p>{t('inbox.context.position')}<strong>{ended ? t('inbox.context.ended') : step}</strong></p>
        <dl>
          <div>
            <dt>{t('inbox.context.intake')}</dt>
            <dd>{intake.length ? intake.map(line => <span key={line.label}><b>{line.label}</b> {line.value}</span>) : <em>{t('inbox.context.noIntake')}</em>}</dd>
          </div>
          <div>
            <dt>{t('inbox.context.expected')}</dt>
            <dd>{expected.length ? expected.join('、') : <em>{t('inbox.context.noExpected')}</em>}</dd>
          </div>
          {props.orderModelPlan && (
            <div>
              <dt>{t('inbox.context.modelPlan')}</dt>
              <dd>{planSummary(props.orderModelPlan, zh).join('；')}</dd>
            </div>
          )}
        </dl>
        {lineage.parent && (
          <p className="inbox-lineage" data-testid="order-parent" data-order-parent={lineage.parent.orderId}>
            <span>↑ {t('inbox.context.parent')}</span>
            {lineage.parent.row
              ? <button type="button" className="inbox-link" data-testid="order-parent-open" onClick={() => props.onOpenOrder(lineage.parent!.orderId)}>{nameOfOrder(lineage.parent.row)}</button>
              : <em>{t('inbox.context.parentUnknown')}</em>}
            {parentStep && <small>{t('inbox.context.parentStep', { step: parentStep })}</small>}
            {lineage.parent.assets.length > 0 && (
              <small>{t('inbox.context.parentAssets', { n: lineage.parent.assets.length, list: `${lineage.parent.assets.slice(0, 3).join('、')}${lineage.parent.assets.length > 3 ? ' …' : ''}` })}</small>
            )}
          </p>
        )}
        {lineage.children.length > 0 && (
          <div className="inbox-lineage" data-testid="order-children">
            <span>↓ {t('inbox.context.children', { n: lineage.children.length })}</span>
            <ul>
              {lineage.children.map(child => {
                const view = rowView(child, props.flowOf(child.workflow_id)?.name?.trim() || '', zh, nameOf)
                return (
                  <li key={child.order_id} data-testid="order-child" data-order-child={child.order_id}>
                    <button type="button" className="inbox-link" onClick={() => props.onOpenOrder(child.order_id)}>{view.title}</button>
                    <small>{[view.sub, view.word].filter(Boolean).join(' · ')}</small>
                  </li>
                )
              })}
            </ul>
          </div>
        )}
        {lineage.unlistedChildren > 0 && <p className="inbox-lineage" data-testid="order-children-unlisted"><small>{t('inbox.context.childrenUnlisted', { n: lineage.unlistedChildren })}</small></p>}
        {props.onOpenProject && <button type="button" className="inbox-link" data-testid="order-back-to-project" onClick={props.onOpenProject}>{t('inbox.context.back')}</button>}
      </section>

      <section className="inbox-block" data-testid="order-files" aria-label={t('artifacts.title')}>
        <h3>{t('artifacts.title')}<small>{t('inbox.files.source')}</small></h3>
        {props.artifactIndex === null ? (
          <p className="inbox-group__empty">
            {props.artifactsLoading ? t('artifacts.loading') : t('inbox.files.unread')}
            {props.onRefreshArtifacts && !props.artifactsLoading && <button type="button" className="inbox-link" disabled={!connected} onClick={props.onRefreshArtifacts}>{t('artifacts.refresh')}</button>}
          </p>
        ) : files.length === 0 ? (
          <p className="inbox-group__empty">{t('inbox.files.none')}</p>
        ) : (
          <ul className="inbox-files">
            {files.map(file => (
              <li key={file.path} data-file-kind={file.kind}>
                <button type="button" className="inbox-file" disabled={!connected || !props.onOpenArtifact} onClick={() => props.onOpenArtifact?.(file.path)} title={file.path}>
                  <strong>{file.name}</strong>
                  <small>{file.path.slice(0, Math.max(0, file.path.lastIndexOf('/')))}</small>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="inbox-block" data-testid="order-log" aria-label={t('inbox.log')}>
        <h3>{t('inbox.log')}</h3>
        {props.orderTimelineError ? (
          <p className="inbox-group__empty" role="alert">{t(pageErrorMessageKey(props.orderTimelineError) as Parameters<typeof t>[0])}</p>
        ) : !timeline ? (
          <p className="inbox-group__empty" role="status">{t('orders.timelineLoading')}</p>
        ) : log.shown.length === 0 ? (
          <p className="inbox-group__empty">{t('orders.timelineEmpty')}</p>
        ) : (
          <ol className="inbox-log">
            {log.shown.map((entry, index) => (
              <li key={`${entry.at}:${entry.source}:${index}`} data-source={entry.source}>
                <strong>{entry.actor || t('inbox.log.system')}</strong>
                <span> · {timelineEventName(entry.kind, zh)}{entry.detail ? `：${orderEventDetail(entry.detail, zh)}` : ''}</span>
                <time dateTime={entry.at} title={timelineEventTime(entry.at, true)}> · {timelineEventTime(entry.at, true)}</time>
              </li>
            ))}
          </ol>
        )}
        {log.rest > 0 && <button type="button" className="inbox-link" data-testid="order-log-more" onClick={props.onExpandLog}>{t('inbox.log.more', { n: log.rest })}</button>}
      </section>

      {props.reworkInspectionSlot?.(order)}

      {conversation && <WecomConversationBlock conversation={conversation} />}

      <details className="inbox-more" data-testid="order-more">
        <summary>{t('ownerCard.more')}</summary>
        <div className="inbox-more__grid">
          {moreItems.map(item => (
            <button key={item.id} type="button" className={`inbox-link${item.active ? ' is-active' : ''}`} data-more={item.id} disabled={!connected} onClick={item.onPress}>{item.label}</button>
          ))}
        </div>
      </details>

      {panel === 'handling' && (
        <OrderHandlingCard zh={zh} handling={order.handling} ended={ended} employees={props.employees} onSave={props.onUpdateHandling ? patch => props.onUpdateHandling?.(order.order_id, patch) : undefined} busy={props.handlingBusy} />
      )}
      {panel === 'reject' && order.status === 'pending_review' && (
        <OrderRejectForm order={order} connected={connected} receipt={receiptOf('order_review', order.order_id)} onReviewOrder={props.onReviewOrder} />
      )}
      {panel === 'cancel' && cancellable && props.onCancelOrder && (
        <OrderCancelForm order={order} connected={connected} receipt={receiptOf('order_cancel', order.order_id)} onCancelOrder={props.onCancelOrder} />
      )}
      {panel === 'budget' && (
        <OrderBudgetPanel order={order} connected={connected} budgets={props.budgets} pendingReservations={props.pendingReservations} onResolveBudget={props.onResolveBudget}
          onCreateBudget={props.onCreateBudget} onRaiseBudget={props.onRaiseBudget} onCloseBudget={props.onCloseBudget} budgetBusyOrderId={props.budgetBusyOrderId}
          orderMetrics={props.orderMetrics} orderUsage={props.orderUsage} orderMetricsTruncated={props.orderMetricsTruncated} />
      )}
      {panel === 'blueprint' && order.fde_control_plane && (
        <OrderBlueprintBar order={order} connected={connected} blueprints={props.blueprints} onSaveBlueprint={props.onSaveBlueprint} onApplyBlueprint={props.onApplyBlueprint} />
      )}
      {panel === 'fde' && order.fde_control_plane && props.onUpdateFdeControlPlane && (
        <OrderFdeEditor key={order.order_id} order={order} connected={connected} busy={props.fdeBusy} onUpdateFdeControlPlane={props.onUpdateFdeControlPlane} onClose={() => openPanel('fde')} />
      )}
      {panel === 'timeline' && <OrderTimelinePanel order={order} timeline={timeline} error={props.orderTimelineError} />}
      {panel === 'distribution' && props.distributionSlot && <Fragment>{props.distributionSlot(order)}</Fragment>}
    </article>
  )
}
