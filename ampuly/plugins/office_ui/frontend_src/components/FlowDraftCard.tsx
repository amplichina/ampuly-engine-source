// 需求 → 流程草案 (4.7 Q04-B): the card a flow draft rides on in 总控 / 安安. It shows what the draft is and what it
// still lacks; its one action opens it on the builder (the console compiles the stored spec again, creates an empty
// draft on the chosen team and hands the steps to the canvas unsaved). Nothing here saves anything.
import { useState } from 'react'

import type { useI18n } from '../i18n'
import { flowDraftGaps, missingKind, type ComponentAskState, type FlowDraft, type MissingKind } from '../lib/flowDraft'

type Translate = ReturnType<typeof useI18n>['t']

// What is asked for depends on what is missing (missingKind): an interface gets a connector candidate drafted, a skill
// is looked for in the library, a flow is looked for among the flows or drafted as a new one. Nothing is tested,
// approved, installed or saved by any of them.
const ASK_KEY: Record<MissingKind, 'master.flowDraft.askComponent' | 'master.flowDraft.askSkill' | 'master.flowDraft.askFlow'> = {
  connector: 'master.flowDraft.askComponent', skill: 'master.flowDraft.askSkill', flow: 'master.flowDraft.askFlow',
}
// What the placeholder says once asked. Only an interface ends in a candidate (componentAskStates); a skill or a flow is
// answered in the conversation, so its line says where the answer is instead of waiting for a candidate that never comes.
const ASK_SENT_KEY: Record<MissingKind, 'master.flowDraft.askSent' | 'master.flowDraft.askSentSkill' | 'master.flowDraft.askSentFlow'> = {
  connector: 'master.flowDraft.askSent', skill: 'master.flowDraft.askSentSkill', flow: 'master.flowDraft.askSentFlow',
}

/**
 * 让安安起草这个组件 (4.7 A05): the one sentence sent to 安安 for a step's missing component -- the flow and the step it
 * belongs to, the placeholder's name, what the step does, what it takes in and hands over, and that only an inert
 * candidate (or a suggestion) is wanted. '' when the step has no placeholder. The same text is how the conversation
 * is read back (componentAskStates), so it is built in one place.
 */
export function componentAskText(draft: FlowDraft, stepId: string, t: Translate): string {
  const step = draft.steps.find(item => item.id === stepId)
  const gap = draft.missing.find(item => item.step === stepId)
  if (!step || !gap) return ''
  const none = t('master.flowDraft.askNone')
  const list = (values: string[]) => (values.length ? values.join('、') : none)
  const request = t(ASK_KEY[missingKind(draft, stepId)], {
    flow: draft.name, step: step.name, capability: gap.capability || none,
    does: step.does || none, inputs: list(step.inputs), outputs: list(step.delivers),
  })
  if (!draft.reference || missingKind(draft, stepId) === 'connector') return request
  return request + `\n如果没有现成组件，请调用 master_propose_component_candidate 起草结构化候选；只作草稿，不准入或执行。` +
    JSON.stringify({ source: draft.reference, step_id: stepId, kind: missingKind(draft, stepId) })
}

const KIND_KEY: Record<string, 'master.flowDraft.kind.ai' | 'master.flowDraft.kind.human' | 'master.flowDraft.kind.review'> = {
  ai_do: 'master.flowDraft.kind.ai', human_do: 'master.flowDraft.kind.human', human_review: 'master.flowDraft.kind.review',
}

export function FlowDraftCard({ draft, teams, busy, connected, onOpen, onAskComponent, askDisabled = false, asks = {}, onOpenCandidate, t }: {
  draft: FlowDraft
  /** Saved teams (name = id sent, label = shown); the draft's own team is chosen first when it is one of them. */
  teams: ReadonlyArray<{ name: string; label: string }>
  busy: boolean
  connected: boolean
  /** Absent = a console that cannot open it (not the owner's): the card is shown without the action. */
  onOpen?: (team: string) => void
  /** 让安安起草这个组件 (4.7 A05): send 安安 the request for the missing component (componentAskText) through the
   *  ordinary conversation. 安安 may only draft an inert candidate from it: nothing is tested, approved, registered
   *  or enabled. Absent = no such button. */
  onAskComponent?: (request: string) => void
  /** A turn is running: the request would not go out now. */
  askDisabled?: boolean
  /** Per step id, what became of its request (componentAskStates). */
  asks?: Record<string, ComponentAskState>
  /** Open where the drafted candidate is tested and reviewed. Absent = the state is shown without the way there. */
  onOpenCandidate?: (candidateId: string) => void
  t: Translate
}) {
  const [team, setTeam] = useState(teams.some(item => item.name === draft.team) ? draft.team : teams.length === 1 ? teams[0].name : '')
  const gaps = flowDraftGaps(draft)
  const component = (stepId: string) => draft.matches.find(match => match.step === stepId)
  const gapFor = (stepId: string) => draft.missing.find(item => item.step === stepId)
  return (
    <div className="master-flow-draft" data-testid="flow-draft-card">
      <strong>{t('master.flowDraft.title', { name: draft.name })}</strong>
      {draft.summary && <span>{draft.summary}</span>}
      <ol className="master-flow-draft-steps">
        {draft.steps.map(step => {
          const match = component(step.id)
          const gap = gapFor(step.id)
          const ask = gap ? asks[step.id] : undefined
          return (
            <li key={step.id} data-step={step.id}>
              <b>{step.name}</b>
              <small> · {t(KIND_KEY[step.type] ?? 'master.flowDraft.kind.ai')}</small>
              {match && <small className="is-ok"> · {t('master.flowDraft.component', { name: match.name || match.component })}</small>}
              {gap && <small className="is-gap"> · {t('master.flowDraft.placeholder', { capability: gap.capability })}</small>}
              {ask?.state === 'drafted' && (
                <>
                  <small className="is-ok" data-testid="flow-draft-candidate" data-candidate={ask.candidateId}> · {t('master.flowDraft.candidateDrafted')}</small>
                  {onOpenCandidate && !ask.kind && (
                    <button type="button" className="master-flow-draft-ask" data-testid="flow-draft-open-candidate" onClick={() => onOpenCandidate(ask.candidateId)}>
                      {t('master.flowDraft.openCandidate')} →
                    </button>
                  )}
                </>
              )}
              {ask?.state === 'asked' && <small className="master-flow-draft-source" data-testid="flow-draft-asked"> · {t(ASK_SENT_KEY[missingKind(draft, step.id)])}</small>}
              {gap && ask?.state !== 'drafted' && onAskComponent && (
                <button type="button" className="master-flow-draft-ask" data-testid="flow-draft-ask-component" disabled={askDisabled}
                  onClick={() => { const request = componentAskText(draft, step.id, t); if (request) onAskComponent(request) }}>
                  {t('master.flowDraft.askComponentButton')}
                </button>
              )}
              {!step.role && <small className="is-gap"> · {t('master.flowDraft.noRole')}</small>}
              {draft.sources[step.id] && <small className="master-flow-draft-source"> · {t('master.flowDraft.source', { source: draft.sources[step.id] })}</small>}
            </li>
          )
        })}
      </ol>
      {gaps.length > 0 ? (
        <div className="master-flow-draft-gaps" data-testid="flow-draft-gaps">
          <span>{t('master.flowDraft.gaps', { n: gaps.length })}</span>
          <ul>
            {gaps.map((gap, index) => (
              <li key={`${gap.kind}-${index}`}>{t(`master.flowDraft.gap.${gap.kind}` as Parameters<Translate>[0], { step: gap.step, detail: gap.detail })}</li>
            ))}
          </ul>
        </div>
      ) : <span className="master-flow-draft-ready">{t('master.flowDraft.noGaps')}</span>}
      {draft.assumptions.length > 0 && <span className="master-flow-draft-note">{t('master.flowDraft.assumptions', { list: draft.assumptions.join('；') })}</span>}
      <span className="master-node-proposal-hint">{t('master.flowDraft.notCreated')}</span>
      {onOpen && (
        <div className="master-flow-draft-actions">
          <select aria-label={t('master.flowDraft.team')} value={team} onChange={event => setTeam(event.target.value)}>
            <option value="">{teams.length ? t('master.flowDraft.teamPick') : t('workflows.blank.teamNone')}</option>
            {teams.map(item => <option key={item.name} value={item.name}>{item.label}</option>)}
          </select>
          <button type="button" className="master-open-order-btn" data-testid="flow-draft-open" disabled={!connected || !team || busy}
            onClick={() => onOpen(team)}>
            {busy ? t('master.flowDraft.opening') : `${t('master.flowDraft.open')} →`}
          </button>
        </div>
      )}
    </div>
  )
}
