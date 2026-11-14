import { useEffect, useState } from 'react'
import type { ReactElement } from 'react'

import { browserControlContext, browserFrameMatchesSession } from '../workspace/browserAccountState'
import type { BrowserControlContext } from '../workspace/browserAccountState'
import { sessionStateLabel } from '../lib/executionEntry'
import type { BrowserLiveFrame, BrowserSessionRow, BrowserViewInputEvent } from './BrowserSessionsPanel'
import { BrowserLiveView } from './BrowserSessionsPanel'

/** 执行区「浏览器」主表面：嵌入 LiveView，而不是只放列表启动器（FE-EB-01/02）。独立实现，不移植 Evo webview。 */
export type ExecutionBrowserWorkspaceProps = {
  zh: boolean
  sessions: readonly BrowserSessionRow[]
  focused: BrowserSessionRow | undefined
  frame: BrowserLiveFrame | null
  connected: boolean
  canInput: boolean
  emptyText: string
  listHint: string
  onFocusSession: (session: BrowserSessionRow) => void
  onControl: (
    bindingKey: string,
    command: string,
    note?: string,
    ok?: boolean,
    context?: BrowserControlContext,
  ) => void
  onInput?: (bindingKey: string, event: BrowserViewInputEvent, context?: BrowserControlContext) => void
  /** 需要地址栏/书签等完整 chrome 时，打开既有 BrowserSidePane（不削弱门禁/租约）。 */
  onOpenFullTools?: (session: BrowserSessionRow) => void
  /** Main execution window owns the task strip; render just browser chrome and pixels here. */
  surfaceOnly?: boolean
  onViewPick?: (bindingKey: string, x: number, y: number, context?: BrowserControlContext) => void
  pickedElement?: { tag?: string; role?: string; type?: string; text?: string } | null
}

export function ExecutionBrowserWorkspace(props: ExecutionBrowserWorkspaceProps): ReactElement {
  const {
    zh, sessions, focused, frame, connected, canInput,
    emptyText, listHint, onFocusSession, onControl, onInput, onOpenFullTools, surfaceOnly, onViewPick, pickedElement,
  } = props
  const activePage = focused?.tabs?.find(tab => tab.active)?.page || focused?.tabs?.[0]?.page || ''
  const [address, setAddress] = useState(activePage)
  const [picking, setPicking] = useState(false)
  useEffect(() => { setAddress(activePage); setPicking(false) }, [activePage, focused?.binding_key])

  if (!sessions.length) {
    return (
      <div className={`shell-execution-browser${surfaceOnly ? ' execution-surface-empty' : ''}`} data-testid="shell-execution-browser" data-surface="empty">
        <strong>{surfaceOnly ? (zh ? '当前没有浏览器任务' : 'No browser task is open') : emptyText}</strong>
        <p className="shell-card-text" data-testid="shell-execution-browser-honest">{surfaceOnly
          ? (zh ? '任务打开网页后，页面会直接显示在这里。' : 'Pages opened by a task appear here.')
          : (zh
            ? '这里是受控浏览器的画面位（远程帧流），不是本机内嵌浏览器。没有 live 会话时不会假装有网页。'
            : 'This is the controlled-browser picture slot (remote frame stream), not a local embedded browser. No fake page when there is no live session.')}
        </p>
      </div>
    )
  }

  const session = focused
  const bindingKey = session?.binding_key ?? ''
  const context = session ? browserControlContext(session) : null
  const canControl = connected && canInput && Boolean(context)
  const restricted = session?.account?.access_mode === 'verification_only'
  const currentFrame = frame && session && browserFrameMatchesSession(frame, session) ? frame : null
  const frameContext = browserControlContext(currentFrame)
  const state = session?.state ?? ''
  const takeover = canControl && !restricted && Boolean(currentFrame) && state === 'awaiting_human'
  const reason = state === 'awaiting_human' ? (session?.note || '') : ''

  const control = (command: string, value?: string, ok?: boolean) => {
    if (session && canControl && context && !restricted) onControl(bindingKey, command, value, ok, context)
  }
  const drive = (command: string, value?: string) => {
    if (session && canInput && takeover && frameContext) onControl(bindingKey, command, value, undefined, frameContext)
  }

  return (
    <div className={`shell-execution-browser shell-execution-browser--workspace${surfaceOnly ? ' execution-browser-surface' : ''}`} data-testid="shell-execution-browser" data-surface="live">
      {!surfaceOnly && <><p className="shell-card-text" role="note">{listHint}</p>
      <div className="shell-execution-browser-sessions" role="list" data-testid="shell-execution-browser-sessions">
        {sessions.map(row => (
          <button
            key={row.binding_key}
            type="button"
            role="listitem"
            className={`shell-btn${focused?.binding_key === row.binding_key ? ' is-active' : ''}`}
            data-binding={row.binding_key}
            data-testid={focused?.binding_key === row.binding_key ? 'shell-execution-browser-focused' : undefined}
            aria-pressed={focused?.binding_key === row.binding_key}
            onClick={() => onFocusSession(row)}
          >
            {row.order_id || row.task_id || row.binding_key}
            {' · '}
            {sessionStateLabel(row.state, zh)}
            {row.note ? ` · ${row.note}` : ''}
          </button>
        ))}
      </div></>}

      {!session ? (
        <p className="shell-card-text">{zh ? '选择上方会话以查看画面。' : 'Pick a session above to see its picture.'}</p>
      ) : (
        <div className="shell-execution-browser-stage" data-testid="shell-execution-browser-stage" data-state={state}>
          <div className="shell-execution-browser-handoff" data-testid="shell-execution-browser-handoff">
            {surfaceOnly && <>
              <button type="button" className="shell-btn" aria-label={zh ? '后退' : 'Back'} disabled={!takeover} onClick={() => drive('back')}>←</button>
              <button type="button" className="shell-btn" aria-label={zh ? '刷新页面' : 'Reload page'} disabled={!takeover} onClick={() => drive('reload')}>↻</button>
              <form className="execution-address" onSubmit={event => { event.preventDefault(); if (address.trim()) drive('navigate', address.trim()) }}>
                <input aria-label={zh ? '网页地址' : 'Page address'} placeholder={zh ? '等待网页地址' : 'Waiting for a page'} value={address}
                  readOnly={!takeover} onChange={event => setAddress(event.target.value)} />
              </form>
            </>}
            <span>{zh ? (state === 'awaiting_human' ? '等你操作' : state === 'agent_operating' ? '代理操作中' : state) : state}</span>
            {takeover && reason ? <span className="shell-card-text">{reason}</span> : null}
            <span className="shell-execution-browser-spacer" />
            {state === 'awaiting_human' && (
              <button type="button" className="shell-btn" disabled={!canControl || restricted} onClick={() => control('hand_back', zh ? '我操作完了' : 'done')}>
                {zh ? '完成并交还' : 'Done and hand back'}
              </button>
            )}
            {state === 'human_returned' && (
              <>
                <button type="button" className="shell-btn" disabled={!canControl || restricted} onClick={() => control('verify_identity', zh ? '本人确认' : 'owner confirmed', true)}>
                  {zh ? '通过，继续' : 'Verify and continue'}
                </button>
                <button type="button" className="shell-btn" disabled={!canControl || restricted} onClick={() => control('verify_identity', zh ? '未通过' : 'not verified', false)}>
                  {zh ? '未通过' : 'Not verified'}
                </button>
              </>
            )}
            {(state === 'agent_operating' || state === 'identity_verified' || state === 'ready') && (
              <button type="button" className="shell-btn" disabled={!canControl || restricted} onClick={() => control('hand_to_human', zh ? '我来操作' : 'taking over')}>
                {zh ? '交给我' : 'Hand to me'}
              </button>
            )}
            {surfaceOnly && onViewPick && <button type="button" className="shell-btn" aria-pressed={picking} disabled={!canControl || restricted || !currentFrame}
              title={pickedElement?.text || pickedElement?.tag} onClick={() => setPicking(value => !value)}>{zh ? '定位元素' : 'Inspect element'}</button>}
            {!surfaceOnly && onOpenFullTools && (
              <button type="button" className="shell-btn" data-testid="shell-execution-browser-full-tools" onClick={() => onOpenFullTools(session)}>
                {zh ? '完整工具条（地址栏）' : 'Full tools (address bar)'}
              </button>
            )}
          </div>
          {currentFrame ? (
            <BrowserLiveView
              frame={currentFrame}
              takeover={takeover}
              restricted={restricted}
              zh={zh}
              onInput={canInput && onInput && frameContext ? (event) => onInput(bindingKey, event, frameContext) : undefined}
              onPick={picking && canControl && onViewPick && frameContext ? point => onViewPick(bindingKey, point.x, point.y, frameContext) : undefined}
            />
          ) : (
            <p className="shell-card-text" data-testid="shell-execution-browser-waiting">
              {zh ? '等待画面…（会话打开后这里会显示实时帧流）' : 'Waiting for the picture… (the live frame stream appears once the session is open)'}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
