// The product shell's side rail (frontend refactor U1): the brand mark, the five primary entries, and 配置 / 设置 at
// the foot. It owns no state and asks for nothing: the page to open goes to `onOpenPage`, which is the same function
// the earlier top bar uses, so an entry opened from here asks the server for the same lists.
//
// The entries are the interaction prototype's (policy): its glyphs (ShellIcons), its geometry (index.css),
// and on 业务工单 its dot -- shown when orders wait on the owner, with no number on it; the count is in the title.

import { useI18n } from '../../i18n'
import type { RestorableAppPage } from '../../lib/appPageState'
import { FOOT_NAV, PAGE_LABEL_KEYS, PRIMARY_NAV, railSelection, type RailPage } from '../../lib/shellNav'
import { ShellIcon } from './ShellIcons'

export function ShellRail({ activePage, ordersWaiting, onOpenPage, pendingApprovalBadge }: {
  activePage: RestorableAppPage
  /** Orders waiting on the owner (总控's 需要关注, lib/attention): the dot on 业务工单 while there are any. */
  ordersWaiting: number
  onOpenPage: (page: RailPage) => void
  /** Top5 item5: pending-approval badge from blockers channel (flag-gated by App). quiet never hides. */
  pendingApprovalBadge?: { count: number; visible: boolean; text: string; stale?: boolean } | null
}) {
  const { t } = useI18n()
  const selected = railSelection(activePage)
  const waiting = Number.isFinite(ordersWaiting) && ordersWaiting > 0 ? Math.floor(ordersWaiting) : 0
  const pendingCount = pendingApprovalBadge && pendingApprovalBadge.visible
    ? Math.max(0, Math.floor(Number(pendingApprovalBadge.count) || 0))
    : 0
  const entry = (page: RailPage) => {
    const ordersDot = page === 'orders' && waiting > 0
    const pendingDot = page === 'master' && (pendingCount > 0 || pendingApprovalBadge?.stale === true)
    const dot = ordersDot || pendingDot
    const badgeTitle = pendingDot
      ? (pendingApprovalBadge?.text || String(pendingCount))
      : t('shell.ordersWaitingTitle', { n: waiting })
    const badgeLabel = pendingDot ? badgeTitle : t('shell.ordersWaitingLabel')
    return (
      <button key={page} type="button" className={`shell-rail-entry${selected === page ? ' is-active' : ''}`}
        aria-current={selected === page ? 'page' : undefined} onClick={() => onOpenPage(page)}>
        <span className="shell-rail-icon">
          <ShellIcon name={page} />
          {dot && <span className="shell-rail-badge" data-testid={pendingDot ? 'shell-pending-approval-badge' : undefined} data-state={pendingDot ? (pendingApprovalBadge?.stale ? 'stale' : 'fresh') : undefined} role="img" aria-label={badgeLabel} title={badgeTitle} />}
        </span>
        <span className="shell-rail-label">{t(PAGE_LABEL_KEYS[page])}</span>
      </button>
    )
  }
  return (
    <nav className="shell-rail" aria-label={t('shell.nav')}>
      <span className="shell-rail-brand" title={t('shell.brand')}>
        <img className="shell-rail-mark shell-rail-mark--on-light" src="./brand/ampuly-mark.png" alt={t('shell.brand')} width={34} height={29} />
        <img className="shell-rail-mark shell-rail-mark--on-dark" src="./brand/ampuly-mark-light.png" alt={t('shell.brand')} width={34} height={29} />
      </span>
      {PRIMARY_NAV.map(entry)}
      <span className="shell-rail-foot">{FOOT_NAV.map(entry)}</span>
    </nav>
  )
}
