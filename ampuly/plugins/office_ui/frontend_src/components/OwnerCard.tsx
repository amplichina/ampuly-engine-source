// The one owner card (frontend redesign S1-B, policy): a title, one line, ONE primary button in the model's
// own words with what it does under it, 「更多」 for the other answers and 「打开这张单」, a refusal line in words, a held
// badge. No kicker with ids, no <code>: nothing on it is an internal id. Drawn the same on every surface that answers
// a pause (the approval centre, 总控, 项目中心, the browser pane); what it says comes from lib/ownerCard.ts, what a
// press does comes from the one answer path (lib/answerPath.ts) through the callbacks.

import type { ReactNode } from 'react'
import { translateFor, useLocaleOr, type Locale } from '../i18n'
import { ownerCardOf, type OwnerCardAction } from '../lib/ownerCard'
import type { DashboardBlocker } from './DashboardPage'

export interface OwnerCardProps {
  blocker: DashboardBlocker
  /** The language when the card is drawn outside the i18n provider (the browser pane); under it, the provider's. */
  locale?: Locale
  connected?: boolean
  /** The answer is out: the buttons are disabled and the primary says 「发出去了，等回执」. */
  pending?: boolean
  /** What the last answer came back with, in words; stays on the card until the next answer. */
  refusal?: string
  /** `compact` for a rail or a pane: the line is kept, the actions sit under the words. */
  layout?: 'full' | 'compact'
  className?: string
  /** Extra attributes on the article (a surface's data-* hook for its tests); never text the owner reads. */
  attributes?: Record<string, string | undefined>
  onDecide: (blocker: DashboardBlocker, optionId: string) => void
  /** A business gate's primary: the materials view (GatePanel). Absent = the gate cannot be reviewed from here. */
  onReview?: (blocker: DashboardBlocker) => void
  /** 打开这张单. Absent = not offered. */
  onOpen?: (blocker: DashboardBlocker) => void
  onHold?: (blocker: DashboardBlocker) => void
  onRelease?: (blocker: DashboardBlocker) => void
  /** What sits under the card (the gate's materials view when it is open here). */
  children?: ReactNode
}

function actionClass(action: OwnerCardAction): string {
  return action.kind === 'approve' || action.kind === 'deny' ? `dashboard-blocker-option--${action.kind}` : 'workflows-invoke'
}

/** Where a press goes: an answer decides its option, 在这里审 opens the materials view, 打开这张单 opens the order. */
export function pressOwnerCard(action: OwnerCardAction, blocker: DashboardBlocker, on: Pick<OwnerCardProps, 'onDecide' | 'onReview' | 'onOpen'>): void {
  if (action.kind === 'review') on.onReview?.(blocker)
  else if (action.kind === 'open') on.onOpen?.(blocker)
  else on.onDecide(blocker, action.id)
}

export function OwnerCard({ blocker, locale: givenLocale, connected = true, pending = false, refusal, layout = 'full', className, attributes, onDecide, onReview, onOpen, onHold, onRelease, children }: OwnerCardProps) {
  const locale = useLocaleOr(givenLocale ?? 'zh-CN')
  const t = translateFor(locale)
  const card = ownerCardOf(blocker, locale.startsWith('zh'), t)
  const held = !!blocker.snoozed_until
  const press = (action: OwnerCardAction) => pressOwnerCard(action, blocker, { onDecide, onReview, onOpen })
  const canPress = (action: OwnerCardAction) => connected && !pending && (action.kind === 'review' ? !!onReview : action.kind === 'open' ? !!onOpen : true)
  const more = [...card.more]
  const offersOpen = !!onOpen && card.primary?.kind !== 'open'
  const classes = ['owner-card', `owner-card--${layout}`, held ? 'owner-card--held' : '', pending ? 'owner-card--pending' : '', className ?? ''].filter(Boolean).join(' ')
  return (
    <article className={classes} data-blocker-kind={card.kind} data-held={held ? 'true' : undefined} data-pending={pending ? 'true' : undefined}
      data-decides-here={card.decidesHere ? 'true' : 'false'} aria-busy={pending || undefined} {...attributes}>
      <div className="owner-card__body">
        {/* What kind of pause, in words -- no id beside it (the earlier kicker carried the checkpoint id). */}
        {card.kind !== 'suspended' && card.kind !== 'other' && <span className="owner-card__kind">{t(`approvals.kind.${card.kind}` as Parameters<typeof t>[0])}</span>}
        <h3 className="owner-card__title">{card.title}</h3>
        {card.line && <p className="owner-card__line">{card.line}</p>}
        {card.orderLabel && <p className="owner-card__order">{card.orderLabel}</p>}
        {card.raw && (
          <details className="approval-center-raw owner-card__raw">
            <summary>{t('approvals.permission.raw')}</summary>
            <span className="owner-card__raw-text">{card.raw}</span>
          </details>
        )}
        {blocker.feishu && <span className="approval-center-channel owner-card__channel">{t('dashboard.blockers.feishuSent')}</span>}
        {held && (
          <span className="approval-center-channel approval-center-held owner-card__held" data-held-until={blocker.snoozed_until}>
            {t('approvals.heldUntil', { at: blocker.snoozed_until ?? '' })}{blocker.snooze_note ? ` · ${blocker.snooze_note}` : ''}
          </span>
        )}
      </div>
      <div className="owner-card__actions">
        {/* The server may say there is nothing for the owner to press here (an employee's turn): then no primary. */}
        {card.primary && (
          <button type="button" className={`owner-card__primary ${actionClass(card.primary)}`} data-action={card.primary.id}
            disabled={!canPress(card.primary)} onClick={() => press(card.primary!)}>
            {pending ? t('ownerCard.pending') : card.primary.label}
          </button>
        )}
        {!card.primary && pending && <span className="owner-card__waiting">{t('ownerCard.pending')}</span>}
        {card.consequence && <small className="owner-card__consequence">{card.consequence}</small>}
        {(more.length > 0 || offersOpen || (held ? !!onRelease : !!onHold)) && (
          <details className="owner-card__more">
            <summary>{t('ownerCard.more')}</summary>
            <div className="owner-card__more-list">
              {more.map(action => (
                <button key={action.id} type="button" className={`owner-card__option ${actionClass(action)}`} data-action={action.id}
                  disabled={!canPress(action)} onClick={() => press(action)}>
                  {action.label}
                </button>
              ))}
              {offersOpen && (
                <button type="button" className="owner-card__option workflows-cancel owner-card__open" disabled={!connected} onClick={() => onOpen?.(blocker)}>
                  {t('ownerCard.open')}
                </button>
              )}
              {held
                ? onRelease && <button type="button" className="owner-card__option workflows-cancel approval-center-hold" disabled={!connected} onClick={() => onRelease(blocker)}>{t('approvals.release')}</button>
                : onHold && <button type="button" className="owner-card__option workflows-cancel approval-center-hold" disabled={!connected} onClick={() => onHold(blocker)}>{t('approvals.hold')}</button>}
            </div>
          </details>
        )}
      </div>
      {refusal && (
        <p className="approval-center-refusal workflows-error" role="alert" data-testid="approval-decision-refused">{refusal}</p>
      )}
      {children}
    </article>
  )
}
