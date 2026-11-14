// 总控施工模式 (policy, B2): the one live card of a build job, on the job's latest message in 总控's
// conversation (lib/masterBuild liveBuildMessages; the job's earlier messages are plain lines). Nothing of it sits
// above the conversation (policy: 总控只当对话页).
//
//   * 施工中: the step it is on, in words (the raw tool name only in a data attribute), and 停下 / 插话 -- the existing
//     harness_turn_interrupt / harness_turn_steer on the job's session and turn, on the same tracker road and in the
//     same words as 总控's own stop / steer row;
//   * 等你回答: the question in words and 允许一次 / 这次施工都允许 / 不行, each sending exactly its own option through
//     interaction_reply (only the options the question offers are drawn); once answered they stay disabled until the
//     job's next state arrives;
//   * 已交付候选: the summary, what changed by kind (技能 / 节点表 / 组织配置 / 测试 / 其它), what is still the owner's to
//     decide, the tools to develop, 在搭建台打开这个草稿 and 接着改;
//   * 已停下 / 中断了 / 到了调用上限 / 出错了: why, in the server's words, and 接着改.
// 接着改 only puts 「接着改：」 in the input and focuses it: the owner finishes the sentence and sends it to 安安.
import { useState } from 'react'

import type { useI18n } from '../i18n'
import type { Answering } from '../lib/answerPath'
import {
  IDLE_BUILD_CONTROL, buildAnswerKey, buildCardView, buildFileLabel, buildStateKey, buildStepWords, controlFor, groupChangedFiles,
  type BuildActivities, type BuildAnswerMarks, type BuildCard, type BuildControl, type BuildQuestion,
} from '../lib/masterBuild'
import { keyOf } from '../lib/requestTracker'

type Translate = ReturnType<typeof useI18n>['t']

/** What a build card can reach, handed down by App for the owner's console only. Absent = the card shows where the
 *  build stands and offers nothing to press. */
export interface MasterBuildHooks {
  /** The last step of each running job (master_build_activity), by job id. */
  activity: BuildActivities
  /** The card's 停下 / 插话 bookkeeping. */
  control: BuildControl
  /** Whether a request is still out on the request tracker (by its key). */
  isBusy: (key: string) => boolean
  /** What each answer came back with, by checkpoint id. */
  answers: BuildAnswerMarks
  /** The answer path's out answers, by checkpoint id. */
  answering: Answering
  onStop: (card: BuildCard, turnId: string) => void
  /** Returns whether the instruction actually went out; text that did not stays in the box. */
  onSteer: (card: BuildCard, turnId: string, text: string) => boolean
  onAnswer: (question: BuildQuestion, option: { id: string; label: string }) => void
  /** 在搭建台打开这个草稿: the builder on that draft (the builder's own open-a-flow request). */
  onOpenDraft?: (draftId: string) => void
}

const KIND_KEY = {
  skill: 'master.build.kind.skill', nodes: 'master.build.kind.nodes', org: 'master.build.kind.org',
  test: 'master.build.kind.test', other: 'master.build.kind.other',
} as const
const CHANGE_KEY = {
  added: 'master.build.change.added', modified: 'master.build.change.modified', removed: 'master.build.change.removed',
} as const
// A long list stays short on the card; the message text the job wrote keeps the first thirty.
const FILES_PER_KIND = 8

export function MasterBuildCard({ card, content, hooks, connected, onContinue, t, zh }: {
  card: BuildCard
  /** The message's own words: why a stopped, interrupted, capped or failed build stopped. */
  content: string
  hooks?: MasterBuildHooks
  connected: boolean
  /** 接着改: put 「接着改：」 in the input and focus it. Absent = no such button. */
  onContinue?: () => void
  t: Translate
  zh: boolean
}) {
  const [steerDraft, setSteerDraft] = useState('')
  const question = card.question
  const mark = question ? hooks?.answers[question.checkpointId] : undefined
  const view = buildCardView(card, hooks?.activity[card.jobId], !!mark?.done)
  const control = controlFor(hooks?.control ?? IDLE_BUILD_CONTROL, card.jobId, view.turnId)
  // Pending only while the request tracker still has that very request out (15 s, or the cut), as on 总控's row.
  const interruptPending = !!control.interruptRequestId && !!hooks?.isBusy(keyOf('harness_turn_interrupt', control.interruptRequestId))
  const steerPending = !!control.steerRequestId && !!hooks?.isBusy(keyOf('harness_turn_steer', control.steerRequestId))
  // The words of the stop / steer, as 总控's row says them: where the stop stands, and the steer's receipt or hint.
  const interruptWords = t(control.interruptRequested ? 'master.interruptRequested' : interruptPending ? 'master.interruptPending' : 'master.interruptHint')
  const steerWords = control.steerNote || (!view.turnId ? t('master.build.steerNoTurn') : t(steerPending ? 'master.steerPending' : 'master.steerHint'))
  const answerOut = question ? hooks?.answering[question.checkpointId]?.state === 'pending' : false
  const settled = view.state !== 'running' && view.state !== 'waiting_owner'
  const reason = view.state === 'failed' ? card.lastError || content : content
  const delivery = view.state === 'delivered' ? card.delivery : null
  const continueButton = onContinue && (
    <button type="button" className="master-open-order-btn" data-build-action="continue" title={t('master.build.continueHint')}
      disabled={!connected} onClick={onContinue}>{t('master.build.continue')}</button>
  )

  return (
    <div className="master-build-card" data-testid="master-build-card" data-build-job={card.jobId} data-build-state={view.state}>
      <div className="master-build-head">
        <span className="master-build-kicker">{t('master.build.kicker')}</span>
        <span className="master-build-state" data-build-state={view.state}>{t(buildStateKey(view.state))}</span>
      </div>
      <strong className="master-build-goal" title={card.goal}>{card.goal}</strong>
      {card.name && <span className="master-build-draft">{t('master.build.draft', { name: card.name })}</span>}

      {view.state === 'running' && <>
        <p className="master-build-step" data-testid="master-build-step">
          {view.step
            ? <span className="master-tool-words" data-tool-name={view.step.toolName || undefined}>{t('master.build.stepNow', { step: buildStepWords(view.step.toolName, zh) })}</span>
            : t('master.build.stepUnknown')}
        </p>
        {card.progress && <p className="master-build-note">{t('master.build.progress', { text: card.progress })}</p>}
        {hooks && (
          <div className="master-build-controls" data-testid="master-build-controls">
            <button type="button" className="master-open-order-btn" data-build-action="stop" onClick={() => hooks.onStop(card, view.turnId)}
              disabled={!connected || !card.sessionId || interruptPending || control.interruptRequested || !!control.interruptError}>
              {t('master.build.stop')}
            </button>
            <p role="status" title={interruptWords}>{interruptWords}</p>
            {control.interruptError && <p role="alert" title={control.interruptError}>{control.interruptError}</p>}
            <div className="master-steer master-build-steer" data-testid="master-build-steer">
              <input
                className="master-input"
                value={steerDraft}
                maxLength={4000}
                placeholder={t('master.steerPlaceholder')}
                aria-label={t('master.steer')}
                disabled={!connected || steerPending}
                onChange={event => setSteerDraft(event.target.value)}
              />
              <button type="button" className="master-open-order-btn" data-build-action="steer"
                disabled={!connected || steerPending || !view.turnId || !card.sessionId || !steerDraft.trim()}
                onClick={() => { if (hooks.onSteer(card, view.turnId, steerDraft.trim())) setSteerDraft('') }}>
                {t('master.steer')}
              </button>
              <p role="status" title={steerWords}>{steerWords}</p>
            </div>
          </div>
        )}
      </>}

      {view.state === 'waiting_owner' && <>
        {(question?.question || content) && <p className="master-build-question" data-testid="master-build-question">{question?.question || content}</p>}
        {hooks && question && (question.options.length > 0
          ? (
            <div className="master-build-actions" data-testid="master-build-answers">
              {question.options.map(option => (
                <button key={option.id} type="button" className="master-open-order-btn" data-build-answer={option.id}
                  disabled={!connected || answerOut || !!mark?.done}
                  onClick={() => hooks.onAnswer(question, option)}>
                  {t(buildAnswerKey(option.id))}
                </button>
              ))}
            </div>
          )
          : <p className="master-build-note">{t('master.build.noAnswers')}</p>)}
        {hooks && answerOut && <p className="master-build-note" role="status">{t('request.busy')}</p>}
        {hooks && mark?.done && !mark.words && <p className="master-build-note" role="status">{t('master.build.answered')}</p>}
        {hooks && mark?.words && <p className="master-build-note" role="alert">{mark.words}</p>}
      </>}

      {delivery && <>
        {delivery.summary && <p className="master-build-summary">{delivery.summary}</p>}
        <div className="master-build-files" data-testid="master-build-files">
          <span className="master-build-label">{t('master.build.files')}</span>
          {delivery.changedFiles.length === 0
            ? <span className="master-build-note">{t('master.build.filesNone')}</span>
            : (
              <ul>
                {groupChangedFiles(delivery.changedFiles).map(group => (
                  <li key={group.kind} data-file-kind={group.kind}>
                    <b>{t(KIND_KEY[group.kind])}</b>{zh ? `（${group.files.length}）：` : ` (${group.files.length}): `}
                    {group.files.slice(0, FILES_PER_KIND).map((file, index) => (
                      <span key={`${file.path}:${index}`} className="master-build-file" data-file-path={file.path} data-file-change={file.change} title={file.path}>
                        {index > 0 ? (zh ? '、' : ', ') : ''}{t(CHANGE_KEY[file.change])} {buildFileLabel(file.path)}
                      </span>
                    ))}
                    {group.files.length > FILES_PER_KIND && <span className="master-build-note"> {t('master.build.filesMore', { n: group.files.length - FILES_PER_KIND })}</span>}
                  </li>
                ))}
              </ul>
            )}
          {delivery.truncated && <span className="master-build-note">{t('master.build.filesTruncated')}</span>}
        </div>
        {delivery.openQuestions.length > 0 && (
          <div className="master-build-list" data-testid="master-build-open-questions">
            <span className="master-build-label">{t('master.build.openQuestions')}</span>
            <ul>{delivery.openQuestions.map((item, index) => <li key={index}>{item}</li>)}</ul>
          </div>
        )}
        {delivery.toolsToDevelop.length > 0 && (
          <div className="master-build-list" data-testid="master-build-tools">
            <span className="master-build-label">{t('master.build.toolsToDevelop')}</span>
            <ul>{delivery.toolsToDevelop.map((item, index) => <li key={index}>{item}</li>)}</ul>
          </div>
        )}
        <p className="master-build-note">{t('master.build.simulated')}</p>
        {delivery.notesUpdated === false && <p className="master-build-note">{t('master.build.notesNotUpdated')}</p>}
        <div className="master-build-actions">
          {hooks?.onOpenDraft && card.draftId
            ? <button type="button" className="master-open-order-btn" data-build-action="open-draft" onClick={() => hooks.onOpenDraft?.(card.draftId)}>{t('master.build.openDraft')} →</button>
            : <span className="master-build-note">{t('master.build.draftWhere', { name: card.name || card.goal })}</span>}
          {continueButton}
        </div>
      </>}

      {/* A hand-over whose record did not come with the message is said by the message's own words, like a stop. */}
      {settled && !delivery && <>
        {reason && <p className="master-build-reason" data-testid="master-build-reason">{reason}</p>}
        {continueButton && <div className="master-build-actions">{continueButton}</div>}
      </>}
    </div>
  )
}
