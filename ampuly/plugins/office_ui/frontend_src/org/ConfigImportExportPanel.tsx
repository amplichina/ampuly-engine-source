import { OrgIcon } from './OrgIcon'
import { useEffect, useRef, useState, type ChangeEvent } from 'react'
import { useI18n } from '../i18n'

/* ── Inline SVG icon data-URIs ──────────────────────────────────── */

interface ConfigImportExportPanelProps {
  onExport: () => void
  onImport: (yaml: string, dryRun: boolean) => void
  configExportYaml?: string | null
  importPreview?: { roles_added: number; roles_removed: number; employees_changed: number } | null
  importError?: string | null
}

export function ConfigImportExportPanel({
  onExport, onImport, configExportYaml, importPreview, importError,
}: ConfigImportExportPanelProps) {
  const { t } = useI18n()
  const [yamlText, setYamlText] = useState('')
  const [fileName, setFileName] = useState<string | null>(null)
  const [dryRunDone, setDryRunDone] = useState(false)
  const exportPending = useRef(false)
  const lastExportedYaml = useRef<string | null>(null)

  // Trigger browser download when server returns the exported YAML
  useEffect(() => {
    if (!exportPending.current) return
    if (!configExportYaml) return
    if (configExportYaml === lastExportedYaml.current) return
    lastExportedYaml.current = configExportYaml
    exportPending.current = false

    const blob = new Blob([configExportYaml], { type: 'application/x-yaml' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
    a.download = `org_config_${stamp}.yaml`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }, [configExportYaml])

  // Reset dry-run state when user edits the YAML
  useEffect(() => {
    setDryRunDone(false)
  }, [yamlText])

  // Flip dry-run state on successful preview (not on error)
  useEffect(() => {
    if (importPreview && !importError) setDryRunDone(true)
  }, [importPreview, importError])

  const handleExport = () => {
    exportPending.current = true
    onExport()
  }

  const handleFile = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setFileName(file.name)
    const reader = new FileReader()
    reader.onload = () => {
      const result = reader.result
      if (typeof result === 'string') setYamlText(result)
    }
    reader.readAsText(file)
    e.target.value = '' // allow re-selecting same file
  }

  const handleDryRun = () => {
    if (!yamlText.trim()) return
    onImport(yamlText, true)
  }

  const handleApply = () => {
    if (!dryRunDone || !yamlText.trim()) return
    if (!confirm(t('cfg.confirmApply'))) return
    onImport(yamlText, false)
    setDryRunDone(false)
    setYamlText('')
    setFileName(null)
  }

  const handleClear = () => {
    setYamlText('')
    setFileName(null)
    setDryRunDone(false)
  }

  return (
    <div className="cfg-io-panel" data-testid="config-import-export-panel">
      <div className="cfg-io-header">
        <h3 className="cfg-io-title">{t('cfg.title')}</h3>
        <p className="cfg-io-subtitle">{t('cfg.subtitle')}</p>
      </div>

      {/* Export */}
      <div className="cfg-io-section">
        <div className="cfg-io-section-header">
          <OrgIcon name="download" size={16} className="cfg-io-section-icon" />
          <span className="cfg-io-section-title">{t('cfg.downloadSection')}</span>
        </div>
        <button className="cfg-io-btn cfg-io-btn-primary" onClick={handleExport}>
          <OrgIcon name="download" size={13} className="cfg-io-btn-icon" /> {t('cfg.downloadYaml')}
        </button>
      </div>

      {/* Import */}
      <div className="cfg-io-section">
        <div className="cfg-io-section-header">
          <OrgIcon name="upload" size={16} className="cfg-io-section-icon" />
          <span className="cfg-io-section-title">{t('cfg.uploadSection')}</span>
        </div>

        <div className="cfg-io-upload-row">
          <label className="cfg-io-file-label">
            <input type="file" accept=".yaml,.yml" onChange={handleFile} className="cfg-io-file-input" />
            <span className="cfg-io-file-btn">{t('cfg.chooseFile')}</span>
            <span className="cfg-io-file-name">{fileName ?? t('cfg.noFile')}</span>
          </label>
        </div>

        <textarea
          className="cfg-io-textarea"
          placeholder={t('cfg.pastePlaceholder')}
          value={yamlText}
          onChange={e => setYamlText(e.target.value)}
          spellCheck={false}
          rows={10}
        />

        <div className="cfg-io-actions">
          <button className="cfg-io-btn cfg-io-btn-ghost"
            onClick={handleDryRun}
            disabled={!yamlText.trim()}>
            {t('cfg.dryRun')}
          </button>
          <button className="cfg-io-btn cfg-io-btn-primary"
            onClick={handleApply}
            disabled={!dryRunDone || !yamlText.trim()}>
            {t('cfg.apply')}
          </button>
          {yamlText && (
            <button className="cfg-io-btn cfg-io-btn-ghost" onClick={handleClear}>{t('rt.clear')}</button>
          )}
        </div>

        {/* Preview */}
        {importPreview && !importError && (
          <div className="cfg-io-preview">
            <OrgIcon name="check" size={16} className="cfg-io-preview-icon" />
            <div className="cfg-io-preview-body">
              <div className="cfg-io-preview-title">{t('cfg.dryRunOk')}</div>
              <div className="cfg-io-preview-stats">
                <span className="cfg-io-preview-stat">
                  <span className="cfg-io-stat-label">{t('cfg.rolesAdded')}</span>
                  <span className="cfg-io-stat-value">{importPreview.roles_added}</span>
                </span>
                <span className="cfg-io-preview-stat">
                  <span className="cfg-io-stat-label">{t('cfg.rolesRemoved')}</span>
                  <span className="cfg-io-stat-value">{importPreview.roles_removed}</span>
                </span>
                <span className="cfg-io-preview-stat">
                  <span className="cfg-io-stat-label">{t('cfg.employeesChanged')}</span>
                  <span className="cfg-io-stat-value">{importPreview.employees_changed}</span>
                </span>
              </div>
            </div>
          </div>
        )}

        {/* Error */}
        {importError && (
          <div className="cfg-io-error">
            <OrgIcon name="warn" size={16} className="cfg-io-error-icon" />
            <div className="cfg-io-error-body">
              <div className="cfg-io-error-title">{t('cfg.validationFailed')}</div>
              <pre className="cfg-io-error-text">{importError}</pre>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
