import type { EmployeeLifecycleController, LifecycleCandidate, LifecycleInstanceSelection } from '../lib/aiEmployeeLifecycleController'
import { lifecycleCandidateKey } from '../lib/aiEmployeeLifecycleController'
import type { LifecycleHistory } from '../lib/aiEmployeeLifecycleHistory'
import { lifecycleStateWords } from '../lib/aiEmployeeLifecycleHistory'

export function EmployeeLifecycleHistory({ controller, candidate, history, connected, onSelect, run }: {
  controller: EmployeeLifecycleController; candidate: LifecycleCandidate; history: LifecycleHistory | undefined
  connected: boolean; onSelect: (key: string) => void; run: (operation: () => unknown) => void
}) {
  const select = (revision: string, fingerprint: string, selection?: LifecycleInstanceSelection): void => onSelect(lifecycleCandidateKey({
    project_id: candidate.project_id, package_id: candidate.package_id, revision, fingerprint,
    ...(selection ? { instance_selection: selection } : {}) }))
  const trials = history?.trials.filter(item => candidate.instance_selection
    ? item.instance_selection?.instance_id === candidate.instance_selection.instance_id : !item.instance_selection) ?? []
  return <section className="ai-employee-editor__section" aria-label="岗位原测试与发布历史">
    <h4>接着原来的测试往下做</h4>
    <p className="ai-employee-editor__muted">刷新页面不会启动新测试。先读取原请求，再查看结果、审核或发布；原来没有收到回执的操作仍然保留。</p>
    <button type="button" disabled={!connected || history?.status === 'pending'} onClick={() => run(() => controller.readHistory(candidate))}>
      {history?.status === 'pending' ? '正在读取原记录…' : '读取原测试与发布记录（不计费）'}
    </button>
    {!history && <p role="status">先核对历史记录，避免把原来的付费测试重开一遍。</p>}
    {history?.status === 'failed' && <p role="alert">历史记录尚不能核实；没有启动测试或把无记录当作未执行。</p>}
    {history?.blocked_elsewhere && history.status === 'ready' && <p role="status">还有未完成或结果未知的原操作；请先在对应岗位核对，暂不开始新的付费测试。</p>}
    {history?.status === 'ready' && <>
      {history.trials.length === 0 && history.publications.length === 0 && <p>本项目、本岗位下暂无可读取的原记录。这不解除本页仍保留的未知请求。</p>}
      {trials.map(item => <div className="ai-employee-editor__status" key={item.trial_id}>
        <p>测试 {item.trial_id} · {lifecycleStateWords[item.state]} · 内容 {item.fingerprint.slice(0, 12)} · {item.observed_at}</p>
        <button type="button" disabled={!connected} onClick={() => run(() => {
          controller.restoreTrial(candidate.project_id, candidate.package_id, item.trial_id); select(item.candidate_revision, item.fingerprint, item.instance_selection)
        })}>继续查看这次测试（不重新运行）</button>
      </div>)}
      {!candidate.instance_selection && history.publications.map(item => <div className="ai-employee-editor__status" key={item.request_id}>
        <p>发布原请求 {item.request_id} · {item.state} · 内容 {item.fingerprint.slice(0, 12)} · {item.observed_at}</p>
        <button type="button" disabled={!connected} onClick={() => run(() => {
          controller.restorePublication(candidate.project_id, candidate.package_id, item.request_id); select(item.candidate_revision, item.fingerprint)
        })}>只读核对这次发布</button>
      </div>)}
      {history.has_more && <p className="ai-employee-editor__muted">这里只列最近的 8 条记录，未完成项优先。更早的原请求未删除；可用保留的原请求编号核对。</p>}
    </>}
  </section>
}
