// The order drawer (frontend refactor U3): one order read as a piece of business -- where it is, whose turn it is,
// what happened, what was delivered, what was said -- beside the list instead of spread over expanding table rows.
// It shows what the page already holds and asks for nothing but the order's timeline. The heavy actions (reviewing
// the intake, deciding an acceptance, budgets, cancelling) stay where they are, tested, in the order's row: the
// drawer takes the person there; it does not grow a second copy of them.

import { useEffect, useState, type ReactNode } from 'react'
import { planSummary, type ModelPlan } from '../workspace/modelTiers'

import { DRAWER_TABS, DRAWER_TAB_LABELS, entriesFor, evidenceFiles, feedbackNotes, orderStep, recordedFiles, type DrawerTab, type FileKind } from '../lib/orderDrawer'
import type { Waiting } from '../lib/orderWaiting'
import type { OrderTimeline, WorkOrderSummary } from './WorkflowsPage'
import { orderEventDetail, orderEventName } from '../lib/orderEventNames'
import { OrderHandlingCard } from './OrderHandlingCard'

export const STEP_TEXT: Record<string, readonly [string, string, string, string]> = {
  // [zh title, zh hint, en title, en hint]
  intake_review: ['等接单审核', '审核通过会启动这张工单的运行，可能产生模型费用。', 'Waiting for intake review', 'Approving launches the run and may spend on models.'],
  acceptance_decision: ['等验收决定', '交付证据已提交：先看交付物，再决定通过或打回。', 'Waiting for the acceptance decision', 'Evidence was submitted: look at it, then accept or send it back.'],
  rework: ['验收已打回，返工中', '打回意见见「沟通与反馈」；返工后会开新一轮送审。', 'Sent back; rework under way', 'See Feedback for what was said; a new round follows the rework.'],
  collecting: ['收集需求中', '需求还没收齐，收齐后进入接单审核。', 'Collecting the brief', 'Once complete it goes to intake review.'],
  running: ['执行中', '运行里需要人决定的事项在顶栏「待审批」里。', 'Running', 'Decisions the run needs from a person are under Approvals in the top bar.'],
  accepted: ['已验收，待结案', '客户已验收这一轮交付；结案前交付与证据仍可查看。', 'Accepted; not closed yet', 'This round was accepted; the deliverables and evidence stay readable until it closes.'],
  closed: ['已结案', '这张工单已经结束；交付与记录保留。', 'Closed', 'The order is over; its deliverables and records are kept.'],
  rejected: ['已打回', '接单审核没有通过；意见见「沟通与反馈」。', 'Rejected', 'The intake review refused it; see Feedback.'],
}

const KIND_TEXT = { image: ['图片', 'Image'], text: ['文档', 'Text'], other: ['文件', 'File'] } as const

/** One file of the order: what it is, its name, where it sits, and -- with a way to open it -- a preview. */
function FileRow({ zh, path, kind, note, trailing, openPath, onOpenFile }: {
  zh: boolean; path: string; kind: FileKind; note?: string; trailing?: string; openPath: string; onOpenFile?: (path: string) => void
}) {
  const name = path.slice(path.lastIndexOf('/') + 1)
  const folder = path.slice(0, Math.max(0, path.lastIndexOf('/')))
  const body = (
    <>
      <span className="order-drawer-file-kind" data-kind={kind} aria-hidden="true">{KIND_TEXT[kind][zh ? 0 : 1]}</span>
      <span className="order-drawer-file-name"><strong>{name}</strong>{(note || folder) && <small>{note || folder}</small>}</span>
      {trailing && <small>{trailing}</small>}
    </>
  )
  return (
    <li data-file-kind={kind}>
      {onOpenFile && openPath
        ? <button type="button" className="order-drawer-file" onClick={() => onOpenFile(openPath)} title={zh ? '预览' : 'Preview'}>{body}</button>
        : <div className="order-drawer-file">{body}</div>}
    </li>
  )
}

export function OrderDrawer({ zh, order, flowName, waiting = null, parent = null, children = [], onOpenOrder, statusLabel, acceptanceLabel = '', employees = [], handlingBusy = false, onUpdateHandling, queueLabel, timeline, timelineError, modelPlan = null, onClose, onOpenTask, onGoToRow, onOpenAcceptance, onOpenFile, extra = null }: {
  zh: boolean
  /** A card the page adds under 经办 for this order (4.7-A08: the owner's 分发授权单). Absent = nothing. */
  extra?: ReactNode
  /** What settings this order was launched under, read from its own event. null = it has none. */
  modelPlan?: ModelPlan
  order: WorkOrderSummary
  /** 经办: the roster to pick an assignee from, and the way to save a change (owner's console only). */
  employees?: ReadonlyArray<{ employee_id: string; name: string }>
  handlingBusy?: boolean
  onUpdateHandling?: (patch: Record<string, unknown>) => void
  /** Where the acceptance stands, in the words the order's row uses; the bare status is shown when absent. */
  acceptanceLabel?: string
  /** The flow's name, when the page knows it; its id is shown either way. */
  flowName?: string
  /** The pause this order is stopped at, when it is stopped at one. Read only: answering it is the approval centre's. */
  waiting?: Waiting | null
  /** The order that sent this one out, when this one is a child; `known` is false when it is not on this page. */
  parent?: { orderId: string; nodeId: string; assets: string[]; label: string; known: boolean } | null
  /** The orders this one sent out. */
  children?: Array<{ orderId: string; label: string; status: string }>
  /** Read another order of this project in the same drawer. */
  onOpenOrder?: (orderId: string) => void
  statusLabel: string
  queueLabel: string
  /** This order's timeline once it has arrived; null while it has not. */
  timeline: OrderTimeline | null
  timelineError?: string | null
  onClose: () => void
  onOpenTask?: (taskId: string) => void
  /** Close the drawer and bring the order's row into view: the review, budget and cancel controls live there. */
  onGoToRow: () => void
  onOpenAcceptance?: () => void
  /** Preview one recorded file (the console's bounded, audited read). Absent: the files are listed, not opened. */
  onOpenFile?: (path: string) => void
}) {
  const [tab, setTab] = useState<DrawerTab>('progress')
  useEffect(() => { setTab('progress') }, [order.order_id])
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const step = orderStep(order)
  const text = STEP_TEXT[step.key] ?? STEP_TEXT.running
  const notes = feedbackNotes(order)
  const files = recordedFiles(order)
  const evidence = order.acceptance?.evidence ?? []
  const proof = evidenceFiles(order)
  const entries = tab === 'progress' || tab === 'runs' ? entriesFor(tab, timeline?.entries ?? []) : []
  const title = [order.customer_name, order.brand_name].filter(Boolean).join(' · ') || order.order_id

  return (
    <aside className="order-drawer" role="dialog" aria-label={zh ? `工单 ${order.order_id}` : `Order ${order.order_id}`} data-order-drawer={order.order_id}>
      <header className="order-drawer-head">
        <div>
          <small>{order.order_id}</small>
          <h2 title={title}>{title}</h2>
        </div>
        <button type="button" className="order-drawer-close" onClick={onClose} aria-label={zh ? '关闭' : 'Close'}>×</button>
      </header>
      <div className="order-drawer-scroll">
        <section className="order-drawer-run">
          <strong>{zh ? '运行与业务分开看' : 'The run and the business, apart'}</strong>
          <p>{zh ? '业务流' : 'Flow'} {flowName ? `${flowName}（${order.workflow_id}）` : order.workflow_id}{order.acceptance?.workflow_version ? ` · v${order.acceptance.workflow_version}` : ''}
            {order.acceptance?.round_no ? (zh ? ` · 第 ${order.acceptance.round_no} 轮验收` : ` · acceptance round ${order.acceptance.round_no}`) : ''}
            {(order.running_child_orders ?? 0) > 0 ? (zh ? ` · ${order.running_child_orders} 张子单在运行` : ` · ${order.running_child_orders} child orders running`) : ''}</p>
          {/* 模型方案: what this order was launched under. An order placed before phase 1 has none, and says so
              rather than being shown today's settings as though they were its own. */}
          <div className="order-drawer-model-plan" data-testid="order-model-plan">
            <strong>{zh ? '模型方案' : 'Model plan'}</strong>
            {modelPlan
              ? <ul>{planSummary(modelPlan, zh).map(line => <li key={line}>{line}</li>)}</ul>
              : <p><small>{zh ? '这张单是第 1 期之前发起的，没有锁定方案。'
                               : 'Placed before model plans existed; nothing was locked in.'}</small></p>}
          </div>
          {/* Where this order sits among the others: who sent it out, and what it sent out. Reading only. */}
          {parent && (
            <p className="order-drawer-lineage" data-testid="order-parent">
              <span>↑ {zh ? '主工单' : 'Parent order'}</span>
              {parent.known && onOpenOrder
                ? <button type="button" onClick={() => onOpenOrder(parent.orderId)}>{parent.label}</button>
                : <em title={zh ? '不在当前筛选的工单里' : 'Not among the orders shown'}>{parent.label}</em>}
              {parent.nodeId && <small>{zh ? `由步骤 ${parent.nodeId} 发起` : `from step ${parent.nodeId}`}</small>}
              {parent.assets.length > 0 && <small>{zh ? `带着 ${parent.assets.length} 项上游材料：${parent.assets.slice(0, 3).join('、')}${parent.assets.length > 3 ? ' …' : ''}` : `${parent.assets.length} upstream assets: ${parent.assets.slice(0, 3).join(', ')}${parent.assets.length > 3 ? ' …' : ''}`}</small>}
            </p>
          )}
          {children.length > 0 && (
            <div className="order-drawer-lineage" data-testid="order-children">
              <span>↓ {zh ? `子工单 ${children.length}` : `${children.length} child orders`}</span>
              <ul>
                {children.map(item => (
                  <li key={item.orderId} data-testid="order-child">
                    {onOpenOrder
                      ? <button type="button" onClick={() => onOpenOrder(item.orderId)}>{item.label}</button>
                      : <em>{item.label}</em>}
                    <small>{item.status}</small>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div className="order-drawer-actions">
            {order.launched_task_id && onOpenTask && (
              <button type="button" className="shell-btn" onClick={() => onOpenTask(order.launched_task_id)}>{zh ? '打开执行工作区' : 'Open the execution workspace'}</button>
            )}
            <button type="button" className="shell-btn" onClick={onGoToRow}>{zh ? '在工单表里操作（预算 / 取消 / 审单）' : 'Act in the table (budget / cancel / review)'}</button>
          </div>
        </section>

        <OrderHandlingCard zh={zh} handling={order.handling} ended={step.key === 'closed' || step.key === 'rejected'}
          employees={employees} onSave={onUpdateHandling} busy={handlingBusy} />
        {extra}

        <div className="order-drawer-tags">
          <span className={`shell-tag${step.person ? ' shell-tag--warn' : ''}`}>{queueLabel}</span>
          <span className="shell-tag">{statusLabel}</span>
          {order.acceptance?.status && <span className="shell-tag" title={order.acceptance.status}>{zh ? '验收' : 'acceptance'}: {acceptanceLabel || order.acceptance.status}</span>}
          <span className="shell-tag">{order.events_chain_verified === true ? (zh ? '审计链已校验' : 'audit chain verified') : order.events_chain_verified === false ? (zh ? '审计链异常' : 'audit chain broken') : (zh ? '审计链未知' : 'audit chain unknown')}</span>
        </div>

        <section className={`order-drawer-step${step.person || waiting ? ' needs-person' : ''}`}>
          <small>{zh ? '当前环节' : 'Where it is'}</small>
          <h3>{waiting ? (zh ? `等人：${waiting.step || '运行里的一处审批'}` : `Waiting: ${waiting.step || 'an approval in the run'}`) : text[zh ? 0 : 2]}</h3>
          <p>{waiting
            ? (zh
              ? `${waiting.who ? `轮到 ${waiting.who}。` : '这一处还没有指定签字人。'}${waiting.since ? `自 ${waiting.since.replace('T', ' ').slice(0, 16)} 起等待。` : ''}${waiting.count > 1 ? `这张工单共有 ${waiting.count} 处在等。` : ''}在顶栏「待审批」里答复。`
              : `${waiting.who ? `${waiting.who}'s turn. ` : 'No signer is named for it. '}${waiting.since ? `Waiting since ${waiting.since.replace('T', ' ').slice(0, 16)}. ` : ''}${waiting.count > 1 ? `${waiting.count} pauses on this order. ` : ''}It is answered under Approvals in the top bar.`)
            : text[zh ? 1 : 3]}</p>
          <div className="order-drawer-actions">
            {step.action === 'review' && <button type="button" className="shell-btn shell-btn--primary" onClick={onGoToRow}>{zh ? '去审单' : 'Review the intake'}</button>}
            {step.action === 'acceptance' && onOpenAcceptance && <button type="button" className="shell-btn shell-btn--primary" onClick={onOpenAcceptance}>{zh ? '打开验收' : 'Open the acceptance'}</button>}
            {step.action === 'task' && onOpenTask && <button type="button" className="shell-btn" onClick={() => onOpenTask(order.launched_task_id)}>{zh ? '查看运行' : 'See the run'}</button>}
            <button type="button" className="shell-btn" onClick={() => setTab('deliverables')}>{zh ? '查看交付物' : 'Deliverables'}</button>
          </div>
        </section>

        <div className="order-drawer-tabs" role="group" aria-label={zh ? '工单分页' : 'Order pages'}>
          {DRAWER_TABS.map(item => (
            <button key={item} type="button" data-drawer-tab={item} aria-pressed={tab === item} className={tab === item ? 'is-active' : ''} onClick={() => setTab(item)}>
              {DRAWER_TAB_LABELS[item][zh ? 0 : 1]}
            </button>
          ))}
        </div>

        {(tab === 'progress' || tab === 'runs') && (
          <section className="order-drawer-page" data-page={tab}>
            {timelineError ? <p role="alert" className="order-drawer-empty">{timelineError}</p>
              : !timeline ? <p className="order-drawer-empty" role="status">{zh ? '正在读取时间线…' : 'Reading the timeline…'}</p>
              : entries.length === 0 ? <p className="order-drawer-empty">{tab === 'runs' ? (zh ? '还没有运行记录。' : 'No run records yet.') : (zh ? '还没有业务事件。' : 'No business events yet.')}</p>
              : (
                <ol className="order-drawer-timeline">
                  {entries.map((entry, index) => (
                    <li key={`${entry.at}:${index}`} data-source={entry.source}>
                      <span className="order-drawer-when">{entry.at.replace('T', ' ').slice(0, 16)}</span>
                      <strong title={entry.kind}>{orderEventName(entry.kind, zh)}</strong>
                      {entry.actor && <em>{entry.actor}</em>}
                      {entry.detail && <p>{orderEventDetail(entry.detail, zh)}</p>}
                    </li>
                  ))}
                </ol>
              )}
            {timeline && tab === 'runs' && timeline.integrity?.summary && <p className="order-drawer-note">{timeline.integrity.summary}</p>}
            {tab === 'progress' && (
              // Where this order's data lives. Facts only: the flow and the order it belongs to, and the directory
              // the run writes into -- the same `orders/<id>/` the project's 资料 tab groups files by (lib/artifactScope).
              <div className="order-drawer-home" data-testid="order-home">
                <strong>{zh ? '数据归属与交付目录' : 'Where this order’s data lives'}</strong>
                <p>{flowName || order.workflow_id} / {order.order_id}</p>
                <p><code>orders/{order.order_id}/</code> {zh
                  ? '这张单的交付物放在项目工作区的这个目录下；项目中心「资料」页签按同一目录归组。'
                  : 'This order’s deliverables live here, inside the project workspace; the project centre’s Files tab groups by the same directory.'}</p>
              </div>
            )}
          </section>
        )}

        {tab === 'deliverables' && (
          <section className="order-drawer-page" data-page="deliverables">
            {files.state === 'none'
              ? <p className="order-drawer-empty">{zh ? '还没有验收轮次记录下交付文件。运行中的产物在「打开执行工作区」里看。' : 'No acceptance round has recorded files yet. A running order’s output is in its task session.'}</p>
              : (
                <>
                  <p className="order-drawer-note">{files.state === 'verified'
                    ? (zh ? `第 ${order.acceptance?.round_no ?? 1} 轮送审的固定版本（已校验）：` : `The fixed version submitted in round ${order.acceptance?.round_no ?? 1} (verified):`)
                    : (zh ? `第 ${order.acceptance?.round_no ?? 1} 轮送审记录的文件（未通过校验或未校验）：` : `Files recorded for round ${order.acceptance?.round_no ?? 1} (not verified):`)}</p>
                  <ul className="order-drawer-files">
                    {files.files.map(file => (
                      // What opens is the copy fixed at submission, never the working file.
                      <FileRow key={file.path} zh={zh} path={file.path} kind={file.kind} trailing={`${Math.max(1, Math.round(file.size / 1024))} KB`} openPath={file.openPath} onOpenFile={onOpenFile} />
                    ))}
                  </ul>
                </>
              )}
            {evidence.length > 0 && <p className="order-drawer-note">{zh ? `验收证据 ${evidence.length} 条；逐条核对在「打开验收」里。` : `${evidence.length} evidence items; they are checked one by one in the acceptance.`}</p>}
            {proof.length > 0 && (
              <ul className="order-drawer-files" data-files="evidence">
                {proof.map(file => <FileRow key={file.path} zh={zh} path={file.path} kind={file.kind} note={file.criterion} openPath={file.path} onOpenFile={onOpenFile} />)}
              </ul>
            )}
            {order.acceptance && onOpenAcceptance && <button type="button" className="shell-btn" onClick={onOpenAcceptance}>{zh ? '打开验收' : 'Open the acceptance'}</button>}
          </section>
        )}

        {tab === 'feedback' && (
          <section className="order-drawer-page" data-page="feedback">
            {notes.length === 0
              ? <p className="order-drawer-empty">{zh ? '这张工单上还没有人留下意见。' : 'Nobody has left a note on this order yet.'}</p>
              : notes.map(note => (
                <blockquote key={note.kind} className="order-drawer-quote" data-note={note.kind}>
                  <small>{note.kind === 'intake' ? (zh ? '接单审核意见' : 'Intake review') : note.kind === 'decision' ? (zh ? `验收决定（第 ${note.round ?? 1} 轮）` : `Acceptance decision (round ${note.round ?? 1})`) : (zh ? `打回意见（第 ${note.round ?? 1} 轮）` : `Rework feedback (round ${note.round ?? 1})`)}</small>
                  <p>{note.text}</p>
                </blockquote>
              ))}
            <p className="order-drawer-note">{zh ? '这里只有工单自己带着的意见；谁在什么时候说的，见「进度」。内部备注功能服务端还没有。' : 'Only the notes the order itself carries; who said what and when is under Progress. Internal notes have no server support yet.'}</p>
          </section>
        )}
      </div>
    </aside>
  )
}
