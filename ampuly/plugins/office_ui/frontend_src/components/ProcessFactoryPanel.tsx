import { useState } from 'react'
import { useI18n } from '../i18n'
import { classifyAction, messageKey, type ActionState } from '../lib/actionState'
import { ProcessDraftEditor, type ProcessDraft } from './ProcessDraftEditor'

/**
 * Describing a delivery process, and seeing what the description cannot yet
 * answer.
 *
 * The reason this panel exists is that the alternative is YAML, which puts the
 * person who understands the work and the person who can edit the file in
 * different chairs.
 *
 * So the unfinished state is the important one to render well. A description
 * with holes is listed as having holes, with the questions spelled out, and
 * the guesses it had to make are shown beside them -- a guess that looks like
 * a decision is how the wrong thing gets built confidently.
 */

export type SpecSummary = {
  spec_id: string
  version: string
  author: string
  at: string
  missing: string[]
  ready: boolean
}

export type BuilderRound = {
  round: number
  outcome: string
  failed_step: string
  reason: string
  detail?: string
  proposal?: { step_id?: string; change?: string; because?: string }
  actor?: string
}

export type ProcessFactoryPanelProps = {
  specs: SpecSummary[]
  selected?: string
  review?: { missing: string[]; assumptions: Array<{ about: string; took: string; because: string }>; ready: boolean;
    previous_version?: string; previous_unreadable?: string; diff?: { new: boolean; added_steps: string[]; removed_steps: string[]; changed_steps: string[]; standard_changed: string[]; acceptance_changed: boolean }
  } | null
  rounds?: BuilderRound[]
  connected?: boolean
  onSelect?: (specId: string, version: string) => void
  /** Owner-only. Absent means this account may look and not try. */
  onTrial?: (specId: string) => void
  /** Owner-only. Continues an attempt parked at a human node with the person's decision. */
  onResume?: (specId: string, attempt: number, decision: string) => void
  /** The server's refusal, verbatim, so the banner can tell its kind. */
  error?: string | null
  draft?: ProcessDraft | null
  saving?: boolean
  trialState?: 'pending' | 'unknown'
  onSave?: (draft: ProcessDraft) => void
  onNew?: () => void
  onCandidate?: (templateId: string, draftId: string, generateSkills: boolean) => void
  templates?: Array<{ id: string; name: string; version: string; status: string; valid: boolean; skill_count: number }>
  templateError?: string | null
  onRefreshTemplates?: () => void
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

/** Where an attempt stopped for a person, read from the trial's own words. */
export function parkedGate(detail?: string): string {
  const match = /(?:parked at human gate|a person is needed at) ([A-Za-z0-9_]+)/.exec(detail ?? '')
  return match ? match[1] : ''
}

/** The branches hanging off the parked node, as the trial wrote them.
 *
 * The reply is what chooses a branch, so the words that choose it belong
 * beside the box the person types in, not in the error they get for
 * guessing. */
export function branchOptions(detail?: string): Array<{ id: string; when: string }> {
  const match = /; branches: ([^;]+(?:; [^;=]+=[^;]+)*)$/.exec(detail ?? '')
  if (!match) return []
  return match[1].split('; ').map(part => {
    const at = part.indexOf('=')
    return at < 0 ? { id: part.trim(), when: '' } : { id: part.slice(0, at).trim(), when: part.slice(at + 1).trim() }
  }).filter(option => option.id && option.when)
}

/** Which attempt directory a round ran in, so a continuation names the same one. */
export function attemptNumber(detail?: string): number {
  const match = /attempt-(\d+)/.exec(detail ?? '')
  return match ? Number(match[1]) : 0
}

export function ProcessFactoryPanel({
  specs, selected = '', review, rounds = [], connected = true, onSelect, onTrial, onResume, error, draft, saving, onSave, onNew, onCandidate,
  templates = [], templateError, onRefreshTemplates, trialState,
}: ProcessFactoryPanelProps) {
  const { t } = useI18n()
  const [decision, setDecision] = useState('')
  const latestRound = (rounds ?? []).reduce((most, round) => Math.max(most, round.round), 0)
  const [templateId, setTemplateId] = useState('')
  const [draftId, setDraftId] = useState('')
  const [makeSkills, setMakeSkills] = useState(false)
  const choices = templates.filter(item => ['draft', 'testing', 'published'].includes(item.status))
  const chosen = choices.find(item => item.id === templateId && item.valid)
  // A durable claim may mean execution is active OR cleanup was not confirmed.
  // Refresh can read it, but elapsed time must never turn it into permission.
  const unsettled = rounds.some(round => round.outcome === 'running')
  const busy = !!saving || !!trialState || unsettled
  const canTrial = connected && !busy && !!review?.ready
  const canCreate = connected && !busy && !!review?.ready && !!chosen && !templateError && !!draftId.trim()

  return (
    <article className="capability-panel" data-testid="process-factory">
      <div className="capability-panel-title">
        <h2>{t('factory.title')}</h2>
        <span>{specs.length}</span>
      </div>
      <div className="capability-muted">{t('factory.hint')}</div>
      <StateBanner state={classifyAction({ connected, error, count: specs.length })} detail={error} />
      {saving && <p role="status" data-testid="factory-pending">等待操作回执；请勿重复提交。</p>}
      {trialState && <p role="status" data-testid="factory-trial-pending">{trialState === 'pending'
        ? '试跑请求已发出，等待对应回执；不会重复派发。'
        : '试跑回执超时，结果未知。请刷新原版本记录核验；不会自动重试。'}</p>}
      {unsettled && <p role="status" data-testid="factory-unsettled">已有试跑认领尚未结清：可能仍在执行，也可能清理结果待核验。刷新仅查询记录，不会重跑或放行。</p>}
      {selected && draft && onSelect && <button type="button" disabled={!connected || saving || trialState === 'pending'}
        onClick={() => onSelect(selected, draft.version)}>刷新本版本与试跑记录</button>}
      {onNew && <button type="button" disabled={!connected || busy} onClick={onNew}>新建另一份草稿</button>}
      {onSave && <ProcessDraftEditor key={draft ? `${draft.spec_id}@${draft.version}` : 'new'}
        initial={draft} connected={connected} saving={busy} onSave={onSave} />}

      {specs.length === 0 ? (
        <div className="capability-muted" data-testid="factory-empty">{t('factory.none')}</div>
      ) : (
        <div className="capability-list">
          {specs.map(spec => (
            <button
              type="button"
              className="capability-list-item"
              key={`${spec.spec_id}@${spec.version}`}
              data-testid="factory-spec"
              disabled={!connected || saving || trialState === 'pending' || !onSelect}
              onClick={() => onSelect?.(spec.spec_id, spec.version)}
            >
              <span>
                <strong>{spec.spec_id} {spec.version}</strong>
                {/* Unfinished has to be visible without opening it. */}
                {!spec.ready && (
                  <small className="capability-detail capability-detail--warn" data-testid="factory-unfinished">
                    {t('factory.unfinished', { n: spec.missing.length })}
                  </small>
                )}
                {spec.author && <small className="capability-detail">{spec.author}</small>}
              </span>
              <span className={`capability-pill capability-pill--${spec.ready ? 'ok' : 'warn'}`}>
                {spec.ready ? t('factory.ready') : t('factory.notReady')}
              </span>
            </button>
          ))}
        </div>
      )}

      {selected && review && (
        <div className="capability-channel-check" data-testid="factory-review">
          {onCandidate && draft && <form data-testid="factory-candidate" onSubmit={event => {
            event.preventDefault()
            if (canCreate) onCandidate(templateId, draftId.trim(), makeSkills)
          }}>
            <h3>从已保存的 {draft.spec_id}@{draft.version} 生成候选包</h3>
            <p className="capability-muted">复用已有模板的角色和技能；不包含编辑器中尚未保存的修改。不继承工具放行或通过记录，不安装、不发布。生成后到工作流列表查看。</p>
            <label>工作流模板<select aria-label="工作流模板" value={chosen ? templateId : ''} disabled={!connected || saving || !!templateError}
              onChange={event => setTemplateId(event.target.value)}>
              <option value="">请选择已有模板</option>
              {choices.map(item => <option key={item.id} value={item.id} disabled={!item.valid}>
                {item.name} · {item.version} · {item.status}{item.valid ? '' : ' · 校验未通过'}
              </option>)}
            </select></label>
            {onRefreshTemplates && <button type="button" disabled={!connected || saving} onClick={onRefreshTemplates}>刷新模板</button>}
            {templateError ? <p role="status">模板列表暂不可用，请刷新重试。</p>
              : choices.length === 0 && <p role="status">尚无可选模板，请先加载或创建工作流模板，再刷新。</p>}
            {chosen && <p className="capability-muted">模板标识：{chosen.id}；包含 {chosen.skill_count} 个技能。可选择不代表与当前描述兼容，生成时仍由服务端校验。</p>}
            <label>新候选包标识<input aria-label="新候选包标识" value={draftId} onChange={event => setDraftId(event.target.value)} /></label>
            <label data-testid="factory-make-skills"><input type="checkbox" checked={makeSkills} onChange={event => setMakeSkills(event.target.checked)} />
              描述里提到、模板没有的技能：按描述生成草稿合同（写明未评审、未跑过；能加载和试跑，不能锁定发布）</label>
            <button type="submit" disabled={!canCreate}>生成独立候选包</button>
          </form>}
          {review.previous_unreadable && <p className="capability-detail capability-detail--warn" data-testid="factory-previous-unreadable">上一版本无法按当前规则读取，没有生成差异：{review.previous_unreadable}</p>}
          {review.diff && <div data-testid="factory-diff">
            <h3>{review.diff.new ? '新候选，尚无上一版本' : `相对 ${review.previous_version} 的变化`}</h3>
            <p>新增步骤：{review.diff.added_steps.join('、') || '无'}；移除步骤：{review.diff.removed_steps.join('、') || '无'}；修改步骤：{review.diff.changed_steps.join('、') || '无'}</p>
            <p className="capability-detail capability-detail--warn">完成标准变化：{review.diff.standard_changed.join('、') || '无'}；客户验收标准{review.diff.acceptance_changed ? '有变化，需重新评审' : '未变化'}</p>
          </div>}
          {review.missing.length > 0 && (
            <div data-testid="factory-questions">
              <div className="capability-panel-title"><h2>{t('factory.questions')}</h2></div>
              {review.missing.map(question => (
                <div className="capability-detail capability-detail--warn" key={question}>{question}</div>
              ))}
            </div>
          )}
          {review.assumptions.length > 0 && (
            <div data-testid="factory-assumptions">
              <div className="capability-panel-title"><h2>{t('factory.assumptions')}</h2></div>
              {review.assumptions.map(assumption => (
                <div className="capability-detail" key={assumption.about}>
                  {t('factory.assumed', { about: assumption.about, took: assumption.took })}
                  {assumption.because ? `（${assumption.because}）` : ''}
                </div>
              ))}
            </div>
          )}
          {onTrial && (
            <button
              type="button"
              className="capability-refresh-btn"
              data-testid="factory-trial"
              disabled={!canTrial}
              onClick={() => { if (canTrial) onTrial(selected) }}
            >
              {t('factory.trial')}
            </button>
          )}
          {rounds.length > 0 && (
            <div data-testid="factory-rounds">
              <div className="capability-panel-title"><h2>{t('factory.rounds')}</h2></div>
              {rounds.map(round => (
                <div className="capability-list-item capability-list-item--stacked" key={`${round.round}-${round.outcome}-${round.reason}`} data-testid="factory-round">
                  <div>
                    <strong>{round.outcome === 'running' ? '认领未结清（执行中或待核验）' : t(`factory.outcome.${round.outcome}` as Parameters<typeof t>[0])}</strong>
                    {round.reason && <small className="capability-detail">{round.reason}</small>}
                    {round.proposal?.change && (
                      <small className="capability-detail" data-testid="factory-proposal">
                        {t('factory.proposal', { change: round.proposal.change })}
                      </small>
                    )}
                    {onResume && selected && parkedGate(round.detail) && attemptNumber(round.detail) > 0 && round.round === latestRound && (
                      <span data-testid="factory-resume">
                        <small className="capability-detail">停在人工节点「{parkedGate(round.detail)}」。你来决定，试跑接着走：</small>
                        {branchOptions(round.detail).length > 0 && (
                          <small className="capability-detail" data-testid="factory-branch-options">
                            这一步下面有分支，点一条把它的条件原话填进理由里，没选中的分支不会跑：
                            {branchOptions(round.detail).map(option => (
                              <button key={option.id} type="button" className="capability-refresh-btn"
                                data-testid={`factory-branch-${option.id}`}
                                onClick={() => setDecision(decision.trim() ? `${decision.trim()}，${option.when}` : option.when)}>
                                {option.id}「{option.when}」
                              </button>
                            ))}
                          </small>
                        )}
                        <input
                          className="capability-refresh-btn"
                          placeholder="放行或打回的理由（会写进记录）"
                          value={decision}
                          onChange={event => setDecision(event.target.value)}
                        />
                        <button type="button" className="capability-refresh-btn" data-testid="factory-resume-approve"
                          disabled={!connected || saving || trialState === 'pending' || !decision.trim()}
                          onClick={() => { onResume(selected, attemptNumber(round.detail), `放行：${decision.trim()}`); setDecision('') }}>放行并继续</button>
                        <button type="button" className="workflows-cancel" data-testid="factory-resume-reject"
                          disabled={!connected || saving || trialState === 'pending' || !decision.trim()}
                          onClick={() => { onResume(selected, attemptNumber(round.detail), `打回：${decision.trim()}`); setDecision('') }}>打回</button>
                      </span>
                    )}
                  </div>
                  {/* A round that never ran established nothing, and the badge
                      must not let it read as either a pass or a failure. */}
                  <span className={`capability-pill capability-pill--${round.outcome === 'passed' ? 'ok' : round.outcome === 'failed' ? 'warn' : ''}`}>
                    {round.outcome === 'unrun' ? t('factory.provedNothing') : `#${round.round}`}
                  </span>
                </div>
              ))}
              <div className="capability-detail" data-testid="factory-simulated">{t('factory.simulated')}</div>
            </div>
          )}
        </div>
      )}
    </article>
  )
}
