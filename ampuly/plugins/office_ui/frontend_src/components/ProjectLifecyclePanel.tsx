import { useState } from 'react'
import { useI18n } from '../i18n'
import type { RestorePreflightReport } from './ProjectSelector'
import { memberLabel, type NotRestored } from '../lib/dataBrowser'

/** One row of a project's lifecycle trail, as `project_lifecycle` answers it (newest first). */
export interface ProjectLifecycleEvent {
  id: number
  at: string
  action: string
  from_status: string
  to_status: string
  actor: string
  reason: string
  metadata: Record<string, unknown>
}

export interface ProjectLifecycleTrail {
  project_id: string
  status: string
  updated_at: string
  updated_by: string
  customer_id: string
  /** File name only: the server's absolute path says nothing the owner needs here. */
  latest_backup_name: string
  events: ProjectLifecycleEvent[]
}

export function normalizeLifecycleTrail(projectId: string, lifecycle: unknown, events: unknown): ProjectLifecycleTrail {
  const record = (lifecycle && typeof lifecycle === 'object' ? lifecycle : {}) as Record<string, unknown>
  const rows = Array.isArray(events) ? events : []
  return {
    project_id: projectId,
    status: String(record.status ?? ''),
    updated_at: String(record.updated_at ?? ''),
    updated_by: String(record.updated_by ?? ''),
    customer_id: String(record.customer_id ?? ''),
    latest_backup_name: String(record.latest_backup_path ?? '').split(/[\\/]/).pop() ?? '',
    events: rows.filter((row): row is Record<string, unknown> => !!row && typeof row === 'object').map(row => ({
      id: Number(row.id ?? 0),
      at: String(row.at ?? ''),
      action: String(row.action ?? ''),
      from_status: String(row.from_status ?? ''),
      to_status: String(row.to_status ?? ''),
      actor: String(row.actor ?? ''),
      reason: String(row.reason ?? ''),
      metadata: (row.metadata && typeof row.metadata === 'object' ? row.metadata : {}) as Record<string, unknown>,
    })),
  }
}

const shortTime = (value: string) => (value || '').replace('T', ' ').slice(0, 16)

/** The trail itself. Read-only; an action this build has no words for is shown as the server wrote it. */
export function ProjectLifecycleView({ trail, error = null, loading = false }: {
  trail: ProjectLifecycleTrail | null
  error?: string | null
  loading?: boolean
}) {
  const { t, translateMaybe } = useI18n()
  if (error) return <p role="alert" data-testid="project-lifecycle-error" style={{ margin: 0, fontSize: 13, color: '#ef4444' }}>{error}</p>
  if (!trail) return <p data-testid="project-lifecycle-loading" style={{ margin: 0, fontSize: 13, color: 'var(--text-secondary)' }}>{loading ? t('project.lifecycleLoading') : ''}</p>
  const status = translateMaybe('project.lifecycleStatusName', trail.status || 'active')
  return (
    <div data-testid="project-lifecycle-trail" data-status={trail.status || 'active'} style={{ fontSize: 13 }}>
      <p style={{ margin: '0 0 4px', fontWeight: 600 }}>
        {trail.updated_at
          ? t('project.lifecycleStatus', { status, at: shortTime(trail.updated_at), by: trail.updated_by || '—' })
          : t('project.lifecycleStatusUntouched', { status })}
      </p>
      {trail.customer_id && <p style={{ margin: '0 0 4px', color: 'var(--text-secondary)' }}>{t('project.lifecycleCustomer', { customer: trail.customer_id })}</p>}
      {trail.latest_backup_name && <p style={{ margin: '0 0 4px', color: 'var(--text-secondary)' }}>{t('project.lifecycleLatestBackup', { name: trail.latest_backup_name })}</p>}
      {trail.events.length === 0
        ? <p data-testid="project-lifecycle-empty" style={{ margin: '8px 0 0', color: 'var(--text-secondary)' }}>{t('project.lifecycleEmpty')}</p>
        : <ol style={{ margin: '8px 0 0', padding: 0, listStyle: 'none' }}>
          {trail.events.map(event => {
            const details: string[] = []
            if (event.from_status && event.to_status && event.from_status !== event.to_status) {
              details.push(`${translateMaybe('project.lifecycleStatusName', event.from_status)} → ${translateMaybe('project.lifecycleStatusName', event.to_status)}`)
            }
            if (event.actor) details.push(t('project.lifecycleBy', { actor: event.actor }))
            // The default reason repeats the action's own name; only a reason somebody wrote is worth a line.
            if (event.reason && event.reason !== event.action && !event.reason.startsWith('project_')) details.push(t('project.lifecycleReason', { reason: event.reason }))
            const meta = event.metadata
            if (typeof meta.backup === 'string' && meta.backup) details.push(t('project.lifecycleMetaBackup', { name: meta.backup }))
            if ('from_customer_id' in meta || (typeof meta.customer_id === 'string' && meta.customer_id)) {
              details.push('from_customer_id' in meta
                ? t('project.lifecycleMetaCustomerChange', { from: String(meta.from_customer_id || '—'), to: String(meta.customer_id || '—') })
                : t('project.lifecycleCustomer', { customer: String(meta.customer_id) }))
            }
            if (typeof meta.files === 'number') details.push(t('project.lifecycleMetaFiles', { count: meta.files }))
            return (
              <li key={event.id} data-testid="project-lifecycle-event" data-action={event.action} style={{ padding: '6px 0', borderTop: '1px solid var(--border)' }}>
                <span style={{ color: 'var(--text-secondary)', marginRight: 8 }}>{shortTime(event.at)}</span>
                <span style={{ fontWeight: 600 }}>{translateMaybe('project.lifecycleAction', event.action)}</span>
                {details.length > 0 && <div style={{ color: 'var(--text-secondary)', marginTop: 2 }}>{details.join(' · ')}</div>}
              </li>
            )
          })}
        </ol>}
    </div>
  )
}

export type RestoreBackupState =
  | { phase: 'idle' }
  | { phase: 'pending' }
  | { phase: 'done'; projectId: string; notRestored?: NotRestored[] }
  | { phase: 'failed'; error: string }

/**
 * The restore itself, under a preflight report. Offered only for the archive that report is about, only when the
 * report found nothing that would refuse it, and only for an archive whose content was verified. It restores what the
 * report named -- never whatever is typed in the boxes above it now.
 */
export function RestoreFromBackupControl({ report, stale, connected, state, onRestore, onOpenLifecycle, initiallyArmed = false }: {
  report: RestorePreflightReport | null
  /** The path or the name was edited after this report was made. */
  stale: boolean
  connected: boolean
  state: RestoreBackupState
  onRestore: (archive: string, projectId: string) => void
  onOpenLifecycle?: (projectId: string) => void
  initiallyArmed?: boolean
}) {
  const { t } = useI18n()
  const [armed, setArmed] = useState(initiallyArmed)
  if (state.phase === 'done') {
    return (
      <div data-testid="restore-backup-done" style={{ fontSize: 13, marginBottom: 16 }}>
        <p style={{ margin: '0 0 6px', fontWeight: 600 }}>{t('project.restoreBackupDone', { project: state.projectId })}</p>
        {/* 数据与文件 P3: 备份里有、却没有放回去的东西。不说这一句，恢复就又是一次「看起来是全的」。 */}
        {state.notRestored && state.notRestored.length > 0 && (
          <div data-testid="restore-backup-not-restored" style={{ margin: '0 0 8px' }}>
            <p style={{ margin: '0 0 4px', color: 'var(--text-secondary)' }}>这些在备份里，但没有放回去：</p>
            <ul style={{ margin: 0, paddingLeft: 18, color: 'var(--text-secondary)' }}>
              {state.notRestored.map(item => (
                <li key={item.member}>{memberLabel(item.member)}——{item.why_zh}</li>
              ))}
            </ul>
          </div>
        )}
        {onOpenLifecycle && <button className="pill-btn" data-testid="restore-backup-open-trail" onClick={() => onOpenLifecycle(state.projectId)} style={{ fontSize: 12, padding: '4px 12px' }}>
          {t('project.lifecycleOpenFor', { project: state.projectId })}
        </button>}
      </div>
    )
  }
  if (!report || report.would_be_refused || !report.project_id) return null
  if (!report.content_verified) {
    return <p data-testid="restore-backup-unverified" style={{ margin: '0 0 16px', fontSize: 13, color: 'var(--text-secondary)' }}>{t('project.restoreBackupUnverified')}</p>
  }
  if (stale) {
    return <p data-testid="restore-backup-stale" style={{ margin: '0 0 16px', fontSize: 13, color: 'var(--text-secondary)' }}>{t('project.restoreBackupStale')}</p>
  }
  const pending = state.phase === 'pending'
  return (
    <div data-testid="restore-backup" style={{ fontSize: 13, marginBottom: 16, paddingTop: 12, borderTop: '1px solid var(--border)' }}>
      <p style={{ margin: '0 0 6px', color: 'var(--text-secondary)' }}>{t('project.restoreBackupWhat', { project: report.project_id })}</p>
      {report.bound_customer_id && <p data-testid="restore-backup-customer" style={{ margin: '0 0 6px', color: 'var(--text)' }}>{t('project.restoreBackupCustomer', { customer: report.bound_customer_id })}</p>}
      {state.phase === 'failed' && <p role="alert" data-testid="restore-backup-error" style={{ margin: '0 0 6px', color: '#ef4444' }}>{state.error}</p>}
      {pending && <p data-testid="restore-backup-pending" style={{ margin: '0 0 6px', color: 'var(--text-secondary)' }}>{t('project.restoreBackupPending')}</p>}
      <button
        className="pill-btn"
        data-testid={armed ? 'restore-backup-confirm' : 'restore-backup-run'}
        disabled={!connected || pending}
        onClick={() => {
          if (!armed) { setArmed(true); return }
          setArmed(false)
          onRestore(report.archive, report.project_id)
        }}
        style={{ padding: '6px 18px', fontSize: 13 }}
      >
        {armed ? t('project.restoreBackupConfirm', { project: report.project_id }) : t('project.restoreBackupRun')}
      </button>
    </div>
  )
}
