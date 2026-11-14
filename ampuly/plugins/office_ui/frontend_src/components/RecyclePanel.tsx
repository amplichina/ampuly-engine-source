// 回收区: what a model "deleted" in this project. Nothing a model deletes is erased -- it lands here, and only the
// owner puts it back or clears it, from this card. Every path shown is relative to the project folder; the ids
// sent back are the server's own.

import { useI18n } from '../i18n'
import { formatBytes } from '../lib/dataBrowser'
import { folderError, recycleAreaLabel, recycleMovedAt, type RecycleState } from '../lib/folderAdmin'

export interface RecyclePanelProps {
  recycle: RecycleState
  connected?: boolean
  onRefresh: () => void
  onRestore: (id: string) => void
  /** Show the second question before anything is deleted for good. */
  onAskClear: () => void
  onCancelClear: () => void
  onClear: (token: string) => void
}

export function RecyclePanel({
  recycle, connected = true, onRefresh, onRestore, onAskClear, onCancelClear, onClear,
}: RecyclePanelProps) {
  const { locale } = useI18n()
  const zh = locale.startsWith('zh')
  const list = recycle.list
  const working = !!recycle.busy

  return (
    <section className="dashboard-card project-folder" data-testid="recycle-panel">
      <h3 className="dashboard-card-title">{zh ? '回收区' : 'Recycle area'}</h3>
      <p className="data-browser-meta">
        {zh ? '模型「删除」的文件不会真的删掉，都先放到这里。恢复会把文件放回原来的位置；清空才是真的删除，删之前会再问你一次。'
            : 'A model never erases a file: what it "deletes" lands here. Restore puts a file back where it was; clearing deletes for good, and asks first.'}
      </p>

      {!list && recycle.loading && <div className="dashboard-empty">{zh ? '正在读取…' : 'Reading…'}</div>}
      {!list && !recycle.loading && <div className="dashboard-empty">{zh ? '还没有读取' : 'Not read yet'}</div>}
      {list && list.count === 0 && (
        <div className="dashboard-empty" data-testid="recycle-empty">{zh ? '回收区是空的' : 'The recycle area is empty'}</div>
      )}

      {list && list.count > 0 && (
        <>
          <ul className="data-browser-list" data-testid="recycle-list">
            {list.entries.map(entry => (
              <li key={entry.id} className="project-folder-row">
                <div>
                  <code className="project-folder-path">{entry.original}</code>
                  <small className="data-browser-meta">
                    {recycleAreaLabel(entry.area, zh)}
                    {' · '}{zh ? '移入时间 ' : 'moved '}{recycleMovedAt(entry.moved_at) || '—'}
                    {' · '}{formatBytes(entry.size)}
                  </small>
                </div>
                <div className="project-folder-actions">
                  <button type="button" className="settings-btn" data-testid="recycle-restore"
                    disabled={!connected || working} onClick={() => onRestore(entry.id)}>
                    {recycle.busy === entry.id ? (zh ? '正在恢复…' : 'Restoring…') : (zh ? '恢复' : 'Restore')}
                  </button>
                </div>
              </li>
            ))}
          </ul>
          {list.truncated && (
            <p className="settings-note">
              {zh ? `回收区里的文件太多，这里只列了最近的 ${list.entries.length} 个；一共 ${list.count} 个。`
                  : `Only the newest ${list.entries.length} of ${list.count} files are listed.`}
            </p>
          )}
          <p className="data-browser-meta" data-testid="recycle-total">
            {zh ? `共 ${list.count} 个文件，${formatBytes(list.total_bytes)}` : `${list.count} files, ${formatBytes(list.total_bytes)}`}
          </p>
        </>
      )}

      <div className="project-folder-actions">
        <button type="button" className="settings-btn" data-testid="recycle-refresh"
          disabled={!connected || recycle.loading} onClick={onRefresh}>{zh ? '刷新' : 'Refresh'}</button>
        {list && list.count > 0 && !recycle.confirming && (
          <button type="button" className="settings-btn" data-testid="recycle-clear"
            disabled={!connected || working} onClick={onAskClear}>{zh ? '清空回收区…' : 'Clear the recycle area…'}</button>
        )}
      </div>

      {recycle.confirming && list && (
        <div className="project-folder-confirm" data-testid="recycle-confirm" role="alertdialog">
          <p className="project-folder-alarm">
            {zh ? `要把回收区里的 ${list.count} 个文件（${formatBytes(list.total_bytes)}）彻底删除吗？删掉以后就找不回来了。`
                : `Delete all ${list.count} files (${formatBytes(list.total_bytes)}) for good? They cannot be brought back.`}
          </p>
          <div className="project-folder-actions">
            <button type="button" className="shell-btn shell-btn--primary" data-testid="recycle-clear-confirm"
              disabled={!connected || working || !list.token} onClick={() => onClear(list.token)}>
              {recycle.busy === 'clear' ? (zh ? '正在删除…' : 'Deleting…') : (zh ? '确认彻底删除' : 'Delete for good')}
            </button>
            <button type="button" className="settings-btn" data-testid="recycle-clear-cancel" onClick={onCancelClear}>
              {zh ? '取消' : 'Cancel'}
            </button>
          </div>
        </div>
      )}

      {recycle.note === 'restored' && (
        <p className="settings-note" role="status" data-testid="recycle-restored">
          {zh ? '已恢复到 ' : 'Restored to '}<code>{recycle.restored}</code>
        </p>
      )}
      {recycle.note === 'cleared' && recycle.cleared && (
        <p className="settings-note" role="status" data-testid="recycle-cleared">
          {zh ? `回收区已清空：删除了 ${recycle.cleared.removed} 个文件，释放 ${formatBytes(recycle.cleared.bytes)}。`
              : `Cleared: ${recycle.cleared.removed} files deleted, ${formatBytes(recycle.cleared.bytes)} freed.`}
        </p>
      )}
      {recycle.error && (
        <p className="settings-note project-folder-alarm" role="alert">{folderError(recycle.error, zh)}</p>
      )}
    </section>
  )
}
