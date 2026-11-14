import { useEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n'
import { customerServiceCaseRows, customerServicePrepareParams, newCustomerServiceSetup, type CustomerServiceState } from '../lib/customerService'

export interface CustomerServiceCardProps {
  state: CustomerServiceState
  connected: boolean
  onSend: (action: string, params: Record<string, unknown>) => boolean
}

export function CustomerServiceCard({ state, connected, onSend }: CustomerServiceCardProps) {
  const { locale } = useI18n(), zh = locale !== 'en'
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const [setup, setSetup] = useState(newCustomerServiceSetup)
  const [suiteId, setSuiteId] = useState(state.suiteId)
  const [budget, setBudget] = useState({ max_cost_cny: '5', max_model_calls: '20', max_wall_clock_s: '120' })
  const [checked, setChecked] = useState(false), [preparing, setPreparing] = useState(false)
  const [error, setError] = useState(''), [notes, setNotes] = useState<Record<string, string>>({})
  const [reason, setReason] = useState('')
  const [pauseAccount, setPauseAccount] = useState('')
  const detail = state.detail?.suite_id === suiteId ? state.detail : null
  const rows = customerServiceCaseRows(detail)
  useEffect(() => { if (state.suiteId) setSuiteId(state.suiteId) }, [state.suiteId])
  useEffect(() => { setNotes({}) }, [suiteId])
  const send = (action: string, params: Record<string, unknown>) => {
    if (connected && !preparing) onSend(action, params)
  }
  const budgetParams = () => ({ max_cost_cny: budget.max_cost_cny, max_model_calls: Number(budget.max_model_calls), max_wall_clock_s: Number(budget.max_wall_clock_s) })
  const prepare = async () => {
    if (!connected || !checked || preparing) return
    setPreparing(true); setError('')
    try { const params = await customerServicePrepareParams(setup); if (mounted.current) onSend('customer_service_prepare', params) }
    catch (exc) { if (mounted.current) setError(exc instanceof Error ? exc.message : '配置无效') }
    finally { if (mounted.current) setPreparing(false) }
  }
  return <section className="shell-card cs-setup" data-testid="customer-service-card" aria-label={zh ? '客服配置与验收' : 'Customer service setup and acceptance'}>
    <style>{`.cs-setup fieldset{display:grid;gap:12px;min-width:0;margin:16px 0;padding:16px;border:1px solid var(--border-color,#ccd3dd);border-radius:10px}.cs-setup label{display:grid;gap:6px;min-width:0}.cs-setup input,.cs-setup textarea,.cs-setup select{width:100%;max-width:100%;box-sizing:border-box;padding:8px;font:inherit}.cs-setup input[type=checkbox]{width:auto;justify-self:start}.cs-setup textarea{min-height:64px;resize:vertical}.cs-setup .shell-card-row{display:grid;gap:10px;min-width:0}.cs-setup button{max-width:100%;white-space:normal;justify-self:start}.cs-setup p,.cs-setup li{overflow-wrap:anywhere}`}</style>
    <header className="shell-page-head"><h2>{zh ? '客服配置与验收' : 'Customer service setup and acceptance'}</h2>
      <p>配置 → 准备 → 评测 → 逐项评审 → 准入。准入仅记录授权依据，不启用渠道或发送客户消息。</p>
      <p>开始评测前，请确认所选模型与费用上限；准备套件不会调用模型。</p></header>
    <fieldset disabled={!connected || preparing}><legend>1. 服务范围与已发布员工包</legend>
      <p>从员工库复制已发布的结构化客服包信息；未发布的草稿不能用于准入。</p>
      <label>员工包 ID<input aria-label="员工包 ID" value={setup.packageId} onChange={e => setSetup({ ...setup, packageId: e.target.value })} /></label>
      <label>版本<input aria-label="版本" value={setup.version} onChange={e => setSetup({ ...setup, version: e.target.value })} /></label>
      <label>内容指纹<input aria-label="内容指纹" value={setup.fingerprint} onChange={e => setSetup({ ...setup, fingerprint: e.target.value })} /></label>
      <label>渠道<select aria-label="渠道" value={setup.channel} onChange={e => { setChecked(false); setSetup({ ...setup, channel: e.target.value as 'console' | 'wecom_cs' }) }}><option value="console">控制台验收（不接外部账号）</option><option value="wecom_cs">企业微信（仅准备验收，不启用）</option></select></label>
      {setup.channel === 'wecom_cs' && <label>客服账号 ID<input value={setup.accountId} onChange={e => setSetup({ ...setup, accountId: e.target.value })} /></label>}
      <label>有效期（UTC）<input type="datetime-local" value={setup.validUntil} onChange={e => setSetup({ ...setup, validUntil: e.target.value })} /></label>
      <label>澄清上限<input type="number" min="0" max="8" value={setup.maxClarifications} onChange={e => setSetup({ ...setup, maxClarifications: Number(e.target.value) })} /></label>
      <label>自动消息总上限<input type="number" min="1" value={setup.maxMessages} onChange={e => setSetup({ ...setup, maxMessages: Number(e.target.value) })} /></label>
      <p>本轮仅准入商品知识问答及澄清；查单、退款和其他执行动作不开放。</p>
      <label>话术原则<textarea value={setup.instructions} onChange={e => setSetup({ ...setup, instructions: e.target.value })} /></label>
    </fieldset>
    <fieldset disabled={!connected || preparing}><legend>2. 审核知识快照</legend>
      <p>来源位置只是标签，不会抓取网页。填写经核对的完整答案；此处记录不替代服务人工验收。</p>
      {setup.knowledge.map((row, index) => <div className="shell-card-row" key={index}>
        {(['question', 'answer', 'title', 'reference'] as const).map(key => <label key={key}>{({ question: '问题', answer: '已核对答案', title: '来源标题', reference: '来源位置' })[key]} {index + 1}
          <textarea aria-label={`${key}-${index + 1}`} value={row[key]} onChange={e => { setChecked(false); setSetup({ ...setup, knowledge: setup.knowledge.map((item, i) => i === index ? { ...item, [key]: e.target.value } : item) }) }} /></label>)}
        {setup.knowledge.length > 1 && <button type="button" onClick={() => { setChecked(false); setSetup({ ...setup, knowledge: setup.knowledge.filter((_, i) => i !== index) }) }}>移除知识 {index + 1}</button>}
      </div>)}
      <button type="button" disabled={setup.knowledge.length >= 200} onClick={() => { setChecked(false); setSetup({ ...setup, knowledge: [...setup.knowledge, { question: '', answer: '', title: '', reference: '' }] }) }}>添加知识</button>
      <label><input type="checkbox" checked={checked} onChange={e => setChecked(e.target.checked)} />我已核对这些答案和来源，仅采用为本次验收快照</label>
    </fieldset>
    <fieldset disabled={!connected || preparing}><legend>3. 试聊与预期</legend>
      <p>至少保留正常回复、澄清、转人工三类。正常用例请填写上方知识问题或真实问法。</p>
      {setup.cases.map((row, index) => <label key={row.id}>{row.id} · {row.action}<textarea aria-label={`case-${row.id}`} value={row.message} onChange={e => setSetup({ ...setup, cases: setup.cases.map((item, i) => i === index ? { ...item, message: e.target.value } : item) })} /></label>)}
      <button type="button" data-testid="cs-prepare" disabled={!checked || preparing} onClick={() => void prepare()}>{preparing ? '检查配置中…' : '准备验收套件'}</button>
    </fieldset>
    <fieldset disabled={!connected || preparing}><legend>4. 评测与逐项审核</legend>
      <label>套件 ID<input value={suiteId} onChange={e => setSuiteId(e.target.value)} data-testid="cs-suite" /></label>
      <button type="button" data-testid="cs-detail" disabled={!suiteId.trim()} onClick={() => send('customer_service_detail', { suite_id: suiteId.trim() })}>刷新结果</button>
      {Object.entries(budget).map(([key, value]) => <label key={key}>{({ max_cost_cny: '费用上限（元）', max_model_calls: '调用次数上限', max_wall_clock_s: '超时（秒）' })[key as keyof typeof budget]}<input value={value} onChange={e => setBudget({ ...budget, [key]: e.target.value })} /></label>)}
      <button type="button" data-testid="cs-test-start" disabled={!suiteId.trim() || rows.some(row => row.status !== 'pending')} onClick={() => send('customer_service_test_start', { suite_id: suiteId.trim(), budget: budgetParams() })}>开始评测（使用预算）</button>
      <p>评测状态：{detail ? String(detail.status ?? '待刷新') : '尚无结果'}；模型完成：{detail?.ai_complete === true ? '是' : '否'}；人工完成：{detail?.human_complete === true ? '是' : '否'}</p>
      {rows.map(row => <article className="shell-card" key={row.id} data-testid={`cs-case-${row.id}`}><h3>{row.id} · {row.status}</h3>
        {row.turns?.map((turn, i) => <p key={i}>输入：{turn.text}</p>)}<p>回复：{row.reply || '尚未生成可审核回复'}</p>
        <ul>{row.sources.map((source, i) => <li key={i}>依据：{String(source.title ?? source.id)} · {String(source.version ?? '')} · {String(source.source_location ?? '')}</li>)}</ul>
        {row.handoff && <p>人工建议：{row.handoff.required === true ? '需要人工' : '无需交接'} · {String(row.handoff.summary ?? '')}</p>}
        {String(row.errors) && <p>检查信息：{String(row.errors)}</p>}
        {row.review ? <p>已审核：{String(row.review.verdict)} · {String(row.review.note)}</p> : <>
          <label>审核说明<textarea aria-label={`note-${row.id}`} value={notes[row.id] ?? ''} onChange={e => setNotes({ ...notes, [row.id]: e.target.value })} /></label>
          {(['accept', 'reject'] as const).map(verdict => <button key={verdict} type="button" disabled={!row.hash || row.status !== 'passed' || !notes[row.id]?.trim()}
            onClick={() => send('customer_service_review', { suite_id: suiteId.trim(), case_id: row.id, candidate_sha256: row.hash, verdict, note: notes[row.id].trim() })}>{verdict === 'accept' ? '接受此回复' : '拒绝此回复'}</button>)}
        </>}
      </article>)}
      <button type="button" data-testid="cs-admit" disabled={!suiteId.trim() || detail?.human_complete !== true || detail?.revoked === true} onClick={() => send('customer_service_admit', { suite_id: suiteId.trim() })}>记录准入（不启用渠道）</button>
      <label>撤销原因<input value={reason} onChange={e => setReason(e.target.value)} /></label>
      <button type="button" disabled={!suiteId.trim() || !reason.trim()} onClick={() => send('customer_service_revoke', { suite_id: suiteId.trim(), reason_code: reason.trim() })}>撤销</button>
    </fieldset>
    <fieldset disabled={!connected || preparing}><legend>已启用渠道的停止开关</legend>
      <label>需要暂停的客服账号 ID<input aria-label="需要暂停的客服账号 ID" value={pauseAccount} onChange={e => setPauseAccount(e.target.value)} /></label>
      <button type="button" data-testid="cs-channel-pause" disabled={!pauseAccount.trim()} onClick={() => send('customer_service_channel_pause', { account_id: pauseAccount.trim() })}>暂停此账号自动客服</button>
    </fieldset>
    <p>渠道启用、替换和真实试发需独立上线确认，本页不会自动执行。</p>
    {(error || state.error) && <p role="alert">{error || state.error}</p>}
    {state.lastAction && <p role="status">{state.lastAction}：{state.lastOk === true ? '服务端已确认，请刷新结果' : state.lastOk === false ? '未完成，请检查错误' : '等待服务端确认'}</p>}
  </section>
}
