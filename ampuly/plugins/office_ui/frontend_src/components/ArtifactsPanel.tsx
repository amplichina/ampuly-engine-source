import { useEffect, useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useI18n } from '../i18n'
import { classifyPageError, pageErrorMessageKey } from '../lib/pageError'
import { filesInScope, scopesOf } from '../lib/artifactScope'
import { artifactQuoteSelection, type ArtifactQuoteSelection } from '../lib/artifactReference'

export interface ArtifactFile {
  path: string
  name: string
  group: string
  kind: 'text' | 'image' | 'binary'
  size: number
  modified_at: string
}

export interface ArtifactIndex {
  workspace_root: string
  files: ArtifactFile[]
  groups: Array<{ name: string; count: number }>
  truncated: boolean
}

export interface ArtifactPreview {
  project_id?: string
  path: string
  name: string
  kind: 'text' | 'image' | 'binary'
  size: number
  absolute_path?: string
  content?: string
  data_url?: string
  truncated?: boolean
  preview_revision?: string
}

interface ArtifactsPanelProps {
  index: ArtifactIndex | null
  preview: ArtifactPreview | null
  loading?: boolean
  error?: string | null
  connected?: boolean
  previewLoading?: boolean
  previewError?: string | null
  onRefresh: () => void
  onOpen: (path: string) => void
  onReveal: (path: string) => void
  onClosePreview: () => void
  onQuote?: (selection: ArtifactQuoteSelection) => void
}

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${bytes} B`
}

function formatWhen(iso: string): string {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return iso
  const hm = `${String(parsed.getHours()).padStart(2, '0')}:${String(parsed.getMinutes()).padStart(2, '0')}`
  const today = new Date()
  const sameDay = parsed.toDateString() === today.toDateString()
  return sameDay ? hm : `${parsed.getMonth() + 1}/${parsed.getDate()} ${hm}`
}

export function ArtifactsPanel({
  index,
  preview,
  loading = false,
  error = null,
  connected = true,
  previewLoading = false,
  previewError = null,
  onRefresh,
  onOpen,
  onReveal,
  onClosePreview,
  onQuote,
}: ArtifactsPanelProps) {
  const { t } = useI18n()
  const [activeGroup, setActiveGroup] = useState<string>('')

  // On a real workspace every file is under `orders/<order id>/…`, so grouping by the first path segment left one
  // chip reading "orders" and three files called 需求单.md told apart only by a truncated directory line. Group by
  // the order when that is how the files are laid out; keep the old grouping for a workspace laid out any other way.
  const { kind: scopeKind, scopes } = useMemo(() => scopesOf(index?.files ?? []), [index])
  const files = useMemo(() => filesInScope(index?.files ?? [], scopeKind, activeGroup), [index, scopeKind, activeGroup])
  // A chip that is gone after a refresh must not leave the panel showing nothing.
  useEffect(() => {
    if (activeGroup && !scopes.some(scope => scope.id === activeGroup)) setActiveGroup('')
  }, [scopes, activeGroup])

  return (
    <section className="dashboard-card dashboard-artifacts">
      <div className="artifacts-header">
        <h3 className="dashboard-card-title">{t('artifacts.title')}</h3>
        <button className="artifacts-refresh" onClick={onRefresh} disabled={loading || !connected}>
          {loading ? t('artifacts.loading') : t('artifacts.refresh')}
        </button>
      </div>

      {error ? (
        <div className={`artifacts-state artifacts-state--error artifacts-state--${classifyPageError(error)}`} role="alert" data-error-kind={classifyPageError(error)}>
          <div><strong>{t(pageErrorMessageKey(error) as Parameters<typeof t>[0])}</strong>: {error}</div>
          <button className="settings-btn" onClick={onRefresh} disabled={loading || !connected}>{t('artifacts.retry')}</button>
        </div>
      ) : loading && !index ? (
        <div className="artifacts-state" aria-live="polite">{t('artifacts.loading')}</div>
      ) : !index || index.files.length === 0 ? (
        <div className="dashboard-empty">{t('artifacts.empty')}</div>
      ) : (
        <>
          <div className="artifacts-groups">
            <button
              className={`artifacts-chip${activeGroup === '' ? ' active' : ''}`}
              onClick={() => setActiveGroup('')}
            >
              {t('artifacts.all')} {index.files.length}
            </button>
            {scopes.map(scope => (
              <button
                key={scope.id || 'outside'}
                className={`artifacts-chip${activeGroup === scope.id ? ' active' : ''}`}
                data-scope={scopeKind}
                data-scope-id={scope.id || undefined}
                onClick={() => setActiveGroup(scope.id)}
              >
                {scope.label || t('artifacts.outsideOrders')} {scope.count}
              </button>
            ))}
          </div>

          <ul className="artifacts-list">
            {files.map(file => (
              <li key={file.path} className="artifacts-row">
                <button className="artifacts-name" onClick={() => onOpen(file.path)} disabled={!connected} title={file.path}>
                  <span className="artifacts-icon" data-kind={file.kind}>
                    {file.kind === 'image' ? 'IMG' : file.kind === 'text' ? 'TXT' : 'BIN'}
                  </span>
                  {file.name}
                  {file.path.includes('/') && (
                    <span className="artifacts-dir" data-artifact-dir={file.path.slice(0, file.path.lastIndexOf('/'))}>{file.path.slice(0, file.path.lastIndexOf('/'))}</span>
                  )}
                </button>
                <span className="artifacts-meta">{formatSize(file.size)} · {formatWhen(file.modified_at)}</span>
                <button className="artifacts-reveal" onClick={() => onReveal(file.path)} disabled={!connected} title={t('artifacts.reveal')}>
                  ↗
                </button>
              </li>
            ))}
          </ul>
          {index.truncated && <div className="settings-note">{t('artifacts.truncated')}</div>}
          <div className="artifacts-root" title={index.workspace_root}>
            {t('artifacts.root')}: {index.workspace_root}
          </div>
        </>
      )}

      <ArtifactPreviewOverlay
        preview={preview} loading={previewLoading} error={previewError} connected={connected}
        onReveal={onReveal} onClose={onClosePreview}
        onQuote={onQuote}
      />
    </section>
  )
}

/** The preview drawer on its own: the order's deliverables use it, and so does anything else that hands it the same
 *  bounded payload (a node trial's files). `notice` says what the reader is looking at when it is not a deliverable;
 *  without `onReveal` there is no "show in folder" -- a trial's place on disk is not the page's to know. */
export function ArtifactPreviewOverlay({ preview, loading = false, error = null, connected = true, notice, onReveal, onClose, downloadHref, onQuote }: {
  preview: ArtifactPreview | null
  loading?: boolean
  error?: string | null
  connected?: boolean
  notice?: string
  onReveal?: (path: string) => void
  onClose: () => void
  /** Where this file is served, when the reader may keep a copy (the 资料 entry gives one; a trial's files do not). */
  downloadHref?: string
  onQuote?: (selection: ArtifactQuoteSelection) => void
}) {
  const { t, locale } = useI18n()
  const zh = locale.startsWith('zh')
  const [sourceMode, setSourceMode] = useState(false)
  const [selection, setSelection] = useState<ArtifactQuoteSelection | null>(null)
  useEffect(() => { setSourceMode(false); setSelection(null) }, [preview?.path, preview?.preview_revision])
  const canQuote = !!onQuote && preview?.kind === 'text' && !!preview.project_id && !!preview.preview_revision
  const currentSelection = selection && preview?.path === selection.reference.path
    && preview.project_id === selection.reference.project_id
    && preview.preview_revision === selection.reference.preview_revision ? selection : null
  // The drawer is used during approval and acceptance, where the operator
  // must return to the decision without losing context.  Escape is the
  // keyboard equivalent of the close affordance and also closes transient
  // loading/error states through the same owner callback.
  useEffect(() => {
    if (!preview && !loading && !error) return undefined
    // Capture phase, and the event stops here: the drawer is the topmost thing, so one Escape closes the drawer
    // and not also whatever it was opened from (the canvas's node editor listens on the same window).
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.stopPropagation()
      onClose()
    }
    window.addEventListener('keydown', handleKeyDown, true)
    return () => window.removeEventListener('keydown', handleKeyDown, true)
  }, [onClose, preview, error, loading])

  return (
    <>
      {loading && (
        <div className="artifacts-overlay artifacts-overlay--status" role="status" aria-live="polite">
          <div className="artifacts-preview artifacts-preview--status">{t('artifacts.previewLoading')}</div>
        </div>
      )}

      {error && !loading && (
        <div className={`artifacts-overlay artifacts-overlay--status artifacts-overlay--${classifyPageError(error)}`} role="alert" data-error-kind={classifyPageError(error)}>
          <div className="artifacts-preview artifacts-preview--status">
            <div><strong>{t(pageErrorMessageKey(error) as Parameters<typeof t>[0])}</strong>: {error}</div>
            <button className="settings-btn" onClick={onClose}>{t('artifacts.close')}</button>
          </div>
        </div>
      )}

      {preview && !error && (
        <div className="artifacts-overlay" onClick={onClose}>
          <div className="artifacts-preview" role="dialog" aria-modal="true" aria-label={t('artifacts.title')} onClick={event => event.stopPropagation()}>
            <div className="artifacts-preview-header">
              <span className="artifacts-preview-name">{preview.path}</span>
              <button className="settings-close" onClick={onClose}>✕</button>
            </div>
            {notice && <div className="settings-note artifacts-preview-notice" role="note">{notice}</div>}
            {canQuote && <div className="artifacts-preview-notice">
              <button type="button" className="settings-btn" aria-pressed={sourceMode}
                onClick={() => { setSourceMode(mode => !mode); setSelection(null) }}>
                {sourceMode ? (zh ? '返回预览' : 'Preview') : (zh ? '选取原文' : 'Select source text')}
              </button>
              <span className="settings-note">{zh ? '选择需要讨论的文字，再引用到安安（最多 4000 字符）。' : 'Select an excerpt, then quote it to Anan (up to 4000 UTF-16 units).'}</span>
            </div>}
            <div className="artifacts-preview-body">
              {preview.kind === 'image' && preview.data_url && (
                <img src={preview.data_url} alt={preview.name} className="artifacts-preview-image" />
              )}
              {preview.kind === 'text' && (
                sourceMode && canQuote
                  ? <textarea readOnly spellCheck={false} className="artifacts-preview-pre"
                      style={{width: '100%', minHeight: 280, resize: 'vertical'}}
                      aria-label={zh ? '产物原文选区' : 'Artifact source selection'} value={preview.content ?? ''}
                      onSelect={event => setSelection(artifactQuoteSelection(preview,
                        event.currentTarget.selectionStart, event.currentTarget.selectionEnd))} />
                  : preview.name.toLowerCase().endsWith('.md')
                  ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{preview.content ?? ''}</ReactMarkdown>
                  : <pre className="artifacts-preview-pre">{preview.content}</pre>
              )}
              {preview.kind === 'binary' && (
                <div className="dashboard-empty">{t('artifacts.binary')}</div>
              )}
              {preview.truncated && <div className="settings-note">{t('artifacts.previewTruncated')}</div>}
            </div>
            <div className="artifacts-preview-footer">
              <span>{formatSize(preview.size)}</span>
              {canQuote && <button type="button" className="settings-btn" disabled={!connected || !currentSelection}
                onClick={() => { if (currentSelection) onQuote?.(currentSelection) }}>
                {zh ? '引用到安安' : 'Quote to Anan'}
              </button>}
              {onReveal && (
                <button className="settings-btn" onClick={() => onReveal(preview.path)} disabled={!connected}>
                  {t('artifacts.reveal')}
                </button>
              )}
              {downloadHref && (
                <a className="settings-btn" href={downloadHref} download={preview.name} rel="noopener">
                  {t('artifacts.download')}
                </a>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  )
}
