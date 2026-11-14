/** Server history is a read-only index, never proof that an absent request did not execute. */
export interface LifecycleTrialHistory {
  trial_id: string; request_id: string; candidate_revision: string; fingerprint: string
  instance_selection?: { instance_id: string; expected_revision: number; expected_fingerprint: string }
  state: 'prepared' | 'running' | 'awaiting_review' | 'approved' | 'rejected' | 'failed' | 'cancelled' | 'outcome_unknown' | 'abandoned'; observed_at: string
}
export interface LifecyclePublicationHistory {
  request_id: string; candidate_revision: string; fingerprint: string
  state: 'prepared' | 'unknown' | 'committed'; observed_at: string
}
export interface LifecycleHistory {
  project_id: string; package_id: string; status: 'pending' | 'ready' | 'failed'
  trials: readonly Readonly<LifecycleTrialHistory>[]; publications: readonly Readonly<LifecyclePublicationHistory>[]
  has_more: boolean; blocked_elsewhere: boolean
}
const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/, SHA = /^[a-f0-9]{64}$/, TRIAL = /^et-[a-f0-9]{24}$/
const states = new Set(['prepared', 'running', 'awaiting_review', 'approved', 'rejected', 'failed', 'cancelled', 'outcome_unknown', 'abandoned'])
export const lifecycleStateWords: Record<LifecycleTrialHistory['state'], string> = {
  prepared: '已受理，等待执行', running: '正在测试', awaiting_review: '测试已结束，等你逐条审核',
  approved: '真实测试与人工审核已通过', rejected: '人工审核未通过', failed: '测试未完成', cancelled: '测试已取消',
  outcome_unknown: '测试结果未知', abandoned: '已作废，保留原记录与费用',
}
const row = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const valid = (value: unknown, re: RegExp): value is string => typeof value === 'string' && re.test(value)
function need(value: unknown): asserts value { if (!value) throw new Error('历史记录不能核实；未启动新测试。') }
export function decodeLifecycleHistory(value: unknown, project: string, pkg: string): Readonly<LifecycleHistory> {
  need(row(value) && value.project_id === project && value.package_id === pkg && valid(value.revision, SHA)
    && value.effects_produced === false && value.external_effects_produced === false
    && Array.isArray(value.allowed_actions) && value.allowed_actions.length === 0
    && typeof value.has_more === 'boolean' && typeof value.blocked_elsewhere === 'boolean')
  const seen = new Set<string>()
  const parse = (rows: unknown, publications: boolean): readonly Readonly<LifecycleTrialHistory | LifecyclePublicationHistory>[] => {
    need(Array.isArray(rows) && rows.length <= 8)
    return Object.freeze(rows.map(r => {
      need(row(r) && Object.keys(r).length === (publications ? 5 : r.instance_selection === undefined ? 6 : 7) && valid(r.request_id, ID)
        && valid(r.candidate_revision, SHA) && valid(r.fingerprint, SHA)
        && typeof r.observed_at === 'string' && r.observed_at.length > 0 && r.observed_at.length <= 128 && !/[\x00-\x1f]/.test(r.observed_at)
        && typeof r.state === 'string' && (publications ? ['prepared', 'unknown', 'committed'].includes(r.state) : states.has(r.state)))
      if (!publications) need(valid(r.trial_id, TRIAL))
      if (!publications && r.instance_selection !== undefined) {
        const selection = r.instance_selection
        need(row(selection) && Object.keys(selection).length === 3 && valid(selection.instance_id, /^ei-[a-f0-9]{16}$/)
          && Number.isSafeInteger(selection.expected_revision) && Number(selection.expected_revision) > 0 && valid(selection.expected_fingerprint, SHA))
      }
      const id = `${publications}/${r.request_id}`; need(!seen.has(id)); seen.add(id)
      return Object.freeze({ ...r, ...(r.instance_selection ? {
        instance_selection: Object.freeze({ ...r.instance_selection as Record<string, unknown> }),
      } : {}) }) as unknown as LifecycleTrialHistory | LifecyclePublicationHistory
    }))
  }
  const trials = parse(value.trials, false) as readonly Readonly<LifecycleTrialHistory>[]
  const publications = parse(value.publications, true) as readonly Readonly<LifecyclePublicationHistory>[]
  return Object.freeze({ project_id: project, package_id: pkg, status: 'ready', trials, publications, has_more: value.has_more, blocked_elsewhere: value.blocked_elsewhere })
}
export function historyBlocksNewTest(history: LifecycleHistory): boolean {
  return history.status !== 'ready' || history.blocked_elsewhere
    || history.trials.some(t => ['prepared', 'running', 'outcome_unknown'].includes(t.state))
    || history.publications.some(p => p.state !== 'committed')
}
