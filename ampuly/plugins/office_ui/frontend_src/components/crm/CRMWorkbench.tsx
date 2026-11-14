import { useEffect, useRef, useState } from 'react'
import { crmErrorMessage, defaults, labels, money } from './client'
import type { CRMAction, CRMKind, CRMRecord, CRMRequest } from './client'
import './crm.css'

type Props = { request: CRMRequest; connected: boolean; projectId: string; onBusyChange?: (busy: boolean) => void }
type ErasurePlan = { token: string; record_id: string; mode: 'delete' | 'anonymize'; phrase: string;
  affected: { id: string; kind: string; revision: number }[]; retired_evidence_count: number }
const fieldLabels: Record<string, string> = {
  name: '名称', website: '网站', notes: '业务事实与备注', owner_id: '记录负责人（不授予访问权限）', company_id: '销售对象公司',
  contact_id: '联系人（可选）', email: '邮箱', phone: '电话', pipeline_status: '销售进展', amount_minor: '金额（最小货币单位）',
  currency: '币种', next_action: '下一步', next_due: '跟进时间（含时区）', opportunity_id: '关联商机',
  occurred_at: '活动时间（含时区）', source_reference: '来源引用',
}
const pipelineStatuses = ['lead', 'qualified', 'proposal', 'won', 'lost']
const pipelineLabels: Record<string, string> = { lead: '线索', qualified: '已确认需求', proposal: '方案中', won: '已成交', lost: '已流失' }

// List/detail/search interaction adapted from Kargul Studio sales-crm, MIT.
// Data and execution authority stay in the engine; no demo business metrics.
export function CRMWorkbench({ request, connected, projectId, onBusyChange }: Props) {
  const [kind, setKind] = useState<CRMKind>('company')
  const [records, setRecords] = useState<CRMRecord[]>([])
  const [related, setRelated] = useState<CRMRecord[]>([])
  const [owners, setOwners] = useState<string[]>([])
  const [query, setQuery] = useState('')
  const [owner, setOwner] = useState('')
  const [pipelineStatus, setPipelineStatus] = useState('')
  const [archived, setArchived] = useState(false)
  const [selected, setSelected] = useState<CRMRecord | null>(null)
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const [customer, setCustomer] = useState('')
  const [erasure, setErasure] = useState<ErasurePlan | null>(null)
  const [confirmPhrase, setConfirmPhrase] = useState('')
  const [confirmErasure, setConfirmErasure] = useState(false)
  const generation = useRef(0)
  const inFlight = useRef(false)
  const mutationKeys = useRef(new Map<string, string>())
  const search = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); search.current?.focus() }
      if (event.key === 'Escape' && !busy) { setDraft(null); setSelected(null); setErasure(null) }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [busy])

  useEffect(() => {
    setSelected(null); setDraft(null); setRecords([]); setRelated([])
    setErasure(null); setConfirmPhrase(''); setConfirmErasure(false)
    mutationKeys.current.clear()
    return () => { generation.current++ }
  }, [projectId])

  useEffect(() => {
    const token = ++generation.current
    if (!connected) { setRecords([]); setError('连接已断开，无法读取或保存'); return }
    setLoading(true); setError('')
    Promise.all([
      request('crm_list', { kind, query, owner_id: owner, pipeline_status: kind === 'opportunity' ? pipelineStatus : '', archived }),
      ...(['company', 'contact', 'opportunity'] as CRMKind[]).map((value) => request('crm_list', { kind: value })),
    ]).then(([result, ...links]) => {
      if (token !== generation.current) return
      setRecords(result.records ?? []); setOwners(result.owners ?? [])
      setCustomer(String(result.customer_id ?? ''))
      setRelated(links.flatMap((item) => item.records ?? []))
    }).catch((reason: Error) => { if (token === generation.current) { setRecords([]); setError(reason.message) } })
      .finally(() => { if (token === generation.current) setLoading(false) })
    return () => { generation.current++ }
  }, [request, projectId, connected, kind, query, owner, pipelineStatus, archived, refresh])

  async function mutate(action: CRMAction, params: Record<string, unknown>) {
    if (inFlight.current || !connected) return
    inFlight.current = true
    const token = generation.current
    const signature = JSON.stringify({ action, params })
    let key = mutationKeys.current.get(signature)
    if (!key) { key = crypto.randomUUID(); mutationKeys.current.set(signature, key) }
    setBusy(true); setError(''); setNotice('')
    onBusyChange?.(true)
    try {
      const result = await request(action, { ...params, request_key: key })
      if (token !== generation.current) return
      mutationKeys.current.delete(signature)
      setDraft(null); setSelected(null)
      if (action === 'crm_erase') {
        setRecords([]); setRelated([]); setErasure(null); mutationKeys.current.clear()
        generation.current++
        setNotice(`已处理 ${String(result.count)} 条关联记录；${result.mode === 'delete' ? '记录已删除' : '字段已清除并归档'}。既有导出、备份和外部任务副本未被清除。`)
      } else setNotice('已保存服务器记录')
      setRefresh((value) => value + 1)
    } catch (reason) {
      if (token === generation.current) setError(reason instanceof Error ? reason.message : '保存失败')
      if (reason instanceof Error && /confirmation_stale|confirmation_expired/.test(reason.message)) setErasure(null)
    } finally { inFlight.current = false; setBusy(false); onBusyChange?.(false) }
  }

  async function exportRecords() {
    if (inFlight.current || !connected || !window.confirm('导出当前项目全部销售记录（含已归档记录和联系资料）。请妥善保管下载文件。确认导出？')) return
    inFlight.current = true
    setBusy(true); setError('')
    const token = generation.current
    try {
      const result = await request('crm_export', { confirmed: true })
      if (token !== generation.current) return
      const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' }))
      const link = document.createElement('a'); link.href = url; link.download = `sales-${projectId}.json`; link.click()
      URL.revokeObjectURL(url)
      setNotice('已导出当前项目记录，请妥善保管文件')
    } catch (reason) { if (token === generation.current) setError(reason instanceof Error ? reason.message : '导出失败') }
    finally { inFlight.current = false; setBusy(false) }
  }

  async function previewErasure(mode: 'delete' | 'anonymize') {
    if (!selected || inFlight.current || !connected) return
    inFlight.current = true; setBusy(true); setError(''); setErasure(null)
    const token = generation.current
    try {
      const result = await request('crm_erasure_preview', { record_id: selected.id, mode })
      if (token !== generation.current) return
      setErasure(result as unknown as ErasurePlan); setConfirmPhrase(''); setConfirmErasure(false)
    } catch (reason) { if (token === generation.current) setError(reason instanceof Error ? reason.message : '预览失败') }
    finally { inFlight.current = false; setBusy(false) }
  }

  // Show the list row at once, then re-read the server's current revision so
  // edits and erasure previews start from the latest record, not a stale list.
  function open(record: CRMRecord) {
    setSelected(record); setDraft(null); setNotice(''); setErasure(null)
    const token = generation.current
    request('crm_get', { record_id: record.id }).then((fresh) => {
      if (token !== generation.current) return
      const current = fresh as unknown as CRMRecord
      setSelected((shown) => shown && shown.id === current.id ? current : shown)
    }).catch((reason: Error) => { if (token === generation.current) setError(reason.message) })
  }
  function changeKind(value: CRMKind) { if (busy) return; setKind(value); setSelected(null); setDraft(null); setPipelineStatus(''); setErasure(null) }

  return <section className="crm-workbench" aria-label="项目销售记录">
    <header><div><small>当前项目 · 关联业务资料</small><h2>项目销售记录</h2><p>项目 {projectId} · 客户归属 {customer || '待读取'} · 销售对象是本项目业务资料，不创建客户账号</p></div>
      <button disabled={!connected || busy || !owners.length} onClick={() => { setSelected(null); setDraft(defaults(kind, owners[0] ?? '')) }}>新建{labels[kind]}</button></header>
    <p>这是人工维护的数据视图。员工工具、建议审核和任务交接尚未接通，请通过原业务流安排工作。记录负责人仅表示业务归属，不改变访问权限。</p>
    <nav aria-label="销售记录分类">{(Object.keys(labels) as CRMKind[]).map((value) => <button key={value} aria-current={kind === value ? 'page' : undefined} disabled={busy} onClick={() => changeKind(value)}>{labels[value]}</button>)}</nav>
    <div className="crm-filters"><label>搜索<input ref={search} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="名称、备注或来源 · Ctrl K" /></label>
      <label>记录负责人<select value={owner} onChange={(event) => setOwner(event.target.value)}><option value="">全部记录负责人</option>{owners.map((id) => <option key={id}>{id}</option>)}</select></label>
      <label className="crm-confirm"><input type="checkbox" checked={archived} disabled={busy} onChange={event => { setArchived(event.target.checked); setSelected(null); setDraft(null); setErasure(null) }} />查看已归档</label>
      {kind === 'opportunity' && <label>销售进展<select value={pipelineStatus} onChange={(event) => setPipelineStatus(event.target.value)}><option value="">全部进展</option>{pipelineStatuses.map((id) => <option value={id} key={id}>{pipelineLabels[id]}</option>)}</select></label>}
      <button disabled={loading || busy} onClick={() => setRefresh((value) => value + 1)}>刷新</button><button disabled={busy || !connected} onClick={() => void exportRecords()}>导出项目记录</button></div>
    {error && <div role="alert" title={error}>{crmErrorMessage(error)} <span>未确认成功时请勿另建重复记录；相同请求可重试核对。</span></div>}
    {notice && <p role="status">{notice}</p>}
    <div className="crm-body"><div className="crm-list" aria-busy={loading}>
      {loading ? <p role="status">正在读取服务器记录…</p> : records.length ? <table><thead><tr><th>名称／摘要</th><th>记录负责人</th><th>金额</th><th>更新时间</th></tr></thead><tbody>{records.map((row) => <tr key={row.id} data-selected={selected?.id === row.id}>
        <td><button onClick={() => open(row)}>{String(row.fields.name ?? row.fields.summary ?? row.id)}</button></td><td>{String(row.fields.owner_id ?? '未分配')}</td><td>{row.kind === 'opportunity' ? money(row) : '—'}</td><td><time>{new Date(row.updated_at).toLocaleString()}</time></td></tr>)}</tbody></table> : !error && <p className="crm-empty">没有符合条件的{labels[kind]}。创建真实记录后将在这里显示。</p>}
    </div>
    {(selected || draft) && <aside aria-label="CRM 详情"><header><h3>{draft ? selected ? '编辑记录' : `新建${labels[kind]}` : '记录详情'}</h3><button disabled={busy} onClick={() => { setSelected(null); setDraft(null); setErasure(null) }}>关闭</button></header>
      {draft ? <form onSubmit={(event) => { event.preventDefault(); void mutate('crm_save', { kind, fields: draft, ...(selected ? { record_id: selected.id, expected_revision: selected.revision } : {}) }) }}>
        {Object.entries(draft).map(([key, value]) => <label key={key}>{fieldLabels[key] ?? key}
          {key === 'notes' ? <textarea aria-label={fieldLabels[key] ?? key} value={String(value)} onChange={(event) => setDraft({ ...draft, [key]: event.target.value })} />
            : key === 'owner_id' ? <select aria-label={fieldLabels[key] ?? key} value={String(value)} onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}>{owners.map((id) => <option key={id}>{id}</option>)}</select>
            : ['company_id', 'contact_id', 'opportunity_id'].includes(key) ? <select aria-label={fieldLabels[key] ?? key} value={String(value)} onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}><option value="">请选择</option>{related.filter((row) => row.kind === key.replace('_id', '')).map((row) => <option key={row.id} value={row.id}>{String(row.fields.name)}</option>)}</select>
            : key === 'pipeline_status' || key === 'currency' ? <select aria-label={fieldLabels[key] ?? key} value={String(value)} onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}>{(key === 'pipeline_status' ? pipelineStatuses : ['CNY', 'USD', 'EUR', 'GBP', 'JPY', 'HKD']).map((id) => <option key={id} value={id}>{pipelineLabels[id] ?? id}</option>)}</select>
            : <input aria-label={fieldLabels[key] ?? key} type={key === 'amount_minor' ? 'number' : key === 'email' ? 'email' : 'text'} min={0} step={1} value={String(value)} required={key === 'name'} onChange={(event) => setDraft({ ...draft, [key]: key === 'amount_minor' ? Number(event.target.value) : event.target.value })} />}</label>)}
        <footer><button type="button" disabled={busy} onClick={() => setDraft(null)}>取消编辑</button><button type="submit" disabled={busy || !connected}>{busy ? '正在保存…' : '保存到服务器'}</button></footer></form>
        : selected && <><p className="crm-meta">{selected.id} · 版本 {selected.revision}</p><dl>{Object.entries(selected.fields).map(([key, value]) => <div key={key}><dt>{fieldLabels[key] ?? key}</dt><dd>{typeof value === 'object' ? JSON.stringify(value) : String(value || '未记录')}</dd></div>)}</dl>
          {!selected.archived && <footer><button disabled={busy || !!erasure} onClick={() => setDraft({ ...selected.fields })}>编辑</button><button disabled={busy || !!erasure} onClick={() => { if (window.confirm('归档保留历史；有活动关联时将拒绝。确认归档？')) void mutate('crm_archive', { record_id: selected.id, expected_revision: selected.revision }) }}>归档</button></footer>}
          <div className="crm-governance"><h4>受控资料清理</h4><p>先预览目标及关联范围。不会清除既有导出、备份或外部任务副本；字段清除保留记录 ID 和审计，不保证无法重新识别。</p>
            <button disabled={busy} onClick={() => void previewErasure('anonymize')}>预览匿名化</button>{' '}
            <button disabled={busy} onClick={() => void previewErasure('delete')}>预览永久删除</button>
          </div>
          {erasure && <form aria-label="确认资料清理" onSubmit={event => { event.preventDefault(); void mutate('crm_erase', {
            token: erasure.token, record_id: erasure.record_id, mode: erasure.mode, phrase: confirmPhrase, confirmed: confirmErasure }) }}>
            <h4>{erasure.mode === 'delete' ? '永久删除' : '清除字段并归档'} · {erasure.affected.length} 条记录</h4>
            <p>当前项目 {projectId} / 客户归属 {customer}，目标 {erasure.record_id}。10 分钟内有效，记录或关联内容变化后需要重新预览。</p>
            <ul>{erasure.affected.map(row => <li key={row.id}>{labels[row.kind as CRMKind] ?? row.kind} · {row.id} · 版本 {row.revision}</li>)}</ul>
            <p>另清理 {erasure.retired_evidence_count} 条明确关联的退休证据副本，保留非敏感标识和审计。</p>
            <label>输入确认语句 {erasure.phrase}<input aria-label="清理确认语句" value={confirmPhrase} onChange={event => { setConfirmPhrase(event.target.value); setConfirmErasure(false) }} /></label>
            <label className="crm-confirm"><input type="checkbox" checked={confirmErasure} onChange={event => setConfirmErasure(event.target.checked)} />我已核对目标和关联范围，确认不可恢复地清除当前库中的这些资料</label>
            <footer><button type="button" disabled={busy} onClick={() => setErasure(null)}>取消清理</button><button type="submit" disabled={busy || !connected || !confirmErasure || confirmPhrase !== erasure.phrase}>确认执行清理</button></footer>
          </form>}
        </>}
    </aside>}
    </div>
    <p className="crm-governance">当前仅用于隔离合成数据，真实数据准入未开放。归档不会删除个人信息；资料清理需逐目标确认，旧库只能显式迁移到新副本。</p>
  </section>
}
