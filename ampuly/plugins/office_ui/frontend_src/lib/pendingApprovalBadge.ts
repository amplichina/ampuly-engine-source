/** Global pending-approval badge: count + visibility. quietMode must not hide when count > 0. */
import { badgeText } from './shellNav'

export type PendingApprovalLike = {
  id?: string
  status?: string
  state?: string
  kind?: string
  type?: string
  pending?: boolean
  approval_id?: string
  checkpoint_id?: string
  enterprise_id?: string
  project_id?: string
  customer_id?: string
  environment_id?: string
  revision?: number
  event_seq?: number
  sequence?: number
}

const PENDING = new Set([
  'pending',
  'awaiting',
  'awaiting_approval',
  'needs_human_review',
  'needs_review',
  'submitted',
  'queued_for_approval',
])

const TERMINAL = new Set([
  'done', 'completed', 'succeeded', 'success', 'approved', 'rejected', 'denied',
  'cancelled', 'canceled', 'expired', 'resolved', 'closed', 'failed', 'retired',
  'finished', 'superseded',
])

const statusOf = (item: PendingApprovalLike) => String(item.status || item.state || '').trim().toLowerCase()
const revisionOf = (item: PendingApprovalLike) => {
  const n = item.revision ?? item.event_seq ?? item.sequence ?? 0
  return Number.isSafeInteger(n) && n >= 0 ? n : 0
}

function uniqueFacts(items: readonly PendingApprovalLike[] | null | undefined): { items: PendingApprovalLike[]; unidentified: number } {
  const facts = new Map<string, PendingApprovalLike>()
  const unidentified: PendingApprovalLike[] = []
  for (const item of items ?? []) {
    const id = String(item.id || item.approval_id || item.checkpoint_id || '').trim()
    if (!id) { unidentified.push(item); continue }
    const key = JSON.stringify([item.enterprise_id ?? '', item.project_id ?? '', item.customer_id ?? '', item.environment_id ?? '', id])
    const previous = facts.get(key)
    if (!previous || revisionOf(item) > revisionOf(previous) || (
      revisionOf(item) === revisionOf(previous) && TERMINAL.has(statusOf(item)) && !TERMINAL.has(statusOf(previous))
    )) facts.set(key, item)
  }
  return { items: [...facts.values(), ...unidentified], unidentified: unidentified.length }
}

export function countPendingApprovals(items: readonly PendingApprovalLike[] | null | undefined): number {
  let n = 0
  for (const raw of uniqueFacts(items).items) {
    const status = statusOf(raw)
    const kind = String(raw.kind || raw.type || '').trim().toLowerCase()
    if (TERMINAL.has(status) || (status && !PENDING.has(status))) continue
    if (PENDING.has(status) || (!status && (kind === 'approval' || kind === 'approval_request' || kind === 'blocker' || raw.pending === true || !!raw.checkpoint_id))) {
      n += 1
    }
  }
  return n
}

/** Badge model for the shell rail. quietMode never forces hidden when count > 0. */
export function pendingApprovalBadge(input: {
  items?: readonly PendingApprovalLike[] | null
  count?: number
  quietMode?: boolean
}): { count: number; visible: boolean; text: string; hiddenByQuiet: false } {
  const count = typeof input.count === 'number' && Number.isFinite(input.count) ? Math.max(0, Math.floor(input.count)) : countPendingApprovals(input.items)
  const visible = count > 0
  // quietMode is accepted but ignored for hiding — safety parity with Preferences F1.
  void input.quietMode
  return { count, visible, text: badgeText(count), hiddenByQuiet: false }
}
