import type { ReactElement } from 'react'

import type { ProgressEntry } from '../types/kanban'
import {
  extractProgressArtifactPath,
  matchArtifactIndexPath,
  resolveProgressBrowserBinding,
  type ArtifactPathLike,
} from '../lib/progressLocate'

/** Events the「工具事件」tab shows: tool calls, thinking, and status changes — not a system shell (FE-EB-05). */
const TERMINAL_TYPES = new Set(['tool_call', 'thinking', 'status_change'])

export type ExecutionTerminalViewProps = {
  zh: boolean
  entries: ProgressEntry[] | null | undefined
  /** FE-EB-13：当前工单关联 live binding；detail 无显式 key 时供 browser_* 行使用。 */
  correlatedBrowserBindingKey?: string | null
  onLocateBrowser?: (bindingKey: string, entry: ProgressEntry) => void
  /** FE-EB-12：产物索引（仅索引内路径可预览）。 */
  artifactFiles?: readonly ArtifactPathLike[] | null
  onPreviewArtifact?: (path: string, entry: ProgressEntry) => void
}

function formatTime(ts: number): string {
  try {
    return new Date(ts).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch {
    return ''
  }
}

export function filterTerminalEntries(entries: ProgressEntry[] | null | undefined): ProgressEntry[] {
  if (!entries || entries.length === 0) return []
  return entries.filter(entry => TERMINAL_TYPES.has(entry.type))
}

export function ExecutionTerminalView(props: ExecutionTerminalViewProps): ReactElement {
  const {
    zh,
    entries,
    correlatedBrowserBindingKey,
    onLocateBrowser,
    artifactFiles,
    onPreviewArtifact,
  } = props
  const rows = filterTerminalEntries(entries)

  return (
    <div className="shell-execution-terminal" data-testid="shell-execution-terminal">
      <p className="shell-card-text" role="note" data-testid="shell-execution-terminal-note">
        {zh
          ? '工具事件：当前工单的工具调用、思考与状态变化（不是系统 shell，不能在这里敲命令）。'
          : 'Tool events: tool calls, thinking, and status changes for this work order (not a system shell; you cannot type commands here).'}
      </p>
      {rows.length === 0 ? (
        <p className="shell-card-text" data-testid="shell-execution-terminal-empty">
          {zh
            ? '还没有可显示的事件。运行中的步骤产生工具调用或状态变化后会出现在这里。'
            : 'No events yet. Tool calls and status changes from a running step will appear here.'}
        </p>
      ) : (
        <ol className="shell-execution-terminal-list" data-testid="shell-execution-terminal-list">
          {rows.map((entry, index) => {
            const bindingKey = resolveProgressBrowserBinding(entry, correlatedBrowserBindingKey)
            const artifactPath = matchArtifactIndexPath(extractProgressArtifactPath(entry), artifactFiles)
            return (
              <li
                key={`${entry.timestamp}-${entry.type}-${entry.toolCallId || entry.itemId || index}`}
                className="shell-execution-terminal-row"
                data-terminal-type={entry.type}
              >
                <span className="shell-execution-terminal-time">{formatTime(entry.timestamp)}</span>
                <span className="shell-execution-terminal-kind">{entry.type}</span>
                <span className="shell-execution-terminal-summary">{entry.summary || (zh ? '（无摘要）' : '(no summary)')}</span>
                {entry.detail ? (
                  <pre className="shell-execution-terminal-detail">{entry.detail}</pre>
                ) : null}
                {(bindingKey || artifactPath) ? (
                  <div className="shell-execution-terminal-actions" data-testid="shell-execution-terminal-actions">
                    {bindingKey && onLocateBrowser ? (
                      <button
                        type="button"
                        className="shell-btn"
                        data-testid="shell-execution-terminal-locate"
                        data-binding={bindingKey}
                        onClick={() => onLocateBrowser(bindingKey, entry)}
                      >
                        {zh ? '定位到画面' : 'Locate picture'}
                      </button>
                    ) : null}
                    {artifactPath && onPreviewArtifact ? (
                      <button
                        type="button"
                        className="shell-btn"
                        data-testid="shell-execution-terminal-preview"
                        data-path={artifactPath}
                        onClick={() => onPreviewArtifact(artifactPath, entry)}
                      >
                        {zh ? '预览产物' : 'Preview artifact'}
                      </button>
                    ) : null}
                  </div>
                ) : null}
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
}
