import type { ReactElement } from 'react'

/** FE-EB-23：执行工作台内 Observatory 深链入口（专家/负责人）；不污染主路径。 */
export type ExecutionObservatoryPanelProps = {
  zh: boolean
  projectId?: string | null
  activeTaskId?: string | null
  /** 打开既有全屏协作图谱（ExecutionObservatoryHost），不发执行命令。 */
  onOpenFullObservatory: (taskId?: string | null) => void
}

export function ExecutionObservatoryPanel(props: ExecutionObservatoryPanelProps): ReactElement {
  const { zh, projectId, activeTaskId, onOpenFullObservatory } = props
  const task = String(activeTaskId || '').trim()
  const project = String(projectId || '').trim()

  return (
    <div
      className="shell-execution-observatory"
      data-testid="shell-execution-observatory"
      data-surface="panel"
      data-owner-only="yes"
    >
      <p className="shell-card-text" role="note" data-testid="shell-execution-observatory-honest">
        {zh
          ? '协作观测（专家）：在此进入同一执行工作台内的 Observatory；默认主路径不展示此页签。打开图谱不会下发执行命令。'
          : 'Observatory (expert): enter the collaboration graph from this execution-workbench tab; the tab stays off the default path. Opening the graph sends no execution command.'}
      </p>
      <p className="shell-card-text" role="status" data-testid="shell-execution-observatory-context">
        {zh ? '当前项目：' : 'Project: '}
        <code>{project || (zh ? '（未选）' : '(none)')}</code>
        {' · '}
        {zh ? '关注任务：' : 'Task: '}
        <code>{task || (zh ? '（未选）' : '(none)')}</code>
      </p>
      <div className="shell-execution-observatory-actions" data-testid="shell-execution-observatory-actions">
        <button
          type="button"
          className="shell-btn"
          data-testid="shell-execution-observatory-open"
          onClick={() => onOpenFullObservatory(task || null)}
        >
          {zh ? '打开协作图谱' : 'Open collaboration graph'}
        </button>
      </div>
      <p className="shell-card-text" role="note" data-testid="shell-execution-observatory-deeplink">
        {zh
          ? '深链：地址栏 ?view=observatory 仍可直接打开全屏图谱；本页签是工作台内入口。'
          : 'Deep link: ?view=observatory still opens the full graph; this tab is the in-workbench entry.'}
      </p>
    </div>
  )
}
