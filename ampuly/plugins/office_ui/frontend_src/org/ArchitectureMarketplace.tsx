import { useMemo, useState, type ReactNode } from 'react'
import type {
  ArchitecturePreset,
  ArchitecturePresetDetail,
  InstalledPackageInfo,
  ChannelStatusInfo,
  ReorgProposalInfo,
} from '../types/visual'
import type { InteractionReplyReceipt } from '../types/chat'
import type { Answering } from '../lib/answerPath'
import { PackageCard } from './PackageCard'
import { CollapsibleSection } from './CollapsibleSection'
import { OrgIcon } from './OrgIcon'
import { useI18n } from '../i18n'

/* ── Inline SVG icon data-URIs (no external CDN) ────────────────── */

const PATTERN_LABELS: Record<string, string> = {
  pipeline: 'Pipeline',
  hub_spoke: 'Hub & Spoke',
  review_loop: 'Review Loop',
  hierarchical: 'Hierarchical',
  flat: 'Flat Team',
}

interface ArchitectureMarketplaceProps {
  presets: ArchitecturePreset[]
  installedIds: Set<string>
  previewData: ArchitecturePresetDetail | null
  applyingPresetId: string | null
  readOnly: boolean
  onPreview: (presetId: string) => void
  onApplyPreset: (presetId: string, strategy: string) => void
  onClearPreview: () => void
  installedPackages: InstalledPackageInfo[]
  channels: ChannelStatusInfo[]
  reorgProposals: ReorgProposalInfo[]
  isCustomMode: boolean
  onReorgDecide: (
    proposal: ReorgProposalInfo,
    approved: boolean,
    notes?: string,
  ) => Promise<InteractionReplyReceipt>
  /** By checkpoint id: the answer out on the one answer path (S3-7); a proposal whose card has one waits. */
  answering?: Answering
  onMarketInstall: (path: string, strategy: string) => void
  onMarketUninstall: (packageId: string) => void
}

export function ArchitectureMarketplace({
  presets, installedIds, previewData, applyingPresetId, readOnly,
  onPreview, onApplyPreset, onClearPreview,
  installedPackages, channels, reorgProposals, isCustomMode,
  onReorgDecide, answering, onMarketInstall, onMarketUninstall,
}: ArchitectureMarketplaceProps) {
  const { t, translateMaybe } = useI18n()
  const [search, setSearch] = useState('')
  const [activeCategory, setActiveCategory] = useState<string | null>(null)
  const [activePattern, setActivePattern] = useState<string | null>(null)
  const [showImportForm, setShowImportForm] = useState(false)
  const [importPath, setImportPath] = useState('')
  const [uninstallingId, setUninstallingId] = useState<string | null>(null)
  const [submittingReorgId, setSubmittingReorgId] = useState<string | null>(null)
  const [acceptedReorgIds, setAcceptedReorgIds] = useState<Set<string>>(
    () => new Set(),
  )
  const [reorgError, setReorgError] = useState<Record<string, string>>({})

  const categories = useMemo(() => {
    const cats = new Set<string>()
    for (const p of presets) { if (p.category) cats.add(p.category) }
    return Array.from(cats).sort()
  }, [presets])

  const patterns = useMemo(() => {
    const pats = new Set<string>()
    for (const p of presets) { if (p.collaboration_pattern) pats.add(p.collaboration_pattern) }
    return Array.from(pats).sort()
  }, [presets])

  const filteredPresets = useMemo(() => {
    let result = presets
    if (activeCategory) result = result.filter(p => p.category === activeCategory)
    if (activePattern) result = result.filter(p => p.collaboration_pattern === activePattern)
    if (search.trim()) {
      const q = search.trim().toLowerCase()
      result = result.filter(p =>
        p.name.toLowerCase().includes(q) ||
        p.description.toLowerCase().includes(q) ||
        p.tags.some(t => t.toLowerCase().includes(q)) ||
        (PATTERN_LABELS[p.collaboration_pattern] || '').toLowerCase().includes(q),
      )
    }
    return result
  }, [presets, activeCategory, activePattern, search])

  const handleImport = () => {
    if (!importPath.trim()) return
    onMarketInstall(importPath.trim(), 'namespace')
    setShowImportForm(false)
    setImportPath('')
  }

  const handleUninstall = (pkgId: string) => {
    if (!confirm(t('arch.confirmUninstall'))) return
    setUninstallingId(pkgId)
    onMarketUninstall(pkgId)
    setTimeout(() => setUninstallingId(null), 3000)
  }

  const handleReorgDecision = async (
    proposal: ReorgProposalInfo,
    approved: boolean,
  ) => {
    if (!proposal.checkpoint_id || proposal.checkpoint_status !== 'pending') return
    setSubmittingReorgId(proposal.proposal_id)
    setReorgError(current => ({ ...current, [proposal.proposal_id]: '' }))
    try {
      const receipt = await onReorgDecide(proposal, approved)
      if (!receipt.accepted) {
        setReorgError(current => ({
          ...current,
          [proposal.proposal_id]: receipt.reason || receipt.error || t('arch.decisionNotAccepted'),
        }))
        return
      }
      // The local control changes only after the durable interaction ACK.
      setAcceptedReorgIds(current => {
        const next = new Set(current)
        next.add(proposal.proposal_id)
        return next
      })
    } catch (error) {
      setReorgError(current => ({
        ...current,
        [proposal.proposal_id]: error instanceof Error ? error.message : t('arch.decisionFailed'),
      }))
    } finally {
      setSubmittingReorgId(current => (
        current === proposal.proposal_id ? null : current
      ))
    }
  }

  return (
    <div className="mkt-container" data-testid="architecture-marketplace">
      {/* Toolbar */}
      <div className="mkt-toolbar">
        <div className="mkt-search-wrap">
          <OrgIcon name="search" size={15} className="mkt-search-icon" />
          <input
            className="mkt-search"
            placeholder={t('arch.searchPlaceholder')}
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
        </div>
        <span className="mkt-count">{t('arch.count', { count: filteredPresets.length })}</span>
      </div>

      {/* Filter pills */}
      <div className="mkt-filters">
        {categories.length > 0 && (
          <div className="mkt-pill-row">
            <button className={`mkt-pill${!activeCategory ? ' active' : ''}`}
              onClick={() => setActiveCategory(null)}>{t('talent.all')}</button>
            {categories.map(cat => (
              <button key={cat} className={`mkt-pill${activeCategory === cat ? ' active' : ''}`}
                onClick={() => setActiveCategory(activeCategory === cat ? null : cat)}
              >{translateMaybe('talent.category', cat)}</button>
            ))}
          </div>
        )}
        {patterns.length > 0 && (
          <div className="mkt-pill-row">
            <button className={`mkt-pill mkt-pill-pattern${!activePattern ? ' active' : ''}`}
              onClick={() => setActivePattern(null)}>{t('arch.allPatterns')}</button>
            {patterns.map(pat => (
              <button key={pat} className={`mkt-pill mkt-pill-pattern${activePattern === pat ? ' active' : ''}`}
                onClick={() => setActivePattern(activePattern === pat ? null : pat)}
              >{translateMaybe('arch.pattern', PATTERN_LABELS[pat] || pat)}</button>
            ))}
          </div>
        )}
      </div>

      {/* Architecture Blueprints grid */}
      {filteredPresets.length > 0 ? (
        <div className="mkt-section">
          <div className="mkt-section-header">
            <OrgIcon name="architecture" size={16} className="mkt-section-icon" />
            <h3 className="mkt-section-title">{t('arch.blueprints')}</h3>
            <span className="mkt-section-count">{filteredPresets.length}</span>
          </div>
          <div className="mkt-arch-grid">
            {filteredPresets.map(p => (
              <ArchCard key={p.id} preset={p}
                isInstalled={installedIds.has(p.id)}
                isApplying={applyingPresetId === p.id}
                readOnly={readOnly}
                onPreview={() => onPreview(p.id)}
                onApply={() => onApplyPreset(p.id, 'namespace')}
              />
            ))}
          </div>
        </div>
      ) : (
        <div className="mkt-empty"><p>{t('arch.noMatch')}</p></div>
      )}

      {/* Installed Packages */}
      <CollapsibleSection icon="package" title={t('arch.installedPackages')} count={installedPackages.length}
        extra={isCustomMode ? (
          <button className="myorg-inline-btn" onClick={() => setShowImportForm(!showImportForm)}>
            <OrgIcon name="download" size={13} className="myorg-inline-icon" /> {t('arch.import')}
          </button>
        ) : undefined}>
        {isCustomMode && showImportForm && (
          <div className="myorg-form">
            <div className="oc-form-row">
              <label>{t('arch.path')}</label>
              <input value={importPath} onChange={e => setImportPath(e.target.value)}
                placeholder="/path/to/package.ampulypkg" />
            </div>
            <div className="oc-form-actions">
              <button className="oc-btn-primary" onClick={handleImport} disabled={!importPath.trim()}>{t('arch.install')}</button>
              <button className="oc-btn-ghost" onClick={() => setShowImportForm(false)}>{t('common.cancel')}</button>
            </div>
          </div>
        )}
        {installedPackages.length > 0 ? (
          <div className="pkg-grid">
            {installedPackages.map(pkg => (
              <PackageCard key={pkg.package_id} pkg={pkg}
                onUninstall={isCustomMode ? handleUninstall : undefined}
                uninstallingId={uninstallingId} />
            ))}
          </div>
        ) : (
          <div className="myorg-empty-hint">{t('arch.noPackages')}</div>
        )}
      </CollapsibleSection>

      {/* Channels & Connectors */}
      <CollapsibleSection icon="channels" title={t('arch.channels')} count={channels.length}>
        {channels.length > 0 ? (
          <div className="org-channels-grid">
            {channels.map(ch => (
              <div key={ch.name} className={`org-channel-card${ch.running ? ' org-ch-running' : ''}${!ch.enabled ? ' org-ch-disabled' : ''}`}>
                <div className="org-ch-header">
                  <span className={`org-ch-dot${ch.running ? ' running' : ch.ready ? ' ready' : ch.configured ? ' configured' : ''}`} />
                  <span className="org-ch-name">{ch.name}</span>
                </div>
                <div className="org-ch-status-row">
                  {ch.enabled && <span className="org-ch-badge org-ch-enabled">{t('arch.enabled')}</span>}
                  {ch.running && <span className="org-ch-badge org-ch-running-badge">{t('arch.running')}</span>}
                  {!ch.enabled && <span className="org-ch-badge org-ch-off">{t('arch.disabled')}</span>}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="myorg-empty-hint">{t('arch.noChannels')}</div>
        )}
      </CollapsibleSection>

      {/* Reorg Proposals */}
      {reorgProposals.length > 0 && (
        <CollapsibleSection icon="reorg" title={t('arch.reorgProposals')} count={reorgProposals.length}>
          <div className="org-reorg-list">
            {reorgProposals.map(p => {
              const isPending = p.status === 'proposed'
              // Out on this card: the local mark until the promise settles, and the answer path's own per-card
              // pending, which also holds a second click while the first answer is out.
              const isSubmitting = submittingReorgId === p.proposal_id
                || (!!p.checkpoint_id && answering?.[p.checkpoint_id]?.state === 'pending')
              const wasAccepted = acceptedReorgIds.has(p.proposal_id)
              const canDecide = (
                isPending
                && isCustomMode
                && p.checkpoint_status === 'pending'
                && Boolean(p.checkpoint_id)
                && !wasAccepted
              )
              return (
                <div key={p.proposal_id} className={`org-reorg-card org-reorg-${p.status}`}>
                  <div className="org-reorg-header">
                    <span className="org-reorg-title">{p.title || p.summary || t('arch.untitled')}</span>
                    <span className="org-reorg-status">{translateMaybe('arch.reorgStatus', p.status)}</span>
                  </div>
                  {p.summary && <div className="org-reorg-summary">{p.summary}</div>}
                  {canDecide && (
                    <div className="org-reorg-actions">
                      <button
                        className="org-reorg-approve"
                        disabled={isSubmitting}
                        onClick={() => void handleReorgDecision(p, true)}
                      >{isSubmitting ? t('arch.submitting') : t('arch.approve')}</button>
                      <button
                        className="org-reorg-deny"
                        disabled={isSubmitting}
                        onClick={() => void handleReorgDecision(p, false)}
                      >{t('arch.deny')}</button>
                    </div>
                  )}
                  {isPending && isCustomMode && wasAccepted && (
                    <div className="org-reorg-status">{t('arch.decisionSubmitted')}</div>
                  )}
                  {isPending && isCustomMode && p.user_confirmation_required
                    && !p.checkpoint_id && !wasAccepted && (
                    <div className="org-reorg-status">
                      {t('arch.approvalUnavailable')}
                    </div>
                  )}
                  {reorgError[p.proposal_id] && (
                    <div className="org-reorg-status">{reorgError[p.proposal_id]}</div>
                  )}
                </div>
              )
            })}
          </div>
        </CollapsibleSection>
      )}

      {/* Architecture preview modal */}
      {previewData && (
        <ArchPreviewModal
          data={previewData}
          isInstalled={installedIds.has(previewData.id)}
          isApplying={applyingPresetId === previewData.id}
          onApply={() => onApplyPreset(previewData.id, 'namespace')}
          onClose={onClearPreview}
        />
      )}
    </div>
  )
}

/* ── Architecture Card ───────────────────────────────────────────────── */

function ArchCard({ preset: p, isInstalled, isApplying, readOnly, onPreview, onApply }: {
  preset: ArchitecturePreset
  isInstalled: boolean
  isApplying: boolean
  readOnly: boolean
  onPreview: () => void
  onApply: () => void
}) {
  const { t, translateMaybe } = useI18n()
  const patternLabel = PATTERN_LABELS[p.collaboration_pattern] || p.collaboration_pattern

  return (
    <div className="mkt-arch-card" style={{ borderLeftColor: p.color || 'var(--accent)' }}
      onClick={onPreview}>
      <div className="mkt-arch-header">
        <span className="mkt-arch-emoji">{p.emoji}</span>
        <div className="mkt-arch-title-wrap">
          <span className="mkt-arch-name">{translateMaybe('arch.name', p.name)}</span>
          <div className="mkt-arch-badges">
            <span className="mkt-arch-category">{translateMaybe('talent.category', p.category)}</span>
            {p.collaboration_pattern && <span className="mkt-arch-pattern">{translateMaybe('arch.pattern', patternLabel)}</span>}
          </div>
        </div>
      </div>

      {p.dag_summary && <div className="mkt-arch-dag-summary">{p.dag_summary}</div>}
      <div className="mkt-arch-desc">{translateMaybe('arch.desc', p.description)}</div>

      <div className="mkt-arch-stats">
        <span className="mkt-arch-stat">{t('arch.rolesCount', { count: p.roles_count })}</span>
        <span className="mkt-arch-stat">{t('arch.templatesCount', { count: p.work_item_templates_count })}</span>
        {p.gates_count > 0 && <span className="mkt-arch-stat">{t('arch.checkpointsCount', { count: p.gates_count })}</span>}
        {p.team_size && <span className="mkt-arch-stat">{t('arch.peopleCount', { count: p.team_size })}</span>}
      </div>

      {p.tags.length > 0 && (
        <div className="mkt-arch-tags">
          {p.tags.slice(0, 4).map(t => <span key={t} className="mkt-tag">{t}</span>)}
        </div>
      )}

      <div className="mkt-arch-actions">
        {isInstalled ? (
          <span className="mkt-installed-badge">{t('arch.installed')}</span>
        ) : !readOnly ? (
          <button className="mkt-btn mkt-btn-primary mkt-btn-sm"
            disabled={isApplying}
            onClick={e => { e.stopPropagation(); onApply() }}
          >{isApplying ? t('arch.applying') : t('arch.useThis')}</button>
        ) : null}
        <button className="mkt-btn mkt-btn-ghost mkt-btn-sm"
          onClick={e => { e.stopPropagation(); onPreview() }}
        >{t('arch.preview')}</button>
      </div>
    </div>
  )
}

/* ── Architecture Preview Modal ──────────────────────────────────────── */

function ArchPreviewModal({ data, isInstalled, isApplying, onApply, onClose }: {
  data: ArchitecturePresetDetail
  isInstalled: boolean
  isApplying: boolean
  onApply: () => void
  onClose: () => void
}) {
  const { t, translateMaybe } = useI18n()
  return (
    <div className="mkt-modal-overlay" onClick={onClose}>
      <div className="mkt-modal" onClick={e => e.stopPropagation()}>
        <div className="mkt-modal-header" style={{ borderBottomColor: data.color || 'var(--border)' }}>
          <span className="mkt-modal-emoji">{data.emoji}</span>
          <div>
            <h2 className="mkt-modal-name">{translateMaybe('arch.name', data.name)}</h2>
            <span className="mkt-modal-category">{translateMaybe('talent.category', data.category)}</span>
          </div>
          <button className="mkt-modal-close" onClick={onClose}>&times;</button>
        </div>

        <div className="mkt-modal-body">
          <p className="mkt-modal-desc">{translateMaybe('arch.desc', data.description)}</p>

          {data.tags.length > 0 && (
            <div className="mkt-modal-section">
              <div className="mkt-modal-label">{t('talent.tags')}</div>
              <div className="mkt-modal-tags">
                {data.tags.map(t => <span key={t} className="mkt-tag">{t}</span>)}
              </div>
            </div>
          )}

          <div className="mkt-modal-section">
            <div className="mkt-modal-label">{t('arch.rolesHeader', { count: data.roles.length })}</div>
            <div className="mkt-role-list">
              {data.roles.map(r => (
                <div key={r.id} className="mkt-role-item">
                  <div className="mkt-role-name">{r.name} <code>{r.id}</code></div>
                  <div className="mkt-role-resp">{r.responsibility}</div>
                  <div className="mkt-role-meta">
                    {t('arch.reportsToInline')} <code>{r.reports_to}</code>
                    {r.can_spawn && r.can_spawn.length > 0 && (
                      <> &middot; {t('arch.spawnsInline')} {r.can_spawn.map(s => <code key={s}>{s}</code>)}</>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Work item templates */}
          <div className="mkt-modal-section">
            <div className="mkt-modal-label">{t('arch.workItemTemplates', { count: data.work_item_templates.length })}</div>
            <div className="mkt-dag-wrap">
              <ModalWorkItemTemplates templates={data.work_item_templates} />
            </div>
          </div>
        </div>

        <div className="mkt-modal-footer">
          {isInstalled ? (
            <span className="mkt-installed-badge">{t('arch.alreadyInstalled')}</span>
          ) : (
            <button className="mkt-btn mkt-btn-primary" disabled={isApplying} onClick={onApply}>
              {isApplying ? t('arch.applying') : t('arch.useThisArchitecture')}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

function ModalWorkItemTemplates({ templates }: { templates: ArchitecturePresetDetail['work_item_templates'] }): ReactNode {
  const { t, translateMaybe } = useI18n()
  type Group = { group: string | null; templates: typeof templates }
  const groups: Group[] = []
  let currentGroup: string | null = '__init__'
  let currentTemplates: typeof templates = []
  for (const template of templates) {
    if (template.parallel_group !== currentGroup) {
      if (currentTemplates.length > 0) groups.push({ group: currentGroup, templates: currentTemplates })
      currentGroup = template.parallel_group
      currentTemplates = [template]
    } else {
      currentTemplates.push(template)
    }
  }
  if (currentTemplates.length > 0) groups.push({ group: currentGroup, templates: currentTemplates })

  return (
    <div className="org-dag">
      {groups.map((g, gi) => (
        <div key={gi} className="org-dag-group-wrap">
          {gi > 0 && <div className="org-dag-arrow"><OrgIcon name="arrow" size={16} className="org-dag-arrow-icon" /></div>}
          <div className={`org-dag-group${g.templates.length > 1 ? ' org-dag-parallel' : ''}`}>
            {g.templates.length > 1 && <div className="org-dag-parallel-label">{t('arch.parallel')}</div>}
            {g.templates.map(template => (
              <div key={template.id} className="org-dag-node">
                <div className="org-dag-node-header">
                  <span className="org-dag-node-title">{template.title}</span>
                  <span className="org-dag-node-role">{template.role_id}</span>
                </div>
                <div className="org-dag-node-id">{template.id}</div>
                {template.gate && (
                  <div className={`org-dag-gate org-gate-${template.gate.type}`}>
                    <OrgIcon name={template.gate.type === 'review' ? 'review' : template.gate.type === 'approval' ? 'gate' : 'hold'} size={12} className="org-gate-icon" />
                    <span>{translateMaybe('arch.gate', template.gate.type)}</span>
                    {template.gate.reviewer_role && <span className="org-gate-reviewer">{t('arch.gateBy', { name: template.gate.reviewer_role })}</span>}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
