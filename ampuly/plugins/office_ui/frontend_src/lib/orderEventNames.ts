// What an order's business events are called, in the words a person running the business uses. The record itself
// keeps its own vocabulary (`created`, `acceptance.criteria_defined`, a status name for a status change); the drawer
// showed those verbatim. Pure: a name that is not known here is shown as it is rather than hidden or guessed, and the
// original is kept as the element's title so nothing is lost.
//
// The list follows what `business_store.py` writes to `order_events` / `gate_events` (2026-09-20). The runtime and
// effect halves of the timeline are already labelled by the server (`order_timeline.py`) in their `detail`.

const NAMES: Record<string, readonly [zh: string, en: string]> = {
  // an order's own life: `created`, then each status it moves to -- the same words the orders table uses for the
  // status (`orders.status.*`), so one thing has one name on the page
  created: ['工单已创建', 'Order created'],
  collecting: ['收集中', 'Collecting what is needed'],
  pending_review: ['待审单', 'Waiting for review'],
  launching: ['发起中', 'Starting the run'],
  'model_plan.pinned': ['模型设置已锁定', 'Model settings locked in'],
  'employee_run.pinned': ['本单员工版本已锁定', 'Employee versions locked for this order'],
  launched: ['已发起', 'Run started'],
  producing: ['生产中', 'In production'],
  delivering: ['交付中', 'Delivering'],
  closed: ['已结案', 'Closed'],
  rejected: ['已打回', 'Rejected'],
  // acceptance
  'acceptance.criteria_defined': ['验收标准已冻结', 'Acceptance criteria set'],
  'acceptance.evidence_added': ['已补充验收证据', 'Acceptance evidence added'],
  'acceptance.submitted': ['已提交验收', 'Submitted for acceptance'],
  'acceptance.accepted': ['已验收', 'Accepted'],
  'acceptance.accepted_with_exceptions': ['带例外验收', 'Accepted with exceptions'],
  'acceptance.rejected': ['验收未通过', 'Not accepted'],
  'acceptance.rework_started': ['已开始返工', 'Rework started'],
  'acceptance.rework_runtime_started': ['返工运行已发起', 'Rework run started'],
  // planning and money
  'solution_blueprint.saved': ['方案蓝图已保存', 'Blueprint saved'],
  'solution_blueprint.applied': ['方案蓝图已采用', 'Blueprint applied'],
  'run_budget.granted': ['运行预算已发放', 'Run budget granted'],
  'run_budget.raised': ['运行预算已上调', 'Run budget raised'],
  'run_budget.ceilings_set': ['运行预算上限已设置', 'Run budget ceilings set'],
  'run_budget.lifted': ['运行预算上限已取消（仍记账）', 'Run budget ceilings lifted (still recorded)'],
  'handling.updated': ['经办信息已更新（负责人 / 截止 / 金额）', 'Handling updated (assignee / due / amounts)'],
  'fde_control_plane.updated': ['交付控制设置已更新', 'Delivery controls updated'],
  // 4.7-A08 批量自动化: the one authorization an order sends its list out under
  'distribution_authorization.drafted': ['分发授权单已起草', 'Distribution authorization drafted'],
  'distribution_authorization.approved': ['分发授权单已批准', 'Distribution authorization approved'],
  'distribution_authorization.revoked': ['分发授权单已撤销', 'Distribution authorization revoked'],
  // a decision card sent to a person
  sent: ['决策卡已发出', 'Decision card sent'],
  read: ['决策卡已读', 'Decision card read'],
  viewed: ['决策卡已查看', 'Decision card viewed'],
  file_sent: ['文件已发出', 'File sent'],
  delivered_file: ['文件已送达', 'File delivered'],
  unauthorized_attempt: ['无权限者尝试决策（已拒绝）', 'Unauthorised attempt to decide (refused)'],
  // a browser step's decision (order_timeline.py, source 'browser'): the detail says who suggested, checked, took over
  'browser.decision': ['浏览器步骤的决定', 'Browser step decision'],
}

/** The name to show for an event kind; the kind itself when it is not one of ours. */
export function orderEventName(kind: string, zh: boolean): string {
  const known = NAMES[kind]
  return known ? known[zh ? 0 : 1] : kind
}

/** 4.7-AR: the name the orders table's timeline row shows. The same list as the drawer; a kind it does not know is
 *  said as a plain 「系统记录」 rather than its internal name (the runtime and effect halves carry their own words in
 *  the detail, from the server). */
export function timelineEventName(kind: string, zh: boolean): string {
  const known = NAMES[kind]
  return known ? known[zh ? 0 : 1] : (zh ? '系统记录' : 'System record')
}

const two = (value: number) => String(value).padStart(2, '0')

/** 4.7-AR: when a timeline entry happened, on the owner's own clock: `HH:MM:SS`, or with `full` the date as well. The
 *  server says every moment with its offset (order_timeline.py); a stamp that names no moment is sliced as before. */
export function timelineEventTime(at: string, full = false): string {
  const text = String(at ?? '')
  // More than milliseconds (Python writes microseconds) is not part of the standard date format: keep three digits.
  const moment = new Date(Date.parse(text.replace(/(\.\d{3})\d+/, '$1')))
  if (!text || Number.isNaN(moment.getTime())) return full ? text.replace('T', ' ').slice(0, 19) : (text.slice(11, 19) || text.slice(0, 10))
  const time = `${two(moment.getHours())}:${two(moment.getMinutes())}:${two(moment.getSeconds())}`
  return full ? `${moment.getFullYear()}-${two(moment.getMonth() + 1)}-${two(moment.getDate())} ${time}` : time
}

/** An event's detail line. A status change carries the bare status as its detail; say it in words. Anything else --
 *  a sentence, a server-side label, a note -- is left exactly as it is. */
export function orderEventDetail(detail: string, zh: boolean): string {
  const bare = detail.trim()
  return NAMES[bare] ? NAMES[bare][zh ? 0 : 1] : detail
}

export const KNOWN_ORDER_EVENTS: readonly string[] = Object.keys(NAMES)
