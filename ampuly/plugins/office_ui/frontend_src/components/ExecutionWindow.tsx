import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import type { BrowserSessionRow } from './BrowserSessionsPanel'

export type ExecutionDetail = 'record' | 'files' | 'subsessions' | 'observatory'

export function executionWindowTitle(session: BrowserSessionRow, zh: boolean): string {
  const page = session.tabs?.find(tab => tab.active)?.page || session.tabs?.[0]?.page
  if (session.node_name) return session.node_name
  if (page) {
    try { const host = new URL(page.includes('://') ? page : `https://${page}`).host; if (host) return host } catch { /* use the domain */ }
  }
  return session.allowed_domains?.[0] || (zh ? '浏览器' : 'Browser')
}

/** One execution surface. Tabs are actual windows, never another application shell. */
export function ExecutionWindow({ zh, surface, sessions, bindingKey, desktopTitle, onBrowser, onDesktop, onDetails, onClose, onStop, onResume, children }: {
  zh: boolean
  surface: 'browser' | 'desktop'
  sessions: readonly BrowserSessionRow[]
  bindingKey?: string
  desktopTitle?: string
  onBrowser: (session?: BrowserSessionRow) => void
  onDesktop: () => void
  onDetails: (detail: ExecutionDetail) => void
  onClose: () => void
  onStop?: () => void
  onResume?: () => void
  children: ReactNode
}) {
  const [expanded, setExpanded] = useState(false)
  const menu = useRef<HTMLDetailsElement>(null)
  const details: Array<[ExecutionDetail, string]> = [
    ['record', zh ? '运行记录' : 'Run history'], ['files', zh ? '交付文件' : 'Deliverables'],
    ['subsessions', zh ? '协作任务' : 'Collaborating tasks'], ['observatory', zh ? '协作观测' : 'Observatory'],
  ]
  return <aside className={`shell-assistant shell-execution${expanded ? ' shell-execution--expanded' : ''}`}
    aria-label={zh ? '执行区' : 'Execution'} data-testid="shell-execution-panel">
    <header className="execution-window-bar">
      <nav className="execution-window-tabs" role="tablist" aria-label={zh ? '执行窗口' : 'Execution windows'}>
        {sessions.length ? sessions.map(session => <button key={session.binding_key} type="button" role="tab"
          aria-selected={surface === 'browser' && session.binding_key === bindingKey}
          title={`${executionWindowTitle(session, zh)} · ${session.order_id || session.task_id}`}
          onClick={() => onBrowser(session)}>
          <span className={`execution-window-dot${session.state === 'awaiting_human' ? ' needs-person' : ''}`} />
          <span>{executionWindowTitle(session, zh)}</span>
        </button>) : <button type="button" role="tab" aria-selected={surface === 'browser'} onClick={() => onBrowser()}>{zh ? '浏览器' : 'Browser'}</button>}
        <button type="button" role="tab" aria-selected={surface === 'desktop'} onClick={onDesktop}
          title={desktopTitle || (zh ? '软件窗口' : 'Application window')}>{desktopTitle || (zh ? '软件窗口' : 'Application window')}</button>
      </nav>
      {onStop && <button className="shell-btn execution-stop-task" type="button" onClick={onStop}>{zh ? '停止任务' : 'Stop task'}</button>}
      {onResume && <button className="shell-btn" type="button" onClick={onResume}>{zh ? '继续任务' : 'Resume task'}</button>}
      <details ref={menu} className="execution-window-menu">
        <summary aria-label={zh ? '查看记录与文件' : 'View history and files'}>···</summary>
        <div role="menu">{details.map(([tab, title]) => <button key={tab} type="button" role="menuitem"
          onClick={() => { if (menu.current) menu.current.open = false; onDetails(tab) }}>{title}</button>)}</div>
      </details>
      <button className="execution-window-icon" type="button" onClick={() => setExpanded(value => !value)}
        aria-label={expanded ? (zh ? '还原执行区' : 'Restore execution') : (zh ? '展开执行区' : 'Expand execution')}>{expanded ? '↙' : '↗'}</button>
      <button className="execution-window-icon" type="button" onClick={onClose} title={zh ? '收起窗口，任务继续运行' : 'Hide window; the task keeps running'}
        aria-label={zh ? '收起执行区' : 'Hide execution'}>×</button>
    </header>
    <div className="execution-window-surface" data-execution-surface={surface}>{children}</div>
  </aside>
}

/** Inspection is opened separately, never embedded as a second console in the execution viewport. */
export function ExecutionInspector({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const element = dialog.current
    element?.showModal()
    return () => { element?.close(); previous?.focus() }
  }, [])
  return <dialog ref={dialog} className="execution-inspector" aria-label={title} onCancel={event => { event.preventDefault(); onClose() }}>
    <header><strong>{title}</strong><button type="button" onClick={onClose} aria-label="Close">×</button></header>
    <div className="execution-inspector-body">{children}</div>
  </dialog>
}
