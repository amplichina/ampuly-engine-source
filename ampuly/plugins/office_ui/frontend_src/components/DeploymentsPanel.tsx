import { useState } from 'react'
import { useI18n } from '../i18n'
import { classifyAction, messageKey, type ActionState } from '../lib/actionState'

/**
 * Which customer, on which environment, is running which version -- and the
 * controls for moving them.
 *
 * Before this the answer lived in a database nobody opens, so "is acme still
 * on the old one" was settled by asking somebody who might be wrong. The
 * panel shows the manifest as it is, and shows an in-flight rollout per
 * target: a rollout that is half done reads as half done, never as a single
 * green tick.
 *
 * The controls follow the same rule the server does. A target is advanced one
 * at a time; health is recorded as a verdict somebody gives, not inferred
 * from time passing; and finishing is only offered once every target has been
 * established, because the server would refuse it anyway and a button that
 * exists to be refused teaches people to ignore refusals.
 */

export type Deployment = {
  customer_id: string
  environment_id: string
  solution_id: string
  version: string
  state: string
  updated_at: string
  updated_by: string
}

export type RolloutTarget = {
  customer_id: string
  environment_id: string
  from_version: string
  state: string
  health: string
  checks?: Array<{ name: string; result: string; detail?: string }>
}

export type Rollout = {
  rollout_id: string
  solution_id: string
  version: string
  state: string
  updated_by?: string
  note?: string
  targets: RolloutTarget[]
}

export type SolutionPackage = {
  solution_id: string
  version: string
  created_at?: string
}

export type DeploymentsPanelProps = {
  deployments: Deployment[]
  rollouts?: Rollout[]
  /** True when the server's list is its newest 50 rollouts and older ones exist: the panel then says so instead of
   *  showing the window as all of them. */
  rolloutsHasMore?: boolean
  /** Versions that are actually stored; a rollout can only name one of these. */
  packages?: SolutionPackage[]
  connected?: boolean
  /** Owner-only. Absent means the page is read-only for this account. */
  onPlanRollout?: (solutionId: string, version: string, targets: Array<{ customer_id: string; environment_id: string }>) => void
  onAdvance?: (rolloutId: string, customerId: string, environmentId: string) => void
  onHealth?: (rolloutId: string, customerId: string, environmentId: string, passed: boolean) => void
  onPromote?: (rolloutId: string) => void
  onRollback?: (rolloutId: string, note: string) => void
  error?: string | null
}


/**
 * One banner, six states, the same words everywhere. A page that invents its
 * own phrasing for "denied" is how two pages come to disagree about what it
 * means.
 */
function StateBanner({ state, detail }: { state: ActionState; detail?: string | null }) {
  const { t } = useI18n()
  if (state === 'ready') return null
  return (
    <div className={`capability-state capability-state--${state}`} data-testid="action-state" data-state={state} role="status">
      <span>{t(messageKey(state) as Parameters<typeof t>[0])}</span>
      {detail && state !== 'offline' && <code className="capability-detail">{detail}</code>}
    </div>
  )
}

function where(customerId: string, environmentId: string): string {
  return environmentId ? `${customerId} · ${environmentId}` : customerId
}

export function DeploymentsPanel({
  deployments, rollouts = [], rolloutsHasMore = false, packages = [], connected = true,
  onPlanRollout, onAdvance, onHealth, onPromote, onRollback, error,
}: DeploymentsPanelProps) {
  const { t } = useI18n()
  // A finished rollout is history; this panel is about what is in motion.
  const live = rollouts.filter(rollout => rollout.state === 'planned' || rollout.state === 'in_progress')
  const canControl = !!onAdvance && !!onPromote && !!onRollback
  const [version, setVersion] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [rollbackFor, setRollbackFor] = useState('')
  const [note, setNote] = useState('')

  const targetKey = (d: { customer_id: string; environment_id: string }) => `${d.customer_id}\u0000${d.environment_id}`
  const chosen = packages.find(p => `${p.solution_id}@${p.version}` === version)
  const candidates = chosen ? deployments.filter(d => d.solution_id === chosen.solution_id) : []

  return (
    <article className="capability-panel" data-testid="deployments">
      <div className="capability-panel-title">
        <h2>{t('deployments.title')}</h2>
        <span>{deployments.length}</span>
      </div>
      <div className="capability-muted">{t('deployments.hint')}</div>
      <StateBanner
        state={classifyAction({ connected, error, count: deployments.length })}
        detail={error}
      />
      {error && <div className="workflows-error" data-testid="deployments-error">{error}</div>}

      {deployments.length === 0 ? (
        <div className="capability-muted" data-testid="deployments-empty">{t('deployments.none')}</div>
      ) : (
        <div className="capability-list">
          {deployments.map(deployment => (
            <div
              className="capability-list-item"
              key={`${deployment.customer_id}-${deployment.environment_id}-${deployment.solution_id}`}
              data-testid="deployment-row"
            >
              <div>
                <strong>{where(deployment.customer_id, deployment.environment_id)}</strong>
                <small>{deployment.solution_id}</small>
                {deployment.updated_by && (
                  <small className="capability-detail">{t('deployments.changedBy', { who: deployment.updated_by })}</small>
                )}
              </div>
              <span className="capability-pill">{deployment.version}</span>
            </div>
          ))}
        </div>
      )}

      {onPlanRollout && packages.length > 0 && (
        <div className="capability-channel-check" data-testid="rollout-plan">
          <div className="capability-panel-title"><h2>{t('deployments.planTitle')}</h2></div>
          <div className="capability-muted">{t('deployments.planHint')}</div>
          <select
            data-testid="rollout-version"
            value={version}
            disabled={!connected}
            onChange={event => { setVersion(event.target.value); setPicked([]) }}
          >
            <option value="">{t('deployments.pickVersion')}</option>
            {packages.map(item => (
              <option key={`${item.solution_id}@${item.version}`} value={`${item.solution_id}@${item.version}`}>
                {item.solution_id} → {item.version}
              </option>
            ))}
          </select>
          {chosen && candidates.map(candidate => (
            <label className="capability-list-item" key={targetKey(candidate)} data-testid="rollout-candidate">
              <input
                type="checkbox"
                disabled={!connected}
                checked={picked.includes(targetKey(candidate))}
                onChange={event => setPicked(current => (
                  event.target.checked
                    ? [...current, targetKey(candidate)]
                    : current.filter(key => key !== targetKey(candidate))
                ))}
              />
              <span>
                {where(candidate.customer_id, candidate.environment_id)}
                {' '}
                <small className="capability-detail">{t('deployments.wasOn', { version: candidate.version })}</small>
              </span>
            </label>
          ))}
          <button
            type="button"
            className="capability-refresh-btn"
            data-testid="rollout-plan-submit"
            disabled={!connected || !chosen || picked.length === 0}
            onClick={() => {
              if (!chosen) return
              onPlanRollout(chosen.solution_id, chosen.version, picked.map(key => {
                const [customer_id, environment_id] = key.split('\u0000')
                return { customer_id, environment_id }
              }))
              setVersion('')
              setPicked([])
            }}
          >
            {t('deployments.plan')}
          </button>
        </div>
      )}

      {live.length > 0 && (
        <div className="capability-rollouts" data-testid="rollouts">
          <div className="capability-panel-title">
            <h2>{t('deployments.rollouts')}</h2>
            <span>{live.length}</span>
          </div>
          {live.map(rollout => {
            const blocked = rollout.targets.filter(target => target.state !== 'healthy')
            return (
              <div className="capability-list-item capability-list-item--stacked" key={rollout.rollout_id} data-testid="rollout-row">
                <div>
                  <strong>{rollout.solution_id} → {rollout.version}</strong>
                  {rollout.targets.map(target => (
                    <div
                      className={`capability-detail capability-detail--${target.state === 'healthy' ? 'ok' : target.state === 'unhealthy' ? 'warn' : ''}`}
                      key={`${target.customer_id}-${target.environment_id}`}
                      data-testid="rollout-target"
                    >
                      {where(target.customer_id, target.environment_id)}: {t(`deployments.target.${target.state}` as Parameters<typeof t>[0])}
                      {target.from_version ? ` (${t('deployments.wasOn', { version: target.from_version })})` : ''}
                      {canControl && target.state === 'pending' && (
                        <button
                          type="button"
                          className="workflows-cancel"
                          data-testid="rollout-advance"
                          disabled={!connected}
                          onClick={() => onAdvance?.(rollout.rollout_id, target.customer_id, target.environment_id)}
                        >
                          {t('deployments.advance')}
                        </button>
                      )}
                      {canControl && target.state === 'deployed' && onHealth && (
                        <>
                          <button
                            type="button"
                            className="workflows-cancel"
                            data-testid="rollout-health-pass"
                            disabled={!connected}
                            onClick={() => onHealth(rollout.rollout_id, target.customer_id, target.environment_id, true)}
                          >
                            {t('deployments.healthPass')}
                          </button>
                          <button
                            type="button"
                            className="workflows-cancel"
                            data-testid="rollout-health-fail"
                            disabled={!connected}
                            onClick={() => onHealth(rollout.rollout_id, target.customer_id, target.environment_id, false)}
                          >
                            {t('deployments.healthFail')}
                          </button>
                        </>
                      )}
                    </div>
                  ))}
                  {canControl && blocked.length > 0 && (
                    <small className="capability-detail" data-testid="rollout-blocked">
                      {t('deployments.blocked', { n: blocked.length })}
                    </small>
                  )}
                  {rollbackFor === rollout.rollout_id && (
                    <span data-testid="rollback-reason">
                      <input
                        className="capability-refresh-btn"
                        placeholder={t('deployments.rollbackWhy')}
                        value={note}
                        onChange={event => setNote(event.target.value)}
                      />
                      <button
                        type="button"
                        className="workflows-cancel"
                        data-testid="rollback-confirm"
                        disabled={!connected}
                        onClick={() => { onRollback?.(rollout.rollout_id, note); setRollbackFor(''); setNote('') }}
                      >
                        {t('deployments.rollbackConfirm')}
                      </button>
                    </span>
                  )}
                </div>
                <span className="capability-rollout-actions">
                  {canControl && (
                    <>
                      <button
                        type="button"
                        className="capability-refresh-btn"
                        data-testid="rollout-promote"
                        // Offered only when the server would accept it. A button
                        // that exists to be refused teaches people to click past
                        // refusals.
                        disabled={!connected || blocked.length > 0}
                        title={blocked.length > 0 ? t('deployments.blocked', { n: blocked.length }) : ''}
                        onClick={() => onPromote?.(rollout.rollout_id)}
                      >
                        {t('deployments.promote')}
                      </button>
                      <button
                        type="button"
                        className="workflows-cancel"
                        data-testid="rollout-rollback"
                        disabled={!connected}
                        onClick={() => setRollbackFor(rollout.rollout_id)}
                      >
                        {t('deployments.rollback')}
                      </button>
                    </>
                  )}
                  <span className={`capability-pill capability-pill--${rollout.state === 'in_progress' ? 'warn' : ''}`}>
                    {t(`deployments.state.${rollout.state}` as Parameters<typeof t>[0])}
                  </span>
                </span>
              </div>
            )
          })}
        </div>
      )}
      {rolloutsHasMore && (
        // The server hands over its newest 50 rollouts. Older ones exist: said here, so the window is not read as
        // all of them (a rollout in flight from before the window is not on this page).
        <div className="capability-muted" data-testid="rollouts-has-more">{t('deployments.rolloutsHasMore')}</div>
      )}
    </article>
  )
}
