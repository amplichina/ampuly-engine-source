// The product shell's top bar (frontend refactor U1): where the person is, which project they are in, whether the
// console is really connected, 执行区 and 安安. No counters for the sake of filling space; no "demo" tag -- the status
// shown is the socket's. Since S3-7 (policy) it opens no page of its own: the 待审批 button went with its
// page (what waits is 总控's rows), and the 更多 menu went with the three older pages it led to.

import { useEffect, useRef, useState, type ReactNode } from 'react'

import { useI18n } from '../../i18n'
import type { RestorableAppPage } from '../../lib/appPageState'
import type { BuilderCrumb } from '../../lib/builderCrumb'
import { sessionStateLabel, type ExecutionEntry } from '../../lib/executionEntry'
import { PAGE_LABEL_KEYS, badgeText } from '../../lib/shellNav'
import { ShellIcon } from './ShellIcons'

export type ShellConnection = 'connected' | 'connecting' | 'disconnected'

export function ShellTopbar({ activePage, connection, connectionDetail, projectSlot, builderFlow = null, execution, assistant, observatory }: {
  activePage: RestorableAppPage
  connection: ShellConnection
  /** Shown as the status tooltip: the raw socket state and address, for whoever needs to debug a connection. */
  connectionDetail?: string
  /** The project selector. Every request the console sends names a project, so it is on every page. */
  projectSlot: ReactNode
  /** The flow open in the builder: named after the page, with its real status and whether changes are unsaved. */
  builderFlow?: BuilderCrumb | null
  /** 安安's side panel. Absent on 总控 itself, where the conversation already is the page. */
  assistant?: { open: boolean; onToggle: () => void }
  /** Owner-only read view over existing execution events. */
  observatory?: { onOpen: () => void }
  /** 执行区: the live controlled-browser sessions, the way to open one, and the way to its side window. Absent for
   *  anyone but the owner (callback or undefined); the server checks the owner on every browser action anyway. */
  execution?: { entry: ExecutionEntry<{ binding_key: string; state: string; task_id?: string; order_id?: string; note?: string }>; onOpen: (bindingKey: string) => void; onOpenAll: () => void }
}) {
  const { t, locale } = useI18n()
  const [executionOpen, setExecutionOpen] = useState(false)
  const executionRef = useRef<HTMLDivElement | null>(null)
  const zh = locale.startsWith('zh')
  useEffect(() => {
    if (!executionOpen) return undefined
    const close = (event: Event) => {
      if (event instanceof KeyboardEvent ? event.key === 'Escape' : !executionRef.current?.contains(event.target as Node)) setExecutionOpen(false)
    }
    document.addEventListener('keydown', close)
    document.addEventListener('pointerdown', close)
    return () => { document.removeEventListener('keydown', close); document.removeEventListener('pointerdown', close) }
  }, [executionOpen])
  // The package's real status, in the person's language when the catalogue has it.
  const flowStatus = (status: string) => {
    const key = `workflows.status.${status}` as Parameters<typeof t>[0]
    const label = t(key)
    return label === key ? status : label
  }
  // The info card closes on a click elsewhere and on Escape (a bare <details> does neither).
  const infoRef = useRef<HTMLDetailsElement | null>(null)
  useEffect(() => {
    const close = (event: Event) => {
      const card = infoRef.current
      if (!card?.open) return
      if (event instanceof KeyboardEvent ? event.key === 'Escape' : !card.contains(event.target as Node)) card.open = false
    }
    document.addEventListener('keydown', close)
    document.addEventListener('pointerdown', close)
    return () => { document.removeEventListener('keydown', close); document.removeEventListener('pointerdown', close) }
  }, [])
  return (
    <header className="shell-topbar">
      <div className="shell-topbar-left">
        <nav className="shell-breadcrumb" aria-label={t('shell.breadcrumb')}>
          <span className="shell-breadcrumb-root">{t('shell.brand')}</span>
          <span className="shell-breadcrumb-sep" aria-hidden="true">/</span>
          <span className="shell-breadcrumb-page" aria-current={builderFlow ? undefined : 'page'}>{t(PAGE_LABEL_KEYS[activePage])}</span>
          {builderFlow && <>
            <span className="shell-breadcrumb-sep" aria-hidden="true">/</span>
            <strong className="shell-breadcrumb-flow" aria-current="page" title={builderFlow.name}>{builderFlow.name}</strong>
            <span className="shell-tag">{flowStatus(builderFlow.status)} · v{builderFlow.version}</span>
            <span className="shell-tag" data-testid="builder-flow-owner">{builderFlow.project ? t('shell.flow.project', { project: builderFlow.project }) : t('shell.flow.template')}</span>
            {builderFlow.unsaved && <span className="shell-tag shell-tag--warn">{t('shell.flow.unsaved')}</span>}
            <details className="shell-flow-info" ref={infoRef}>
              <summary aria-label={t('shell.flow.info')} title={t('shell.flow.info')}>ⓘ</summary>
              <div>
                <strong>{t('shell.flow.info')}</strong>
                <p>{t('shell.flow.source', { id: builderFlow.id, version: builderFlow.version, status: flowStatus(builderFlow.status) })}</p>
                <p>{t('shell.flow.counts', { steps: builderFlow.steps, wires: builderFlow.wires })}</p>
                <p>{builderFlow.unsaved ? t('shell.flow.unsavedNote') : t('shell.flow.savedNote')}</p>
                <p>{builderFlow.project ? t('shell.flow.projectNote', { project: builderFlow.project, from: builderFlow.derivedFrom || '—' }) : t('shell.flow.globalNote')}</p>
              </div>
            </details>
          </>}
        </nav>
        {projectSlot}
      </div>
      <div className="shell-topbar-right">
        <span className={`shell-status shell-status--${connection}`} role="status" title={connectionDetail}>
          <span className="shell-status-dot" aria-hidden="true" />
          <span className="shell-status-text">{t(`shell.status.${connection}`)}</span>
        </span>
        {observatory && <button type="button" className="shell-topbar-btn ao-nav-button" data-testid="shell-observatory" onClick={observatory.onOpen}
          aria-label={zh ? '协作图谱' : 'Collaboration atlas'} title={zh ? '查看 AI 员工与人类员工在组织、项目与业务中的协作' : 'See how AI and human employees collaborate across the organization, projects and business'}>
          <span aria-hidden="true">◉</span><span className="ao-nav-label">{zh ? '协作图谱' : 'Collaboration atlas'}</span>
        </button>}
        {execution && (
          <div className="shell-more" ref={executionRef}>
            <button type="button" className={`shell-topbar-btn${execution.entry.waiting > 0 ? ' has-waiting' : ''}`} data-testid="shell-execution"
              aria-expanded={executionOpen} aria-controls="shell-execution-list" aria-label={t('shell.execution')} title={t('shell.executionTitle')}
              onClick={() => setExecutionOpen(open => !open)}>
              <ShellIcon name="execution" size={17} />
              <span>{t('shell.execution')}</span>
              {execution.entry.waiting > 0 && <span className="shell-topbar-count">{badgeText(execution.entry.waiting)}</span>}
            </button>
            {executionOpen && (
              <div className="shell-more-list shell-execution-list" id="shell-execution-list">
                <p>{execution.entry.sessions.length > 0 ? t('shell.executionSome') : t('shell.executionNone')}</p>
                {execution.entry.sessions.map(session => (
                  <button key={session.binding_key} type="button" data-binding={session.binding_key}
                    onClick={() => { setExecutionOpen(false); execution.onOpen(session.binding_key) }}>
                    <strong>{session.order_id || session.task_id || session.binding_key}</strong>
                    <small>{sessionStateLabel(session.state, zh)}{session.note ? ` · ${session.note}` : ''}</small>
                  </button>
                ))}
                <button type="button" data-testid="shell-execution-open" onClick={() => { setExecutionOpen(false); execution.onOpenAll() }}>{t('shell.executionAll')}</button>
              </div>
            )}
          </div>
        )}
        {assistant && (
          <button type="button" className={`shell-topbar-btn shell-assistant-toggle${assistant.open ? ' is-active' : ''}`} data-testid="shell-assistant-toggle"
            aria-expanded={assistant.open} aria-label={t('shell.assistant')} onClick={assistant.onToggle}>
            <img src="./brand/anan-avatar.png" alt="" width={22} height={19} />
            <span>{assistant.open ? t('shell.assistantHide') : t('shell.assistantShow')}</span>
          </button>
        )}
      </div>
    </header>
  )
}
