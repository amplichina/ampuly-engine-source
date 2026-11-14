// 组件 · 查看: what the console's component_detail returns for one internal-library skill, and the grouping the
// read-only detail shows it in. Kept free of React so the contract tests can hold the rules without a DOM.

export interface ComponentDetailUse {
  workflow_id: string
  workflow_name: string
  status: string
  node_id: string
  node_name: string
  /** The version this step runs the skill at (pinned on the step, else the version its package locks). */
  pinned_version: string
  /** The step carries parameters of its own for this skill; they never change the skill itself. */
  has_params: boolean
}

export interface ComponentDetail {
  kind: 'skill'
  id: string
  name: string
  summary: string
  version: string
  contract: { id: string; version: string; provenance: Record<string, unknown>; tests: string[]; success_criteria: string[]; complete: boolean; problems: string[] }
  admission: Record<string, unknown> | null
  license: { declared: string | null; file: boolean; class: 'permissive' | 'restricted' | 'unknown' }
  requires: { tools: string[]; bins: string[]; env: string[] }
  files: Array<{ path: string; bytes: number }>
  files_truncated: boolean
  body: string
  body_truncated: boolean
  used_on: ComponentDetailUse[]
}

export interface DetailFlowGroup {
  workflowId: string
  workflowName: string
  status: string
  steps: ComponentDetailUse[]
  /** More than one version of the skill is in use inside this one flow. */
  mixedVersions: boolean
}

/** The steps using a skill, one group per flow, in the order the console listed them. */
export function groupUsesByFlow(uses: readonly ComponentDetailUse[]): DetailFlowGroup[] {
  const groups = new Map<string, DetailFlowGroup>()
  for (const use of uses) {
    let group = groups.get(use.workflow_id)
    if (!group) {
      group = { workflowId: use.workflow_id, workflowName: use.workflow_name || use.workflow_id, status: use.status, steps: [], mixedVersions: false }
      groups.set(use.workflow_id, group)
    }
    group.steps.push(use)
  }
  for (const group of groups.values()) {
    group.mixedVersions = new Set(group.steps.map(step => step.pinned_version)).size > 1
  }
  return [...groups.values()]
}

/** Steps whose pinned version is not the version the library holds now: they keep running what they pinned. */
export function stepsBehindLibrary(detail: Pick<ComponentDetail, 'version' | 'used_on'>): ComponentDetailUse[] {
  const current = String(detail.version ?? '').trim()
  if (!current) return []
  return detail.used_on.filter(use => use.pinned_version && use.pinned_version !== current)
}

// 组件 · 编辑 / 测试 / 发布 (4.7 A2-4): the console's component_versions answer and what each candidate may do next.
// 4.7-A03: the whole chain in order -- 测试 → (没通过就修订) → 双跑评估 → 批准 → 暂存 → 启用 → 回滚 -- with the server's
// own reason, in words, whenever approval is not open.

export interface ComponentCandidateHistory { at: string; actor: string; action: string; from: string; to: string; reason: string }

export interface ComponentEvaluationStandard {
  hypothesis: string
  min_samples: number
  metrics: Array<{ name: string; label: string; direction: string; min_delta: string }>
}

export interface ComponentEditFields { version: string; description: string; instructions: string; success_criteria: string[]; test_plan: string[] }

export interface ComponentCandidate {
  candidate_id: string
  candidate_fingerprint: string
  skill_id: string
  version: string
  current_version: string
  status: 'draft' | 'test_passed' | 'test_failed' | 'approved' | 'rejected' | string
  created_at: string
  tested_at: string
  reviewed_at: string
  /** A dual-run comparison is on record for this candidate. */
  evaluation_bound: boolean
  source: 'owner_edit' | 'improvement_proposal' | string
  /** Made against the live skill as it is now; a stale one can no longer be staged. */
  current: boolean
  test_note?: string
  diff: string
  diff_truncated: boolean
  activation: { deployment_fingerprint: string; status: string; staged_at: string; activated_at: string; rolled_back_at: string } | null
  /** The frozen dual-run standard is bound (it cannot change after that). */
  evaluation_spec_bound?: boolean
  evaluation_standard?: ComponentEvaluationStandard | null
  evaluation_verdict?: 'passed' | 'failed' | '' | string
  /** The recorded comparison in one line. */
  evaluation_summary?: string
  /** Why approval is not open now, in the owner's words; '' when it is (or the candidate is decided). */
  approval_blocker?: string
  /** The candidate this one revises / the one that revised it. */
  revises?: string
  superseded_by?: string
  history?: ComponentCandidateHistory[]
  /** The candidate's own text, which a revision starts from. */
  edit?: ComponentEditFields | null
}

export interface ComponentVersions {
  skill_id: string
  /** The live skill's tree fingerprint: an edit is made against exactly this. */
  fingerprint: string
  candidates: ComponentCandidate[]
}

export type CandidateNext = 'test' | 'revise' | 'evaluation_spec' | 'evaluation_run' | 'review' | 'stage' | 'activate' | 'rollback' | 'none'

/** The one thing a candidate can have done to it next, and why the rest is not open (kept on the card, in words). */
export function candidateNext(candidate: ComponentCandidate): { next: CandidateNext; note: readonly [string, string] | null } {
  const activation = candidate.activation
  const blocker = String(candidate.approval_blocker ?? '').trim()
  const because = (fallback: string): readonly [string, string] => [blocker || fallback, blocker || fallback]
  if (activation) {
    if (activation.status === 'staged') return { next: 'activate', note: null }
    if (activation.status === 'active') return { next: 'rollback', note: null }
    if (activation.status === 'rolled_back') return { next: 'none', note: ['已回滚到上一版。', 'Rolled back to the previous version.'] }
    return { next: 'none', note: [`切换中（${activation.status}）。`, `Switching (${activation.status}).`] }
  }
  const verdict = String(candidate.evaluation_verdict ?? '')
  if (candidate.status === 'draft') return { next: 'test', note: null }
  if (candidate.status === 'test_failed') {
    if (candidate.superseded_by) return { next: 'none', note: ['测试没通过，已修订成新候选：在新候选上继续。', 'The test did not pass; it was revised into a new candidate: continue there.'] }
    return { next: 'revise', note: because('测试没通过，不能批准：修订成新候选后再测。') }
  }
  if (candidate.status === 'test_passed') {
    if (verdict === 'failed') {
      if (candidate.superseded_by) return { next: 'none', note: ['双跑评估没通过，已修订成新候选：在新候选上继续。', 'The dual run did not pass; it was revised into a new candidate: continue there.'] }
      return { next: 'revise', note: because('双跑评估没通过，不能批准：修订成新候选后再测。') }
    }
    if (verdict === 'passed' || candidate.evaluation_bound) {
      return blocker ? { next: 'none', note: because(blocker) } : { next: 'review', note: null }
    }
    if (!candidate.evaluation_spec_bound) return { next: 'evaluation_spec', note: because('还没定双跑评估标准，不能批准。') }
    return { next: 'evaluation_run', note: because('双跑评估还没做，不能批准。') }
  }
  if (candidate.status === 'rejected') return { next: 'none', note: ['已拒绝。', 'Rejected.'] }
  if (candidate.status === 'approved') {
    if (!candidate.current) return { next: 'none', note: ['技能在这之后被改过：这个候选不能再暂存，基于现在的版本重新编辑。', 'The skill changed after this was made: it cannot be staged; edit the current version again.'] }
    if (!candidate.evaluation_bound || verdict === 'failed') return { next: 'none', note: ['这个候选是在双跑评估接通之前批准的，没有它自己的评估记录，不能暂存：在「契约」里基于现在的版本重新编辑一个候选。', 'This was approved before the dual run was wired and has no evaluation of its own, so it cannot be staged: edit the current version again under Contract.'] }
    return { next: 'stage', note: null }
  }
  return { next: 'none', note: null }
}

export type CandidateStep = 'test' | 'evaluation' | 'review' | 'stage' | 'activate' | 'rollback'
export type CandidateStepState = 'done' | 'current' | 'blocked' | 'waiting'
export const CANDIDATE_STEPS: ReadonlyArray<readonly [CandidateStep, string, string]> = [
  ['test', '测试', 'Test'], ['evaluation', '双跑评估', 'Dual run'], ['review', '批准', 'Approve'],
  ['stage', '暂存', 'Stage'], ['activate', '启用', 'Activate'], ['rollback', '回滚', 'Roll back'],
]
const STEP_OF: Partial<Record<CandidateNext, CandidateStep>> = {
  test: 'test', evaluation_spec: 'evaluation', evaluation_run: 'evaluation', review: 'review', stage: 'stage', activate: 'activate', rollback: 'rollback',
}

/** Where one candidate stands on the chain: each step done, the one open now, the one it is stuck at, or still ahead. */
export function candidateLadder(candidate: ComponentCandidate): Array<{ step: CandidateStep; label: readonly [string, string]; state: CandidateStepState }> {
  const { next } = candidateNext(candidate)
  const activation = candidate.activation
  const verdict = String(candidate.evaluation_verdict ?? '')
  const decided = candidate.status === 'approved' || candidate.status === 'rejected'
  const done: Record<CandidateStep, boolean> = {
    test: candidate.status === 'test_passed' || decided,
    evaluation: verdict === 'passed',
    review: candidate.status === 'approved',
    stage: Boolean(activation),
    activate: Boolean(activation && ['active', 'rolling_back', 'rolled_back'].includes(activation.status)),
    rollback: activation?.status === 'rolled_back',
  }
  const blocked: Record<CandidateStep, boolean> = {
    test: candidate.status === 'test_failed',
    evaluation: verdict === 'failed' || (candidate.status === 'approved' && verdict !== 'passed'),
    review: candidate.status === 'rejected',
    stage: candidate.status === 'approved' && !activation && !candidate.current,
    activate: false,
    rollback: false,
  }
  const current = STEP_OF[next]
  return CANDIDATE_STEPS.map(([step, zh, en]) => ({
    step, label: [zh, en] as const,
    state: done[step] ? 'done' : step === current ? 'current' : blocked[step] ? 'blocked' : 'waiting',
  }))
}

/** The measures a dual run can take from the run records themselves; the value is sent, the words are shown. */
export const DUAL_RUN_METRICS: ReadonlyArray<readonly [string, string, string]> = [
  ['completion_rate', '完成率（越高越好）', 'Completion rate (higher is better)'],
  ['artifact_rate', '交出产物的比例（越高越好）', 'Share with a deliverable (higher is better)'],
  ['failure_rate', '失败率（越低越好）', 'Failure rate (lower is better)'],
  ['human_pause_rate', '停下来等人的比例（越低越好）', 'Share that stopped for a person (lower is better)'],
]

// What the console answers when it refuses, for the few answers that are not already in the owner's words.
const REFUSALS: ReadonlyArray<readonly [RegExp, string]> = [
  [/owner_required/, '只有负责人能做这一步。'],
  [/fingerprint changed; (reload|regenerate)|target fingerprint changed/i, '内容在这之后变过：刷新后再操作。'],
  [/can receive its test verdict/i, '这个候选已经记过测试结论，结论不能改；没通过就修订成新候选再测。'],
  [/test outcome must be passed or failed/i, '测试结论只能是通过或没通过。'],
  [/only a test-passed skill candidate can be reviewed/i, '只有测试通过的候选才能审批。'],
  [/opposite owner decision/i, '这个候选已经有相反的审批结论，不能改。'],
  [/what was checked/i, '写明试了什么、看到了什么（至少 8 个字）。'],
  [/only an approved skill candidate can be staged/i, '只有批准过的候选才能暂存。'],
  [/requires a bound dual-run evaluation/i, '暂存前要有这个候选自己的双跑评估记录。'],
  [/requires a passed dual-run evaluation/i, '双跑评估没通过，不能暂存。'],
  [/live skill (changed|drifted)|active skill drifted/i, '技能在这之后被别处改过：不能继续，基于现在的版本重新编辑。'],
  [/identical to the live skill/i, '新版本和正在使用的技能完全一样，不用暂存。'],
  [/another process is changing this skill/i, '另一个操作正在改这个技能：稍后再试。'],
  [/reload before (activation|rollback)/i, '暂存记录在这之后变过：刷新后再操作。'],
  [/cannot start in state|explicit recovery is required|unknown filesystem state/i, '现在的状态做不了这一步：刷新看看最新状态。'],
  [/has no frozen evaluation spec/i, '还没定双跑评估标准。'],
  [/too few|smaller than the frozen minimum/i, '样本数少于评估标准的要求。'],
  [/one binding, one turn and one terminal event|event chain is inconsistent/i, '有会话没有按要求正好跑完一轮（先登记、再开跑、再结束）。'],
  [/binding differs from the frozen spec/i, '有会话登记的不是这套评估标准，或者新旧分组放反了。'],
  [/unknown outcome|without a known outcome/i, '有会话的结果未知，不能拿来评估。'],
  [/belongs to another project/i, '有会话属于别的项目。'],
  [/already has an evaluation binding/i, '有会话已经登记过，不能再登记。'],
  [/before the first turn/i, '会话要在第一轮开跑之前登记。'],
  [/only an active idle session/i, '只有还开着、没在跑的会话才能登记。'],
  [/not found|does not exist/i, '有会话或候选找不到了：刷新后再试。'],
  [/session ids must be unique|1-1000 named/i, '会话编号要各不相同，每组 1 到 1000 个。'],
  [/cannot derive metric|evidence contract/i, '有指标不能从运行记录里算出来。'],
  [/greater than the current version/i, '新版本号要比现在的版本大。'],
  [/semantic x\.y\.z/i, '版本号要写成 1.2.3 这样的三段数字。'],
  [/secret-shaped/i, '内容里有像密钥的字符串：删掉再提交。'],
  [/success_criteria|test_plan/i, '成功标准和测试计划各要写 1 到 20 条。'],
  [/carries version, description/i, '只能改版本号、用途、说明、成功标准和测试计划。'],
  [/is required and must be at most/i, '有必填的内容没写，或者写得太长。'],
]

/** A refusal the owner can read: the console's own words when it already gave them, else the known ones translated. */
export function componentRefusal(message: string): string {
  const text = String(message ?? '').trim()
  if (!text) return '这一步没有做成。'
  if (/[一-鿿]/.test(text)) return text
  for (const [pattern, words] of REFUSALS) if (pattern.test(text)) return words
  return '这一步没有做成：刷新后再试一次。'
}

/** The next minor version: 1.2.0 → 1.3.0. An unreadable one is left for the person to write. */
export function nextMinor(version: string): string {
  const match = String(version ?? '').trim().match(/^(\d+)\.(\d+)\.(\d+)$/)
  return match ? `${match[1]}.${Number(match[2]) + 1}.0` : ''
}

/** SKILL.md without its front matter: the instructions an edit starts from. */
export function skillInstructions(body: string): string {
  const text = String(body ?? '')
  if (!text.startsWith('---')) return text.trim()
  const end = text.indexOf('\n---', 3)
  return end < 0 ? text.trim() : text.slice(end + 4).trim()
}

/** A file size a person reads: 812 B, 3.4 KB, 1.2 MB. */
export function readableBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// 能启用了吗 (frontend redesign S3-5, policy): the design's 「测过才出现验收准入」 for a component, worded as the
// question the dialog title asks. Four yes / no rows read off the same ladder the card shows; only the title is new.
// It is offered once the candidate has a test verdict, and it never opens anything the chain does not.

export type CanActivateAnswer = 'yes' | 'no' | 'not_yet'
export interface CanActivateRow {
  step: Extract<CandidateStep, 'test' | 'evaluation' | 'review' | 'stage'>
  question: readonly [string, string]
  answer: readonly [string, string]
  state: CanActivateAnswer
}

const CAN_ACTIVATE_QUESTIONS: ReadonlyArray<readonly [CanActivateRow['step'], readonly [string, string], Record<CanActivateAnswer, readonly [string, string]>]> = [
  ['test', ['测试通过了吗', 'Did the test pass?'], { yes: ['通过了', 'Yes'], no: ['没通过：修订成新候选再测', 'No: revise into a new candidate and test again'], not_yet: ['还没测', 'Not yet'] }],
  ['evaluation', ['双跑评估通过了吗', 'Did the dual run pass?'], { yes: ['通过了', 'Yes'], no: ['没通过：修订成新候选再测', 'No: revise into a new candidate and test again'], not_yet: ['还没做', 'Not yet'] }],
  ['review', ['批准了吗', 'Is it approved?'], { yes: ['已批准', 'Yes'], no: ['已拒绝', 'No: rejected'], not_yet: ['还没批', 'Not yet'] }],
  ['stage', ['暂存了吗', 'Is it staged?'], { yes: ['已暂存', 'Yes'], no: ['不能暂存：技能在这之后被改过', 'No: the skill changed since'], not_yet: ['还没暂存', 'Not yet'] }],
]

/** Whether the question is asked yet (a verdict exists), its four rows, and whether every answer is yes -- which is when 启用 is open. */
export function canActivateRows(candidate: ComponentCandidate): { tested: boolean; rows: CanActivateRow[]; ready: boolean } {
  const tested = candidate.status !== 'draft'
  const ladder = new Map(candidateLadder(candidate).map(row => [row.step, row.state]))
  const rows = CAN_ACTIVATE_QUESTIONS.map(([step, question, answers]) => {
    const state: CanActivateAnswer = ladder.get(step) === 'done' ? 'yes' : ladder.get(step) === 'blocked' ? 'no' : 'not_yet'
    return { step, question, answer: answers[state], state }
  })
  return { tested, rows, ready: tested && rows.every(row => row.state === 'yes') }
}
