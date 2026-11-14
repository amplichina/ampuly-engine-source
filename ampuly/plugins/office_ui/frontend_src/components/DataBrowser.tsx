// 「资料」: one project's own files, as the people who own them think of them -- by order, by step, by folder.
// Data phase 2, D8. This version shows and downloads; uploading, deleting and moving come later.
//
// Everything here comes from the server's own answers (D6): what may be seen was decided there, so this file never
// hides or reveals anything by itself, never builds a path of its own, and never shows where a file sits on disk.

import { useState } from 'react'

import { useI18n } from '../i18n'
import { attachmentDownloadUrl } from '../lib/attachmentUrl'
import {
  areaLabel, dataDownloadUrl, errorLabel, formatBytes,
  type DataEntry, type DataState, type OrderStep, type UsageEntry,
} from '../lib/dataBrowser'
import { ArtifactPreviewOverlay, type ArtifactPreview } from './ArtifactsPanel'

/** Where the reader is: the project, one order, or one folder. Three levels, as the files themselves are kept. */
export type DataView =
  | { kind: 'project' }
  | { kind: 'order'; orderId: string }
  | { kind: 'folder'; folder: string }

export interface DataBrowserProps {
  projectId: string
  data: DataState
  usage?: UsageEntry | null
  connected?: boolean
  loading?: boolean
  onRefreshIndex: () => void
  onOpenOrder: (orderId: string) => void
  onOpenFolder: (folder: string, offset?: number) => void
  onPreview: (path: string) => void
  onClosePreview: () => void
  onOpenAttachments: (offset: number) => void
  onRefreshUsage: () => void
  /** Open this folder in the file manager of the machine the console runs on. Absent on a console that may not. */
  onReveal?: (folder: string) => void
  /** Where to open. The reader moves from here by clicking; nothing outside needs to follow along. */
  initialView?: DataView
}

export function DataBrowser({
  projectId, data, usage = null, connected = true, loading = false,
  onRefreshIndex, onOpenOrder, onOpenFolder, onPreview, onClosePreview, onOpenAttachments, onRefreshUsage,
  onReveal, initialView,
}: DataBrowserProps) {
  const { locale } = useI18n()
  const zh = locale.startsWith('zh')
  const [view, setView] = useState<DataView>(initialView ?? { kind: 'project' })
  const index = data.index
  const owner = index?.viewer?.owner === true

  const openOrder = (orderId: string) => { setView({ kind: 'order', orderId }); onOpenOrder(orderId) }
  const openFolder = (folder: string) => { setView({ kind: 'folder', folder }); onOpenFolder(folder, 0) }

  const failure = (key: string) => (data.errors[key] ? errorLabel(data.errors[key], zh) : '')
  const anyError = failure('data_project_index') || failure('data_folder_list') || failure('data_order_view')

  return (
    <section className="dashboard-card data-browser" data-testid="data-browser">
      <div className="data-browser-head">
        <h3 className="dashboard-card-title">{zh ? '资料' : 'Files'}</h3>
        <div className="data-browser-actions">
          {view.kind !== 'project' && (
            <button type="button" className="settings-btn" data-testid="data-browser-back"
              onClick={() => setView({ kind: 'project' })}>{zh ? '← 返回项目' : '← Back to project'}</button>
          )}
          <button type="button" className="settings-btn" disabled={!connected} onClick={onRefreshIndex}>
            {zh ? '刷新' : 'Refresh'}
          </button>
        </div>
      </div>

      {anyError && <div className="settings-note data-browser-error" role="alert">{anyError}</div>}
      {loading && !index && <div className="dashboard-empty">{zh ? '正在读取…' : 'Loading…'}</div>}

      {view.kind === 'project' && index && (
        <ProjectLevel
          zh={zh} projectId={projectId} data={data} usage={usage} owner={owner}
          onOpenOrder={openOrder} onOpenFolder={openFolder} onOpenAttachments={onOpenAttachments}
          onRefreshUsage={onRefreshUsage} connected={connected} onReveal={onReveal}
        />
      )}
      {view.kind === 'order' && (
        <OrderLevel zh={zh} projectId={projectId} data={data} orderId={view.orderId} owner={owner}
          onOpenFolder={openFolder} onPreview={onPreview} />
      )}
      {view.kind === 'folder' && (
        <FolderLevel zh={zh} projectId={projectId} data={data} folder={view.folder} onReveal={onReveal}
          onOpenFolder={openFolder} onPreview={onPreview} onMore={(offset) => onOpenFolder(view.folder, offset)} />
      )}

      <ArtifactPreviewOverlay
        preview={data.preview as unknown as ArtifactPreview | null}
        onClose={onClosePreview}
        connected={connected}
        downloadHref={data.preview ? dataDownloadUrl(projectId, data.preview.path) : undefined}
      />
    </section>
  )
}

function ProjectLevel({ zh, projectId, data, usage, owner, onOpenOrder, onOpenFolder, onOpenAttachments, onRefreshUsage, connected, onReveal }: {
  zh: boolean
  projectId: string
  data: DataState
  usage: UsageEntry | null
  owner: boolean
  onOpenOrder: (orderId: string) => void
  onOpenFolder: (folder: string) => void
  onOpenAttachments: (offset: number) => void
  onRefreshUsage: () => void
  connected: boolean
  onReveal?: (folder: string) => void
}) {
  const index = data.index!
  const attachments = data.attachments
  const measured = usage?.usage ?? index.usage ?? null
  const attachmentBytes = measured?.buckets?.attachments?.bytes ?? 0
  return (
    <>
      <div className="data-browser-usage" data-testid="data-browser-usage">
        {measured ? (
          <span>
            {zh ? '本项目占用 ' : 'This project uses '}
            {measured.complete ? '' : (zh ? '至少 ' : 'at least ')}
            <b>{formatBytes(measured.total_bytes)}</b>
            {zh ? `（附件 ${formatBytes(attachmentBytes)}）` : ` (attachments ${formatBytes(attachmentBytes)})`}
            {measured.computed_at && (
              <span className="data-browser-meta">
                {zh ? ` · 统计于 ${measured.computed_at.slice(5, 16).replace('T', ' ')}` : ` · measured ${measured.computed_at.slice(5, 16).replace('T', ' ')}`}
              </span>
            )}
          </span>
        ) : (
          <span>{usage?.computing ? (zh ? '正在统计…' : 'Measuring…') : (zh ? '还没有统计占用' : 'Not measured yet')}</span>
        )}
        <button type="button" className="settings-btn" disabled={!connected} onClick={onRefreshUsage}>
          {zh ? '重新统计' : 'Measure again'}
        </button>
      </div>

      <div className="data-browser-block">
        <h4>{zh ? '工单' : 'Orders'}</h4>
        {index.orders_unavailable && <div className="settings-note">{zh ? '订单库暂时读不了，先只显示文件夹' : 'The order store could not be read'}</div>}
        {index.orders.length === 0 ? (
          <div className="dashboard-empty">{zh ? '这个项目还没有工单' : 'No orders yet'}</div>
        ) : (
          <ul className="data-browser-list">
            {index.orders.map(order => (
              <li key={order.order_id}>
                <button type="button" className="data-browser-link" onClick={() => onOpenOrder(order.order_id)}>
                  {order.order_id}
                </button>
                <span className="data-browser-meta">
                  {order.status} · {order.customer_name || order.customer_id || (zh ? '未指定客户' : 'no customer')}
                  {order.created_at ? ` · ${order.created_at.slice(0, 10)}` : ''}
                  {' · '}
                  {order.has_folder ? (zh ? '有文件' : 'has files') : (zh ? '还没有文件' : 'no files yet')}
                </span>
              </li>
            ))}
          </ul>
        )}
        {index.orders_truncated && owner && (
          <div className="settings-note">{zh ? '较早的工单没有列出，可在其他文件夹里找到' : 'Older orders are not listed here'}</div>
        )}
      </div>

      <div className="data-browser-block">
        <h4>{zh ? '项目资料' : 'Project materials'}</h4>
        {!index.materials.exists ? (
          <div className="dashboard-empty">
            {zh
              ? '还没有项目资料；第一版只能查看和下载，上传在下一版。文件夹名是 project-materials/'
              : 'No project materials yet; this version only views and downloads. The folder is project-materials/'}
          </div>
        ) : (
          <ul className="data-browser-list">
            {index.materials.common && (
              <li>
                <button type="button" className="data-browser-link" onClick={() => onOpenFolder('project-materials/common')}>
                  {areaLabel('common', zh)}
                </button>
              </li>
            )}
            {index.materials.customers.filter(row => row.exists).map(row => (
              <li key={row.segment}>
                <button type="button" className="data-browser-link"
                  onClick={() => onOpenFolder(`project-materials/customers/${row.segment}`)}>
                  {row.customer_id}
                </button>
                <span className="data-browser-meta">{areaLabel('customers', zh)}</span>
              </li>
            ))}
            {index.materials.loose && (index.materials.loose.files ?? 0) + (index.materials.loose.folders ?? 0) > 0 && (
              <li>
                <button type="button" className="data-browser-link" onClick={() => onOpenFolder('project-materials')}>
                  {zh ? '其他资料' : 'Other materials'}
                </button>
              </li>
            )}
          </ul>
        )}
      </div>

      <div className="data-browser-block">
        <h4>{zh ? '附件' : 'Attachments'} <span className="data-browser-meta">
          {index.attachments.truncated ? (zh ? '至少 ' : 'at least ') : ''}{index.attachments.count}
        </span></h4>
        {attachments === null ? (
          <button type="button" className="settings-btn" disabled={!connected} onClick={() => onOpenAttachments(0)}>
            {zh ? '查看附件' : 'Show attachments'}
          </button>
        ) : attachments.attachments.length === 0 ? (
          <div className="dashboard-empty">{zh ? '还没有上传过附件' : 'Nothing uploaded yet'}</div>
        ) : (
          <ul className="data-browser-list">
            {attachments.attachments.map(row => (
              <li key={row.attachment_id}>
                <span>{row.name}</span>
                <span className="data-browser-meta">{formatBytes(row.size)} · {row.modified_at.slice(0, 16).replace('T', ' ')}</span>
                {row.blocked
                  ? <span className="data-browser-blocked">{errorLabel(String(row.blocked), zh)}</span>
                  : (
                    <a className="data-browser-download" download={row.name} rel="noopener"
                      href={attachmentDownloadUrl({ attachment_id: row.attachment_id, filename: row.name, disk_path: `projects/${projectId}/attachments/${row.disk_path}` })}>
                      {zh ? '下载' : 'Download'}
                    </a>
                  )}
              </li>
            ))}
            {attachments.next_offset > 0 && (
              <li>
                <button type="button" className="settings-btn" onClick={() => onOpenAttachments(attachments.next_offset)}>
                  {zh ? '更多' : 'More'}
                </button>
              </li>
            )}
          </ul>
        )}
      </div>

      <div className="data-browser-block">
        <h4>{zh ? '其他文件' : 'Other files'}</h4>
        <button type="button" className="data-browser-link" onClick={() => onOpenFolder('')}>
          {zh ? '在这里浏览项目文件夹' : 'Browse the project folder here'}
        </button>
        {onReveal && (
          <button type="button" className="settings-btn" data-testid="data-browser-reveal" disabled={!connected}
            onClick={() => onReveal('')}>
            {zh ? '在资源管理器中打开' : 'Open in the file manager'}
          </button>
        )}
        {owner && index.other_order_folders.length > 0 && (
          <div className="data-browser-legacy">
            <span className="data-browser-meta">{zh ? '旧任务文件夹' : 'Folders from older runs'}</span>
            <ul className="data-browser-list">
              {index.other_order_folders.map(name => (
                <li key={name}>
                  <button type="button" className="data-browser-link" onClick={() => onOpenOrder(name)}>{name}</button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </>
  )
}

function OrderLevel({ zh, projectId, data, orderId, owner, onOpenFolder, onPreview }: {
  zh: boolean
  projectId: string
  data: DataState
  orderId: string
  owner: boolean
  onOpenFolder: (folder: string) => void
  onPreview: (path: string) => void
}) {
  const view = data.orders[orderId]
  const refused = data.errors[`data_order_view:${orderId}`] || data.errors.data_order_view
  if (refused) return <div className="settings-note" role="alert">{errorLabel(refused, zh)}</div>
  if (!view) return <div className="dashboard-empty">{zh ? '正在读取…' : 'Loading…'}</div>
  const legacy = view.kind === 'legacy_folder'
  return (
    <div className="data-browser-order" data-testid="data-browser-order">
      <h4>{orderId}</h4>
      <div className="data-browser-meta">
        {legacy
          ? (zh ? '这是一个旧任务留下的文件夹，不是业务工单' : 'A folder left by an older run, not a business order')
          : `${view.order.status ?? ''} · ${view.order.customer_name || view.order.customer_id || (zh ? '未指定客户' : 'no customer')}`}
      </div>
      {!view.counts_complete && (
        <div className="settings-note">{zh ? '文件太多，部分数量没有统计完' : 'Too many files: some counts are incomplete'}</div>
      )}

      {view.step_table.state === 'valid' ? (
        <div className="data-browser-block">
          <h5>{zh ? '按步骤' : 'By step'}</h5>
          <ul className="data-browser-steps">
            {view.steps.map(step => (
              <StepRow key={step.id} step={step} zh={zh} onOpenFolder={onOpenFolder} />
            ))}
          </ul>
        </div>
      ) : (
        <div className="settings-note">
          {zh ? '这张单没有步骤表（第 1 期以前开的单），按文件夹显示' : 'No step table for this order; showing folders instead'}
        </div>
      )}

      <div className="data-browser-block">
        <h5>{zh ? '文件夹' : 'Folders'}</h5>
        <ul className="data-browser-list">
          {view.areas.map(area => (
            <li key={area.path}>
              <button type="button" className="data-browser-link" disabled={!area.exists} onClick={() => onOpenFolder(area.path)}>
                {area.label || areaLabel(area.area, zh)}
              </button>
              <span className="data-browser-meta">
                {area.exists ? (area.files === null ? (zh ? '数量未统计' : 'count unknown') : `${area.files} ${zh ? '个文件' : 'files'}`)
                  : (zh ? '还没有内容' : 'nothing here yet')}
              </span>
            </li>
          ))}
        </ul>
      </div>

      {view.history.exists && (
        <div className="data-browser-block">
          <h5>{zh ? '验收历史' : 'Acceptance rounds'}</h5>
          <ul className="data-browser-list">
            {view.history.rounds.map(round => (
              <li key={round.path}>
                <button type="button" className="data-browser-link" onClick={() => onOpenFolder(round.path)}>{round.name}</button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {view.exports.exists && (
        <div className="data-browser-block">
          <h5>{areaLabel('delivery-exports', zh)}</h5>
          <FileRows zh={zh} projectId={projectId} entries={view.exports.files} onPreview={onPreview} />
        </div>
      )}

      {owner && view.portfolio?.exists && (
        <div className="data-browser-block">
          <h5>{areaLabel('portfolio-exports', zh)}</h5>
          <FileRows zh={zh} projectId={projectId} entries={view.portfolio.files} onPreview={onPreview} />
        </div>
      )}
    </div>
  )
}

function StepRow({ step, zh, onOpenFolder }: { step: OrderStep; zh: boolean; onOpenFolder: (folder: string) => void }) {
  // A count the server could not finish is not a zero: it is left out of both sides of the tally and said plainly,
  // or a fully delivered order whose walk ran out of budget reads as "delivered 0/3".
  const measured = step.deliverables.filter(item =>
    item.path ? item.exists !== null && item.exists !== undefined : item.matched !== null && item.matched !== undefined)
  const present = measured.filter(item =>
    item.path ? item.exists === true : Number(item.matched ?? 0) >= 1).length
  const unknown = step.deliverables.length - measured.length
  return (
    <li className="data-browser-step">
      <div className="data-browser-step-head">
        <strong>{step.name || step.id}</strong>
        {step.deliverables.length > 0 && (
          <span className="data-browser-meta">
            {measured.length > 0
              ? (zh ? `交付 ${present}/${measured.length}` : `delivered ${present}/${measured.length}`)
              : (zh ? '交付情况未统计' : 'not measured')}
            {unknown > 0 && (zh ? `（${unknown} 项未统计）` : ` (${unknown} not measured)`)}
          </span>
        )}
      </div>
      <ul className="data-browser-list">
        {step.folders.map(folder => (
          <li key={folder.path}>
            <button type="button" className="data-browser-link" disabled={!folder.exists} onClick={() => onOpenFolder(folder.path)}>
              {areaLabel(folder.kind, zh)}
            </button>
            <span className="data-browser-meta">
              {folder.exists ? (folder.files === null ? (zh ? '数量未统计' : 'count unknown') : `${folder.files}`) : (zh ? '空' : 'empty')}
            </span>
          </li>
        ))}
      </ul>
      {step.deliverables.length > 0 && (
        <ul className="data-browser-deliverables">
          {step.deliverables.map((item, position) => (
            <li key={item.path ?? item.pattern ?? position}>
              <span>{item.path ?? item.pattern}</span>
              <span className="data-browser-meta">
                {item.path
                  ? (item.exists === true ? '✓' : item.exists === null ? (zh ? '未统计' : 'unknown') : (zh ? '缺' : 'missing'))
                  : (item.matched === null || item.matched === undefined
                    ? (zh ? '未统计' : 'unknown')
                    : `${item.matched} ${zh ? '个' : 'matched'}`)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

function FolderLevel({ zh, projectId, data, folder, onOpenFolder, onPreview, onMore, onReveal }: {
  zh: boolean
  projectId: string
  data: DataState
  folder: string
  onOpenFolder: (folder: string) => void
  onPreview: (path: string) => void
  onMore: (offset: number) => void
  onReveal?: (folder: string) => void
}) {
  const page = data.folders[folder]
  // Both keys: a refusal that names the folder, and one from before the server echoed it back.
  const refused = data.errors[`data_folder_list:${folder}`] || (page ? '' : data.errors.data_folder_list)
  if (refused) return <div className="settings-note" role="alert">{errorLabel(refused, zh)}</div>
  if (!page) return <div className="dashboard-empty">{zh ? '正在读取…' : 'Loading…'}</div>
  const crumbs = folder ? folder.split('/') : []
  return (
    <div className="data-browser-folder" data-testid="data-browser-folder">
      <nav className="data-browser-crumbs">
        <button type="button" className="data-browser-link" onClick={() => onOpenFolder('')}>{zh ? '项目' : 'Project'}</button>
        {crumbs.map((name, position) => (
          <button key={`${name}-${position}`} type="button" className="data-browser-link"
            onClick={() => onOpenFolder(crumbs.slice(0, position + 1).join('/'))}>{name}</button>
        ))}
      </nav>
      <div className="data-browser-folder-head">
        {page.label && <span className="data-browser-meta">{page.label}</span>}
        {onReveal && page.exists && (
          <button type="button" className="settings-btn" data-testid="data-browser-reveal-folder"
            onClick={() => onReveal(folder)}>
            {zh ? '在资源管理器中打开' : 'Open in the file manager'}
          </button>
        )}
      </div>
      {!page.exists ? (
        <div className="dashboard-empty">{zh ? '这个文件夹还没有内容' : 'Nothing here yet'}</div>
      ) : (
        <>
          <FileRows zh={zh} projectId={projectId} entries={page.entries} onPreview={onPreview} onOpenFolder={onOpenFolder} />
          {page.truncated && (
            <div className="settings-note">
              {zh ? '这个文件夹超过 20,000 项，只看了前 20,000 项（按磁盘顺序）'
                : 'More than 20,000 entries: only the first 20,000 were read (in disk order)'}
            </div>
          )}
          {Number(page.links_skipped ?? 0) > 0 && (
            <div className="settings-note">
              {zh ? `跳过了 ${page.links_skipped} 个链接` : `${page.links_skipped} link(s) skipped`}
            </div>
          )}
          {Number(page.next_offset ?? 0) > 0 && (
            <button type="button" className="settings-btn" onClick={() => onMore(Number(page.next_offset))}>
              {zh ? '更多' : 'More'}
            </button>
          )}
        </>
      )}
    </div>
  )
}

function FileRows({ zh, projectId, entries, onPreview, onOpenFolder }: {
  zh: boolean
  projectId: string
  entries: DataEntry[]
  onPreview: (path: string) => void
  onOpenFolder?: (folder: string) => void
}) {
  if (entries.length === 0) return <div className="dashboard-empty">{zh ? '空文件夹' : 'Empty'}</div>
  return (
    <ul className="data-browser-rows">
      {entries.map(entry => (
        <li key={entry.path} className={entry.kind === 'dir' ? 'is-dir' : ''}>
          {entry.kind === 'dir' && onOpenFolder ? (
            <button type="button" className="data-browser-link" onClick={() => onOpenFolder(entry.path)}>{entry.name}/</button>
          ) : (
            <span className="data-browser-name">{entry.name}</span>
          )}
          <span className="data-browser-meta">
            {entry.kind === 'file' ? formatBytes(entry.size) : ''}
            {entry.modified_at ? ` · ${entry.modified_at.slice(0, 16).replace('T', ' ')}` : ''}
          </span>
          {entry.kind === 'file' && (entry.blocked
            ? <span className="data-browser-blocked">{errorLabel(String(entry.blocked), zh)}</span>
            : (
              <>
                {(entry.file_kind === 'text' || entry.file_kind === 'image') && (
                  <button type="button" className="settings-btn" onClick={() => onPreview(entry.path)}>
                    {zh ? '预览' : 'Preview'}
                  </button>
                )}
                <a className="data-browser-download" href={dataDownloadUrl(projectId, entry.path)} download={entry.name} rel="noopener">
                  {zh ? '下载' : 'Download'}
                </a>
              </>
            ))}
        </li>
      ))}
    </ul>
  )
}
