import type { FlowComponentCandidate, FlowDraftReference } from '../lib/flowComponentCandidate'
import type { ReactNode } from 'react'

/** The preview is inert; optional adoption is an owner action on original server sources. */
export function FlowComponentCandidateCard({ candidate, disabled = false, onContinue, onAdopt, adoptionControl }: {
  candidate: FlowComponentCandidate
  disabled?: boolean
  onContinue?: (source: FlowDraftReference) => void
  onAdopt?: (candidateId: string, source: FlowDraftReference) => void
  adoptionControl?: ReactNode
}) {
  return <section className="master-flow-draft" data-testid="flow-component-candidate" data-kind={candidate.kind}>
    <strong>{candidate.kind === 'skill' ? '技能草稿' : '子流程草稿'} · {candidate.capability}</strong>
    <span>待编辑与测试</span>
    {candidate.kind === 'skill' && Object.entries(candidate.payload.files ?? {}).map(([name, text]) =>
      <details key={name}><summary>{name === 'SKILL.md' ? '工作方法与职责' : '测试范围与交付标准'}</summary><pre>{text}</pre></details>)}
    {candidate.kind === 'flow' && <span>子流程的步骤在本条回复的流程草稿卡中，打开后仍需保存、测试与准入。</span>}
    {adoptionControl}
    <div className="master-flow-draft-actions">
      {onAdopt && <button type="button" disabled={disabled} onClick={() => onAdopt(candidate.candidate_id, candidate.source)}>采用为草稿</button>}
      {onContinue && <button type="button" disabled={disabled} onClick={() => onContinue(candidate.source)}>继续原流程</button>}
    </div>
  </section>
}
