// 项目文件夹: where this project's files are kept, and how the owner changes it.
//
// Two steps, as the owner asked for them: pointing the project at a folder moves nothing, and what the old folder
// still holds is a separate question that stays available until it is answered. Every path shown came from a
// reply; this file composes none, and the chooser only ever offers what the server said may be chosen.

import { useState } from 'react'

import { useI18n } from '../i18n'
import { formatBytes } from '../lib/dataBrowser'
import { folderError, folderWarn, type DirPage, type FolderState } from '../lib/folderAdmin'

export interface ProjectFolderPanelProps {
  /** The 真实位置 reply: where things are, and what must be said about them. */
  locations: {
    locations: { key: string; label: string; path: string; exists: boolean; note: string }[]
    project_root_source?: string
    default_root_set_by?: string
    default_root_shadowed?: boolean
    project_root_missing?: boolean
    legacy_folder?: { path: string; files: number | null; folders: number | null } | null
    dropped_entries?: { project_id: string; code: string }[]
  } | null
  folder: FolderState
  connected?: boolean
  /** Open the machine's own folder dialog. The owner browses and creates folders there, not here. */
  onPick: (scope: 'project' | 'default') => void
  onOpenChooser: (scope: 'project' | 'default') => void
  onCloseChooser: () => void
  onBrowse: (path: string) => void
  onCreate: (parent: string, name: string) => void
  onSelect: (path: string) => void
  onPlan: (scope: 'project' | 'default', target: string) => void
  onSet: (scope: 'project' | 'default', target: string, token: string) => void
  onImport: (source: string, token: string) => void
  onReveal: (folder: string, legacy?: boolean) => void
  /** Ask what the old folder holds now, when this session was not the one that pointed it. */
  onLegacy: () => void
}

export function ProjectFolderPanel({
  locations, folder, connected = true,
  onPick, onOpenChooser, onCloseChooser, onBrowse, onCreate, onSelect, onPlan, onSet, onImport, onReveal,
  onLegacy,
}: ProjectFolderPanelProps) {
  const { locale } = useI18n()
  const zh = locale.startsWith('zh')
  const rows = locations?.locations ?? []
  const own = rows.find(row => row.key === 'project_workplace')
  const root = rows.find(row => row.key === 'default_root')
  const legacy = folder.legacy ?? (locations?.legacy_folder
    ? { path: locations.legacy_folder.path, files: locations.legacy_folder.files ?? 0, bytes: 0, token: '', complete: true }
    : null)

  return (
    <section className="dashboard-card project-folder" data-testid="project-folder-panel">
      <h3 className="dashboard-card-title">{zh ? '项目文件夹' : 'Project folder'}</h3>

      <div className="project-folder-row">
        <div>
          <div className="project-folder-label">{zh ? '这个项目的文件放在' : 'This project keeps its files in'}</div>
          <code className="project-folder-path">{own?.path ?? '—'}</code>
          <small className="data-browser-meta">
            {locations?.project_root_source === 'project'
              ? (zh ? '由你指定' : 'chosen by you')
              : (zh ? '按默认根目录自动放置' : 'placed under the default root')}
          </small>
        </div>
        <div className="project-folder-actions">
          <button type="button" className="settings-btn" data-testid="project-folder-reveal" disabled={!connected}
            onClick={() => onReveal('')}>{zh ? '在资源管理器中打开' : 'Open in the file manager'}</button>
          <button type="button" className="shell-btn" data-testid="project-folder-change"
            disabled={!connected || folder.picking}
            onClick={() => onPick('project')}>
            {folder.picking ? (zh ? '选择窗口已打开…' : 'The dialog is open…') : (zh ? '更换文件夹…' : 'Change folder…')}
          </button>
          {locations?.project_root_source === 'project' && (
            <button type="button" className="settings-btn" data-testid="project-folder-default" disabled={!connected}
              onClick={() => onPlan('project', '')}>
              {zh ? '回到默认根目录' : 'Back to the default root'}
            </button>
          )}
        </div>
      </div>

      {locations?.project_root_missing && (
        <p className="settings-note project-folder-alarm" role="alert">
          {zh ? '指定的项目文件夹现在不在了（磁盘没插上，或者被移走了）。在它回来之前，这个项目不会新建文件，「资料」也看不到东西。'
              : 'The folder this project was pointed at is not there right now.'}
        </p>
      )}

      {legacy && (
        <div className="project-folder-legacy" data-testid="project-folder-legacy">
          <p>
            {zh ? '原文件夹里还有旧文件：' : 'The old folder still holds files: '}
            <code>{legacy.path}</code>
            {legacy.files ? (zh ? `，共 ${legacy.files} 个文件` : `, ${legacy.files} files`) : ''}
            {legacy.bytes ? `（${formatBytes(legacy.bytes)}）` : ''}
          </p>
          <p className="data-browser-meta">
            {zh ? '「资料」显示的是新位置，不会显示这些文件。可以现在一键导入，也可以以后再说；系统不会替你删掉它们。'
                : 'They are not shown in 资料. Import them now or later; nothing is deleted on your behalf.'}
          </p>
          <div className="project-folder-actions">
            <button type="button" className="shell-btn shell-btn--primary" data-testid="project-folder-import"
              disabled={!connected || folder.importing}
              onClick={() => (legacy.token ? onImport(legacy.path, legacy.token) : onLegacy())}>
              {folder.importing ? (zh ? '正在导入…' : 'Importing…')
                : legacy.token ? (zh ? '一键导入旧文件' : 'Import the old files')
                : (zh ? '一键导入旧文件…' : 'Import the old files…')}
            </button>
            <button type="button" className="settings-btn" data-testid="project-folder-reveal-legacy"
              disabled={!connected}
              onClick={() => onReveal('', true)}>{zh ? '先去看看' : 'Take a look first'}</button>
          </div>
          {folder.importing && folder.progress && (
            <p className="data-browser-meta">
              {zh ? '已搬 ' : 'moved '}{formatBytes(folder.progress.done)} / {formatBytes(folder.progress.total)}
            </p>
          )}
        </div>
      )}

      <div className="project-folder-row">
        <div>
          <div className="project-folder-label">{zh ? '新项目的默认根目录' : 'Where new projects go'}</div>
          <code className="project-folder-path">{root?.path ?? '—'}</code>
          {locations?.default_root_shadowed && (
            <small className="project-folder-alarm">
              {zh ? '你设过一个默认根目录，但启动时的环境变量把它盖住了，现在实际用的是上面这个。'
                  : 'Your setting is recorded but an environment variable is in force instead.'}
            </small>
          )}
        </div>
        <div className="project-folder-actions">
          <button type="button" className="shell-btn" data-testid="project-folder-root"
            disabled={!connected || folder.picking}
            onClick={() => onPick('default')}>{zh ? '更换默认根目录…' : 'Change the default root…'}</button>
        </div>
      </div>
      <p className="data-browser-meta">
        {zh ? '改默认根目录不会搬动任何现有项目：每个已经有文件的项目都会被钉在它现在的位置。'
            : 'Changing it moves nothing: every project that has files is pinned where it is.'}
      </p>

      {(locations?.dropped_entries ?? []).length > 0 && (
        <p className="settings-note project-folder-alarm" role="alert">
          {zh ? '有设置没有生效：' : 'Some settings were not used: '}
          {(locations?.dropped_entries ?? []).map(entry =>
            `${entry.project_id || (zh ? '默认根目录' : 'default root')} — ${folderError(entry.code, zh)}`).join('；')}
        </p>
      )}

      {folder.note === 'imported' && (
        <p className="settings-note" role="status" data-testid="project-folder-imported">
          {folder.leftovers
            ? (zh ? `旧文件已经搬过来了。原文件夹 ${folder.leftovers.path} 里还剩 ${folder.leftovers.entries} 项不是系统放的，没有删，要不要留着由你决定。`
                  : `The old files were brought over. ${folder.leftovers.entries} item(s) that were not ours were left in ${folder.leftovers.path}.`)
            : (zh ? '旧文件已经搬过来了，原文件夹已经清空删除。' : 'The old files were brought over and the old folder is gone.')}
        </p>
      )}

      {folder.picking && (
        <p className="settings-note" role="status">
          {zh ? '文件夹选择窗口已经在你的屏幕上打开了，选好（或新建）一个文件夹再回来。'
              : 'The folder dialog is open on your screen; pick or create one there.'}
        </p>
      )}

      {folder.plan && !folder.open && (
        <div className="project-folder-confirm" data-testid="project-folder-confirm">
          {folder.plan.check.code ? (
            <p className="project-folder-alarm" role="alert">
              <code>{folder.plan.target}</code> — {folderError(folder.plan.check.code, zh)}
            </p>
          ) : (
            <>
              <p>
                {folder.plan.scope === 'default'
                  ? (zh ? '要把新项目的默认根目录改成 ' : 'Put new projects under ')
                  : (zh ? '要把这个项目指向 ' : 'Point this project at ')}
                <code>{folder.plan.target}</code>{zh ? ' 吗？' : '?'}
              </p>
              <p className="data-browser-meta">
                {zh ? '这一步不会搬动任何文件。' : 'This step moves no files.'}
                {folder.plan.plan && folder.plan.plan.files > 0 && (
                  zh ? ` 换好以后会问你：原文件夹里的 ${folder.plan.plan.files} 个文件（${formatBytes(folder.plan.plan.bytes)}）要不要一键导入。`
                     : ` Afterwards you will be asked about the ${folder.plan.plan.files} files still in the old folder.`
                )}
              </p>
              {(folder.plan.check.warn ?? []).map(code => (
                <p key={code} className="settings-note">{folderWarn(code, zh)}</p>
              ))}
              {folder.plan.busy && <p className="project-folder-alarm">{folderError(folder.plan.busy, zh)}</p>}
              <div className="project-folder-actions">
                <button type="button" className="shell-btn shell-btn--primary" data-testid="project-folder-confirm-btn"
                  disabled={!connected || !!folder.plan.busy || !folder.plan.token}
                  onClick={() => onSet(folder.plan!.scope, folder.plan!.target, folder.plan!.token)}>
                  {zh ? '确认' : 'Confirm'}
                </button>
                <button type="button" className="settings-btn" onClick={() => onSelect('')}>
                  {zh ? '取消' : 'Cancel'}
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {folder.error && !folder.open && (
        <p className="settings-note project-folder-alarm" role="alert">{folderError(folder.error, zh)}</p>
      )}

      {/* A machine whose own dialog will not open still has to be able to change the folder, so the list this
          console can draw itself stays as the way back -- offered only then, never as the first thing shown. */}
      {(folder.error === 'unsupported' || folder.error === 'pick_timed_out') && !folder.open && (
        <div className="project-folder-actions">
          <button type="button" className="settings-btn" data-testid="project-folder-fallback" disabled={!connected}
            onClick={() => onOpenChooser(folder.scope)}>
            {zh ? '在控制台里选文件夹' : 'Choose it here instead'}
          </button>
        </div>
      )}

      {folder.open && (
        <FolderChooser
          zh={zh} state={folder} connected={connected}
          onClose={onCloseChooser} onBrowse={onBrowse} onCreate={onCreate} onSelect={onSelect}
          onPlan={(target) => onPlan(folder.scope, target)}
          onConfirm={(target, token) => onSet(folder.scope, target, token)}
        />
      )}
    </section>
  )
}

function FolderChooser({ zh, state, connected, onClose, onBrowse, onCreate, onSelect, onPlan, onConfirm }: {
  zh: boolean
  state: FolderState
  connected: boolean
  onClose: () => void
  onBrowse: (path: string) => void
  onCreate: (parent: string, name: string) => void
  onSelect: (path: string) => void
  onPlan: (target: string) => void
  onConfirm: (target: string, token: string) => void
}) {
  const [naming, setNaming] = useState(false)
  const [name, setName] = useState('')
  const page: DirPage | null = state.page
  const check = state.plan?.check
  const chosen = state.selected || page?.path || ''

  return (
    <div className="artifacts-overlay" onClick={onClose}>
      <div className="artifacts-preview project-folder-chooser" role="dialog" aria-modal="true"
        data-testid="folder-chooser" onClick={event => event.stopPropagation()}>
        <div className="artifacts-preview-header">
          <span className="artifacts-preview-name">
            {state.scope === 'default'
              ? (zh ? '选择新项目的默认根目录' : 'Choose where new projects go')
              : (zh ? '选择这个项目的文件夹' : 'Choose this project’s folder')}
          </span>
          <button className="settings-close" onClick={onClose}>✕</button>
        </div>

        <div className="project-folder-crumbs">
          <button type="button" className="data-browser-link" onClick={() => onBrowse('')}>
            {zh ? '此电脑' : 'This computer'}
          </button>
          {page?.parent && (
            <button type="button" className="data-browser-link" onClick={() => onBrowse(page.parent)}>
              {zh ? '上一层' : 'Up'}
            </button>
          )}
          {page?.path && <code>{page.path}</code>}
        </div>

        <div className="artifacts-preview-body project-folder-list">
          {state.looking && !page && <div className="dashboard-empty">{zh ? '正在读取…' : 'Reading…'}</div>}
          {page && page.entries.length === 0 && (
            <div className="dashboard-empty">{zh ? '这里没有子文件夹' : 'No folders here'}</div>
          )}
          <ul className="data-browser-list">
            {(page?.entries ?? []).map(entry => (
              <li key={entry.path}>
                <button type="button" className="data-browser-link" disabled={!!entry.refused}
                  onClick={() => { onSelect(entry.path); onBrowse(entry.path) }}>
                  {entry.name}{entry.kind === 'home' || entry.kind ? '' : '/'}
                </button>
                {entry.refused && <span className="data-browser-blocked">{folderError(entry.refused, zh)}</span>}
                {entry.link && <span className="data-browser-meta">{zh ? '链接' : 'link'}</span>}
              </li>
            ))}
          </ul>
          {page?.truncated && (
            <p className="settings-note">{zh ? '这个文件夹里的子目录太多，只列了前面一部分。' : 'Only the first folders are listed.'}</p>
          )}
        </div>

        <div className="project-folder-footer">
          {naming ? (
            <div className="project-folder-actions">
              <input className="shell-input" value={name} autoFocus placeholder={zh ? '新文件夹的名字' : 'Folder name'}
                data-testid="folder-chooser-name" onChange={event => setName(event.target.value)} />
              <button type="button" className="shell-btn" disabled={!name.trim() || !page?.path}
                onClick={() => { onCreate(page!.path, name.trim()); setNaming(false); setName('') }}>
                {zh ? '创建' : 'Create'}
              </button>
              <button type="button" className="settings-btn" onClick={() => { setNaming(false); setName('') }}>
                {zh ? '取消' : 'Cancel'}
              </button>
            </div>
          ) : (
            <div className="project-folder-actions">
              <button type="button" className="settings-btn" data-testid="folder-chooser-new"
                disabled={!connected || !page?.can_create} onClick={() => setNaming(true)}>
                {zh ? '新建文件夹' : 'New folder'}
              </button>
              <button type="button" className="settings-btn" data-testid="folder-chooser-check"
                disabled={!connected || !chosen} onClick={() => onPlan(chosen)}>
                {zh ? '就用这个文件夹' : 'Use this folder'}
              </button>
            </div>
          )}

          {state.error && <p className="settings-note project-folder-alarm" role="alert">{folderError(state.error, zh)}</p>}

          {check && check.code !== '' && (
            <p className="settings-note project-folder-alarm" role="alert">{folderError(check.code, zh)}</p>
          )}
          {check && check.code === '' && state.plan && (
            <div className="project-folder-confirm" data-testid="folder-chooser-confirm">
              <p>
                {state.plan.scope === 'default'
                  ? (zh ? '要把新项目的默认根目录改成 ' : 'Put new projects under ')
                  : (zh ? '要把这个项目指向 ' : 'Point this project at ')}
                <code>{state.plan.target}</code>{zh ? ' 吗？' : '?'}
              </p>
              <p className="data-browser-meta">
                {zh ? '这一步不会搬动任何文件。' : 'This step moves no files.'}
                {state.plan.plan && state.plan.plan.files > 0 && (
                  zh ? ` 换好以后会问你：原文件夹里的 ${state.plan.plan.files} 个文件（${formatBytes(state.plan.plan.bytes)}）要不要一键导入。`
                     : ` Afterwards you will be asked about the ${state.plan.plan.files} files still in the old folder.`
                )}
              </p>
              {(state.plan.check.warn ?? []).map(code => (
                <p key={code} className="settings-note">{folderWarn(code, zh)}</p>
              ))}
              {state.plan.busy && <p className="settings-note project-folder-alarm">{folderError(state.plan.busy, zh)}</p>}
              <button type="button" className="shell-btn shell-btn--primary" data-testid="folder-chooser-confirm-btn"
                disabled={!connected || !!state.plan.busy || !state.plan.token}
                onClick={() => onConfirm(state.plan!.target, state.plan!.token)}>
                {zh ? '确认指向这里' : 'Point it here'}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
