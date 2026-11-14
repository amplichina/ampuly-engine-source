import { useCallback, useEffect, useRef, useState } from 'react'
import { browserAccountLines, browserControlContext, browserFrameMatchesSession, browserOutcomeText, browserTaskControlText } from '../workspace/browserAccountState'
import type { BrowserAccountStatus, BrowserControlContext } from '../workspace/browserAccountState'
import { BrowserAccountInspection } from './BrowserAccountInspection'
import { browserInspectionMatchesSession, type BrowserAccountInspectionView } from '../workspace/browserAccountInspection'
import { BrowserLiveInput, browserInputScope, browserPoint, browserWheel, type InputResult } from '../lib/browserLiveInput'

export interface BrowserAgentFacts {
  available?: boolean
  in_flight?: number
  last_action?: { kind: string; outcome: string; reason?: string; at?: number } | null
  proposals?: number
  cancelled?: number
  demo?: { recording: boolean; demo_id?: string; steps: number; label?: string }
}

export interface BrowserSessionRow {
  binding_key: string
  project_id: string
  resource_id?: string
  control_fence?: string
  account?: BrowserAccountStatus | null
  customer_id: string
  environment_id: string
  task_id: string
  /** X05: the order (origin_task_id) a company-mode work-item's session belongs to; empty otherwise. */
  order_id?: string
  /** Q05 B5: the flow step whose work item opened it (its id and name as the flow has them); empty outside a flow. */
  node_id?: string
  node_name?: string
  /** X05 step 2: the order's anchor runtime session, so the console can place this session under the order's approval. */
  anchor_session_id?: string
  state: string
  note: string
  allowed_domains: string[]
  live: boolean
  quarantined: boolean
  lease_seconds_left?: number | null
  agent?: BrowserAgentFacts
  latest_screenshot?: string
  tabs?: Array<{ index: number; page: string; active: boolean }>
  /** X05 / C26: what the request-level policy refused for this session -- host names and a count only. */
  egress?: { policy: string[]; blocked: number; hosts: string[] }
}

export interface BrowserScreenshot {
  binding_key: string
  name: string
  data_url: string
}

export interface BrowserLiveFrame {
  binding_key: string
  project_id?: string
  resource_id?: string
  control_fence?: string
  data: string
  width?: number | null
  height?: number | null
}

export type BrowserViewInputEvent = Record<string, unknown>

export interface BrowserLimits {
  max_live: number
  live: number
  max_per_customer: number
  min_free_memory_mb: number
  free_memory_mb: number | null
}

export interface BrowserCombination {
  available: boolean
  verified?: string[]
  failed?: string[]
  missing?: string[]
  admitted?: boolean
  advice?: string
  executor?: string
  error?: string
}

export interface BrowserTrailEntry {
  source: string
  type: string
  at?: number | null
  kind?: string
  outcome?: string
  reason?: string
  state?: string
  effect_id?: string
  page?: string
  domains?: string[]
  by?: string
  to?: string
  status?: string
  name?: string
  /** 4.7-A17: who suggested a decision (puly / model:<name> / none), who checked it (code_check / owner /
   *  employee:<id> / none) and who has it now (owner / model:<name> / none); `said` is the server's sentence for it. */
  suggested_by?: string
  reviewed_by?: string
  fallback_to?: string
  said?: string
}

export interface BrowserTrail {
  binding_key: string
  task_id: string
  summary: {
    admitted: number
    refused: Record<string, number>
    receipts: Record<string, number>
    in_flight: number
    proposals: number
    effects: Record<string, number>
    open_questions: string[]
    lease_changes: number
    console?: Record<string, number>
    artifacts?: Record<string, number>
  }
  timeline: BrowserTrailEntry[]
}

/** One compiled candidate as `browser_candidate_control list` reports it; a damaged file says what is wrong instead. */
export interface BrowserCandidateRow {
  candidate_id: string
  demo_id?: string
  steps?: number
  intact: boolean
  admitted: boolean
  admitted_by?: string
  problem?: string
  /** The step numbers that reach outside the page; each needs its own approved approval at admission. */
  external_effect_steps?: number[]
}

/** "2=checkpoint-a, 5=checkpoint-b" -> {"2": "checkpoint-a", "5": "checkpoint-b"}; anything that is not
 *  step=reference is dropped rather than guessed at. */
export function parseApprovalRefs(text: string): Record<string, string> {
  const refs: Record<string, string> = {}
  for (const part of text.split(/[,，、;；\s]+/)) {
    // A reference may hold ':' (effect-approval:<id>) but never '=': a part with a second '=' is two pairs glued
    // together by a separator this does not know, and sending half of it as a reference would be a guess.
    const match = /^(\d{1,4})[=:：]([^\s=]{1,200})$/.exec(part.trim())
    if (match) refs[String(Number(match[1]))] = match[2]
  }
  return refs
}

export interface BrowserSessionsPanelProps {
  sessions: BrowserSessionRow[]
  connected: boolean
  error?: string | null
  locale?: string
  limits?: BrowserLimits | null
  combination?: BrowserCombination | null
  trail?: BrowserTrail | null
  lookBackNote?: string | null
  auditExportNote?: string | null
  screenshot?: BrowserScreenshot | null
  liveFrame?: BrowserLiveFrame | null
  liveBinding?: string | null
  accountInspection?: BrowserAccountInspectionView | null
  onInspectAccount?: (session: BrowserSessionRow) => void
  onRefresh: () => void
  onControl?: (bindingKey: string, command: string, note: string, ok?: boolean, context?: BrowserControlContext) => void
  onAuditTrail?: (bindingKey: string) => void
  onAuditExport?: (bindingKey: string) => void
  onLookBack?: (effectId: string) => void
  onScreenshot?: (bindingKey: string, name: string) => void
  onViewStart?: (bindingKey: string, context?: BrowserControlContext) => void
  onViewStop?: (bindingKey: string, context?: BrowserControlContext) => void
  onViewInput?: (bindingKey: string, event: BrowserViewInputEvent, context?: BrowserControlContext) => void
  /** X07 (owner): null = list not loaded yet. The whole candidate area is absent without `onCandidate`. */
  candidates?: BrowserCandidateRow[] | null
  candidateNote?: string | null
  /** The demonstration this console just sealed; its id is the only thing a compile needs. */
  sealedDemo?: { demo_id: string; steps: number } | null
  onCandidate?: (command: 'compile' | 'admit' | 'list', args: { demoId?: string; candidateId?: string; approvalRefs?: Record<string, string> }) => void
}

const MOUSE_BUTTONS = ['left', 'middle', 'right']

/** The live picture of one session, and -- while a person holds the page -- their mouse and keys on it. */
export function BrowserLiveView({ frame, takeover, restricted = false, onInput, onPick, zh }: { frame: BrowserLiveFrame; takeover: boolean; restricted?: boolean; onInput?: (event: BrowserViewInputEvent) => void; onPick?: (coord: { x: number; y: number }) => void; zh: boolean }) {
  const imageRef = useRef<HTMLImageElement | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const bridgeRef = useRef<BrowserLiveInput | null>(null)
  if (!bridgeRef.current) bridgeRef.current = new BrowserLiveInput()
  const bridge = bridgeRef.current
  const scope = browserInputScope(frame)
  const enabled = takeover && !restricted && !onPick && Boolean(onInput) && scope !== null
  bridge.configure(scope, enabled)
  const [inputNotice, setInputNotice] = useState<InputResult['error']>()
  const apply = (result: InputResult) => {
    if (!bridge.accepts(scope)) return
    if (result.error) setInputNotice(result.error)
    else if (result.events.length) setInputNotice(undefined)
    for (const event of result.events) onInput?.(event)
  }
  const scale = useCallback((clientX: number, clientY: number, image: HTMLImageElement) => {
    const rect = image.getBoundingClientRect()
    const width = frame.width || image.naturalWidth || rect.width
    const height = frame.height || image.naturalHeight || rect.height
    return browserPoint(clientX, clientY, rect, width, height)
  }, [frame.width, frame.height])
  useEffect(() => {
    setInputNotice(undefined)
    if (inputRef.current) inputRef.current.value = ''
  }, [scope, enabled])
  // React's delegated wheel listener is passive in current browsers. An explicit
  // non-passive listener keeps a remote scroll from scrolling the console too.
  useEffect(() => {
    const image = imageRef.current
    if (!image || !enabled) return
    const wheel = (event: WheelEvent) => {
      if (!bridge.accepts(scope)) return
      const rect = image.getBoundingClientRect()
      const point = scale(event.clientX, event.clientY, image)
      const delta = browserWheel(event.deltaX, event.deltaY, event.deltaMode,
        frame.width || image.naturalWidth || rect.width, frame.height || image.naturalHeight || rect.height)
      if (!point || !delta) return
      event.preventDefault()
      onInput?.({ kind: 'mouse', type: 'mouseWheel', ...point, ...delta, button: 'none' })
    }
    image.addEventListener('wheel', wheel, { passive: false })
    return () => image.removeEventListener('wheel', wheel)
  }, [enabled, scope, frame.width, frame.height, scale, onInput, bridge])
  const mouse = (type: string) => (event: React.MouseEvent<HTMLImageElement>) => {
    if (!bridge.accepts(scope)) return
    event.preventDefault()
    const point = scale(event.clientX, event.clientY, event.currentTarget)
    if (!point) return
    if (type === 'mousePressed') inputRef.current?.focus({ preventScroll: true })
    onInput?.({ kind: 'mouse', type, ...point,
      button: type === 'mouseMoved' ? (event.buttons & 1 ? 'left' : event.buttons & 2 ? 'right' : event.buttons & 4 ? 'middle' : 'none')
        : MOUSE_BUTTONS[event.button] ?? 'left', click_count: 1 })
  }
  const pick = (event: React.MouseEvent<HTMLImageElement>) => {
    if (!onPick) return
    event.preventDefault()
    const point = scale(event.clientX, event.clientY, event.currentTarget)
    if (point) onPick(point)
  }
  const key = (type: 'keyDown' | 'keyUp') => (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    const result = bridge.key(scope, type, { key: event.key, code: event.code, ctrlKey: event.ctrlKey, altKey: event.altKey,
      metaKey: event.metaKey, shiftKey: event.shiftKey, isComposing: event.nativeEvent.isComposing,
      keyCode: event.nativeEvent.keyCode, altGraph: event.getModifierState('AltGraph') })
    if (result.preventDefault) event.preventDefault()
    apply(result)
  }
  return <figure className={`capability-live${enabled ? ' capability-live--takeover' : ''}`} data-testid="browser-live-view" style={{ position: 'relative' }}>
    <img ref={imageRef} src={`data:image/jpeg;base64,${frame.data}`} alt={zh ? '会话实时画面' : 'live view of the session'} draggable={false}
      onClick={pick} onMouseMove={mouse('mouseMoved')} onMouseDown={mouse('mousePressed')} onMouseUp={mouse('mouseReleased')} onContextMenu={event => event.preventDefault()} />
    {enabled && <>
      <textarea key={scope} ref={inputRef} data-testid="browser-live-keyboard" aria-label={zh ? '输入到远端浏览器' : 'Type into the remote browser'}
        autoComplete="off" autoCorrect="off" autoCapitalize="off" spellCheck={false} rows={1}
        style={{ position: 'absolute', left: 4, bottom: 4, width: 2, height: 2, padding: 0, opacity: 0 }}
        onKeyDown={key('keyDown')} onKeyUp={key('keyUp')}
        onCompositionStart={() => bridge.compositionStart(scope)}
        onCompositionEnd={event => { apply(bridge.compositionEnd(scope, event.data)); event.currentTarget.value = '' }}
        onInput={event => {
          const native = event.nativeEvent as InputEvent
          apply(bridge.input(scope, event.currentTarget.value, native.isComposing))
          if (!native.isComposing && !bridge.isComposing(scope)) event.currentTarget.value = ''
        }}
        onPaste={event => {
          const result = bridge.paste(scope, event.clipboardData.getData('text/plain'))
          if (result.preventDefault) event.preventDefault()
          apply(result)
          if (!bridge.isComposing(scope)) event.currentTarget.value = ''
        }}
        onBlur={event => { apply(bridge.blur(scope)); event.currentTarget.value = '' }} />
      <button type="button" data-testid="browser-live-focus" onClick={() => inputRef.current?.focus({ preventScroll: true })}>{zh ? '键盘输入' : 'Keyboard input'}</button>
    </>}
    <figcaption className="capability-detail">{restricted
      ? (zh ? '受限核验：仅查看当前画面，业务操作不可用。' : 'Restricted verification: viewing the current picture; business actions unavailable.')
      : onPick
      ? (zh ? '选取页面元素：点击只读取元素，文字和滚轮不会操作页面。' : 'Pick an element: clicks only inspect; typing and scrolling do not operate the page.')
      : enabled
      ? (zh ? '你在操作这个页面（交给人）：点击后可输入中文、粘贴或滚动；单次文本最多 512 字。代理此时被拒。' : 'You are operating this page (handed to you): click to type, paste or scroll; up to 512 characters per text edit. The agent is refused meanwhile.')
      : (zh ? '当前画面只看不动；取得人工控制权后才能操作。' : 'Watch only; page input requires a current human control lease.')}</figcaption>
    {enabled && inputNotice && <p role="status" className="capability-detail" data-testid="browser-live-input-notice">{inputNotice === 'text_too_long'
      ? (zh ? '这段文字超过 512 字，未发送。请缩短后重新输入。' : 'This text exceeds 512 characters and was not sent. Shorten it and enter it again.')
      : (zh ? '文字含有无法传递的字符，未发送。' : 'This text contains unsupported characters and was not sent.')}</p>}
  </figure>
}

function consoleActionText(action: string, zh: boolean): string {
  const table: Record<string, [string, string]> = {
    hand_to_human: ['交给人工', 'handed to a person'], human_returned: ['人工交回', 'handed back'],
    identity_verified: ['核验通过', 'identity verified'], verification_failed: ['核验未过', 'verification failed'],
    revoked: ['已撤销', 'revoked'], lease_renewed: ['续租', 'lease renewed'],
  }
  const pair = table[action]
  return pair ? (zh ? pair[0] : pair[1]) : action
}

function whoText(value: string, zh: boolean): string {
  if (value === 'puly') return 'PULY'
  if (value === 'owner') return zh ? '你' : 'you'
  if (value === 'code_check') return zh ? '代码核对' : 'the code check'
  if (value.startsWith('model')) {
    const name = value.includes(':') ? value.slice(value.indexOf(':') + 1) : ''
    return zh ? (name ? `模型（${name}）` : '模型') : (name ? `the model ${name}` : 'the model')
  }
  if (value.startsWith('employee:')) return zh ? `员工 ${value.slice(9)}` : `employee ${value.slice(9)}`
  return zh ? '没有人' : 'nobody'
}

/** 4.7-A17: where a decision came from, who checked it, who has it now -- the server's sentence when there is one,
 *  else the three fields said one by one. '' for an entry that carries none of them. */
export function provenanceText(entry: BrowserTrailEntry, zh: boolean): string {
  if (!entry.suggested_by && !entry.reviewed_by && !entry.fallback_to) return ''
  if (zh && entry.said) return entry.said
  const [from, check, then] = [entry.suggested_by ?? 'none', entry.reviewed_by ?? 'none', entry.fallback_to ?? 'none']
  return zh
    ? `建议：${whoText(from, true)} → 复核：${whoText(check, true)} → 之后：${then === 'none' ? '不用再交给谁' : `交给${whoText(then, true)}`}`
    : `suggested by ${whoText(from, false)} → checked by ${whoText(check, false)} → then ${then === 'none' ? 'nobody else' : whoText(then, false)}`
}

function entryText(entry: BrowserTrailEntry, zh: boolean): string {
  const time = entry.at ? new Date(entry.at * 1000).toLocaleTimeString() : '—'
  const chain = provenanceText(entry, zh)
  if (chain && entry.source === 'effect') {
    return [time, zh ? '提交审批' : 'submit approval', chain, entry.page ?? '', entry.effect_id ? entry.effect_id.slice(0, 8) : ''].filter(Boolean).join(' · ')
  }
  if (chain) return `${time} · ${zh ? '决定' : 'decision'} · ${chain}`
  if (entry.source === 'console') return `${time} · ${zh ? '控制台' : 'console'} · ${consoleActionText(entry.type, zh)}`
  if (entry.source === 'artifact') {
    const what = entry.type === 'screenshot' ? (zh ? '截图' : 'screenshot') + (entry.name ? ` ${entry.name}` : '') : (zh ? '下载' : 'download')
    return `${time} · ${zh ? '交付物' : 'artifact'} · ${what}`
  }
  const parts = [entry.type]
  if (entry.kind) parts.push(entry.kind)
  if (entry.outcome) parts.push(outcomeText(entry.outcome, zh, entry.reason))
  if (entry.reason && !SAID_BY_LABEL.has(entry.reason)) parts.push(entry.reason)
  if (entry.state) parts.push(entry.state)
  if (entry.to) parts.push(`→ ${entry.to}`)
  if (entry.status) parts.push(entry.status)
  if (entry.page) parts.push(entry.page)
  if (entry.domains?.length) parts.push(entry.domains.join(' '))
  if (entry.by) parts.push(entry.by)
  if (entry.effect_id) parts.push(entry.effect_id.slice(0, 8))
  return `${time} · ${entry.source} · ${parts.join(' · ')}`
}

function countText(counts: Record<string, number>): string {
  const keys = Object.keys(counts)
  return keys.length ? keys.map(key => `${key} ${counts[key]}`).join('，') : '0'
}

function total(counts?: Record<string, number>): number {
  return counts ? Object.values(counts).reduce((sum, n) => sum + n, 0) : 0
}

const AGENT_SIDE = ['created', 'ready', 'agent_operating', 'identity_verified']

function leaseText(left: number | null | undefined, zh: boolean): string {
  if (left === null || left === undefined) return ''
  if (left <= 0) return zh ? '租约已到期' : 'lease ended'
  const minutes = Math.floor(left / 60)
  return zh ? `租约剩 ${minutes} 分` : `lease ${minutes} min left`
}

// Reasons the outcome label already says in words.
const SAID_BY_LABEL = new Set(['input_accepted_unverified', 'no_postcondition_stated'])

const outcomeText = browserOutcomeText

export function BrowserAccountSummary({ session, zh }: { session: BrowserSessionRow; zh: boolean }) {
  return <div className="capability-detail" data-testid="browser-account-status">
    {browserAccountLines(session.account, zh).map(line => <div key={line.key} data-account-fact={line.key}>{line.text}</div>)}
    <div data-account-fact="control">{zh ? '任务控制：' : 'Task control: '}{browserTaskControlText(session.state, zh)}</div>
  </div>
}

export function BrowserSessionsPanel({ sessions, connected, error, locale = 'zh-CN', limits, combination, trail, lookBackNote, auditExportNote, screenshot, liveFrame, liveBinding, accountInspection, onInspectAccount, onRefresh, onControl, onAuditTrail, onAuditExport, onLookBack, onScreenshot, onViewStart, onViewStop, onViewInput, candidates, candidateNote, sealedDemo, onCandidate }: BrowserSessionsPanelProps) {
  const zh = locale.startsWith('zh')
  const [notes, setNotes] = useState<Record<string, string>>({})
  const [demoDraft, setDemoDraft] = useState('')
  const [refDrafts, setRefDrafts] = useState<Record<string, string>>({})
  const demoId = (demoDraft.trim() || sealedDemo?.demo_id || '').trim()
  const label = (row: BrowserSessionRow) => row.customer_id || row.task_id || row.binding_key

  return <article className="capability-panel" data-testid="browser-sessions-panel">
    <div className="capability-panel-title">
      <h2>{zh ? '浏览器人工接管' : 'Browser handoff'}</h2>
      <span>{sessions.length}</span>
    </div>
    <div className="capability-muted">
      {zh ? '登录、交还、身份验证彼此独立；身份验证不会批准发布或业务闸门。代理的控制权是有期限的租约，到期只有你能续。' : 'Login, return and identity verification are separate. Verification never approves a release or business gate. The agent holds a lease; only you renew it.'}
    </div>
    {limits && <div className="capability-detail" data-testid="browser-limits">
      {zh ? `浏览器 ${limits.live}/${limits.max_live} · 每客户最多 ${limits.max_per_customer} · 空闲内存 ${limits.free_memory_mb === null ? '读不到' : `${limits.free_memory_mb} MB`}（下限 ${limits.min_free_memory_mb} MB）`
        : `browsers ${limits.live}/${limits.max_live} · per customer ${limits.max_per_customer} · free memory ${limits.free_memory_mb === null ? 'unknown' : `${limits.free_memory_mb} MB`} (floor ${limits.min_free_memory_mb} MB)`}
    </div>}
    {combination && <div className={`capability-detail${combination.available && combination.admitted ? '' : ' capability-detail--warn'}`} data-testid="browser-combination">
      {!combination.available
        ? (zh ? '本机真机证据：读不到账本' : 'Real-browser evidence for this machine: ledger unreadable')
        : combination.admitted
          ? (zh ? `本机真机证据已验证：${(combination.verified ?? []).join(' ')}` : `Real-browser evidence verified here: ${(combination.verified ?? []).join(' ')}`)
          : (zh ? `本机真机证据缺 ${(combination.missing ?? []).join(' ')}${(combination.failed ?? []).length ? `，失败 ${(combination.failed ?? []).join(' ')}` : ''}——先在这台机器上跑探针，再把真实客户的单交给浏览器`
            : `Real-browser evidence missing here: ${(combination.missing ?? []).join(' ')}${(combination.failed ?? []).length ? `, failed ${(combination.failed ?? []).join(' ')}` : ''} -- run the probe on this machine before a real customer's order`)}
    </div>}
    <button className="capability-refresh-btn" onClick={onRefresh} disabled={!connected}>{zh ? '刷新会话' : 'Refresh sessions'}</button>
    {error && <div className="capability-state capability-state--error" role="alert">{error}</div>}
    {sessions.length === 0 ? <div className="capability-muted">{zh ? '当前项目没有浏览器会话' : 'No browser session in this project'}</div> :
      <div className="capability-list">{sessions.map(row => {
        const agent = row.agent
        const factsAvailable = agent?.available !== false
        const recording = factsAvailable && Boolean(agent?.demo?.recording)
        const context = browserControlContext(row)
        const canControl = connected && Boolean(context)
        const restricted = row.account?.access_mode === 'verification_only'
        const currentFrame = liveFrame && browserFrameMatchesSession(liveFrame, row) ? liveFrame : null
        const frameContext = browserControlContext(currentFrame)
        const inspection = accountInspection && browserInspectionMatchesSession(accountInspection, row) ? accountInspection : null
        const act = (command: string, ok?: boolean) => {
          if (canControl && context) onControl?.(row.binding_key, command, notes[row.binding_key] ?? '', ok, context)
        }
        return <div className="capability-list-item capability-list-item--stacked" key={row.binding_key} data-testid="browser-session-row">
          <div>
            <strong>{label(row)}</strong>
            <small>{row.order_id ? `${zh ? '订单' : 'order'} ${row.order_id} · ` : ''}{row.node_name ? `${zh ? '步骤' : 'step'} ${row.node_name} · ` : ''}{row.task_id}{row.environment_id ? ` · ${row.environment_id}` : ''}</small>
            <small className="capability-detail">{row.state}{row.live ? ' · live' : ''}{row.quarantined ? ' · quarantined' : ''}{leaseText(row.lease_seconds_left, zh) ? ` · ${leaseText(row.lease_seconds_left, zh)}` : ''}</small>
            <BrowserAccountSummary session={row} zh={zh} />
            {onInspectAccount && <div className="capability-detail">
              <button type="button" onClick={() => onInspectAccount(row)} disabled={!connected || inspection?.status === 'pending'}>{zh ? '账号资料' : 'Account data'}</button>
              {inspection && <BrowserAccountInspection view={inspection} zh={zh} />}
            </div>}
            {row.note && <small className="capability-detail">{row.note}</small>}
            {row.allowed_domains.length > 0 && <small className="capability-detail">{row.allowed_domains.join(' · ')}</small>}
            {row.tabs && row.tabs.length > 0 && <small className="capability-detail" data-testid="browser-tabs">
              {zh ? '标签页：' : 'tabs: '}{row.tabs.map(tab => `${tab.active ? '▶ ' : ''}${tab.index} ${tab.page}`).join(' · ')}
            </small>}
            {agent && <small className="capability-detail" data-testid="browser-agent-facts">
              {!factsAvailable ? (zh ? '代理事实：不可用' : 'Agent facts: unavailable')
                : (zh ? `代理：飞行中 ${agent.in_flight ?? '未知'} · 待审批提交 ${agent.proposals ?? '未知'} · 已取消 ${agent.cancelled ?? '未知'}` : `agent: in flight ${agent.in_flight ?? 'unknown'} · submits awaiting approval ${agent.proposals ?? 'unknown'} · cancelled ${agent.cancelled ?? 'unknown'}`)}
              {factsAvailable && agent.last_action ? (zh ? ` · 最近 ${agent.last_action.kind} ${outcomeText(agent.last_action.outcome, zh, agent.last_action.reason)}` : ` · last ${agent.last_action.kind} ${outcomeText(agent.last_action.outcome, zh, agent.last_action.reason)}`) : ''}
              {recording ? (zh ? ` · 录制中（${agent?.demo?.steps ?? 0} 步）` : ` · recording (${agent?.demo?.steps ?? 0} steps)`) : ''}
            </small>}
          </div>
          {onControl && <div className="capability-inline-actions">
            <input aria-label={`${label(row)} note`} value={notes[row.binding_key] ?? ''} onChange={event => setNotes(current => ({ ...current, [row.binding_key]: event.target.value }))} placeholder={zh ? '说明或核验记录' : 'Reason or verification note'} />
            {AGENT_SIDE.includes(row.state) && <button onClick={() => act('hand_to_human')} disabled={!canControl || restricted}>{zh ? '交给我登录' : 'Hand to human'}</button>}
            {row.state === 'awaiting_human' && <button onClick={() => act('hand_back')} disabled={!canControl || restricted}>{zh ? '我已交还' : 'Return to agent'}</button>}
            {row.state === 'human_returned' && <>
              <button onClick={() => act('verify_identity', true)} disabled={!canControl || restricted}>{zh ? '核验通过' : 'Verify'}</button>
              <button onClick={() => act('verify_identity', false)} disabled={!canControl || restricted}>{zh ? '核验失败' : 'Reject identity'}</button>
            </>}
            {(row.state === 'lease_expired' || AGENT_SIDE.includes(row.state)) && <button onClick={() => act('renew_lease')} disabled={!canControl || restricted}>{row.state === 'lease_expired' ? (zh ? '续期让代理继续' : 'Renew lease') : (zh ? '续期' : 'Renew')}</button>}
            {AGENT_SIDE.includes(row.state) && !recording && <button onClick={() => act('demo_start')} disabled={!canControl || restricted || !factsAvailable}>{zh ? '开始录制演示' : 'Start recording'}</button>}
            {recording && <button onClick={() => act('demo_stop')} disabled={!canControl || restricted}>{zh ? '停止并密封演示' : 'Stop and seal demo'}</button>}
            {row.quarantined && <button onClick={() => act('retry_close')} disabled={!canControl}>{zh ? '重试关闭' : 'Retry close'}</button>}
            <button onClick={() => act('revoke_and_close')} disabled={!canControl || (notes[row.binding_key] ?? '').trim().length < 2}>{zh ? '撤销并关闭' : 'Revoke and close'}</button>
            {onAuditTrail && <button onClick={() => onAuditTrail(row.binding_key)} disabled={!connected}>{zh ? '查看审计' : 'Audit trail'}</button>}
            {onScreenshot && row.latest_screenshot && <button onClick={() => onScreenshot(row.binding_key, row.latest_screenshot!)} disabled={!connected}>{zh ? '查看截图' : 'Screenshot'}</button>}
            {onViewStart && row.live && liveBinding !== row.binding_key && <button onClick={() => context && onViewStart(row.binding_key, context)} disabled={!canControl}>{zh ? '打开画面' : 'Live view'}</button>}
            {onViewStop && liveBinding === row.binding_key && <button onClick={() => context && onViewStop(row.binding_key, context)} disabled={!canControl}>{zh ? '关闭画面' : 'Close view'}</button>}
          </div>}
          {liveBinding === row.binding_key && currentFrame && <BrowserLiveView
            frame={currentFrame} takeover={canControl && !restricted && row.state === 'awaiting_human'} restricted={restricted} zh={zh}
            onInput={onViewInput && frameContext ? (event) => onViewInput(row.binding_key, event, frameContext) : undefined} />}
          {screenshot && screenshot.binding_key === row.binding_key && <figure className="capability-screenshot" data-testid="browser-screenshot">
            <img src={screenshot.data_url} alt={zh ? `会话 ${label(row)} 的脱敏截图 ${screenshot.name}` : `masked screenshot ${screenshot.name} of ${label(row)}`} />
            <figcaption className="capability-detail">{zh ? `脱敏截图 ${screenshot.name}（密码框与敏感字段已遮罩）` : `masked screenshot ${screenshot.name} (password and sensitive fields covered)`}</figcaption>
          </figure>}
          {trail && trail.binding_key === row.binding_key && <div className="capability-detail" data-testid="browser-audit-trail">
            <div>{zh
              ? `审计：准入 ${trail.summary.admitted} · 飞行中 ${trail.summary.in_flight} · 提议 ${trail.summary.proposals} · 拒绝 ${countText(trail.summary.refused)} · 回执 ${countText(trail.summary.receipts)} · 效果 ${countText(trail.summary.effects)}${total(trail.summary.console) ? ` · 控制台 ${total(trail.summary.console)}` : ''}${total(trail.summary.artifacts) ? ` · 交付物 ${total(trail.summary.artifacts)}` : ''}${trail.summary.open_questions.length ? ` · 结果未知 ${trail.summary.open_questions.length} 项待回查` : ''}`
              : `audit: admitted ${trail.summary.admitted} · in flight ${trail.summary.in_flight} · proposals ${trail.summary.proposals} · refused ${countText(trail.summary.refused)} · receipts ${countText(trail.summary.receipts)} · effects ${countText(trail.summary.effects)}${total(trail.summary.console) ? ` · console ${total(trail.summary.console)}` : ''}${total(trail.summary.artifacts) ? ` · artifacts ${total(trail.summary.artifacts)}` : ''}${trail.summary.open_questions.length ? ` · ${trail.summary.open_questions.length} outcome unknown, look back` : ''}`}</div>
            {onAuditExport && <div className="capability-inline-actions">
              <button onClick={() => onAuditExport(row.binding_key)} disabled={!connected}>{zh ? '导出审计包' : 'Export audit bundle'}</button>
              {auditExportNote && <small className="capability-detail" data-testid="browser-audit-export-note">{auditExportNote}</small>}
            </div>}
            {trail.summary.open_questions.length > 0 && onLookBack && <div className="capability-inline-actions" data-testid="browser-look-back">
              {trail.summary.open_questions.map(effectId => <button key={effectId} onClick={() => onLookBack(effectId)} disabled={!connected}>
                {zh ? `回查 ${effectId.slice(0, 8)}` : `Look back ${effectId.slice(0, 8)}`}
              </button>)}
              <small className="capability-detail">{zh ? '回查只看页面证据：预期成立才判为已发生，绝不重发。' : 'A look-back reads the page: applied only when the expectation holds; never a second send.'}</small>
            </div>}
            {lookBackNote && <div className="capability-detail" data-testid="browser-look-back-note">{lookBackNote}</div>}
            <ol className="capability-timeline">{trail.timeline.slice(-40).map((entry, index) => <li key={`${entry.source}-${index}`}>{entryText(entry, zh)}</li>)}</ol>
          </div>}
        </div>
      })}</div>}
    {onCandidate && <div className="capability-detail" data-testid="browser-candidates">
      <strong>{zh ? '演示候选' : 'Demonstration candidates'}</strong>
      <div className="capability-muted">
        {zh ? '密封的演示先编译成候选，再由你准入；含外部动作的步骤必须各自引用一条已批准的审批记录（步序号=审批编号）。准入不等于运行。'
          : 'A sealed demonstration is compiled into a candidate, then admitted by you; every external-effect step must cite its own approved approval record (step=approval id). Admission is not a run.'}
      </div>
      <div className="capability-inline-actions">
        <input aria-label={zh ? '演示编号' : 'Demonstration id'} value={demoDraft} onChange={event => setDemoDraft(event.target.value)}
          placeholder={sealedDemo?.demo_id ? (zh ? `刚密封：${sealedDemo.demo_id}（${sealedDemo.steps} 步）` : `just sealed: ${sealedDemo.demo_id} (${sealedDemo.steps} steps)`) : (zh ? '演示编号' : 'Demonstration id')} />
        <button data-testid="browser-candidate-compile" onClick={() => onCandidate('compile', { demoId })} disabled={!connected || !demoId}>{zh ? '编译为候选' : 'Compile candidate'}</button>
        <button data-testid="browser-candidate-refresh" onClick={() => onCandidate('list', {})} disabled={!connected}>{zh ? '刷新候选' : 'Refresh candidates'}</button>
      </div>
      {candidateNote && <small className="capability-detail" data-testid="browser-candidate-note">{candidateNote}</small>}
      {candidates === null || candidates === undefined
        ? <div className="capability-muted">{zh ? '候选列表尚未载入' : 'Candidates not loaded yet'}</div>
        : candidates.length === 0
          ? <div className="capability-muted">{zh ? '还没有候选' : 'No candidate yet'}</div>
          : <div className="capability-list">{candidates.map(candidate => <div className="capability-list-item" key={candidate.candidate_id} data-testid="browser-candidate" data-admitted={candidate.admitted ? 'true' : 'false'}>
            <div>
              <strong>{candidate.candidate_id}</strong>
              <small className="capability-detail">
                {!candidate.intact
                  ? (zh ? `文件不完整：${candidate.problem ?? ''}` : `damaged: ${candidate.problem ?? ''}`)
                  : (zh ? `${candidate.steps ?? 0} 步 · 演示 ${candidate.demo_id ?? '—'}` : `${candidate.steps ?? 0} steps · demo ${candidate.demo_id ?? '—'}`)}
              </small>
              {candidate.intact && <small className="capability-detail" data-testid="browser-candidate-effect-steps">
                {(candidate.external_effect_steps ?? []).length === 0
                  ? (zh ? '没有外部动作步，准入不需要审批引用' : 'no external-effect step; admission needs no approval reference')
                  : (zh ? `外部动作步：${(candidate.external_effect_steps ?? []).join('、')}（每一步各需一条已批准的审批）` : `external-effect steps: ${(candidate.external_effect_steps ?? []).join(', ')} (each needs its own approved approval)`)}
              </small>}
              {candidate.intact && !candidate.admitted && <span className="capability-inline-actions">
                <input aria-label={`${candidate.candidate_id} approval refs`} value={refDrafts[candidate.candidate_id] ?? ''}
                  onChange={event => setRefDrafts(current => ({ ...current, [candidate.candidate_id]: event.target.value }))}
                  placeholder={(candidate.external_effect_steps ?? []).length === 0
                    ? (zh ? '没有外部动作步，留空' : 'no external-effect step: leave blank')
                    : (candidate.external_effect_steps ?? []).map(step => `${step}=${zh ? '审批编号' : 'approval id'}`).join(', ')} />
                <button data-testid="browser-candidate-admit" disabled={!connected}
                  onClick={() => onCandidate('admit', { candidateId: candidate.candidate_id, approvalRefs: parseApprovalRefs(refDrafts[candidate.candidate_id] ?? '') })}>
                  {zh ? '准入' : 'Admit'}
                </button>
              </span>}
            </div>
            <span className={`capability-pill capability-pill--${candidate.admitted ? 'ok' : candidate.intact ? 'muted' : 'warn'}`}>
              {candidate.admitted ? (zh ? `已准入 · ${candidate.admitted_by ?? ''}` : `admitted · ${candidate.admitted_by ?? ''}`) : candidate.intact ? (zh ? '未准入' : 'not admitted') : (zh ? '不可用' : 'unusable')}
            </span>
          </div>)}</div>}
    </div>}
  </article>
}
