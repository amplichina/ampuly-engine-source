import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { refusalInWords, useI18n, type translateFor } from '../i18n'
import { classifyPageError } from '../lib/pageError'
import type { AuthAccount } from './LoginScreen'
import type { GateDecision, GateDetail } from './GatePanel'

type Translate = ReturnType<typeof translateFor>

/** One row of my_tasks_list (ws_handler._handle_my_tasks_list): the work item parked on this person, the order it
 *  belongs to, the step, the live card (checkpoint_id, '' when none is out) and since when it waits. */
export interface MyTask {
  task_id: string
  title: string
  status: string
  node: string
  standard: string
  updated_at: string
  order_id?: string
  node_id?: string
  node_name?: string
  checkpoint_id?: string
  /** The live card's type, when the list carries it (interaction_reply refuses a reply without one); not sent today. */
  checkpoint_type?: string
  waiting_since?: string
}

/** What this person handed back since they signed in (App keeps it; no server list of a person's decisions exists yet). */
export interface ReturnedWork {
  key: string
  title: string
  verb: 'approve' | 'reject' | 'reply'
  at: string
}

/** The materials view's payload (gate_detail), plus the consequence sentence when the server projects one. */
export type Materials = GateDetail & { consequence?: string; owner_card?: { consequence?: string } }

interface EmployeeWorkbenchProps {
  account: AuthAccount
  tasks: MyTask[] | null
  tasksError?: string
  connected: boolean
  /** The task opened from the list, and its materials once gate_detail answered. */
  open: MyTask | null
  materials: Materials | null
  /** gate_detail's refusal (a code), or the last decision's refusal (already in words); null when nothing is wrong. */
  materialsError: string | null
  /** A decision is out for the open materials (the request tracker still has it): 放行 / 打回修改 wait and say so. */
  gateBusy: boolean
  /** By checkpoint id: a plain reply that is out (lib/answerPath): its button waits for the receipt. */
  sending?: Readonly<Record<string, { state: 'pending' | 'lost' }>>
  replyError: string
  returned: ReturnedWork[]
  /** The clock, for the waiting words (a test hands in a fixed one). */
  now?: number
  onOpen: (task: MyTask) => void
  onClose: () => void
  onDecideGate: (decision: GateDecision) => void
  onReply: (task: MyTask, text: string) => void
  onLogout: () => void
}

const CODE_LIKE = /^[a-z][a-z0-9_:.-]*$/i

/** 「等了 N 分钟 / 小时 / 天」 from when the card went out; '' when that is not known. */
function waitedWords(t: Translate, since: string | undefined, now: number): string {
  const at = since ? Date.parse(since) : NaN
  if (!Number.isFinite(at)) return ''
  const minutes = Math.max(0, Math.floor((now - at) / 60_000))
  if (minutes < 60) return t('myWork.waitedMinutes', { n: minutes })
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return t('myWork.waitedHours', { n: hours })
  return t('myWork.waitedDays', { n: Math.floor(hours / 24) })
}

function clock(value: string): string {
  const at = Date.parse(value)
  if (!Number.isFinite(at)) return ''
  const date = new Date(at)
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

// 员工页「我的活」 (S3-6B, policy; FRONTEND-IA- 「员工页与客户页」, 「第二轮定稿」 row 员工页、客户页):
// the one page a person with an employee account sees. A list of the work parked on them -- a name, one line of
// context, how long it has waited -- and, opened, the materials (要做什么, the standard, the files handed in) with the
// reply box and 放行 / 打回修改 at the END of the materials, through the same road as the owner's materials view
// (App.decideGate: the answer path and the request tracker). A card that is not a materials gate takes the plain
// reply (交回) through the answer path. 「我交回去的」 is folded at the bottom. No product mark, no refresh button, no
// internal type words, no ids.
export function EmployeeWorkbench({
  account, tasks, tasksError = '', connected, open, materials, materialsError, gateBusy, sending, replyError, returned,
  now = Date.now(), onOpen, onClose, onDecideGate, onReply, onLogout,
}: EmployeeWorkbenchProps) {
  const { t, locale } = useI18n()
  const [draft, setDraft] = useState('')
  const [checked, setChecked] = useState<boolean[]>([])

  // A new task or a new version of its materials starts with an empty reply and nothing ticked.
  useEffect(() => {
    setDraft('')
    setChecked((materials?.standard ?? []).map(() => false))
  }, [open?.task_id, materials?.checkpoint_id, materials?.version, materials?.standard])

  const errorWords = (error: string) => (CODE_LIKE.test(error) ? refusalInWords(locale, 'gate.refused', error) : error)
  const verbWords = (verb: ReturnedWork['verb']) => t(`myWork.verb.${verb}`)

  const list = (
    <>
      {tasksError ? (
        <div className={`person-state person-state--${classifyPageError(tasksError)}`} role="alert" data-error-kind={classifyPageError(tasksError)}>
          <span><strong>{t(`pageError.${classifyPageError(tasksError)}`)}</strong><br />{tasksError}</span>
        </div>
      ) : tasks === null ? (
        <p className="person-empty">{t('myWork.loading')}</p>
      ) : tasks.length === 0 ? (
        <p className="person-empty">{t('myWork.empty')}</p>
      ) : (
        <ul className="person-list my-work-list">
          {tasks.map(task => {
            const name = task.node_name || task.title || t('myWork.untitled')
            const context = task.title && task.title !== name ? task.title : ''
            const waited = waitedWords(t, task.waiting_since, now)
            const openable = !!task.checkpoint_id
            return (
              <li key={task.task_id}>
                <button type="button" className="person-row" disabled={!openable} onClick={() => onOpen(task)} data-openable={openable ? 'true' : 'false'}>
                  <span className="person-row__name">{name}</span>
                  <span className="person-row__when" data-tone="waiting">{waited || t('myWork.waitingYou')}</span>
                  <span className="person-row__verb">{openable ? t('myWork.handle') : t('myWork.noMaterialsYet')}</span>
                  {context && <span className="person-row__context">{context}</span>}
                </button>
              </li>
            )
          })}
        </ul>
      )}
      <details className="person-fold my-work-returned">
        <summary>{t('myWork.returned')}{returned.length > 0 ? `（${returned.length}）` : ''}</summary>
        {returned.length === 0
          ? <p className="person-muted">{t('myWork.returnedEmpty')}</p>
          : <ul>{returned.map(item => <li key={item.key}>{item.title || t('myWork.untitled')} · {verbWords(item.verb)}{clock(item.at) && <time> · {clock(item.at)}</time>}</li>)}</ul>}
        <p className="person-muted">{t('myWork.returnedSince')}</p>
      </details>
    </>
  )

  const opened = open && (() => {
    const name = open.node_name || open.title || t('myWork.untitled')
    const context = open.title && open.title !== name ? open.title : ''
    const out = sending?.[open.checkpoint_id ?? '']?.state === 'pending'
    const plain = materialsError === 'gate_not_found'
    const text = draft.trim()
    const head = (
      <div className="person-open-head">
        <button type="button" className="person-secondary" onClick={onClose}>{t('myWork.back')}</button>
        <h3 className="person-open-title">{name}</h3>
        {context && <p className="person-muted">{context}</p>}
      </div>
    )
    if (plain) {
      // Not a materials gate (a card with no files, no standard): the reply itself is what is handed back. The reply
      // names the card's type (the server refuses one without it), which the list does not carry yet: until it does,
      // the button is off and the page says where to answer instead of sending something that comes back refused.
      const typed = !!(open.checkpoint_type ?? '').trim()
      return (
        <section className="person-open" data-mode="plain" data-typed={typed ? 'true' : 'false'}>
          {head}
          <p className="person-muted">{t('myWork.replyPlain')}</p>
          {!typed && <p className="person-error" role="note">{t('myWork.replyNeedsType')}</p>}
          <label className="person-reply">
            <span>{t('myWork.yourReply')}</span>
            <textarea rows={4} placeholder={t('myWork.replyPlainHint')} value={draft} disabled={!connected || out || !typed} onChange={event => setDraft(event.target.value)} />
          </label>
          <div className="person-actions">
            <button type="button" className="person-primary" disabled={!connected || out || !typed || !text} onClick={() => onReply(open, text)}>
              {out ? t('ownerCard.pending') : t('myWork.handBack')}
            </button>
          </div>
          {replyError && <p className="person-error" role="alert">{replyError}</p>}
        </section>
      )
    }
    if (!materials || materials.checkpoint_id !== open.checkpoint_id) {
      return (
        <section className="person-open" data-mode={materialsError ? 'error' : 'loading'}>
          {head}
          {materialsError
            ? <div className="person-state" role="alert"><span>{errorWords(materialsError)}</span><button type="button" className="person-secondary" onClick={() => onOpen(open)} disabled={!connected}>{t('myWork.retry')}</button></div>
            : <p className="person-empty">{t('myWork.fetching')}</p>}
        </section>
      )
    }
    const allChecked = materials.standard.length > 0 && checked.length === materials.standard.length && checked.every(Boolean)
    const noStandard = materials.standard.length === 0
    const canAnswer = connected && materials.can_answer && !gateBusy
    const canApprove = canAnswer && materials.missing.length === 0 && (allChecked || noStandard || text.length > 0)
    const canReject = canAnswer && text.length > 0
    const consequence = materials.consequence?.trim() || materials.owner_card?.consequence?.trim() || t('ownerCard.consequence.gate')
    const failed = materials.standard.filter((_, index) => !checked[index])
    return (
      <section className="person-open" data-mode="materials">
        {head}
        <h4 className="person-section">{t('myWork.whatToDo')}</h4>
        {materials.prompt && <p className="person-prompt">{materials.prompt}</p>}
        {noStandard
          ? <p className="person-muted">{t('myWork.noStandard')}</p>
          : (
            <ul className="person-checklist">
              {materials.standard.map((item, index) => (
                <li key={index}>
                  <label>
                    <input type="checkbox" disabled={!connected || gateBusy} checked={checked[index] ?? false} onChange={event => setChecked(prev => materials.standard.map((_, j) => (j === index ? event.target.checked : prev[j] ?? false)))} />
                    <span>{item}</span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        <h4 className="person-section">{t('myWork.materials')}</h4>
        {materials.precheck.length > 0 && (
          <div className="person-problems"><span className="person-muted">{t('myWork.problems')}</span><ul>{materials.precheck.map((item, index) => <li key={index}>{item}</li>)}</ul></div>
        )}
        {materials.files.length === 0 && materials.missing.length === 0 && <p className="person-muted">{t('myWork.noFiles')}</p>}
        <div className="person-materials">
          {materials.files.map(file => (
            <figure key={file.path} className="person-file" data-kind={file.kind}>
              <figcaption>{file.path}</figcaption>
              {file.kind === 'image' && file.data_url && <img src={file.data_url} alt={file.path} />}
              {file.kind === 'text' && <div className="person-file-text"><ReactMarkdown remarkPlugins={[remarkGfm]}>{file.content ?? ''}</ReactMarkdown></div>}
              {file.kind === 'binary' && <p className="person-muted">{t('myWork.noPreview')}</p>}
            </figure>
          ))}
          {materials.missing.map(item => <p key={item} className="person-missing">{t('myWork.missing', { name: item })}</p>)}
        </div>
        <div className="person-decide">
          {!materials.can_answer && <p className="person-error" role="note">{t('myWork.notYours')}</p>}
          <label className="person-reply">
            <span>{t('myWork.yourReply')}</span>
            <textarea rows={4} placeholder={t('myWork.replyHint')} value={draft} disabled={!connected || gateBusy} onChange={event => setDraft(event.target.value)} />
          </label>
          <div className="person-actions">
            <button
              type="button"
              className="person-primary"
              data-decision="approve"
              disabled={!canApprove}
              onClick={() => onDecideGate({ checkpoint_id: materials.checkpoint_id, decision: 'approve', exception_note: allChecked || noStandard ? '' : text })}
            >
              {gateBusy ? t('ownerCard.pending') : t('gate.approve')}
            </button>
            <button
              type="button"
              className="person-secondary"
              data-decision="reject"
              disabled={!canReject}
              onClick={() => onDecideGate({
                checkpoint_id: materials.checkpoint_id,
                decision: 'reject',
                // The reply is what should change (the sheet's 要改成什么); the standards left unticked are the ones it fails.
                sheet: { failed_standards: failed, chosen: '', keep: '', change: text, add_remove: '', directions: [], verbal: '' },
              })}
            >
              {gateBusy ? t('ownerCard.pending') : t('gate.reject')}
            </button>
          </div>
          <p className="person-consequence">{consequence}</p>
          {materialsError && <p className="person-error" role="alert">{errorWords(materialsError)}</p>}
        </div>
      </section>
    )
  })()

  return (
    <div className="person-shell employee-shell">
      <header className="person-topbar">
        <span className="person-who">{account.display_name || account.login}</span>
        <button type="button" className="person-secondary" onClick={onLogout}>{t('employee.logout')}</button>
      </header>
      <main className="person-main">
        <h2 className="person-title">{t('myWork.title')}</h2>
        {!connected && <p className="person-note" role="status">{t('myWork.offline')}</p>}
        {opened || list}
      </main>
    </div>
  )
}
