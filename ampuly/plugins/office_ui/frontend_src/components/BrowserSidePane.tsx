import { useEffect, useRef, useState } from 'react'
import type { BrowserLiveFrame, BrowserSessionRow, BrowserViewInputEvent } from './BrowserSessionsPanel'
import { BrowserAccountSummary, BrowserLiveView } from './BrowserSessionsPanel'
import { browserControlContext, browserFrameMatchesSession } from '../workspace/browserAccountState'
import type { BrowserControlContext } from '../workspace/browserAccountState'
import type { DashboardBlocker } from './DashboardPage'
import { OwnerCard } from './OwnerCard'
import type { Answering } from '../lib/answerPath'

// 4.3 X05 / V03: the client's own browser window, on the right. When an
// order's page needs a person -- a login, a code -- the agent hands it over
// and this pane opens with the live picture; the person operates on it
// with a browser's own controls (tabs, address bar, back, reload, frequent
// sites), hands it back, the owner verifies, the agent goes on. Nothing
// leaves the client; every address still goes through the order's domain gate.

export interface BrowserSidePaneProps {
  session: BrowserSessionRow | null
  bindingKey: string
  taskId: string
  orderTitle?: string
  note?: string
  frame: BrowserLiveFrame | null
  connected: boolean
  locale?: string
  onControl: (bindingKey: string, command: string, note?: string, ok?: boolean, context?: BrowserControlContext) => void
  onInput?: (bindingKey: string, event: BrowserViewInputEvent, context?: BrowserControlContext) => void
  /** X05 pointer picking: the owner points at the live view; the console reads what element is there. */
  onViewPick?: (bindingKey: string, x: number, y: number, context?: BrowserControlContext) => void
  pickedElement?: { tag?: string; type?: string; role?: string; placeholder?: string; text?: string } | null
  onClose: () => void
  /** X05: the pauses this order is waiting on, answered here instead of on another page. */
  blockers?: DashboardBlocker[]
  onDecideBlocker?: (blocker: DashboardBlocker, optionId: string) => void
  /** By checkpoint id: the answer that is out (lib/answerPath), and what the last one came back with, in words. */
  answering?: Answering
  decisionErrors?: Record<string, string>
  /** Open an adjacent execution tool without pretending it is part of the browser runtime. */
  onOpenTool?: (tool: 'review' | 'terminal' | 'files' | 'chat' | 'desktop') => void
}

export interface BrowserBookmark { label: string; url: string }
type ExecutionTool = 'review' | 'terminal' | 'browser' | 'files' | 'chat' | 'desktop'
interface ExecutionToolTab { id: number; tool: ExecutionTool }
const TOOL_META: Record<ExecutionTool, { zh: string; en: string; icon: string }> = {
  review: { zh: '审查', en: 'Review', icon: '⌕' }, terminal: { zh: '工具事件', en: 'Tool events', icon: '>_' },
  browser: { zh: '浏览器画面', en: 'Browser surface', icon: '◎' }, files: { zh: '文件', en: 'Files', icon: '▱' },
  chat: { zh: '侧边聊天', en: 'Side chat', icon: '◌' }, desktop: { zh: '桌面控制', en: 'Desktop controls', icon: '▣' },
}

/** The pauses that belong to this browser session's task: the order's own, or -- in company mode -- the work item's that is waiting. */
export function blockersForPane(blockers: DashboardBlocker[], taskId: string, anchorSessionId = '', orderId = ''): DashboardBlocker[] {
  // X05: a company-mode approval is anchored on the order's runtime session, not the work item's own
  // task, so match the order and its anchor session as well as the session's task.
  const ids = new Set([taskId, anchorSessionId, orderId].filter(Boolean))
  if (!ids.size) return []
  return blockers.filter(blocker => ids.has(blocker.task_id ?? '') || ids.has(blocker.session_id ?? '') || ids.has(blocker.waiting_task_id ?? ''))
}

const STATE_ZH: Record<string, string> = {
  awaiting_human: '等你操作', human_returned: '已交还，待核验', identity_verified: '身份已核验', agent_operating: '代理操作中',
  ready: '就绪', created: '已创建', lease_expired: '租约到期', revoked: '已撤销', closed: '已关闭',
}
const WIDTH_KEY = 'superones.browser-pane.width'
const BOOKMARKS_KEY = 'superones.browser-pane.bookmarks'
const MIN_WIDTH = 420

function readStored<T>(key: string, fallback: T): T {
  try {
    const raw = typeof window !== 'undefined' ? window.localStorage.getItem(key) : null
    return raw ? (JSON.parse(raw) as T) : fallback
  } catch { return fallback }
}
function writeStored(key: string, value: unknown): void {
  try { window.localStorage.setItem(key, JSON.stringify(value)) } catch { /* a browser that keeps nothing is fine */ }
}
/** Only the host and path of an address are kept as a frequent site: never a query string, never a credential. */
export function bookmarkUrl(raw: string): string {
  const text = raw.trim()
  if (!text) return ''
  try {
    const url = new URL(text.includes('://') ? text : `https://${text}`)
    return `${url.protocol}//${url.host}${url.pathname === '/' ? '' : url.pathname}`
  } catch { return '' }
}

export function BrowserSidePane({ session, bindingKey, taskId, orderTitle, note, frame, connected, locale = 'zh-CN', onControl, onInput, onViewPick, pickedElement, onClose, blockers = [], onDecideBlocker, answering, decisionErrors, onOpenTool }: BrowserSidePaneProps) {
  const zh = locale.startsWith('zh')
  const state = session?.state ?? ''
  const host = session?.allowed_domains?.[0] ?? ''
  const title = orderTitle || host || taskId.slice(0, 8)
  const context = session?.binding_key === bindingKey ? browserControlContext(session) : null
  const canControl = connected && Boolean(context)
  const restricted = session?.account?.access_mode === 'verification_only'
  const currentFrame = frame && session?.binding_key === bindingKey && browserFrameMatchesSession(frame, session) ? frame : null
  const frameContext = browserControlContext(currentFrame)
  const takeover = canControl && !restricted && Boolean(currentFrame) && state === 'awaiting_human'
  const reason = (state === 'awaiting_human' ? (session?.note || note) : '') || ''
  const tabs = session?.tabs ?? []
  const active = tabs.find(tab => tab.active) ?? tabs[0]
  const currentUrl = active ? `https://${active.page}` : ''

  const [width, setWidth] = useState<number>(() => readStored<number>(WIDTH_KEY, 0))
  const [wide, setWide] = useState(false)
  const [address, setAddress] = useState(currentUrl)
  const [editing, setEditing] = useState(false)
  const [bookmarks, setBookmarks] = useState<BrowserBookmark[]>(() => readStored<BrowserBookmark[]>(BOOKMARKS_KEY, []))
  const [bookmarksOpen, setBookmarksOpen] = useState(false)
  const [toolsOpen, setToolsOpen] = useState(false)
  const [toolTabs, setToolTabs] = useState<ExecutionToolTab[]>([{ id: 1, tool: 'browser' }])
  const [activeToolId, setActiveToolId] = useState(1)
  const [picking, setPicking] = useState(false)
  const dragging = useRef(false)

  useEffect(() => { if (!editing) setAddress(currentUrl) }, [currentUrl, editing])
  useEffect(() => {
    const move = (event: MouseEvent) => {
      if (!dragging.current) return
      const next = Math.max(MIN_WIDTH, Math.min(window.innerWidth * 0.95, window.innerWidth - event.clientX))
      setWidth(next)
    }
    const up = () => { if (dragging.current) { dragging.current = false; setWidth(current => { writeStored(WIDTH_KEY, current); return current }) } }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
    return () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up) }
  }, [])

  // Both closures retain the generation rendered with the control or picture, not a later row lookup.
  const control = (command: string, value?: string, ok?: boolean) => {
    if (canControl && context && !restricted) onControl(bindingKey, command, value, ok, context)
  }
  const drive = (command: string, value?: string) => {
    if (takeover && frameContext) onControl(bindingKey, command, value, undefined, frameContext)
  }
  const go = () => {
    const target = address.trim()
    if (!target) return
    setEditing(false)
    drive('navigate', target)
  }
  const addBookmark = () => {
    const url = bookmarkUrl(address || currentUrl)
    if (!url || bookmarks.some(item => item.url === url)) return
    const next = [...bookmarks, { label: new URL(url).host, url }]
    setBookmarks(next); writeStored(BOOKMARKS_KEY, next); setBookmarksOpen(true)
  }
  const removeBookmark = (url: string) => {
    const next = bookmarks.filter(item => item.url !== url)
    setBookmarks(next); writeStored(BOOKMARKS_KEY, next)
  }
  const driveTitle = restricted ? (zh ? '受限核验期间不接受页面输入' : 'Page input is unavailable during restricted verification')
    : !context ? (zh ? '当前控制信息不可用' : 'Current control context is unavailable')
      : !currentFrame ? (zh ? '等待与当前会话匹配的画面' : 'Waiting for a picture matching the current session')
        : takeover ? undefined : (zh ? '先点「交给我」再操作页面' : 'Hand the page to yourself first')
  const style = wide ? { width: '95vw' } : (width ? { width: `${width}px` } : undefined)
  const activeTool = toolTabs.find(tab => tab.id === activeToolId)
  const addTool = (tool: ExecutionTool) => {
    const existing = toolTabs.find(tab => tab.tool === tool)
    if (existing) setActiveToolId(existing.id)
    else {
      const id = Math.max(0, ...toolTabs.map(tab => tab.id)) + 1
      setToolTabs(current => [...current, { id, tool }]); setActiveToolId(id)
    }
    setToolsOpen(false)
  }
  const removeTool = (id: number) => {
    const next = toolTabs.filter(tab => tab.id !== id)
    setToolTabs(next)
    if (activeToolId === id) setActiveToolId(next.at(-1)?.id ?? 0)
  }

  return <aside className={`browser-pane${wide ? ' browser-pane--wide' : ''}`} style={style} data-testid="browser-side-pane" data-state={state} aria-label={zh ? '内置浏览器' : 'Built-in browser'}>
    <div className="browser-pane__resize" onMouseDown={(event) => { event.preventDefault(); dragging.current = true }} title={zh ? '拖动调整宽度' : 'Drag to resize'} />
    <header className="browser-pane__tools">
      <div className="browser-pane__tool-tabs" role="tablist" aria-label={zh ? '执行工具' : 'Execution tools'}>
        {toolTabs.map(tab => { const meta = TOOL_META[tab.tool]; return <div key={tab.id} className={`browser-pane__tool-tab${tab.id === activeToolId ? ' is-active' : ''}`}>
          <button type="button" role="tab" aria-selected={tab.id === activeToolId} onClick={() => setActiveToolId(tab.id)}>
            <span aria-hidden="true">{meta.icon}</span><span>{zh ? meta.zh : meta.en}</span>
            {tab.tool === 'browser' && <small title={title}>{title}</small>}
          </button>
          <button type="button" className="browser-pane__tool-close" onClick={() => removeTool(tab.id)} aria-label={zh ? `关闭${meta.zh}标签` : `Close ${meta.en} tab`}>×</button>
        </div>})}
      </div>
      <div className="browser-pane__add-wrap">
        <button type="button" className="browser-pane__icon browser-pane__add" aria-label={zh ? '添加执行工具' : 'Add an execution tool'}
          aria-expanded={toolsOpen} onClick={() => setToolsOpen(open => !open)}>+</button>
        {toolsOpen && <div className="browser-pane__tool-menu" role="menu" data-testid="browser-tool-menu">
          {(Object.entries(TOOL_META) as Array<[ExecutionTool, (typeof TOOL_META)[ExecutionTool]]>).map(([tool, meta]) =>
            <button key={tool} type="button" role="menuitem" onClick={() => addTool(tool)}><span>{meta.icon}</span><span>{zh ? meta.zh : meta.en}</span></button>)}
        </div>}
      </div>
      <span className="browser-pane__spacer" />
      <span className={`browser-pane__state-dot browser-pane__state--${state || 'none'}`} title={zh ? (STATE_ZH[state] ?? state ?? '') : state} />
      <button type="button" className="browser-pane__icon" onClick={() => setWide(current => !current)} aria-label={wide ? (zh ? '还原宽度' : 'Restore width') : (zh ? '放大' : 'Expand')}>{wide ? '⤡' : '⤢'}</button>
      <button type="button" className="browser-pane__icon browser-pane__close" onClick={onClose} aria-label={zh ? '关闭内置浏览器' : 'Close the browser'}>×</button>
    </header>
    {activeTool?.tool === 'browser' ? <>
    <div className="browser-pane__tabs browser-pane__page-tabs" role="tablist" aria-label={zh ? '浏览器标签页' : 'Browser tabs'}>
      {tabs.length === 0 && <span className="browser-pane__tab browser-pane__tab--active" role="tab">{zh ? '浏览器' : 'Browser'} · {title}</span>}
      {tabs.map(tab => <span key={tab.index} className={`browser-pane__tab${tab.active ? ' browser-pane__tab--active' : ''}`}>
        <button type="button" role="tab" aria-selected={tab.active} className="browser-pane__tab-title"
          disabled={!connected || !takeover || tab.active} title={driveTitle ?? tab.page} onClick={() => drive('switch_tab', String(tab.index))}>
          {tab.page.split('/')[0]}{tabs.length > 1 ? ` · ${tab.index + 1}` : ''}</button>
        {tab.active && tabs.length > 1 && <button type="button" className="browser-pane__icon browser-pane__tab-close" disabled={!connected || !takeover}
          aria-label={zh ? '关闭标签页' : 'Close tab'} title={driveTitle ?? (zh ? '关闭标签页' : 'Close tab')} onClick={() => drive('close_tab', String(tab.index))}>×</button>}
      </span>)}
      <button type="button" className="browser-pane__icon" disabled={!connected || !takeover || !session?.live} aria-label={zh ? '新标签页' : 'New tab'}
        title={driveTitle ?? (zh ? '在新标签页打开地址栏里的地址（空则打开这一单的站点）' : 'Open the address in a new tab')}
        onClick={() => drive('open_tab', bookmarkUrl(address) || (host ? `https://${host}` : ''))}>+</button>
    </div>
    <div className="browser-pane__bar">
      <button type="button" className="browser-pane__icon" disabled={!connected || !takeover} title={driveTitle ?? (zh ? '后退' : 'Back')} onClick={() => drive('back')}>←</button>
      <button type="button" className="browser-pane__icon" disabled={!connected || !takeover} title={driveTitle ?? (zh ? '刷新' : 'Reload')} onClick={() => drive('reload')}>⟳</button>
      <input className="browser-pane__address" type="text" value={address} aria-label={zh ? '地址' : 'Address'} placeholder={zh ? '只能打开这一单允许的站点' : 'Only this order\'s allowed sites'}
        disabled={!connected || !takeover} title={driveTitle}
        onFocus={() => setEditing(true)} onBlur={() => setEditing(false)} onChange={event => setAddress(event.target.value)}
        onKeyDown={event => { if (event.key === 'Enter') go() }} />
      <button type="button" className="browser-pane__icon" disabled={!connected || !takeover} title={driveTitle ?? (zh ? '前往' : 'Go')} onClick={go}>→</button>
      <button type="button" className="browser-pane__icon" aria-label={zh ? '复制地址' : 'Copy address'} title={zh ? '复制当前地址' : 'Copy the current address'} disabled={!currentUrl}
        onClick={() => { try { void navigator.clipboard?.writeText(currentUrl) } catch { /* a clipboard that refuses is not an error worth a dialog */ } }}>⧉</button>
      <button type="button" className="browser-pane__icon" onClick={addBookmark} title={zh ? '把当前页加入常用网址' : 'Add this page to frequent sites'} disabled={!(address || currentUrl)}>☆</button>
      <button type="button" className="browser-pane__icon" onClick={() => setBookmarksOpen(current => !current)} aria-expanded={bookmarksOpen} title={zh ? '常用网址' : 'Frequent sites'}>{bookmarksOpen ? '▴' : '▾'}</button>
    </div>
    {bookmarksOpen && <ul className="browser-pane__bookmarks" data-testid="browser-pane-bookmarks">
      {bookmarks.length === 0 && <li className="capability-detail">{zh ? '还没有常用网址：打开一个页面后点 ☆ 加入。' : 'No frequent sites yet: open a page and press ☆.'}</li>}
      {bookmarks.map(item => <li key={item.url}>
        <button type="button" disabled={!connected || !takeover} title={driveTitle ?? item.url} onClick={() => drive('navigate', item.url)}>{item.label}</button>
        <span className="capability-detail">{item.url}</span>
        <button type="button" className="browser-pane__icon" aria-label={zh ? '移除' : 'Remove'} onClick={() => removeBookmark(item.url)}>×</button>
      </li>)}
    </ul>}
    {(state === 'awaiting_human' || state === 'human_returned' || state === 'agent_operating' || state === 'identity_verified' || state === 'ready') && <div className="browser-pane__handoff" data-state={state}>
      <span className={`browser-pane__state browser-pane__state--${state || 'none'}`}>{zh ? (STATE_ZH[state] ?? state ?? '') : state}</span>
      {takeover && reason && <span className="browser-pane__handoff-note">{reason}</span>}
      <span className="browser-pane__spacer" />
      {state === 'awaiting_human' && <button onClick={() => control('hand_back', zh ? '我操作完了' : 'done')} disabled={!canControl || restricted}>
        {zh ? '完成并交还' : 'Done and hand back'}</button>}
      {state === 'human_returned' && <>
        <button onClick={() => control('verify_identity', zh ? '本人确认' : 'owner confirmed', true)} disabled={!canControl || restricted}>{zh ? '通过，继续' : 'Verify and continue'}</button>
        <button className="browser-pane__quiet-action" onClick={() => control('verify_identity', zh ? '未通过' : 'not verified', false)} disabled={!canControl || restricted}>{zh ? '未通过' : 'Not verified'}</button>
      </>}
      {(state === 'agent_operating' || state === 'identity_verified' || state === 'ready') && <button onClick={() => control('hand_to_human', zh ? '我来操作' : 'taking over')} disabled={!canControl || restricted}>
        {zh ? '交给我' : 'Hand to me'}</button>}
    </div>}
    {session && <BrowserAccountSummary session={session} zh={zh} />}
    {state === 'identity_verified' && session?.note && <p className="capability-detail browser-pane__facts" data-testid="browser-pane-facts">
      {zh ? '页面事实：' : 'Page facts: '}{session.note}</p>}
    {state === 'human_returned' && <p className="browser-pane__ask browser-pane__facts" data-testid="browser-pane-facts">
      {zh ? '页面自报（不能替代你的核验，页面可以伪造这些）：' : 'Page-reported (not a substitute for your check; a page can forge these): '}{session?.note || (zh ? '（无记录）' : '(none recorded)')}</p>}
    {session?.egress && session.egress.hosts.length > 0 && <section className="browser-pane__egress" data-testid="browser-pane-egress">
      <strong>{zh ? `页面有 ${session.egress.blocked} 个请求被出口策略拦住` : `${session.egress.blocked} requests were refused by the egress policy`}</strong>
      <p className="capability-detail">{session.egress.hosts.join(', ')}</p>
      <button type="button" disabled={!canControl || restricted} onClick={() => control('allow_hosts', session.egress!.hosts.join(','))}>
        {zh ? '放行这些域名并重载页面' : 'Allow these hosts and reload'}</button>
    </section>}
    {blockers.length > 0 && <section className="browser-pane__approvals" data-testid="browser-pane-approvals">
      {/* The order's pauses as the one owner card (components/OwnerCard), compact; answered through the same path
          as everywhere else. A gate is not reviewed in the pane: its card leads to 打开这张单. */}
      {blockers.map(blocker => <OwnerCard key={blocker.checkpoint_id} blocker={blocker} layout="compact" connected={connected} locale={zh ? 'zh-CN' : 'en'}
        attributes={{ 'data-pane-checkpoint': blocker.checkpoint_id }}
        pending={answering?.[blocker.checkpoint_id]?.state === 'pending'} refusal={decisionErrors?.[blocker.checkpoint_id]}
        onDecide={onDecideBlocker ?? (() => undefined)} onReview={onOpenTool ? () => onOpenTool('review') : undefined} onOpen={onOpenTool ? () => onOpenTool('review') : undefined} />)}
    </section>}
    <div className="browser-pane__body">
      {currentFrame
        ? <BrowserLiveView frame={currentFrame} takeover={takeover} restricted={restricted} zh={zh} onInput={onInput && frameContext ? (event) => onInput(bindingKey, event, frameContext) : undefined}
            onPick={connected && picking && onViewPick && frameContext ? (coord) => onViewPick(bindingKey, coord.x, coord.y, frameContext) : undefined} />
        : <p className="capability-detail">{zh ? '等待画面…（页面打开后这里会显示实时画面）' : 'Waiting for the picture… (the live view appears once the page is open)'}</p>}
      {currentFrame && onViewPick && <div className="browser-pane__pick" data-testid="browser-pick">
        <button type="button" onClick={() => setPicking(current => !current)} aria-pressed={picking} disabled={!connected}>
          {picking ? (zh ? '拾取中：点画面看元素' : 'Picking: click the view') : (zh ? '拾取元素' : 'Pick element')}</button>
        {pickedElement && <span className="capability-detail" data-testid="browser-picked">{`<${pickedElement.tag || '?'}>`}{pickedElement.role ? ` role=${pickedElement.role}` : ''}{pickedElement.type ? ` type=${pickedElement.type}` : ''}{pickedElement.text ? ` · ${pickedElement.text}` : ''}</span>}
      </div>}
    </div>
    </> : activeTool ? <div className="browser-pane__tool-content" data-tool={activeTool.tool}>
      <span aria-hidden="true">{TOOL_META[activeTool.tool].icon}</span>
      <strong>{zh ? TOOL_META[activeTool.tool].zh : TOOL_META[activeTool.tool].en}</strong>
      <p>{zh ? '打开执行区对应页签（同一套工作台工具），不在这里复制另一套管理界面。' : 'Opens the matching execution-workspace tab (one tool model); this pane does not duplicate another console.'}</p>
      <button type="button" disabled={!onOpenTool} onClick={() => { if (activeTool.tool !== 'browser') onOpenTool?.(activeTool.tool) }}>{zh ? `打开${TOOL_META[activeTool.tool].zh}` : `Open ${TOOL_META[activeTool.tool].en}`}</button>
    </div> : <div className="browser-pane__tool-content"><strong>{zh ? '点击＋打开工具' : 'Press + to open a tool'}</strong></div>}
  </aside>
}
