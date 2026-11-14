// 流程预览's board (frontend refactor U2-7). Read-mostly: a tile opens the step in the same inspector the canvas
// uses, so every edit goes through the one draft and the one undo history. No stylesheet import (contract tests run
// in node); the rules are in index.css.

import { PREVIEW_STATUS_LABELS, type PreviewGroup } from './flowPreview'

export function FlowPreviewBoard({ zh, groups, selectedId, kindLabel, onOpen }: {
  zh: boolean
  groups: PreviewGroup[]
  selectedId: string
  kindLabel: (kind: string) => string
  onOpen: (stepId: string) => void
}) {
  if (groups.length === 0) {
    return <div className="wf-preview wf-preview--empty" role="status">{zh ? '这条业务流还没有步骤：切到「无限画布」，从左侧组件栏添加。' : 'No steps yet: switch to the canvas and add one from the component panel.'}</div>
  }
  return (
    <div className="wf-preview" data-testid="flow-preview">
      {groups.map(group => (
        <section key={group.key || '(none)'} className="wf-preview-group" aria-label={group.title}>
          <h4><span className="wf-preview-index">{group.index}</span>{group.title}<small>{group.tiles.length}</small></h4>
          <div className="wf-preview-tiles">
            {group.tiles.map(tile => (
              <button key={tile.id} type="button" className={`wf-preview-tile${selectedId === tile.id ? ' is-selected' : ''}`}
                data-step={tile.id} data-status={tile.status} aria-pressed={selectedId === tile.id} onClick={() => onOpen(tile.id)}>
                <span className="wf-preview-tile-head">
                  <strong title={tile.name}>{tile.name}</strong>
                  <span className={`wf-preview-status wf-preview-status--${tile.status}`}>{PREVIEW_STATUS_LABELS[tile.status][zh ? 0 : 1]}</span>
                </span>
                <span className="wf-preview-tags">
                  <span className="shell-tag">{kindLabel(tile.kind)}</span>
                  <span className="wf-preview-gate">{tile.gated ? (zh ? '需人工确认' : 'A person confirms') : (zh ? '自动步骤' : 'Automatic')}</span>
                </span>
                <span className="wf-preview-text">{tile.responsibility || (zh ? '还没写这一步要做成什么。' : 'What this step must get done is not written yet.')}</span>
                <span className="wf-preview-foot">
                  <span>{zh ? `${tile.depends} 项前置依赖` : `${tile.depends} upstream`}</span>
                  <span className="wf-preview-open">{zh ? '查看 →' : 'Open →'}</span>
                </span>
              </button>
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}
