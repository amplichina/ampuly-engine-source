// One owner card for every pause the owner answers (frontend redesign S1-B, policy): a title, one line, ONE
// primary answer with its consequence under it, the other answers and 「打开这张单」 under 「更多」. Pure: a card is read
// from a pause the way every surface reads it (approval centre, 总控, 项目中心, the browser pane), so no surface words
// a pause its own way any more.
//
// The server's own wording wins when it sent one (`owner_card`, S1-A); without it the console words the pause with the
// helpers it already had -- the permission sentence (lib/permissionWording), the order step (STEP_TEXT) and the kind
// (blockerKind). Whatever the source, no text on the card carries a checkpoint id, a task id or a session id: those are
// not things the owner reads (the design's wording rule).

import { STEP_TEXT } from '../components/OrderDrawer'
import { blockerKind, type BlockerKind, type DashboardBlocker, type DashboardBlockerOption } from '../components/DashboardPage'
import { translateFor } from '../i18n'
import { permissionText, type Translate } from './permissionWording'

export type OwnerCardActionKind = 'approve' | 'deny' | 'review' | 'open'

export interface OwnerCardAction {
  id: string
  label: string
  kind: OwnerCardActionKind
}

export interface OwnerCardView {
  title: string
  /** One line under the title: what is asked, in words. */
  line: string
  /** The one thing to press. `review` opens the materials view (a business gate), `open` opens the order. The
   *  console's own wording always names one; the server's card may say there is nothing for the owner to press
   *  (`primary: null` -- an employee's turn, a request that offers no allow), and then there is no primary button. */
  primary: OwnerCardAction | null
  /** The other answers, under 更多. */
  more: OwnerCardAction[]
  /** What pressing the primary does, under the button; '' when there is nothing worth saying. */
  consequence: string
  orderId: string
  orderLabel: string
  /** Whether the decision is taken on this card (its own answers) rather than somewhere the card leads to. */
  decidesHere: boolean
  kind: BlockerKind
  /** A permission request's raw argument, kept folded away under the sentence; '' otherwise. */
  raw: string
  /** Whether the server's wording was used. */
  fromServer: boolean
}

type PauseLike = Pick<DashboardBlocker, 'checkpoint_id' | 'checkpoint_type' | 'task_id' | 'session_id' | 'subject' | 'detail' | 'options'>
  & Partial<Pick<DashboardBlocker, 'kind' | 'gate' | 'permission' | 'waiting_task_id' | 'project_id' | 'order_id' | 'order_title' | 'order_step' | 'owner_card'>>

const text = (value: unknown): string => (typeof value === 'string' ? value.trim() : '')

/** The ids a card must never show, taken out of a sentence wherever they were pasted in. */
export function withoutIds(value: string, ids: readonly string[]): string {
  let out = value
  for (const id of ids) {
    if (!id) continue
    out = out.split(id).join('')
  }
  return out.replace(/[ \t]*[·•:：]\s*$/u, '').replace(/\(\s*\)|（\s*）/g, '').replace(/\s{2,}/g, ' ').trim()
}

function optionAction(option: DashboardBlockerOption, ids: readonly string[] = []): OwnerCardAction {
  return { id: option.id, label: withoutIds(option.label, ids) || option.id, kind: option.kind === 'deny' ? 'deny' : 'approve' }
}

/** The actions a server card offers that are not one of the pause's answers (owner_card.py): the page acts on them
 *  itself. 看材料并处理 and 我来审 open the materials view; 打开这张单, 看交付物并处理 and 续跑 open the order's run,
 *  where those things are. */
const PAGE_ACTION_KINDS: Record<string, OwnerCardActionKind> = {
  open_materials: 'review',
  owner_review: 'review',
  open: 'open',
  open_acceptance: 'open',
  resume: 'open',
}

/** The server's `primary` / `more` entries: an option id (looked up on the pause), or an object with id and label. */
function servedAction(value: unknown, options: DashboardBlockerOption[], ids: readonly string[]): OwnerCardAction | null {
  if (typeof value === 'string') {
    const option = options.find(item => item.id === value.trim())
    return option ? optionAction(option, ids) : null
  }
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  const id = text(record.id) || text(record.option_id)
  const label = withoutIds(text(record.label), ids)
  if (!id || !label) return null
  const said = text(record.kind)
  const own = options.find(item => item.id === id)
  const kind: OwnerCardActionKind = said === 'deny' || said === 'review' || said === 'open' || said === 'approve' ? said
    : PAGE_ACTION_KINDS[id] ?? (own?.kind === 'deny' ? 'deny' : 'approve')
  return { id, label, kind }
}

function servedCard(pause: PauseLike, ids: readonly string[]): Partial<OwnerCardView> | null {
  const served = pause.owner_card
  if (!served || typeof served !== 'object') return null
  const options = pause.options ?? []
  const primary = servedAction(served.primary, options, ids)
  const more = Array.isArray(served.more) ? served.more.map(item => servedAction(item, options, ids)).filter((item): item is OwnerCardAction => !!item) : undefined
  const decides = served.decides_here ?? served.decidesHere
  return {
    title: withoutIds(text(served.title), ids) || undefined,
    line: served.line === undefined ? undefined : withoutIds(text(served.line), ids),
    // `null` is the server saying there is nothing for the owner to press; an entry it could not name is left out.
    primary: served.primary === null ? null : primary ?? undefined,
    more,
    consequence: served.consequence === undefined ? undefined : withoutIds(text(served.consequence), ids),
    orderId: text(served.order_id ?? served.orderId) || undefined,
    orderLabel: withoutIds(text(served.order_label ?? served.orderLabel), ids) || undefined,
    decidesHere: typeof decides === 'boolean' ? decides : undefined,
  }
}

/** The card for a pause: the server's wording where it gave one, the console's own for the rest. */
export function ownerCardOf(pause: PauseLike, zh: boolean, t: Translate = translateFor(zh ? 'zh-CN' : 'en') as Translate): OwnerCardView {
  const kind = blockerKind(pause)
  const ids = [pause.checkpoint_id, pause.task_id, pause.session_id, pause.waiting_task_id ?? ''].map(id => String(id ?? '').trim()).filter(Boolean)
  const options = pause.options ?? []
  const subject = withoutIds(text(pause.subject), ids)
  const detail = withoutIds(text(pause.detail), ids)
  const stopped = pause.checkpoint_type === 'company_runtime_suspended'
  const step = pause.order_step && STEP_TEXT[pause.order_step] ? STEP_TEXT[pause.order_step] : null

  // The console's own reading, by kind.
  let title: string
  let line: string
  let raw = ''
  if (kind === 'gate') {
    title = withoutIds(text(pause.gate?.node_name), ids) || subject || t('approvals.kind.gate')
    line = subject && subject !== title ? subject : detail
  } else if (kind === 'permission') {
    title = withoutIds(permissionText(t, { subject: text(pause.subject), detail: text(pause.detail), summary: pause.permission?.summary }), ids)
    line = t('approvals.permission.lead')
    raw = `${text(pause.subject)} ${text(pause.detail)}`.trim()
  } else if (kind === 'suspended') {
    title = t(stopped ? 'approvals.stoppedTitle' : 'approvals.suspendedTitle')
    line = t(stopped ? 'approvals.stoppedDetail' : 'approvals.suspendedDetail')
  } else {
    // Never the checkpoint type: a pause the server did not word is named by its kind.
    title = subject || t(`approvals.kind.${kind}` as Parameters<Translate>[0])
    line = detail && detail !== title ? detail : ''
  }
  if (!line && step) line = step[zh ? 1 : 3]

  const approve = options.find(option => option.kind !== 'deny') ?? options[0]
  const primary: OwnerCardAction = kind === 'gate'
    ? { id: 'review', label: t('approvals.review'), kind: 'review' }
    : approve ? optionAction(approve, ids) : { id: 'open', label: t('ownerCard.open'), kind: 'open' }
  const more = options.filter(option => option !== approve || kind === 'gate').map(option => optionAction(option, ids))
  const consequence = kind === 'permission' ? t('ownerCard.consequence.permission')
    : kind === 'gate' ? t('ownerCard.consequence.gate')
    : kind === 'suspended' ? t('ownerCard.consequence.suspended')
    : ''
  const own: OwnerCardView = {
    title, line, primary, more, consequence,
    orderId: text(pause.order_id),
    orderLabel: withoutIds(text(pause.order_title), ids),
    decidesHere: primary.kind === 'approve' || primary.kind === 'deny',
    kind, raw, fromServer: false,
  }

  const served = servedCard(pause, ids)
  if (!served) return own
  const view: OwnerCardView = { ...own, fromServer: true }
  for (const key of ['title', 'line', 'primary', 'more', 'consequence', 'orderId', 'orderLabel', 'decidesHere'] as const) {
    const value = served[key]
    if (value !== undefined) (view as unknown as Record<string, unknown>)[key] = value
  }
  // A served primary that is one of the pause's answers keeps the others under 更多, and never twice.
  const primaryId = view.primary?.id
  if (served.primary && served.more === undefined) view.more = options.filter(option => option.id !== primaryId).map(option => optionAction(option, ids))
  else view.more = view.more.filter(action => action.id !== primaryId)
  if (served.decidesHere === undefined) view.decidesHere = view.primary?.kind === 'approve' || view.primary?.kind === 'deny'
  return view
}

/** Every sentence of a card, for the test that reads them all for ids. */
export function cardTexts(card: OwnerCardView): string[] {
  return [card.title, card.line, card.primary?.label ?? '', ...card.more.map(action => action.label), card.consequence, card.orderLabel]
}

/** A19 暂缓提醒: local wall-clock ISO without a zone (the store compares against local time), `hours` from now. It
 *  lived on the approval centre until S3-7; the card offers it wherever it is drawn. */
export function holdUntil(hours = 24, now: number = Date.now()): string {
  const at = new Date(now + hours * 3600 * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}T${pad(at.getHours())}:${pad(at.getMinutes())}:00`
}
