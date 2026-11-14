import { useRef, useState, type MouseEvent } from 'react'
import { useI18n } from '../i18n'
import { relativeWhen, threadDot, threadTitle, type MasterThread, type ThreadGroup } from '../lib/masterThreads'

/** 总控 · 项目与对话栏 (policy, the module contract section 7): inside 总控, right
 * of the global rail and left of the conversation. 「新对话」 on top; the projects below it, the one on screen first
 * and open, the others closed until clicked; each lists its conversations by last activity, with a dot while a build
 * works in one or something in it waits for the owner; 「＋」 starts a conversation in that project; 「已归档」 holds
 * the archived ones with 恢复. Rename and 归档 are in each conversation's small menu. 「新项目」 goes to 项目中心 --
 * projects are not made here. Nothing is deleted from here. What each press does is App's (lib/masterThreads). */
export interface MasterThreadColumnProps {
  groups: ThreadGroup[]
  /** The conversation 总控 shows ('' = a new one, not made yet). */
  openThreadId: string
  connected: boolean
  /** The console's clock, for 「5 分钟」. */
  now: number
  /** Projects whose list could not be read: said instead of 「正在读…」. */
  unread?: Readonly<Record<string, boolean>>
  onNew: (projectId: string) => void
  onOpen: (thread: MasterThread) => void
  onRename: (thread: MasterThread, title: string) => void
  onArchive: (thread: MasterThread, archived: boolean) => void
  /** Another project's group was opened: its list is asked for. */
  onExpandProject: (projectId: string) => void
  onNewProject: () => void
  /** Collapse the column (full width) or close the drawer (narrow). */
  onHide: () => void
}

/** Close the small menu a press was made in (a native disclosure: no state to keep). */
function closeMenu(event: MouseEvent<HTMLElement>) {
  const menu = event.currentTarget.closest('details')
  if (menu) menu.open = false
}

export function MasterThreadColumn({ groups, openThreadId, connected, now, unread = {}, onNew, onOpen, onRename, onArchive, onExpandProject, onNewProject, onHide }: MasterThreadColumnProps) {
  const { t, locale } = useI18n()
  const zh = locale.startsWith('zh')
  // Which project groups are open: the one on screen unless the owner closed it, the others once clicked.
  const [expanded, setExpanded] = useState<Readonly<Record<string, boolean>>>({})
  // The conversation being renamed in place, and whether its box was left with Esc (nothing is saved then).
  const [editing, setEditing] = useState('')
  const cancelRef = useRef(false)
  const words = { legacy: t('master.threads.legacyTitle'), untitled: t('master.threads.untitled') }
  const current = groups.find(group => group.current)

  const toggle = (group: ThreadGroup, open: boolean) => {
    setExpanded(state => ({ ...state, [group.projectId]: !open }))
    if (!open && !group.current) onExpandProject(group.projectId)
  }
  const commit = (thread: MasterThread, value: string) => {
    const cancelled = cancelRef.current
    cancelRef.current = false
    setEditing('')
    const title = value.replace(/\s+/g, ' ').trim()
    if (!cancelled && title && title !== thread.title) onRename(thread, title)
  }

  const threadRow = (thread: MasterThread) => {
    const title = threadTitle(thread, words)
    const dot = threadDot(thread)
    const dotWords = dot === 'waiting' ? t('master.threads.dot.waiting') : dot === 'running' ? t('master.threads.dot.running') : ''
    const open = thread.threadId === openThreadId
    const when = relativeWhen(thread.lastActivityAt, now, zh)
    return (
      <li key={thread.threadId} className={`master-thread${open ? ' is-open' : ''}`} data-thread={thread.threadId} data-dot={dot ?? undefined}>
        {editing === thread.threadId ? (
          <input className="master-thread-rename" data-testid="master-thread-rename" aria-label={t('master.threads.renameLabel')}
            defaultValue={thread.title || title} maxLength={60} autoFocus
            onKeyDown={event => {
              if (event.key === 'Enter') { event.preventDefault(); event.currentTarget.blur() }
              if (event.key === 'Escape') { event.preventDefault(); cancelRef.current = true; event.currentTarget.blur() }
            }}
            onBlur={event => commit(thread, event.currentTarget.value)} />
        ) : (
          <button type="button" className="master-thread-open" aria-current={open ? 'true' : undefined} title={title} onClick={() => onOpen(thread)}>
            {dot && <span className="master-thread-dot" data-dot={dot} role="img" aria-label={dotWords} title={dotWords} />}
            <span className="master-thread-title">{title}</span>
            {when && <small className="master-thread-when">{when}</small>}
          </button>
        )}
        <details className="master-thread-menu">
          <summary aria-label={t('master.threads.menu')} title={t('master.threads.menu')}>⋯</summary>
          <div className="master-thread-menu-list">
            <button type="button" data-thread-action="rename" disabled={!connected} onClick={event => { closeMenu(event); setEditing(thread.threadId) }}>{t('master.threads.rename')}</button>
            <button type="button" data-thread-action="archive" disabled={!connected} onClick={event => { closeMenu(event); onArchive(thread, true) }}>{t('master.threads.archive')}</button>
          </div>
        </details>
      </li>
    )
  }

  return (
    <nav className="master-threads" aria-label={t('master.threads.title')} data-testid="master-threads">
      <div className="master-threads-head">
        <button type="button" className="master-threads-new" data-testid="master-threads-new" disabled={!connected || !current}
          onClick={() => { if (current) onNew(current.projectId) }}>＋ {t('master.threads.new')}</button>
        <button type="button" className="master-threads-hide" aria-label={t('master.threads.hide')} title={t('master.threads.hide')} onClick={onHide}>«</button>
      </div>
      <div className="master-threads-body">
        {groups.map(group => {
          const open = expanded[group.projectId] ?? group.current
          return (
            <section key={group.projectId} className="master-threads-group" data-project={group.projectId}
              data-current={group.current ? 'true' : undefined} data-open={open ? 'true' : 'false'}>
              <div className="master-threads-project">
                <button type="button" className="master-threads-project-toggle" aria-expanded={open} title={group.name} onClick={() => toggle(group, open)}>
                  <span className="master-threads-caret" aria-hidden="true">{open ? '▾' : '▸'}</span>
                  <span className="master-threads-project-name">{group.name}</span>
                  {group.current && <small className="master-threads-current">{t('master.threads.current')}</small>}
                </button>
                <button type="button" className="master-threads-plus" data-testid="master-threads-plus" disabled={!connected}
                  aria-label={t('master.threads.newIn', { project: group.name })} title={t('master.threads.newIn', { project: group.name })}
                  onClick={() => onNew(group.projectId)}>＋</button>
              </div>
              {open && (
                <>
                  {!group.loaded
                    ? <p className="master-threads-note" role="status">{unread[group.projectId] ? t('master.threads.unread') : t('master.threads.loading')}</p>
                    : group.live.length === 0
                      ? <p className="master-threads-note">{t('master.threads.empty')}</p>
                      : <ul className="master-threads-list">{group.live.map(threadRow)}</ul>}
                  {group.archived.length > 0 && (
                    <details className="master-threads-archived" data-testid="master-threads-archived">
                      <summary>{t('master.threads.archived', { n: group.archived.length })}</summary>
                      <ul className="master-threads-list">
                        {group.archived.map(thread => (
                          <li key={thread.threadId} className="master-thread is-archived" data-thread={thread.threadId}>
                            <span className="master-thread-title" title={threadTitle(thread, words)}>{threadTitle(thread, words)}</span>
                            <button type="button" className="master-thread-restore" data-thread-action="restore" disabled={!connected}
                              onClick={() => onArchive(thread, false)}>{t('master.threads.restore')}</button>
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </>
              )}
            </section>
          )
        })}
      </div>
      <button type="button" className="master-threads-project-link" data-testid="master-threads-new-project" title={t('master.threads.newProjectHint')}
        onClick={onNewProject}>＋ {t('master.threads.newProject')}</button>
    </nav>
  )
}
