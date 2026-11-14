import type { EmployeeAssetReadView } from '../../lib/aiEmployeeAssetController'
import { projectEmployeeAdoptionTargets } from '../../lib/aiEmployeeAssetAdapter'
import type { EmployeeAdoptionSection, EmployeeAdoptionTarget } from '../../lib/aiEmployeeAssetAdapter'

type Row = Record<string, unknown>
const row = (value: unknown): value is Row => value !== null && typeof value === 'object' && !Array.isArray(value)
const text = (value: unknown): string => typeof value === 'string' ? value : ''
const SHA = /^[a-f0-9]{64}$/
const SECTION_LABELS: Record<EmployeeAdoptionSection, string> = {
  capability_scope: '负责范围', methods: '方法与交付', tools: '工具与能力要求', models: '模型声明',
  knowledge_protocol: '知识协议', budget: '预算与循环上限', verification_scope: '验证声明范围',
}
const GAP_LABELS: Record<string, string> = {
  adoption_write_unavailable: '确认采用的写入口尚未开放。',
  evaluation_source_unverified: '缺少可核实的真实试单证据。',
  publication_index_unverified: '目标发布索引缺少独立当前尾锚证明。',
  package_retired: '目标版本已经退役，不能作为新采用依据。',
  private_configuration_incompatible: '当前项目私有配置与目标合同仍有兼容性问题。',
}
export interface EmployeeAdoptionIntent extends EmployeeAdoptionTarget { instance_id: string; expected_revision: number }
export interface AIEmployeeAdoptionPreviewProps {
  project_id: string | null; connected: boolean; instance: Row | null
  published?: EmployeeAssetReadView; target?: EmployeeAssetReadView; view?: EmployeeAssetReadView
  onTargetChange: (target: EmployeeAdoptionTarget | null) => void
  onPreview: (intent: EmployeeAdoptionIntent) => void
}
const targetKey = (value: EmployeeAdoptionTarget): string => JSON.stringify([value.package_id, value.package_version, value.expected_fingerprint])

/** No write callback, first-option default, private configuration or fabricated trial facts. */
export function AIEmployeeAdoptionPreview({ project_id, connected, instance, published, target, view, onTargetChange, onPreview }: AIEmployeeAdoptionPreviewProps) {
  const current = row(instance?.instance) ? instance.instance : null
  const scoped = !!project_id && current?.project_id === project_id && typeof instance?.revision === 'number'
    && Number.isSafeInteger(instance.revision) && instance.revision > 0 && typeof instance.fingerprint === 'string' && SHA.test(instance.fingerprint)
  const packageId = scoped ? text(current?.package_id) : ''
  const instanceId = scoped ? text(current?.instance_id) : ''
  const revision = scoped ? instance?.revision as number : 0
  const canRead = connected && scoped && Array.isArray(instance?.allowed_actions) && instance.allowed_actions.includes('employee_instance_adoption_preview')
  const catalogScoped = published?.state === 'ready' && published.request?.project_id === project_id && published.request.source === 'published'
  const candidates = catalogScoped ? projectEmployeeAdoptionTargets(published.data, packageId) : []
  // A selection exists only if an explicit target request was issued. We never
  // select the first/latest catalog row merely because one candidate is visible.
  const selected = target?.request?.project_id === project_id && target.request.action === 'employee_package_detail'
    ? candidates.find(item => item.package_id === target.request?.package_id && item.package_version === target.request?.version
      && item.expected_fingerprint === target.request?.target_fingerprint) : undefined
  const targetReady = !!selected && target?.state === 'ready' && !!target.data && row(target.data.package) && target.data.status === 'published'
    && target.data.package.package_id === packageId && target.data.package.version === selected.package_version
    && target.data.package.status === 'published' && target.data.fingerprint === selected.expected_fingerprint
  const request = view?.request
  const comparison = connected && scoped && targetReady && view?.state === 'ready' && view.receipt?.state === 'succeeded'
    && request?.action === 'employee_instance_adoption_preview' && request.project_id === project_id && request.instance_id === instanceId
    && request.adoption_revision === revision && request.adoption_instance_fingerprint === instance?.fingerprint
    && request.package_id === packageId && request.version === selected?.package_version && request.target_fingerprint === selected?.expected_fingerprint
    && view.data?.preview_scope === 'adoption_comparison' && view.data.verification_scope_kind === 'declared_package_profile_not_trial_evidence'
    && view.data.private_configuration_policy === 'preserved_not_projected' && typeof view.data.new_adoption_allowed === 'boolean' ? view.data : null
  const busy = target?.state === 'loading' || view?.state === 'loading'
  if (!scoped) return null
  return <section className="ai-employee-editor__section" aria-label="只读采用差异预览" data-testid="employee-adoption-preview">
    <h3>只读采用差异预览</h3>
    <p>当前实例 {instanceId} · 修订 {revision} · 当前固定包 {text(current?.package_version)}。</p>
    <p>比较包声明，不改变实例版本、私有配置或在途订单；这不表示允许采用、发布或运行。</p>
    <label className="ai-employee-editor__field">明确目标版本
      <select aria-label="采用比较目标版本" value={selected ? targetKey(selected) : ''} disabled={!canRead || !catalogScoped || busy}
        onChange={event => onTargetChange(candidates.find(item => targetKey(item) === event.target.value) ?? null)}>
        <option value="">请选择已读取发布索引中的明确版本</option>
        {candidates.map(item => <option key={targetKey(item)} value={targetKey(item)}>{item.package_id} · {item.package_version} · {item.expected_fingerprint}</option>)}
      </select>
    </label>
    {!catalogScoped && <p role="status">本项目发布目录尚未重新核实；不会改用其他项目、草稿或最新版。</p>}
    {catalogScoped && candidates.length === 0 && <p role="status">当前发布索引没有可明确选择的同包版本；此处不会自动采用或补造版本。</p>}
    {!canRead && <p role="status">请核对连接与此实例的服务端读取许可。</p>}
    {selected && <p>目标固定指纹：{selected.expected_fingerprint}。已读取发布索引不等于独立尾锚或试单证明。</p>}
    <button type="button" disabled={!canRead || !targetReady || busy}
      onClick={() => { if (canRead && targetReady && !busy && selected) onPreview({ ...selected, instance_id: instanceId, expected_revision: revision }) }}>只读比较采用差异</button>
    {target?.state === 'loading' && <p role="status">正在精确读取目标版本，不使用当前／最新版替代。</p>}
    {selected && target?.state === 'ready' && !targetReady && <p role="status">目标版本指纹或状态已变化；请重新读取发布目录后明确选择。</p>}
    {(target?.state === 'error' || target?.state === 'stale') && <p role="status">目标版本读取尚不能核实；不会沿用旧成功。</p>}
    {view?.state === 'loading' && <p role="status">正在只读比较，旧差异已失效。</p>}
    {(view?.state === 'error' || view?.state === 'stale') && <p role="status">{view.receipt?.message || '读取已过期或连接中断；请重新核对实例与目标版本。此请求不会采用或运行。'}</p>}
    {comparison && <section aria-label="已核实的采用声明差异">
      <p>当前包 {text(comparison.from_version)} · 指纹 {text(comparison.from_fingerprint)}</p>
      <p>目标包 {text(comparison.to_version)} · 指纹 {text(comparison.to_fingerprint)} · 状态 {comparison.target_status === 'retired' ? '已退役' : '发布索引可见'}</p>
      <p>发布尾锚：{comparison.is_current_verified === true ? '本次独立来源已核对' : '尚未核实'}。验证范围仅是包声明，不是真实模型／试单结果。</p>
      <ul>{Array.isArray(comparison.gaps) && comparison.gaps.filter((gap): gap is string => typeof gap === 'string' && Object.hasOwn(GAP_LABELS, gap))
        .map(gap => <li key={gap}>{GAP_LABELS[gap]}</li>)}</ul>
      {Array.isArray(comparison.differences) && comparison.differences.filter(row).map(item => <details key={text(item.section)} open={item.changed === true}>
        <summary>{SECTION_LABELS[item.section as EmployeeAdoptionSection]}：{item.changed === true ? '服务端确认声明变化' : '声明未变化'}</summary>
        <h4>当前固定包声明</h4><pre>{JSON.stringify(item.before, null, 2)}</pre>
        <h4>目标固定包声明</h4><pre>{JSON.stringify(item.after, null, 2)}</pre>
      </details>)}
      <p>空对象表示该项没有声明，不表示无限权限。数值是浏览器展示；变化结论使用服务端严格比较，不在前端重算。</p>
      <p>项目私有配置保留且未投影；比较不修改任何内容。若本项目有采用权限且目标发布依据完整，下方会出现独立的“升级本项目岗位”确认区；它不代表已经就绪或允许运行。</p>
    </section>}
  </section>
}
