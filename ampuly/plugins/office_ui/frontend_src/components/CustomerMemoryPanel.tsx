// 客户记忆 (4.7-A09): what this project remembers, customer by customer, for the owner to read and to delete one
// entry at a time. A delete asks a second time, and what it takes goes to the 回收区 card, where it can be restored.

import { useI18n } from '../i18n'
import { memoryError, memoryPreview, NO_CUSTOMER, type CustomerMemoryState, type MemoryGroup } from '../lib/customerMemory'

export interface CustomerMemoryPanelProps {
  memory: CustomerMemoryState
  connected?: boolean
  onRefresh: () => void
  /** Show the second question for this entry; nothing is deleted yet. */
  onAskDelete: (customer: string, id: string) => void
  onCancelDelete: () => void
  onDelete: (customer: string, id: string) => void
}

export function CustomerMemoryPanel({
  memory, connected = true, onRefresh, onAskDelete, onCancelDelete, onDelete,
}: CustomerMemoryPanelProps) {
  const { locale } = useI18n()
  const zh = locale.startsWith('zh')
  const overview = memory.overview
  const working = !!memory.busy
  const groups: MemoryGroup[] = overview
    ? [
        ...(overview.project.entries.length > 0
          ? [{ customer: NO_CUSTOMER, name: zh ? '不属于某个客户' : 'Not about one customer', entries: overview.project.entries }]
          : []),
        ...overview.customers.filter(group => group.entries.length > 0),
      ]
    : []

  return (
    <section className="dashboard-card project-folder" data-testid="customer-memory-panel">
      <h3 className="dashboard-card-title">{zh ? '客户记忆' : 'What this project remembers'}</h3>
      <p className="data-browser-meta">
        {zh ? '做事时系统会把关于客户的要点记下来，下次给同一个客户干活时参考。记错了或者过时了，可以在这里删掉；删掉的原文会放进「数据与存储」里的回收区，需要时能找回原文。'
            : 'Notes kept about each customer, read again the next time work is done for them. Delete one that is wrong or out of date; it goes to the recycle area under Data & storage and can be restored.'}
      </p>

      {!overview && memory.loading && <div className="dashboard-empty">{zh ? '正在读取…' : 'Reading…'}</div>}
      {!overview && !memory.loading && !memory.error && <div className="dashboard-empty">{zh ? '还没有读取' : 'Not read yet'}</div>}
      {overview && groups.length === 0 && (
        <div className="dashboard-empty" data-testid="customer-memory-empty">
          {zh ? '这个项目还没有记下任何客户记忆' : 'This project has not remembered anything yet'}
        </div>
      )}

      {groups.map(group => (
        <div key={`group-${group.customer}`} className="data-browser-block" data-testid="customer-memory-group">
          <h4>
            {group.customer === NO_CUSTOMER ? group.name : (zh ? `客户：${group.name}` : `Customer: ${group.name}`)}
            <span className="data-browser-meta">{zh ? ` · ${group.entries.length} 条` : ` · ${group.entries.length}`}</span>
          </h4>
          <ul className="data-browser-list">
            {group.entries.map(entry => {
              const asking = memory.confirming?.customer === group.customer && memory.confirming?.id === entry.id
              return (
                <li key={`${group.customer}-${entry.id}`} className="project-folder-row" data-testid="customer-memory-entry">
                  <div>
                    <small className="data-browser-meta">{entry.at || '—'}</small>
                    <div title={entry.text}>{memoryPreview(entry.text)}</div>
                  </div>
                  {asking ? (
                    <div className="project-folder-confirm" data-testid="customer-memory-confirm" role="alertdialog">
                      <p className="project-folder-alarm">
                        {zh ? '删掉这条记忆？原文会放进回收区，需要时能找回原文（找回的是一份文件，不会自动回到记忆里）。' : 'Delete this note? The words go to the recycle area; you can get them back as a file, but not back into memory.'}
                      </p>
                      <div className="project-folder-actions">
                        <button type="button" className="shell-btn shell-btn--primary" data-testid="customer-memory-delete-confirm"
                          disabled={!connected || working} onClick={() => onDelete(group.customer, entry.id)}>
                          {zh ? '确认删除' : 'Delete'}
                        </button>
                        <button type="button" className="settings-btn" data-testid="customer-memory-delete-cancel" onClick={onCancelDelete}>
                          {zh ? '取消' : 'Cancel'}
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="project-folder-actions">
                      <button type="button" className="settings-btn" data-testid="customer-memory-delete"
                        disabled={!connected || working} onClick={() => onAskDelete(group.customer, entry.id)}>
                        {memory.busy === entry.id ? (zh ? '正在删除…' : 'Deleting…') : (zh ? '删除' : 'Delete')}
                      </button>
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        </div>
      ))}

      <div className="project-folder-actions">
        <button type="button" className="settings-btn" data-testid="customer-memory-refresh"
          disabled={!connected || memory.loading} onClick={onRefresh}>{zh ? '刷新' : 'Refresh'}</button>
      </div>

      {memory.note === 'deleted' && (
        <p className="settings-note" role="status" data-testid="customer-memory-deleted">
          {zh ? '已删除。原文在「数据与存储」页签的回收区里，需要时能找回原文。' : 'Deleted. The words are in the recycle area under Data & storage.'}
        </p>
      )}
      {memory.error && (
        <p className="settings-note project-folder-alarm" role="alert">{memoryError(memory.error, zh)}</p>
      )}
    </section>
  )
}
