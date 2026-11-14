// 组件 · 查看 (4.7 Q03): one internal-library skill, read only. Everything here comes from the console's
// component_detail answer; nothing on this panel changes anything -- the only control closes it.
// 4.7-A11: the one addition is ``extra`` (提一个改进), which the owner console passes in; without it the panel stays as it was.
import type { ReactNode } from 'react'
import type { useI18n } from '../i18n'
import { groupUsesByFlow, readableBytes, stepsBehindLibrary, type ComponentDetail } from '../lib/componentDetail'
import { ComponentWorkbench, type ComponentWorkbenchProps } from './ComponentWorkbench'

type Translate = ReturnType<typeof useI18n>['t']

export interface ComponentDetailView {
  id: string
  detail: ComponentDetail | null
  loading: boolean
  error: string | null
}

export function ComponentDetailPanel({ view, onClose, t, workbench, zh = true, extra = null, receiptNote = null }: {
  view: ComponentDetailView
  onClose: () => void
  t: Translate
  /** 编辑 / 测试 (4.7 A2-4): the same detail as 能力 / 契约 / 测试 / 发布 pages with the version chain. Absent for 查看,
   *  which stays read only -- its one control closes it. */
  workbench?: ComponentWorkbenchProps
  zh?: boolean
  /** 4.7-A11 提一个改进 for this skill, shown above the detail once it has loaded. */
  extra?: ReactNode
  /** S3-5: the request tracker's words when the read behind this panel got no receipt (timed out, or the connection
   *  was cut): the panel then says so instead of reading for ever. */
  receiptNote?: string | null
}) {
  const detail = view.detail
  const groups = detail ? groupUsesByFlow(detail.used_on) : []
  const behind = detail ? stepsBehindLibrary(detail) : []
  const provenance = detail ? Object.entries(detail.contract.provenance ?? {}) : []
  const admission = detail?.admission as { admitted_by?: string; admitted_at?: string; source?: { market?: string; slug?: string; version?: string } } | null | undefined
  return (
    <aside className={`cl-detail${workbench ? ' is-workbench' : ''}`} role="dialog" aria-label={t('components.detail.title', { name: detail?.name ?? view.id })} data-testid="component-detail">
      <header className="cl-detail-head">
        <div>
          <strong>{detail?.name ?? view.id}</strong>
          <small className="cl-meta">{view.id}{detail?.version ? ` · v${detail.version}` : ''} · {workbench ? (zh ? '编辑与发布' : 'edit and publish') : t('components.detail.readOnly')}</small>
        </div>
        <button type="button" className="cl-detail-close" onClick={onClose} aria-label={t('components.detail.close')}>×</button>
      </header>
      {view.loading && !receiptNote && <p className="cl-empty">{t('components.detail.loading')}</p>}
      {receiptNote && <p className="cl-empty cl-detail-error" role="status" data-testid="component-detail-receipt">{receiptNote}</p>}
      {view.error && <p className="cl-empty cl-detail-error" role="alert">{view.error}</p>}
      {detail && extra}
      {detail && workbench && (() => {
        const sections = detailSections(detail, groups, behind, provenance, admission, t)
        return <ComponentWorkbench zh={zh} detail={detail} props={workbench}
          ability={<>{detail.summary && <p className="cl-summary">{detail.summary}</p>}{sections.usedOn}{sections.source}{sections.requires}</>}
          contract={<>{sections.contract}{sections.files}{sections.text}</>} />
      })()}
      {detail && !workbench && (() => {
        const sections = detailSections(detail, groups, behind, provenance, admission, t)
        return (
          <div className="cl-detail-body">
            {detail.summary && <p className="cl-summary">{detail.summary}</p>}
            {sections.usedOn}
            {sections.contract}
            {sections.source}
            {sections.requires}
            {sections.files}
            {sections.text}
          </div>
        )
      })()}
    </aside>
  )
}

type Admission = { admitted_by?: string; admitted_at?: string; source?: { market?: string; slug?: string; version?: string } } | null | undefined

// The read-only sections, once: 查看 shows them in a column; 编辑 / 测试 place them on its 能力 and 契约 pages.
function detailSections(detail: ComponentDetail, groups: ReturnType<typeof groupUsesByFlow>, behind: ReturnType<typeof stepsBehindLibrary>, provenance: Array<[string, unknown]>, admission: Admission, t: Translate) {
  return {
    usedOn: (
          <section>
            <h3>{t('components.detail.usedOn')}</h3>
            {groups.length === 0 ? <p className="cl-empty">{t('components.unused')}</p> : groups.map(group => (
              <div key={group.workflowId} className="cl-detail-flow" data-testid="component-detail-flow">
                <b>{group.workflowName}</b>
                <small className="cl-meta"> · {t(`components.flowStatus.${group.status}` as Parameters<Translate>[0])}</small>
                {group.mixedVersions && <span className="cl-chip cl-chip--warn">{t('components.detail.mixedVersions')}</span>}
                <ul>
                  {group.steps.map(step => (
                    <li key={step.node_id}>
                      {step.node_name}
                      <small className="cl-meta"> · {step.pinned_version ? `v${step.pinned_version}` : t('components.detail.unpinned')}</small>
                      {step.has_params && <span className="cl-chip">{t('components.detail.ownParams')}</span>}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
            {behind.length > 0 && <p className="cl-line">{t('components.detail.behind', { count: behind.length, version: detail.version })}</p>}
          </section>
    ),
    contract: (
          <section>
            <h3>{t('components.detail.contract')}</h3>
            <span className={`cl-chip ${detail.contract.complete ? 'cl-chip--ok' : 'cl-chip--warn'}`}>
              {detail.contract.complete ? t('components.contractComplete') : t('components.contractIncomplete')}
            </span>
            {detail.contract.problems.length > 0 && (
              <div className="cl-chips">{detail.contract.problems.map(problem => <span className="cl-chip cl-chip--warn" key={problem}>{problem}</span>)}</div>
            )}
            {detail.contract.tests.length > 0 && (
              <><h4>{t('components.detail.tests')}</h4><ul>{detail.contract.tests.map(item => <li key={item}>{item}</li>)}</ul></>
            )}
            {detail.contract.success_criteria.length > 0 && (
              <><h4>{t('components.detail.criteria')}</h4><ul>{detail.contract.success_criteria.map(item => <li key={item}>{item}</li>)}</ul></>
            )}
          </section>
    ),
    source: (
          <section>
            <h3>{t('components.detail.source')}</h3>
            {admission?.source?.market
              ? <p className="cl-line">{t('components.detail.admitted', { market: admission.source.market, by: admission.admitted_by || '—', at: admission.admitted_at || '—' })}</p>
              : <p className="cl-line">{t('components.detail.ownMade')}</p>}
            {provenance.length > 0 && (
              <ul>{provenance.map(([key, value]) => <li key={key}><code>{key}</code>: {typeof value === 'string' ? value : JSON.stringify(value)}</li>)}</ul>
            )}
            <p className="cl-line">
              {t('components.detail.license')}：{detail.license.declared ?? t('components.detail.licenseUndeclared')} · {t(`components.detail.licenseClass.${detail.license.class}` as Parameters<Translate>[0])}
            </p>
          </section>
    ),
    requires: (detail.requires.tools.length > 0 || detail.requires.bins.length > 0 || detail.requires.env.length > 0) && (
            <section>
              <h3>{t('components.detail.requires')}</h3>
              {detail.requires.tools.length > 0 && <p className="cl-line">{t('components.detail.requiresTools')}：{detail.requires.tools.join('、')}</p>}
              {detail.requires.bins.length > 0 && <p className="cl-line">{t('components.detail.requiresBins')}：{detail.requires.bins.join('、')}</p>}
              {/* Names of environment variables only; a value never reaches this page. */}
              {detail.requires.env.length > 0 && <p className="cl-line">{t('components.detail.requiresEnv')}：{detail.requires.env.join('、')}</p>}
            </section>
    ),
    files: (
          <section>
            <h3>{t('components.detail.files', { count: detail.files.length })}</h3>
            <ul className="cl-detail-files">
              {detail.files.map(file => <li key={file.path}><code>{file.path}</code> <small className="cl-meta">{readableBytes(file.bytes)}</small></li>)}
            </ul>
            {detail.files_truncated && <p className="cl-line">{t('components.detail.filesCut')}</p>}
          </section>
    ),
    text: (
          <section>
            <h3>SKILL.md</h3>
            <pre className="cl-detail-text">{detail.body}</pre>
            {detail.body_truncated && <p className="cl-line">{t('components.detail.textCut')}</p>}
          </section>
    ),
  }
}
