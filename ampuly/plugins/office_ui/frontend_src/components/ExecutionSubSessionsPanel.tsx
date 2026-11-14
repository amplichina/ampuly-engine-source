import { useMemo, useState, type ReactElement } from 'react'

import { AgentProgressBlock } from '../chat/AgentProgressBlock'
import type { ProgressEntry, Session } from '../types/kanban'
import {
  extractProgressArtifactPath,
  matchArtifactIndexPath,
  resolveProgressBrowserBinding,
  type ArtifactPathLike,
} from '../lib/progressLocate'

/** 执行工作台「子会话」页签：进度 + 消息摘要，可一键切到浏览器表面（FE-EB-11）。自研，不抄 Evo。 */
export type ExecutionSubSessionsPanelProps = {
  zh: boolean
  childSessions: readonly Session[]
  /** 当前工单关联的 live 浏览器 binding（无显式元数据时，browser_* 行可关联）。 */
  correlatedBrowserBindingKey?: string | null
  artifactFiles?: readonly ArtifactPathLike[] | null
  onOpenBrowserSurface: (taskId?: string, bindingKey?: string | null) => void
  onPreviewArtifact?: (path: string) => void
  /** 打开子会话为活动会话（完整对话在主区）。 */
  onFocusChildSession?: (taskId: string) => void
}

function statusLabel(status: string, zh: boolean): string {
  if (zh) {
    if (status === 'running') return '运行中'
    if (status === 'done') return '完成'
    if (status === 'failed') return '失败'
    if (status === 'pending') return '等待'
    if (status === 'cancelled') return '已取消'
  }
  return status || '—'
}

export function ExecutionSubSessionsPanel(props: ExecutionSubSessionsPanelProps): ReactElement {
  const {
    zh,
    childSessions,
    correlatedBrowserBindingKey,
    artifactFiles,
    onOpenBrowserSurface,
    onPreviewArtifact,
    onFocusChildSession,
  } = props

  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)

  const selected = useMemo(() => {
    if (!childSessions.length) return null
    const wanted = selectedTaskId && childSessions.find(s => s.taskId === selectedTaskId)
    return wanted || childSessions[0]
  }, [childSessions, selectedTaskId])

  const locateBinding = (entry: ProgressEntry): string | null => (
    resolveProgressBrowserBinding(entry, correlatedBrowserBindingKey)
  )

  const artifactPathFor = (entry: ProgressEntry): string | null => (
    matchArtifactIndexPath(extractProgressArtifactPath(entry), artifactFiles)
  )

  if (!childSessions.length) {
    return (
      <div className="shell-execution-subsessions" data-testid="shell-execution-subsessions" data-surface="empty">
        <p className="shell-card-text" role="note">
          {zh
            ? '当前工单还没有子会话。公司模式委派或子任务启动后，会在这里列出进度与消息摘要。'
            : 'No child sessions on this work order yet. Delegated company-mode or sub-tasks will list progress and message summaries here.'}
        </p>
        <p className="shell-card-text" data-testid="shell-execution-subsessions-honest">
          {zh
            ? '此页签是执行工作台的一部分，不是第二套看板执行面板。'
            : 'This tab is part of the execution workbench, not a second kanban ExecutionPanel.'}
        </p>
      </div>
    )
  }

  return (
    <div className="shell-execution-subsessions" data-testid="shell-execution-subsessions" data-surface="list">
      <p className="shell-card-text" role="note">
        {zh
          ? '子会话：查看子智能体进度与消息摘要；可一键切到浏览器画面。'
          : 'Child sessions: progress and message summaries; jump to the browser surface in one click.'}
      </p>
      <div className="shell-execution-subsessions-layout">
        <div className="shell-execution-subsessions-list" role="list" data-testid="shell-execution-subsessions-list">
          {childSessions.map(session => {
            const active = selected?.taskId === session.taskId
            return (
              <button
                key={session.taskId}
                type="button"
                role="listitem"
                className={`shell-btn${active ? ' is-active' : ''}`}
                data-testid={active ? 'shell-execution-subsession-active' : undefined}
                aria-pressed={active}
                onClick={() => setSelectedTaskId(session.taskId)}
              >
                <strong>{session.title || session.taskId}</strong>
                <small>
                  {statusLabel(session.status, zh)}
                  {session.agentStatus ? ` · ${session.agentStatus}` : ''}
                  {typeof session.messageCount === 'number'
                    ? (zh ? ` · ${session.messageCount} 条消息` : ` · ${session.messageCount} msgs`)
                    : ''}
                </small>
              </button>
            )
          })}
        </div>
        {selected && (
          <div className="shell-execution-subsessions-detail" data-testid="shell-execution-subsessions-detail">
            <div className="shell-execution-subsessions-actions">
              <button
                type="button"
                className="shell-btn"
                data-testid="shell-execution-subsessions-open-browser"
                onClick={() => onOpenBrowserSurface(selected.taskId, correlatedBrowserBindingKey)}
              >
                {zh ? '打开浏览器画面' : 'Open browser surface'}
              </button>
              {onFocusChildSession && (
                <button
                  type="button"
                  className="shell-btn"
                  data-testid="shell-execution-subsessions-focus-child"
                  onClick={() => onFocusChildSession(selected.taskId)}
                >
                  {zh ? '在主区打开会话' : 'Open session in main'}
                </button>
              )}
            </div>
            <div className="shell-execution-subsessions-messages" data-testid="shell-execution-subsessions-messages">
              <h4>{zh ? '消息摘要' : 'Message summary'}</h4>
              {selected.latestPreview ? (
                <p className="shell-card-text">
                  {selected.latestSender ? <strong>{selected.latestSender}: </strong> : null}
                  {selected.latestPreview}
                </p>
              ) : (
                <p className="shell-card-text">{zh ? '还没有消息预览。' : 'No message preview yet.'}</p>
              )}
            </div>
            <div className="shell-execution-subsessions-progress" data-testid="shell-execution-subsessions-progress">
              <h4>{zh ? '进度' : 'Progress'}</h4>
              <AgentProgressBlock
                entries={selected.progressLog ?? []}
                agentStatus={selected.agentStatus}
                currentTool={selected.currentTool}
                sessionStatus={selected.status}
                expandedByDefault
                resolveBrowserBinding={locateBinding}
                onLocateBrowser={(bindingKey) => onOpenBrowserSurface(selected.taskId, bindingKey)}
                resolveArtifactPath={artifactPathFor}
                onPreviewArtifact={onPreviewArtifact}
              />
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
