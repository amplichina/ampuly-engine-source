import { useState, useCallback, useId, useMemo } from 'react'
import type { Project } from '../types/kanban'
import { useI18n } from '../i18n'
import { toProjectSlug, type ProjectIdPolicy } from '../lib/projectIdPolicy'
import { removalCounts, type RemovalPlan } from '../lib/dataBrowser'
import { ProjectLifecycleView, RestoreFromBackupControl, type ProjectLifecycleTrail, type RestoreBackupState } from './ProjectLifecyclePanel'

/** The server names what a backup never carries in English; the owner reads the console's language. */
const NOT_INCLUDED_KEYS: Record<string, string> = {
  'work orders and their event chain': 'orders',
  'human gates and acceptance decisions': 'gates',
  'run budgets and their reservations': 'budgets',
  'solution packages and the deployment manifest': 'packages',
  'credential references': 'credentials',
  'accounts and sessions': 'accounts',
  'the console conversation history of this project': 'chat',
}

const REFUSAL_KEYS = {
  outside_backup_folder: 'project.refusal.outside_backup_folder', no_such_file: 'project.refusal.no_such_file',
  no_manifest: 'project.refusal.no_manifest', manifest_unreadable: 'project.refusal.manifest_unreadable',
  manifest_digest_mismatch: 'project.refusal.manifest_digest_mismatch', member_list_mismatch: 'project.refusal.member_list_mismatch',
  size_wrong: 'project.refusal.size_wrong', content_changed: 'project.refusal.content_changed',
  project_name_not_restorable: 'project.refusal.project_name_not_restorable', holds_another_project: 'project.refusal.holds_another_project',
  path_climbs_out: 'project.refusal.path_climbs_out', path_does_not_belong: 'project.refusal.path_does_not_belong',
  symbolic_link: 'project.refusal.symbolic_link', archive_unreadable: 'project.refusal.archive_unreadable',
  project_exists: 'project.refusal.project_exists', existence_unknown: 'project.refusal.existence_unknown',
} as const
const OCCUPIED_PLACES: Record<string, string> = { 'project folder': 'projectFolder', workplace: 'workplace', 'memory file': 'memoryFile' }
type I18n = ReturnType<typeof useI18n>

/** One refusal in the console's language; a code this build has no words for is shown as the server wrote it. */
export function refusalText(sentence: string, detail: Record<string, string> | undefined, t: I18n['t'], translateMaybe: I18n['translateMaybe']): string {
  const code = (detail?.code ?? '') as keyof typeof REFUSAL_KEYS
  if (!Object.prototype.hasOwnProperty.call(REFUSAL_KEYS, code)) return sentence
  const values: Record<string, string> = { ...detail }
  if (code === 'project_exists') {
    const places = (detail?.where ?? '').split(',').map(item => item.trim()).filter(Boolean)
      .map(item => (OCCUPIED_PLACES[item] ? translateMaybe('project.occupied', OCCUPIED_PLACES[item]) : item))
    values.where = places.length ? places.join(translateMaybe('project.occupied', 'separator')) : translateMaybe('project.occupied', 'unknown')
  }
  return t(REFUSAL_KEYS[code], values)
}

/** What `restore_preflight` found in one backup archive. Read-only: nothing was restored to produce it. */
export interface RestorePreflightReport {
  archive: string
  archive_name: string
  project_id: string
  taken_at: string
  entries: number
  bytes: number
  has_workplace: boolean
  has_memory: boolean
  /** Every reason a restore would be refused, not just the first. */
  refusals: string[]
  /** The same refusals as codes plus the values their sentences name, so they can be said in the console's language. */
  refusal_details?: Array<Record<string, string>>
  /** Business records a project backup never carries. */
  not_included: string[]
  would_be_refused: boolean
  /** The archive carries per-file digests and they were checked. Older archives restore unchecked. */
  content_verified: boolean
  /** The customer this project was bound to; a restore brings the binding back with the files. */
  bound_customer_id: string
}

interface ProjectSelectorProps {
  projects: Project[]
  activeId: string
  projectIdPolicy: ProjectIdPolicy | null
  onSelect: (id: string) => void
  onCreate: (id: string) => void
  onDelete?: (id: string, confirmation: string) => void
  /** 数据与文件 P3: 删之前先看会带走什么。问一次，答案回来前对话框说「正在看」。 */
  onAskRemovalPlan?: (id: string) => void
  removalPlan?: RemovalPlan | null
  onArchive?: (id: string) => void
  onRestore?: (id: string) => void
  onExport?: (id: string) => void
  onBackup?: (id: string) => void
  /** Bind this project to the customer whose delivery space it is (owner only). */
  onSetCustomer?: (id: string, customerId: string) => void
  /** 数据与文件 P4: 已经有过的客户，只做输入提示，不拦人。 */
  customers?: string[]
  /** Owner-only, read-only: look inside a backup before anyone restores it. Absent = the control is not offered. */
  onRestorePreflight?: (archivePath: string, targetProjectId: string) => void
  restorePreflight?: RestorePreflightReport | null
  restorePreflightError?: string | null
  /** Owner-only: restore the archive a clear preflight report named. Absent = only the inspection is offered. */
  onRestoreBackup?: (archive: string, projectId: string) => void
  restoreBackupState?: RestoreBackupState
  /** Owner-only, read-only: what happened to a project and who did it. Absent = the control is not offered. */
  onOpenLifecycle?: (projectId: string) => void
  onCloseLifecycle?: () => void
  /** The project whose trail is open, or null when the panel is closed. */
  lifecycleProjectId?: string | null
  lifecycleTrail?: ProjectLifecycleTrail | null
  lifecycleError?: string | null
  /** The archive this console last made, so the owner need not retype its path. */
  lastBackupPath?: string
  connected?: boolean
  /** Which half this instance shows: `switch` = the selector and new-project only (the new top bar); `manage` =
   *  backup precheck, record, delete, archive, export, backup and customer binding (项目中心 → 数据与存储).
   *  `create` = only the new-project control, worded (项目中心's all-projects level: projects are made and chosen
   *  there, not above every page). Absent = all of it, as the earlier shell uses it. */
  parts?: 'switch' | 'manage' | 'create'
}

export function ProjectSelector({ parts, projects, activeId, projectIdPolicy, onSelect, onCreate, onDelete, onArchive, onRestore, onExport, onBackup, onSetCustomer, onRestorePreflight, restorePreflight = null, restorePreflightError = null, onRestoreBackup, restoreBackupState = { phase: 'idle' }, onOpenLifecycle, onCloseLifecycle, lifecycleProjectId = null, lifecycleTrail = null, lifecycleError = null, lastBackupPath = '', connected = true, onAskRemovalPlan, removalPlan = null, customers = [] }: ProjectSelectorProps) {
  const { t, translateMaybe } = useI18n()
  const validationMessageId = useId()
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  // Beside the top bar's selector the buttons are one or two characters; where there is room they say what they do.
  const full = parts === 'manage'
  const [customerDraft, setCustomerDraft] = useState<string | null>(null)
  const [deleteConfirmation, setDeleteConfirmation] = useState('')
  const [preflightOpen, setPreflightOpen] = useState(false)
  const [preflightPath, setPreflightPath] = useState('')
  const [preflightTarget, setPreflightTarget] = useState('')
  // What the report on screen was asked about. Editing either box afterwards makes that report stale.
  const [inspected, setInspected] = useState<{ path: string; target: string } | null>(null)
  const archivePath = (preflightPath.trim() || lastBackupPath).trim()
  const reportIsStale = !inspected || inspected.path !== archivePath || inspected.target !== preflightTarget.trim()

  const newSlug = useMemo(() => toProjectSlug(newName), [newName])
  const hasName = newName.trim().length > 0
  const slugValid = projectIdPolicy?.matches(newSlug) ?? false
  const nameInvalid = hasName && projectIdPolicy !== null && !slugValid
  const validationMessage = hasName && !slugValid
    ? t(projectIdPolicy ? 'project.nameInvalid' : 'project.policyUnavailable')
    : null
  const activeProject = projects.find(project => project.id === activeId)
  const activeLifecycle = activeProject?.lifecycle_status ?? 'active'
  const isArchived = activeLifecycle === 'archived'

  const handleCreate = useCallback(() => {
    if (!slugValid) return
    onCreate(newSlug)
    setNewName('')
    setCreating(false)
  }, [newSlug, onCreate, slugValid])

  const handleDelete = useCallback(() => {
    if (!confirmDelete || !onDelete) return
    onDelete(confirmDelete, deleteConfirmation)
    setConfirmDelete(null)
    setDeleteConfirmation('')
  }, [confirmDelete, deleteConfirmation, onDelete])

  return (
    <div className="project-selector" data-parts={parts} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      {parts !== 'manage' && parts !== 'create' && <select
        className="theme-select"
        value={activeId}
        disabled={!connected}
        onChange={e => {
          const nextId = e.target.value
          const nextProject = projects.find(project => project.id === nextId)
          if (nextProject?.lifecycle_status === 'archived' && onRestore) {
            onRestore(nextId)
            return
          }
          onSelect(nextId)
        }}
        title={t('project.switch')}
        aria-label={t('project.switch')}
      >
        {projects.map(p => (
          <option key={p.id} value={p.id} disabled={p.lifecycle_status === 'destroyed'}>
            {p.name}{p.lifecycle_status === 'archived' ? ' · archived' : p.lifecycle_status === 'destroyed' ? ' · destroyed' : ''}
          </option>
        ))}
      </select>}
      {parts === 'manage' ? null : creating ? (
        <form
          onSubmit={e => { e.preventDefault(); handleCreate() }}
          style={{ display: 'flex', gap: 4 }}
        >
          <span style={{ position: 'relative', display: 'inline-flex' }}>
            <input
              autoFocus
              className="theme-select"
              value={newName}
              placeholder={t('project.placeholder')}
              onChange={e => setNewName(e.target.value)}
              onKeyDown={e => { if (e.key === 'Escape') setCreating(false) }}
              aria-invalid={nameInvalid || undefined}
              aria-describedby={validationMessage ? validationMessageId : undefined}
              title={validationMessage ?? undefined}
              style={{ width: 120 }}
            />
            {validationMessage && (
              <span
                id={validationMessageId}
                className="project-name-validation"
                role="status"
                aria-live="polite"
              >
                {validationMessage}
              </span>
            )}
          </span>
          <button
            type="submit"
            className="pill-btn"
            disabled={!slugValid}
            aria-label={t('project.create')}
            title={t('project.create')}
            style={{ fontSize: 11, padding: '2px 8px', opacity: slugValid ? 1 : 0.5 }}
          >+</button>
        </form>
      ) : (
        <button
     className="ps-chip pill-btn"
     onClick={() => setCreating(true)}
     disabled={!connected}
     title={t('project.new')}
     aria-label={t('project.new')}
     
    >
          {parts === 'create' ? `＋ ${t('project.new')}` : '+'}
        </button>
      )}
      {parts !== 'switch' && parts !== 'create' && onRestorePreflight && !creating && (
        <button
     className="ps-chip pill-btn"
     data-testid="restore-preflight-open"
     onClick={() => setPreflightOpen(true)}
     disabled={!connected}
     title={t('project.restorePreflight')}
     aria-label={t('project.restorePreflight')}
     
    >
          {t(full ? 'project.restorePreflight' : 'project.restorePreflightShort')}
        </button>
      )}
      {parts !== 'switch' && parts !== 'create' && onOpenLifecycle && !creating && (
        <button
     className="ps-chip pill-btn"
     data-testid="project-lifecycle-open"
     onClick={() => onOpenLifecycle(activeId)}
     disabled={!connected}
     title={t('project.lifecycle')}
     aria-label={t('project.lifecycle')}
     
    >
          {t(full ? 'project.lifecycle' : 'project.lifecycleShort')}
        </button>
      )}
      {full && activeId === 'default' && (
        <small className="ps-default-note" data-testid="project-default-note">{t('project.defaultNote')}</small>
      )}
      {parts !== 'switch' && parts !== 'create' && onDelete && activeId !== 'default' && !confirmDelete && (
        <button
          className="pill-btn"
          onClick={() => { setConfirmDelete(activeId); setDeleteConfirmation(''); onAskRemovalPlan?.(activeId) }}
          disabled={!connected}
          title={t('project.delete')}
          style={{ fontSize: 11, padding: '2px 8px', color: '#ef4444' }}
        >
          {t(full ? 'project.delete' : 'project.deleteButton')}
        </button>
      )}
      {parts !== 'switch' && parts !== 'create' && activeId !== 'default' && !confirmDelete && activeLifecycle !== 'destroyed' && (
        <>
          {isArchived && onRestore ? (
            <button className="ps-chip pill-btn" onClick={() => onRestore(activeId)} disabled={!connected} title={t('project.restore')} aria-label={t('project.restore')} >
              {t(full ? 'project.restore' : 'project.restoreButton')}
            </button>
          ) : onArchive ? (
            <button className="ps-chip pill-btn" onClick={() => onArchive(activeId)} disabled={!connected || isArchived} title={t('project.archive')} aria-label={t('project.archive')} >
              {t(full ? 'project.archive' : 'project.archiveButton')}
            </button>
          ) : null}
          {onExport && (
            <button className="ps-chip pill-btn" onClick={() => onExport(activeId)} disabled={!connected} title={t('project.export')} aria-label={t('project.export')} >
              {t(full ? 'project.export' : 'project.exportButton')}
            </button>
          )}
          {onBackup && (
            <button className="ps-chip pill-btn" onClick={() => onBackup(activeId)} disabled={!connected} title={t('project.backup')} aria-label={t('project.backup')} >
              {t(full ? 'project.backup' : 'project.backupButton')}
            </button>
          )}
          {onSetCustomer && (
            <span className="project-customer" data-testid="project-customer" title={t('project.customerHint')}>
              <input
                aria-label={t('project.customer')}
                data-testid="project-customer-input"
                placeholder={t('project.customerPlaceholder')}
                disabled={!connected}
                value={customerDraft ?? activeProject?.customer_id ?? ''}
                onChange={event => setCustomerDraft(event.target.value)}
                list={customers.length ? 'known-customers' : undefined}
                style={{ fontSize: 11, width: 96, padding: '2px 6px' }}
              />
              {/* 打到一半就把已经有过的客户提出来。只是提示：名单里没有的照样能绑。 */}
              {customers.length > 0 && (
                <datalist id="known-customers" data-testid="known-customers">
                  {customers.map(name => <option key={name} value={name} />)}
                </datalist>
              )}
              <button
        className="ps-chip pill-btn"
        data-testid="project-customer-save"
        disabled={!connected || customerDraft === null || customerDraft === (activeProject?.customer_id ?? '')}
        onClick={() => { onSetCustomer(activeId, (customerDraft ?? '').trim()); setCustomerDraft(null) }}
        
       >
                {t('project.customerSave')}
              </button>
            </span>
          )}
        </>
      )}

      {preflightOpen && onRestorePreflight && (
        <div className="project-delete-confirm" data-testid="restore-preflight" style={{
          position: 'fixed', inset: 0, zIndex: 9999,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          background: 'rgba(0,0,0,0.5)',
        }}>
          <div style={{
            background: 'var(--bg-elevated)', borderRadius: 12, padding: '24px 32px',
            border: '1px solid var(--border)', color: 'var(--text)',
            width: 'min(560px, 92vw)', maxHeight: '86vh', overflowY: 'auto', boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
          }}>
            <p className="ps-heading">{t('project.restorePreflight')}</p>
            <p className="ps-note ps-note--gap">{t('project.restorePreflightHint')}</p>
            <input
              className="theme-select"
              value={preflightPath}
              onChange={event => setPreflightPath(event.target.value)}
              placeholder={lastBackupPath || t('project.restorePreflightPath')}
              aria-label={t('project.restorePreflightPath')}
              style={{ width: '100%', marginBottom: 8, boxSizing: 'border-box' }}
            />
            <input
       className="ps-field theme-select"
       value={preflightTarget}
       onChange={event => setPreflightTarget(event.target.value)}
       placeholder={t('project.restorePreflightTarget')}
       aria-label={t('project.restorePreflightTarget')}
       
      />
            {restorePreflightError && <p role="alert" style={{ margin: '0 0 12px', fontSize: 13, color: '#ef4444' }}>{restorePreflightError}</p>}
            {restorePreflight && (
              <div data-testid="restore-preflight-report" data-refused={restorePreflight.would_be_refused ? 'true' : 'false'} style={{ fontSize: 13, marginBottom: 16 }}>
                <p style={{ margin: '0 0 6px', fontWeight: 600, color: restorePreflight.would_be_refused ? '#ef4444' : 'var(--text)' }}>
                  {t(restorePreflight.would_be_refused ? 'project.restorePreflightRefused' : 'project.restorePreflightClear')}
                </p>
                <p className="ps-note">
                  {t('project.restorePreflightFacts', {
                    name: restorePreflight.archive_name || '—',
                    project: restorePreflight.project_id || '—',
                    taken: (restorePreflight.taken_at || '').replace('T', ' ').slice(0, 16) || '—',
                    entries: restorePreflight.entries,
                    mb: (restorePreflight.bytes / 1_048_576).toFixed(1),
                  })}
                </p>
                <p className="ps-note">
                  {t(restorePreflight.has_workplace ? 'project.restorePreflightWorkplaceYes' : 'project.restorePreflightWorkplaceNo')} · {t(restorePreflight.has_memory ? 'project.restorePreflightMemoryYes' : 'project.restorePreflightMemoryNo')}
                </p>
                {restorePreflight.refusals.length > 0 && <ul style={{ margin: '0 0 6px', paddingLeft: 18 }}>
                  {restorePreflight.refusals.map((reason, index) => (
                    <li key={reason} data-testid="restore-preflight-refusal" data-code={restorePreflight.refusal_details?.[index]?.code ?? ''}>
                      {refusalText(reason, restorePreflight.refusal_details?.[index], t, translateMaybe)}
                    </li>
                  ))}
                </ul>}
                {restorePreflight.not_included.length > 0 && <>
                  <p style={{ margin: '6px 0 4px', fontWeight: 600 }}>{t('project.restorePreflightNotIncluded')}</p>
                  <ul style={{ margin: 0, paddingLeft: 18, color: 'var(--text-secondary)' }}>
                    {restorePreflight.not_included.map(item => <li key={item}>{NOT_INCLUDED_KEYS[item] ? translateMaybe('project.notIncluded', NOT_INCLUDED_KEYS[item]) : item}</li>)}
                  </ul>
                </>}
              </div>
            )}
            {onRestoreBackup && (
              <RestoreFromBackupControl
                // Armed for this report only. The control stays mounted while the dialog is open, so without a key
                // "Restore this backup" clicked for one archive left the next inspected archive one click from restored.
                key={`${restorePreflight?.archive ?? ''}|${restorePreflight?.project_id ?? ''}|${reportIsStale}`}
                report={restorePreflight}
                stale={reportIsStale}
                connected={connected}
                state={restoreBackupState}
                onRestore={onRestoreBackup}
                onOpenLifecycle={onOpenLifecycle ? (projectId) => { setPreflightOpen(false); onOpenLifecycle(projectId) } : undefined}
              />
            )}
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button className="ps-action pill-btn" onClick={() => setPreflightOpen(false)} >
                {t('common.close')}
              </button>
              <button
        className="ps-action pill-btn"
        data-testid="restore-preflight-run"
        onClick={() => { setInspected({ path: archivePath, target: preflightTarget.trim() }); onRestorePreflight(archivePath, preflightTarget.trim()) }}
        disabled={!connected || !archivePath}
        
       >
                {t('project.restorePreflightRun')}
              </button>
            </div>
          </div>
        </div>
      )}

      {parts !== 'switch' && parts !== 'create' && lifecycleProjectId && onOpenLifecycle && (
        <div className="project-delete-confirm" data-testid="project-lifecycle" style={{
          position: 'fixed', inset: 0, zIndex: 9999,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          background: 'rgba(0,0,0,0.5)',
        }}>
          <div style={{
            background: 'var(--bg-elevated)', borderRadius: 12, padding: '24px 32px',
            border: '1px solid var(--border)', color: 'var(--text)',
            width: 'min(560px, 92vw)', maxHeight: '86vh', overflowY: 'auto', boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
          }}>
            <p className="ps-heading">{t('project.lifecycle')} · {lifecycleProjectId}</p>
            <p className="ps-note ps-note--gap">{t('project.lifecycleHint')}</p>
            <ProjectLifecycleView trail={lifecycleTrail} error={lifecycleError} loading={!lifecycleTrail && !lifecycleError} />
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 16 }}>
              <button className="ps-action pill-btn" data-testid="project-lifecycle-close" onClick={() => onCloseLifecycle?.()} >
                {t('common.close')}
              </button>
            </div>
          </div>
        </div>
      )}

      {confirmDelete && (
        <div className="project-delete-confirm" style={{
          position: 'fixed', inset: 0, zIndex: 9999,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          background: 'rgba(0,0,0,0.5)',
        }}>
          <div style={{
            background: 'var(--bg-elevated)', borderRadius: 12, padding: '24px 32px',
            border: '1px solid var(--border)', color: 'var(--text)',
            maxWidth: 400, boxShadow: '0 8px 32px rgba(0,0,0,0.4)', textAlign: 'center',
          }}>
            <p style={{ margin: '0 0 8px', fontWeight: 600, fontSize: 15, color: 'var(--text)' }}>
              {t('project.confirmTitle', { name: confirmDelete })}
            </p>
            <p style={{ margin: '0 0 12px', fontSize: 13, color: 'var(--text-secondary)' }}>
              {t('project.confirmBody')}
            </p>
            {/* 数据与文件 P3: 打确认字之前，先看清会带走什么、不会带走什么。 */}
            <div data-testid="project-removal-plan" style={{ margin: '0 0 16px', fontSize: 12, textAlign: 'left', color: 'var(--text-secondary)' }}>
              {!removalPlan || removalPlan.project_id !== confirmDelete ? (
                <p role="status" style={{ margin: 0 }}>正在看会带走什么…</p>
              ) : (
                <>
                  <p style={{ margin: '0 0 4px' }}>
                    会带走：{removalCounts(removalPlan.goes) || '没有内容'}（工作区、附件、业务记录、聊天、预算、客户记忆）
                  </p>
                  <p style={{ margin: '0 0 4px' }}>
                    不会带走：浏览器留痕、外部代理日志、已经做好的备份
                  </p>
                  {removalPlan.backup ? (
                    <p style={{ margin: '0 0 4px' }}>最近一份备份：{removalPlan.backup.taken_at}</p>
                  ) : null}
                  {removalPlan.blocked_by.length > 0 && (
                    <ul data-removal-blocked style={{ margin: '8px 0 0', paddingLeft: 18, color: '#ef4444' }}>
                      {removalPlan.blocked_by.map(item => <li key={item.code}>{item.why_zh}</li>)}
                    </ul>
                  )}
                </>
              )}
            </div>
            <input
       className="ps-field theme-select"
       value={deleteConfirmation}
       onChange={event => setDeleteConfirmation(event.target.value)}
       placeholder={`destroy:${confirmDelete}`}
       aria-label={t('project.confirmInput')}
       
      />
            <div style={{ display: 'flex', gap: 8, justifyContent: 'center' }}>
              <button
        className="ps-action pill-btn"
        onClick={() => setConfirmDelete(null)}
        
       >
                {t('common.cancel')}
              </button>
              <button
                className="pill-btn"
                onClick={handleDelete}
                disabled={deleteConfirmation !== `destroy:${confirmDelete}`
                  || (removalPlan?.project_id === confirmDelete && removalPlan.blocked_by.length > 0)}
                style={{ padding: '6px 18px', fontSize: 13, background: '#ef4444', color: '#fff' }}
              >
                {t('common.delete')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
