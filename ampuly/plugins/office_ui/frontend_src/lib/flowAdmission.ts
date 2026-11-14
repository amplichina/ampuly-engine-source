// 搭建台's 测试 and 验收准入 (4.7 A2-2): what each says about a flow, from the package's own facts. Pure, so the two
// dialogs cannot drift from the package card that performs the same transitions (提交测试, 申请发布审批, 批准, 发布).
// Saving, testing and admitting are three different things: a save writes the draft; a test produces evidence about
// a saved version; admission (submit for testing → release approval → publish) is what lets a project adopt it.

import { PREVIEW_STATUS_LABELS, previewStatus, type PreviewStatus } from '../workspace/flowPreview'

type TestRecordLike = { outcome: string; execution_mode?: string; date?: string } | null | undefined
type FlowLike = {
  status: string
  version: string
  draft_revision?: string
  latest_test: TestRecordLike
  release_ready?: boolean
  release_blockers?: string[]
  release_approval?: { status?: string } | null
}
type StepLike = { id: string; name?: string; type?: string; depends_on?: readonly string[] }
type TrialLike = { node_id: string; state: string; workflow_version?: string }

export type AdmissionStage = 'draft' | 'testing' | 'awaiting_approval' | 'approved' | 'published' | 'retired'
export type AdmissionAction = 'submit_testing' | 'request_release' | 'decide_release' | 'publish' | 'none'

export const ADMISSION_TRACK: readonly AdmissionStage[] = ['draft', 'testing', 'awaiting_approval', 'approved', 'published']
export const ADMISSION_STAGE_LABELS: Record<AdmissionStage, readonly [string, string]> = {
  draft: ['草案', 'Draft'],
  testing: ['测试中', 'Testing'],
  awaiting_approval: ['待审批', 'Awaiting approval'],
  approved: ['已批准', 'Approved'],
  published: ['已发布', 'Published'],
  retired: ['已停用', 'Retired'],
}

export interface AdmissionView {
  stage: AdmissionStage
  track: Array<{ stage: AdmissionStage; state: 'done' | 'current' | 'todo' }>
  next: AdmissionAction
  /** The next transition may be asked for now. The server checks again; this only says what the page knows. */
  ready: boolean
  /** Why it may not, in the owner's words, most blocking first. */
  blockers: Array<readonly [string, string]>
}

// The package's release blockers are the server's own sentences (English, for logs and tests). The owner reads them
// in Chinese: the known shapes are said again in words; anything else is shown as the server wrote it.
const BLOCKER_WORDS: Array<[RegExp, (match: RegExpMatchArray) => string]> = [
  [/^the latest test record must have outcome: passed$/, () => '最近一次测试记录必须是「通过」：先在「测试」里试跑，再记录一次通过的结果。'],
  [/simulated-only/, () => '最近一次通过的只是合同模拟：需要真实模型或人工的试跑记录。'],
  [/^skill (\S+) has incomplete release contract$/, match => `技能 ${match[1]} 的发布合同不完整。`],
  [/^Skill contract tests are not fully executable: (.+)$/, match => `技能合同测试没有全部可执行（${match[1]}）。`],
  [/^skill_versions must lock production skill versions$/, () => '需要锁定生产用的技能版本（skill_versions）。'],
  [/^skill_versions\.(\S+) does not match SKILL\.md version (\S+)$/, match => `锁定的技能 ${match[1]} 版本与技能文件里的 ${match[2]} 不一致。`],
  [/^release approval is pending$/, () => '发布审批还在等待决定。'],
  [/^release approval was rejected$/, () => '上次的发布审批被拒绝了：改好后重新申请。'],
  [/^workflow status is (\S+)$/, match => `流程当前状态是「${match[1]}」。`],
]

export function blockerWords(text: string): readonly [string, string] {
  for (const [pattern, say] of BLOCKER_WORDS) {
    const match = text.match(pattern)
    if (match) return [say(match), text]
  }
  return [text, text]
}

/** A 提交测试 / 申请发布审批 the server refused, as the page keeps it beside the button until the next action. */
export interface AdmissionRefusal {
  workflowId: string
  action: 'submit_testing' | 'request_release'
  /** The server's sentence, as sent. */
  error: string
}

const STAGE_WORD: Record<string, string> = { draft: '草案', testing: '测试中', published: '已发布', retired: '已停用' }

// What the server says when 提交测试 or 申请发布审批 is refused (workflow_packages.submit_for_testing /
// request_release_approval), in the owner's words. Same rule as BLOCKER_WORDS: a known shape is said again in
// Chinese, anything else is shown as written -- never nothing, since a refusal nobody can read is a refusal nobody can act on.
const REFUSAL_WORDS: Array<[RegExp, (match: RegExpMatchArray) => string]> = [
  [/^workflow '[^']*' is (\S+), not draft$/, match => `这条流程现在是「${STAGE_WORD[match[1]] ?? match[1]}」，不是草案，不能提交测试。`],
  [/^workflow '[^']*' is (\S+), not testing$/, match => `这条流程现在是「${STAGE_WORD[match[1]] ?? match[1]}」，不在测试中，不能申请发布审批。`],
  [/^workflow '[^']*' not found$/, () => '这条流程已经不存在了，请刷新页面。'],
  [/^workflow is invalid: (.+)$/, match => `流程还有没通过的检查：${match[1]}`],
  [/^workflow needs a passed test record before testing submission$/, () => '还没有通过的试跑记录：先在「测试」里试跑，再记录一次通过的结果。'],
  [/^simulated-only test evidence cannot submit a workflow for testing$/, () => '最近一次通过的只是合同模拟，不能作为提交测试的证据：需要真实模型或人工的试跑记录。'],
  [/^latest passed test lacks workflow version or manifest evidence$/, () => '最近一次通过的试跑记录没有记下流程版本：请重新记录一次。'],
  [/^latest passed test targets a different workflow version$/, () => '最近一次通过的试跑是在别的版本上跑的：请在当前版本再试跑并记录一次。'],
  [/^latest passed test targets a different manifest$/, () => '最近一次通过的试跑之后流程又改过了：请在当前内容上再试跑并记录一次。'],
  [/^pending release approval is stale; request approval again$/, () => '之前的发布审批申请已经过期：请重新申请。'],
  [/^workflow is not ready for approval: (.+)$/, match => `还不满足发布条件：${match[1].split('; ').map(item => blockerWords(item)[0]).join(' ')}`],
  [/^owner_required$/, () => '只有企业主能做这一步。'],
]

export function refusalWords(text: string): readonly [string, string] {
  for (const [pattern, say] of REFUSAL_WORDS) {
    const match = text.match(pattern)
    if (match) return [say(match), text]
  }
  return [text, text]
}

export function workflowRevisionMatches(observed: string | undefined, current: string | undefined): boolean {
  return Boolean(observed?.trim() && observed === current)
}

export function admissionView(flow: FlowLike, opts: { draftOpen: boolean; observedRevision?: string }): AdmissionView {
  const approval = String(flow.release_approval?.status ?? '')
  const stage: AdmissionStage = flow.status === 'retired' ? 'retired'
    : flow.status === 'published' ? 'published'
      : flow.status === 'testing' ? (approval === 'pending' ? 'awaiting_approval' : approval === 'approved' ? 'approved' : 'testing')
        : 'draft'
  const at = ADMISSION_TRACK.indexOf(stage)
  const track = ADMISSION_TRACK.map((item, index) => ({
    stage: item,
    state: (stage === 'retired' ? 'done' : index < at ? 'done' : index === at ? 'current' : 'todo') as 'done' | 'current' | 'todo',
  }))
  const blockers: Array<readonly [string, string]> = []
  const observed = opts.observedRevision ?? flow.draft_revision
  if (stage !== 'published' && stage !== 'retired' && !workflowRevisionMatches(observed, flow.draft_revision)) {
    blockers.push(observed?.trim()
      ? ['工作流已变化，请刷新并重新打开对话框。', 'This workflow changed. Refresh it and reopen this dialog.']
      : ['缺少已读取版本，请刷新工作流后再试。', 'The observed revision is missing. Refresh this workflow before retrying.'])
  }
  if (opts.draftOpen && (stage === 'draft' || stage === 'testing')) {
    blockers.push(['画布上有未保存的改动：准入针对的是已保存的版本，先保存草稿。', 'The canvas has unsaved changes: admission is about the saved version -- save the draft first.'])
  }
  if (stage === 'draft') {
    const test = flow.latest_test
    if (!test) blockers.push(['还没有试跑记录：先用「测试」逐步试跑，再记录一次通过的试跑结果。', 'No test record yet: try the steps under Test, then record a passing run.'])
    else if (test.outcome !== 'passed') blockers.push(['最近一次试跑记录没有通过。', 'The latest test record did not pass.'])
    else if (test.execution_mode === 'simulated') blockers.push(['最近一次通过的只是合同模拟，不能作为提交测试的证据。', 'The latest pass is a contract simulation only; it is not evidence for submitting.'])
    return { stage, track, next: 'submit_testing', ready: blockers.length === 0, blockers }
  }
  if (stage === 'testing') {
    if (!flow.release_ready) {
      const listed = (flow.release_blockers ?? []).filter(Boolean)
      if (listed.length) listed.forEach(item => blockers.push(blockerWords(item)))
      else blockers.push(['还不满足发布条件。', 'Not ready for release yet.'])
    }
    return { stage, track, next: 'request_release', ready: blockers.length === 0, blockers }
  }
  if (stage === 'awaiting_approval') return { stage, track, next: 'decide_release', ready: blockers.length === 0, blockers }
  if (stage === 'approved') {
    if (!flow.release_ready) (flow.release_blockers ?? ['还不满足发布条件。']).filter(Boolean).forEach(item => blockers.push(blockerWords(item)))
    return { stage, track, next: 'publish', ready: blockers.length === 0, blockers }
  }
  return { stage, track, next: 'none', ready: false, blockers }
}

export type TestStepState = PreviewStatus | 'older_version'
export const TEST_STEP_LABELS: Record<TestStepState, readonly [string, string]> = {
  ...PREVIEW_STATUS_LABELS,
  older_version: ['只在旧版本试跑过', 'Tried on an older version only'],
}

export interface FlowTestView {
  /** AI steps: each one's latest real trial on the saved version. */
  ai: Array<{ id: string; name: string; state: TestStepState }>
  /** Steps a person does or signs: a trial does not run them; a real order hands them to their owner. */
  people: Array<{ id: string; name: string }>
  /** Steps that call another flow: tried by trying that flow. */
  calls: Array<{ id: string; name: string }>
  passed: number
  /** The AI step to try next: the first, in the flow's order, that has not passed while every AI step it waits for
   *  (through people's steps, which a trial takes as released) has. '' when all passed or none can go yet. */
  next: string
}

/** What 测试 says about each step of the saved version. Trials arrive newest first; one on another version says
 *  nothing about this one (a changed step is a new version), so it is shown as such and not counted. */
export function flowTestView(steps: readonly StepLike[], trials: readonly TrialLike[], savedVersion: string): FlowTestView {
  const view: FlowTestView = { ai: [], people: [], calls: [], passed: 0, next: '' }
  const current = trials.filter(trial => !trial.workflow_version || trial.workflow_version === savedVersion)
  for (const step of steps) {
    const name = step.name || step.id
    const kind = step.type ?? 'ai_do'
    if (kind === 'human_do' || kind === 'human_review') { view.people.push({ id: step.id, name }); continue }
    if (kind === 'invoke_workflow') { view.calls.push({ id: step.id, name }); continue }
    let state: TestStepState = previewStatus(step.id, new Set(), current)
    if (state === 'not_tried' && trials.some(trial => trial.node_id === step.id)) state = 'older_version'
    if (state === 'trial_passed') view.passed += 1
    view.ai.push({ id: step.id, name, state })
  }
  // Which AI steps a step waits for, looking through people's steps and called flows (a trial releases those).
  const byId = new Map(steps.map(step => [step.id, step]))
  const isAi = (id: string) => (byId.get(id)?.type ?? 'ai_do') === 'ai_do'
  const aiUpstream = (id: string, seen = new Set<string>()): string[] => {
    const found: string[] = []
    for (const dep of byId.get(id)?.depends_on ?? []) {
      if (seen.has(dep) || !byId.has(dep) || dep === id) continue
      seen.add(dep)
      if (isAi(dep)) found.push(dep)
      else found.push(...aiUpstream(dep, seen))
    }
    return found
  }
  const passed = new Set(view.ai.filter(step => step.state === 'trial_passed').map(step => step.id))
  view.next = view.ai.find(step => step.state !== 'trial_passed' && step.state !== 'trial_running' && step.state !== 'waiting_for_answer'
    && aiUpstream(step.id).every(dep => passed.has(dep)))?.id ?? ''
  return view
}
