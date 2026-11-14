// 「企业微信（微信客服）」 on 配置 · 服务与连接 · 渠道连接 (policy「按推荐做」, W3b), and the read-only conversation
// an order from 企业微信客服 shows in 业务工单. The card is the owner's only (App hands it in on the owner's console):
// the switch (default off), whether the relay's address and shared token are set -- never their values, and no key is
// typed on 配置 (configCapabilities.test) -- each 客服账号 bound to this project and one of its flows with the fields
// only the owner fills and the welcome text, the knowledge file's place, and the status word from real evidence only
// (可用 needs a real exchange with the relay). Every write goes through the server's owner-only actions; what is sent
// to a customer is still released on the Feishu card, never from here.

import { useEffect, useRef, useState } from 'react'
import {
  accountRequest, draftOf, draftProblem, lineTime, ownerOnlyLabels, wecomTone,
  type WecomAccount, type WecomAccountDraft, type WecomChannelState, type WecomConversation, type WecomFlowChoice,
} from '../lib/wecomChannel'

export const WECOM_CARD_TITLE = '企业微信（微信客服）'

const SWITCH_ON_CONFIRM = '打开后控制台会连上中转站：客户在企业微信里发来的消息由 AI 客服照知识库接待，需求问齐后建一张「待接单」工单等你接。'
  + '价格、退款、日期的承诺不会由 AI 说出去；交付文件要你在飞书卡上放行才会发给客户。确定打开？'
const REMOVE_CONFIRM = '解绑后，这个客服账号收到的消息不再由 AI 接待；已经有的对话和消息都留着。确定解绑？'

export interface WecomChannelCardProps {
  state: WecomChannelState
  connected: boolean
  /** The flows a 客服账号 of this project may start orders on (lib/wecomChannel flowChoices). */
  flows: WecomFlowChoice[]
  /** Asked once when the card shows (the section opened), and by 刷新. */
  onAsk: () => void
  onSwitch: (enabled: boolean) => void
  onSaveAccount: (request: Record<string, unknown>) => void
  onRemoveAccount: (openKfid: string) => void
}

export function WecomChannelCard({ state, connected, flows, onAsk, onSwitch, onSaveAccount, onRemoveAccount }: WecomChannelCardProps) {
  const [editing, setEditing] = useState<string | null>(null)   // an account's id, 'new', or null
  const [draft, setDraft] = useState<WecomAccountDraft>(() => draftOf(null, flows))
  const [problem, setProblem] = useState('')
  // eslint-disable-next-line react-hooks/exhaustive-deps -- read once when the section opens; 刷新 reads again
  useEffect(() => { onAsk() }, [])
  // The form closes when its save came back ok; a refusal keeps it open with the owner's words under it.
  const saving = useRef(false)
  useEffect(() => {
    if (state.busy === 'account') { saving.current = true; return }
    if (saving.current && state.busy === '') {
      saving.current = false
      if (!state.error) setEditing(null)
    }
  }, [state.busy, state.error])

  const status = state.status
  const brain = status?.brain ?? null
  const busy = state.busy !== ''
  const flowName = (id: string) => flows.find(flow => flow.id === id)?.name ?? id
  const tone = wecomTone(status?.status ?? '')
  const open = (account: WecomAccount | null) => {
    setEditing(account ? account.open_kfid : 'new')
    setDraft(draftOf(account, flows))
    setProblem('')
  }
  const save = () => {
    const said = draftProblem(draft, flows)
    setProblem(said)
    if (!said) onSaveAccount(accountRequest(draft, flows))
  }
  const chosenFlow = flows.find(flow => flow.id === draft.workflow_id) ?? null

  return (
    <section className="shell-card" data-testid="wecom-channel-card" data-status={status?.status ?? 'reading'} aria-label={WECOM_CARD_TITLE}>
      <h3>
        {WECOM_CARD_TITLE}{' '}
        {status && <span className={`shell-tag${tone === 'ok' ? ' shell-tag--ok' : tone === 'warn' ? ' shell-tag--warn' : ''}`} data-testid="wecom-status-word">{status.status_word}</span>}
      </h3>
      <p className="shell-card-text">
        客户在个人微信里和你的企业微信客服号聊天：AI 客服照知识库回答、问清需求，问齐后建一张「待接单」工单等你接。交付要你在飞书卡上放行才会发给客户。默认关着。
      </p>
      {!status ? (
        <p className="shell-card-text" role="status">{state.error || '正在读取企业微信通道的状态…'}</p>
      ) : (
        <>
          <ul className="shell-config-list" data-testid="wecom-channel-facts">
            <li data-wecom-row="switch">
              <div><strong>通道开关</strong><small>{status.switch_on ? '已打开' : '关着（默认关）'}{status.muted ? '；外发渠道已静音，静音期间不会连中转站' : ''}</small></div>
              <button type="button" className={`shell-btn${status.switch_on ? '' : ' shell-btn--primary'}`} data-testid="wecom-switch"
                disabled={!connected || busy || (!status.switch_on && !status.ready)}
                onClick={() => {
                  if (!status.switch_on && !window.confirm(SWITCH_ON_CONFIRM)) return
                  onSwitch(!status.switch_on)
                }}>{state.busy === 'switch' ? '处理中…' : status.switch_on ? '关掉' : '打开'}</button>
            </li>
            <li data-wecom-row="relay">
              <div><strong>中转站地址</strong><small>{status.configured.url ? (status.relay_host ? `已填（${status.relay_host}）` : '已填，但样子不对') : '还没填'}</small></div>
              <span className={`shell-tag${status.configured.url ? ' shell-tag--ok' : ''}`}>{status.configured.url ? '已填' : '未填'}</span>
            </li>
            <li data-wecom-row="token">
              <div><strong>共享口令</strong><small>这里只看有没有设置，从不显示内容</small></div>
              <span className={`shell-tag${status.configured.token ? ' shell-tag--ok' : ''}`}>{status.configured.token ? '已设置' : '未设置'}</span>
            </li>
            <li data-wecom-row="link">
              <div><strong>和中转站的连接</strong><small>{status.connected ? '现在连着' : status.switch_on ? '现在没连上' : '通道关着时不连'}</small></div>
            </li>
            <li data-wecom-row="knowledge">
              <div><strong>知识库</strong><small>{status.knowledge_file ? `写在${status.knowledge_file}；AI 只照它说价格、工期和售后，没写的转人工` : '—'}</small></div>
            </li>
            {brain && (
              <li data-wecom-row="brain" data-mode={brain.mode}>
                <div>
                  <strong data-testid="wecom-brain-mode">{`回答方式：${brain.mode === 'jev' ? 'JEV 判断 + 现成答案' : '文字模型'}`}</strong>
                  <small>{brain.mode === 'jev'
                    ? 'JEV 判断客户在问什么，发你在知识库里写好的现成答案；判断不了的交给文字模型起草，或转人工'
                    : 'JEV 的密钥没有设置，由文字模型照知识库起草回答（在「模型与密钥」里设置 JEV 后改用现成答案）'}</small>
                  <small data-testid="wecom-knowledge-counts">{`现成答案 ${brain.knowledge.entries} 条可以直接发；需求提问 ${brain.knowledge.fieldPrompts} 条`
                    + (brain.knowledge.skipped > 0 ? `；另有 ${brain.knowledge.skipped} 条太长、超过 60 条或被截断，不会发` : '')}</small>
                  {brain.knowledge.unfilled > 0 && (
                    <small role="alert" data-testid="wecom-knowledge-unfilled">{`有 ${brain.knowledge.unfilled} 条答案里还有【】没填，这几条不会发给客户`
                      + (brain.knowledge.unfilledQuestions.length > 0 ? `：${brain.knowledge.unfilledQuestions.join('、')}` : '')}</small>
                  )}
                </div>
                {brain.knowledge.unfilled > 0 && <span className="shell-tag shell-tag--warn">{`${brain.knowledge.unfilled} 条没填`}</span>}
              </li>
            )}
            {status.limits.perConversation > 0 && (
              <li data-wecom-row="limits">
                <div><strong>AI 接待上限</strong><small>{`每位客户每天 ${status.limits.perConversation} 次，每个客服账号每天 ${status.limits.perAccount} 次；超过就转人工`}</small></div>
              </li>
            )}
          </ul>
          {status.evidence.length > 0 && (
            <ul className="shell-card-text" data-testid="wecom-evidence" style={{ paddingLeft: 18 }}>
              {status.evidence.map(line => <li key={line}>{line}</li>)}
            </ul>
          )}
          <details className="shell-card-text">
            <summary style={{ cursor: 'pointer' }}>中转站的地址和口令在哪里填</summary>
            <p>它们存在这台电脑的用户环境变量里（地址 AMPULY_WECOM_RELAY_URL，要 wss:// 开头；口令 AMPULY_WECOM_RELAY_TOKEN），和中转站部署时设的是同一个口令。改好后在这里点「刷新」看状态；控制台从不把口令显示出来。</p>
          </details>

          <h4 style={{ margin: '16px 0 6px' }}>这个项目的客服账号</h4>
          {status.accounts.length === 0 ? (
            <p className="shell-card-text" data-testid="wecom-no-account">还没有客服账号接在这个项目上。</p>
          ) : (
            <ul className="shell-config-list" data-testid="wecom-accounts">
              {status.accounts.map(account => (
                <li key={account.open_kfid} data-wecom-account={account.account_name || 'unnamed'}>
                  <div>
                    <strong>{account.account_name ? `客服账号「${account.account_name}」` : '没起名字的客服账号'}</strong>
                    <small>{`接「${flowName(account.workflow_id)}」`}
                      {account.owner_only_fields.length > 0 ? `；只由你填：${ownerOnlyLabels(account, flows).join('、')}` : '；开单项都可以问客户'}
                      {account.welcome_text ? `；欢迎语：${account.welcome_text.slice(0, 40)}${account.welcome_text.length > 40 ? '…' : ''}` : '；没有欢迎语'}</small>
                  </div>
                  <span className={`shell-tag${account.enabled ? ' shell-tag--ok' : ''}`}>{account.enabled ? '已打开' : '关着'}</span>
                  <button type="button" className="shell-btn" disabled={!connected || busy} onClick={() => open(account)}>修改</button>
                  <button type="button" className="shell-btn" disabled={!connected || busy}
                    onClick={() => { if (window.confirm(REMOVE_CONFIRM)) onRemoveAccount(account.open_kfid) }}>解绑</button>
                </li>
              ))}
            </ul>
          )}
          {editing === null ? (
            <p style={{ marginTop: 10 }}>
              <button type="button" className="shell-btn" data-testid="wecom-add-account" disabled={!connected || busy || !status.ready} onClick={() => open(null)}>＋ 接一个客服账号</button>
              {' '}<button type="button" className="shell-btn" data-testid="wecom-refresh" disabled={!connected} onClick={onAsk}>刷新</button>
            </p>
          ) : (
            <form className="shell-card shell-card--muted" data-testid="wecom-account-form" style={{ marginTop: 10 }}
              onSubmit={event => { event.preventDefault(); save() }}>
              <label style={{ display: 'block' }}>客服账号 ID（在企业微信管理后台「微信客服」里复制）
                <input value={draft.open_kfid} disabled={editing !== 'new'} onChange={event => setDraft(prev => ({ ...prev, open_kfid: event.target.value }))} />
              </label>
              <label style={{ display: 'block' }}>名字（你自己认得的叫法）
                <input value={draft.account_name} maxLength={60} onChange={event => setDraft(prev => ({ ...prev, account_name: event.target.value }))} />
              </label>
              <label style={{ display: 'block' }}>接哪条业务流
                <select value={draft.workflow_id} onChange={event => setDraft(prev => ({ ...prev, workflow_id: event.target.value, owner_only_fields: [] }))}>
                  {flows.length === 0 && <option value="">（这个项目还没有能接单的业务流）</option>}
                  {flows.map(flow => <option key={flow.id} value={flow.id}>{flow.name}</option>)}
                </select>
              </label>
              {chosenFlow && chosenFlow.fields.length > 0 && (
                <fieldset data-testid="wecom-owner-only">
                  <legend>只由你填、AI 不去问客户的开单项</legend>
                  {chosenFlow.fields.map(field => (
                    <label key={field.id} style={{ marginRight: 12 }}>
                      <input type="checkbox" checked={draft.owner_only_fields.includes(field.id)}
                        onChange={event => setDraft(prev => ({
                          ...prev,
                          owner_only_fields: event.target.checked
                            ? [...prev.owner_only_fields, field.id]
                            : prev.owner_only_fields.filter(id => id !== field.id),
                        }))} />
                      {field.label}
                    </label>
                  ))}
                </fieldset>
              )}
              <label style={{ display: 'block' }}>欢迎语（客户进来时发的第一句，可以不填）
                <textarea rows={2} value={draft.welcome_text} onChange={event => setDraft(prev => ({ ...prev, welcome_text: event.target.value }))} />
              </label>
              <label style={{ display: 'block' }}>转人工时交给谁（企业微信里的接待人员账号，可以不填）
                <input value={draft.servicer_userid} onChange={event => setDraft(prev => ({ ...prev, servicer_userid: event.target.value }))} />
              </label>
              <label style={{ display: 'block' }}>
                <input type="checkbox" checked={draft.enabled} onChange={event => setDraft(prev => ({ ...prev, enabled: event.target.checked }))} />
                打开这个客服账号（通道也打开时，它收到的消息才由 AI 接待）
              </label>
              {problem && <p className="shell-card-text" role="alert">{problem}</p>}
              <p>
                <button type="submit" className="shell-btn shell-btn--primary" data-testid="wecom-account-save" disabled={!connected || busy}>
                  {state.busy === 'account' ? '保存中…' : '保存'}
                </button>
                {' '}<button type="button" className="shell-btn" onClick={() => { setEditing(null); setProblem('') }}>取消</button>
              </p>
            </form>
          )}
        </>
      )}
      {status && state.error && <p className="shell-card-text" role="alert" data-testid="wecom-error">{state.error}</p>}
    </section>
  )
}

/** An order's 企业微信 conversation in 业务工单: who said what and when, what we sent and how it went. Read only: a
 *  reply to the customer is written in 企业微信 (or by the AI 客服), never here. */
export function WecomConversationBlock({ conversation }: { conversation: WecomConversation }) {
  return (
    <section className="inbox-block" data-testid="order-wecom-conversation" aria-label="企业微信对话">
      <h3>企业微信对话<small>{conversation.stageWord ? `现在：${conversation.stageWord}` : ''}{' · 只读，回复请在企业微信里'}</small></h3>
      {conversation.handoffReason && <p className="inbox-group__empty">{`转人工的原因：${conversation.handoffReason}`}</p>}
      {conversation.lines.length === 0 ? (
        <p className="inbox-group__empty">还没有消息。</p>
      ) : (
        <ol className="inbox-log">
          {conversation.lines.map((line, index) => (
            <li key={`${line.at}:${index}`} data-from={line.from}>
              <strong>{line.from}</strong>
              <span>{` · ${line.text}`}</span>
              {line.outcome && <i>{` · ${line.outcome}`}</i>}
              <time dateTime={line.at}>{` · ${lineTime(line.at)}`}</time>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
