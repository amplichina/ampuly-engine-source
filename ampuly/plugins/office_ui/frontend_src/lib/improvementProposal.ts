// 4.7-A11 改进提案: 提一个改进 on a verified asset's detail, the candidate it makes, and the approval judged on real
// orders. Kept free of React so the contract tests hold the rules without a DOM.
//
// The owner picks what to improve (the flow, one of its skills, one of the project's connectors) and says it in one
// sentence; the console drafts an inert candidate and names the editor it opens in. Whether the change paid off is
// not a number anybody types: the console compares the last N finished real orders before and after, and a key
// measure that got worse refuses the approval with the measure named.

export type ImprovementTargetKind = 'process' | 'skill' | 'connector'

export interface ImprovementTarget {
  kind: ImprovementTargetKind
  id: string
  /** What the owner reads: the flow's, skill's or connector's name. */
  label: string
}

export const TARGET_KIND_WORDS: Record<ImprovementTargetKind, readonly [string, string]> = {
  process: ['这条流程', 'This flow'],
  skill: ['技能', 'Skill'],
  connector: ['连接器', 'Connector'],
}

export const REQUEST_LIMIT = 200
export const DEFAULT_SAMPLE = 5
export const MAX_SAMPLE = 50

export interface ImprovementBenefitMetric {
  name: string
  label: string
  unit: string
  judged: boolean
  baseline?: number
  candidate?: number
  change_ratio?: number | null
  worse?: boolean
  better?: boolean
  note?: string
}

export interface ImprovementBenefit {
  verdict: 'insufficient' | 'negative' | 'neutral' | 'positive' | string
  reason: string
  sample_size: number
  threshold: number
  basis: string
  metrics: ImprovementBenefitMetric[]
  worsened: string[]
  improved: string[]
  evaluated_at: string
  baseline_count: number
  candidate_count: number
}

/** One row of the console's improvement_proposal_list (the fields the page reads). */
export interface ImprovementProposalRow {
  proposal_id: string
  proposal_fingerprint: string
  status: 'draft' | 'materialized' | 'approved' | 'rejected' | string
  title: string
  target: { kind?: string; id?: string; current_fingerprint?: string }
  candidate_kind: string
  evaluation_spec_fingerprint: string
  evaluation_verdict: string
  candidate_id?: string
  candidate_status?: string
  benefit?: ImprovementBenefit | null
  decision_reason?: string
}

/** Where a started improvement's candidate is edited. */
export interface ImprovementEditor {
  kind: ImprovementTargetKind
  workflow_id?: string
  name?: string
  skill_id?: string
  candidate_id?: string
  connector?: string
}

export interface ImprovementStart {
  proposal: ImprovementProposalRow
  editor: ImprovementEditor
  /** Step settings a process description cannot carry, in the owner's words: set them again on the builder. */
  not_carried: string[]
}

/** What an improvement on one published flow can be about: the flow itself, a skill it uses, a project connector. */
export function flowImprovementTargets(
  flow: { id: string; name: string; skill_contracts?: Array<{ id: string }>; nodes?: Array<{ skills?: string[] }> },
  connectors: ReadonlyArray<{ connector: string; status?: string }> = [],
  /** The internal library's names, so the picker shows 品牌简报 rather than the skill's id (when the library is loaded). */
  skillNames: ReadonlyArray<{ id: string; name: string }> = [],
): ImprovementTarget[] {
  const skills = new Set<string>()
  for (const contract of flow.skill_contracts ?? []) if (contract.id) skills.add(contract.id)
  if (skills.size === 0) for (const node of flow.nodes ?? []) for (const skill of node.skills ?? []) if (skill) skills.add(skill)
  const names = new Map(skillNames.map(item => [item.id, item.name]))
  return [
    { kind: 'process', id: flow.id, label: flow.name || flow.id },
    ...[...skills].map(id => ({ kind: 'skill' as const, id, label: names.get(id) || id })),
    ...connectors.filter(row => (row.status ?? 'registered') === 'registered' && row.connector)
      .map(row => ({ kind: 'connector' as const, id: row.connector, label: row.connector })),
  ]
}

/** Why the one sentence cannot be sent yet, or null when it can. */
export function requestProblem(text: string): string | null {
  const clean = String(text ?? '').split(/\s+/).filter(Boolean).join(' ')
  if (!clean) return '写一句话说清楚要改进什么。'
  if (clean.length > REQUEST_LIMIT) return `一句话说清楚就行（${REQUEST_LIMIT} 字以内）。`
  return null
}

export function sampleSizeOf(value: string | number): number | null {
  const count = Number(value)
  return Number.isInteger(count) && count >= 1 && count <= MAX_SAMPLE ? count : null
}

/** Where a listed proposal's candidate is edited (null before it has one). The console calls a process target "workflow". */
export function editorOfProposal(row: Pick<ImprovementProposalRow, 'target' | 'candidate_id'>): ImprovementEditor | null {
  const candidate = String(row.candidate_id ?? '')
  const id = String(row.target?.id ?? '')
  if (!candidate || !id) return null
  const kind = String(row.target?.kind ?? '')
  if (kind === 'workflow' || kind === 'process') return { kind: 'process', workflow_id: candidate }
  if (kind === 'skill') return { kind: 'skill', skill_id: id, candidate_id: candidate }
  if (kind === 'connector') return { kind: 'connector', connector: id, candidate_id: candidate }
  return null
}

/** Where the candidate is edited, in words. */
export function editorWords(editor: ImprovementEditor): readonly [string, string] {
  if (editor.kind === 'process') return [`已生成流程草稿「${editor.name || editor.workflow_id || ''}」：去搭建台改。`, `A draft flow was made (${editor.name || editor.workflow_id || ''}): edit it on the builder.`]
  if (editor.kind === 'skill') return ['已生成技能的候选新版本：去组件工作台的「测试」里看改了什么、试一次。', 'A candidate version of the skill was made: see it under Test in the component workbench.']
  return ['已生成连接器的候选草稿：去能力中心的「连接器候选」里测试和审查。', 'A draft connector candidate was made: test and review it under connector candidates.']
}

export const PROPOSAL_STATUS_WORDS: Record<string, readonly [string, string]> = {
  draft: ['草稿', 'Draft'],
  materialized: ['候选已生成', 'Candidate created'],
  approved: ['已批准', 'Approved'],
  rejected: ['已打回', 'Rejected'],
}

export type ProposalNext = 'evaluate' | 'approve' | 'none'

/** What the owner can do next with a proposal, and the words for why the rest is closed. */
export function proposalNext(row: Pick<ImprovementProposalRow, 'status' | 'benefit'>): { next: ProposalNext; note: readonly [string, string] | null } {
  if (row.status === 'approved') return { next: 'none', note: ['已批准。候选还要走它自己的发布：流程在搭建台提交测试并发布，技能在组件工作台暂存并启用。', 'Approved. The candidate still goes through its own release.'] }
  if (row.status === 'rejected') return { next: 'none', note: ['已打回。', 'Rejected.'] }
  if (row.status !== 'materialized') return { next: 'none', note: ['还没生成候选。', 'No candidate yet.'] }
  const verdict = String(row.benefit?.verdict ?? '')
  if (!verdict) return { next: 'evaluate', note: ['还没有按真实订单评估收益：批准时会先评估一次。', 'Not yet judged on real orders: approving judges it first.'] }
  if (verdict === 'insufficient') return { next: 'evaluate', note: [row.benefit?.reason || '样本不够，不能判定收益。', 'Too few finished orders to judge.'] }
  return { next: 'approve', note: null }
}

function amount(value: number | undefined, name: string): string {
  const number = Number(value ?? 0)
  const digits = name === 'model_cost' ? 4 : name === 'duration_hours' ? 1 : 2
  return number.toFixed(digits)
}

/** One line per measure, in the owner's words: before → after, and whether it got worse or better. */
export function benefitLines(benefit: ImprovementBenefit | null | undefined): string[] {
  if (!benefit) return []
  return (benefit.metrics ?? []).map(metric => {
    if (!metric.judged) return `${metric.label}：${metric.note || '读不到，没有参与判断'}`
    const trend = metric.worse ? '（变差）' : metric.better ? '（变好）' : ''
    return `${metric.label}：平均每单 ${amount(metric.baseline, metric.name)} → ${amount(metric.candidate, metric.name)} ${metric.unit}${trend}`
  })
}

// What the console answers when it refuses, for the few answers that are not already in the owner's words.
const REFUSALS: ReadonlyArray<readonly [RegExp, string]> = [
  [/owner_required/, '只有负责人能做这一步。'],
  [/fingerprint changed; (reload|regenerate)|target fingerprint changed/i, '内容在这之后变过：刷新后再操作。'],
  [/proposal is not materializable|only a materialized proposal/i, '这个改进提案现在的状态做不了这一步：刷新看看。'],
  [/already uses another evaluation spec/i, '这个改进已经定过运行会话双跑的标准，不能再改。'],
  [/has no frozen evaluation spec/i, '还没定运行会话双跑的标准。'],
  [/too few|smaller than the frozen minimum/i, '会话数少于标准要求的样本数。'],
  [/one binding, one turn and one terminal event|event chain is inconsistent|terminal event/i, '有会话没有按要求正好跑完一轮（先登记、再开跑、再结束）。'],
  [/belongs to another project/i, '有会话属于别的项目。'],
  [/already has an evaluation binding/i, '有会话已经登记过，不能再登记。'],
  [/not found|does not exist/i, '找不到了：刷新后再试。'],
  [/Harness registry is unavailable/i, '这台控制台没有接上运行记录，做不了运行会话双跑。'],
  [/secret/i, '内容里有像密钥的字符串：删掉再提。'],
]

/** A refusal the owner can read: the console's own words when it already gave them, else the known ones translated. */
export function improvementRefusal(message: string): string {
  const text = String(message ?? '').trim()
  if (!text) return '这一步没有做成。'
  if (/[一-鿿]/.test(text)) return text
  for (const [pattern, words] of REFUSALS) if (pattern.test(text)) return words
  return '这一步没有做成：刷新后再试一次。'
}
