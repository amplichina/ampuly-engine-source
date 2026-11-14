import type { ReactElement } from 'react'
import { DesktopReadonlyView } from './DesktopReadonlyView'

export type DesktopLeaseState =
  | 'none'
  | 'created'
  | 'agent_operating'
  | 'handoff_requested'
  | 'awaiting_human'
  | 'human_operating'
  | 'human_returned'
  | 'identity_checking'
  | 'identity_verified'
  | 'revoked'
  | 'expired'
  | 'close_pending'
  | 'close_failed'
  | 'closed'
  | string

export type DesktopSessionStatus = {
  state: DesktopLeaseState
  controller: string
  fresh_look_required?: boolean
  /** Last window title the model was shown (projection; no pixels). */
  window_title?: string
  /** Truncated screen hash from the last delivered look. */
  screen_hash?: string
  /** True once a desktop look has been observed for this session. */
  frame_delivered?: boolean
  /** Latest helper / input / gateway refusal reason, if any. */
  last_refusal?: string
  app?: string
}

const STATE_ZH: Record<string, string> = {
  none: '无租约',
  created: '已创建',
  agent_operating: '助手操作中',
  handoff_requested: '已请求接管',
  awaiting_human: '等人接管',
  human_operating: '你在操作',
  human_returned: '已交还，待新画面',
  identity_checking: '核验中',
  identity_verified: '已核验',
  revoked: '已收回',
  expired: '已过期',
  close_pending: '关闭中',
  close_failed: '关闭失败',
  closed: '已关闭',
}

export type DesktopSessionControlsProps = {
  surfaceOnly?: boolean
  projectId?: string
  authToken?: string
  zh: boolean
  taskId: string | null | undefined
  canControl: boolean
  status: DesktopSessionStatus | null
  error: string | null
  busy?: boolean
  onRefresh: () => void
  onTakeover: () => void
  onHandBack: () => void
}

export function DesktopSessionControls(props: DesktopSessionControlsProps): ReactElement {
  const { zh, taskId, canControl, status, error, busy, onRefresh, onTakeover, onHandBack } = props
  const state = status?.state ?? 'none'
  const humanOperating = state === 'human_operating'
  const hasTask = Boolean(taskId && String(taskId).trim())
  const stateLabel = zh ? (STATE_ZH[state] ?? state) : state
  const canView = Boolean(canControl && hasTask && props.projectId && props.authToken)

  if (props.surfaceOnly) return <div className="execution-desktop-surface" data-testid="shell-execution-desktop"
    data-desktop-live-projection={canView ? 'owner-opt-in' : 'blocked'} data-fe-eb-22={canView ? 'owner-opt-in' : 'blocked'}>
    {hasTask && <div className="execution-desktop-toolbar">
      <span className="execution-desktop-title" title={status?.window_title}>{status?.window_title || (zh ? '当前任务窗口' : 'Task window')}</span>
      <span className="shell-card-text" data-testid="shell-execution-desktop-state" data-desktop-state={state}>{stateLabel}</span>
      <button type="button" className="shell-btn" disabled={!canControl || busy || !status || humanOperating}
        data-testid="shell-execution-desktop-takeover" onClick={onTakeover}>{zh ? '接管' : 'Take over'}</button>
      <button type="button" className="shell-btn" disabled={!canControl || busy || !humanOperating}
        data-testid="shell-execution-desktop-handback" onClick={onHandBack}>{zh ? '完成并交还' : 'Done and hand back'}</button>
      <button type="button" className="shell-btn" disabled={!canControl || busy}
        data-testid="shell-execution-desktop-refresh" onClick={onRefresh}>{zh ? '刷新状态' : 'Refresh'}</button>
    </div>}
    {canView ? <DesktopReadonlyView key={`${props.projectId}:${taskId}:${props.authToken}`} compact projectId={props.projectId!}
      taskId={String(taskId)} authToken={props.authToken!} zh={zh} /> : <div className="execution-surface-empty" data-testid="shell-execution-desktop-live-blocked">
      <strong>{zh ? '当前没有可显示的软件窗口' : 'No application window to show'}</strong>
      <p className="shell-card-text">{zh ? '打开含软件操作步骤的任务后，在这里查看或接管。' : 'Open a task with application steps to view it or take over.'}</p>
    </div>}
    {(error || status?.last_refusal) && <p className="execution-surface-notice" role="alert">{error || status?.last_refusal}</p>}
  </div>

  return (
    <div
      className="shell-execution-desktop"
      data-testid="shell-execution-desktop"
      data-desktop-live-projection={canView ? 'owner-opt-in' : 'blocked'}
      data-fe-eb-22={canView ? 'owner-opt-in' : 'blocked'}
    >
      <p className="shell-card-text" role="note" data-testid="shell-execution-desktop-honest">{zh
        ? (canView ? '桌面控制：查看任务窗口、接管或交还。查看画面与操作权限分别控制。' : '桌面控制：租约接管/交还与状态；这里不是内嵌桌面画面。')
        : (canView ? 'View the task window, take over or hand back. Viewing and input permissions are separate.' : 'Desktop controls: lease take-over / hand-back and status; this is not an embedded desktop picture.')}</p>
      {canView ? <DesktopReadonlyView key={`${props.projectId}:${taskId}:${props.authToken}`} projectId={props.projectId!}
        taskId={String(taskId)} authToken={props.authToken!} zh={zh} /> : (
        <p className="shell-card-text" role="note" data-testid="shell-execution-desktop-live-blocked">{zh
          ? '画面未开启。登录负责人账号并打开含桌面步骤的工单后，可主动查看指定窗口。'
          : 'Viewing is off. Sign in as owner and open a desktop task to opt in.'}</p>
      )}
      {!hasTask && (
        <p className="shell-card-text" role="note">{zh
          ? '没有正在看的工单。打开一个带桌面步骤的工单后，可在这里接管或交还。'
          : 'No open work order. Open one with a desktop step to take over or hand back here.'}</p>
      )}
      {hasTask && (
        <>
          <p className="shell-card-text" role="status" data-testid="shell-execution-desktop-state">
            {zh ? '桌面租约：' : 'Desktop lease: '}
            <span data-desktop-state={state}>{stateLabel}</span>
            {status?.controller ? ` · ${status.controller}` : ''}
            {status?.fresh_look_required ? (zh ? ' · 需要新画面' : ' · fresh look required') : ''}
          </p>
          <div className="shell-execution-desktop-projection" data-testid="shell-execution-desktop-projection">
            <p className="shell-card-text" data-testid="shell-execution-desktop-window">
              {zh ? '窗口：' : 'Window: '}
              <span>{status?.window_title || (zh ? '（尚未观察）' : '(not observed yet)')}</span>
              {status?.app ? ` · ${status.app}` : ''}
            </p>
            <p className="shell-card-text" data-testid="shell-execution-desktop-frame">
              {zh ? '画面：' : 'Frame: '}
              <span data-frame-delivered={status?.frame_delivered ? 'yes' : 'no'}>
                {status?.frame_delivered
                  ? (zh ? `已投递${status.screen_hash ? ` · ${status.screen_hash}` : ''}` : `delivered${status.screen_hash ? ` · ${status.screen_hash}` : ''}`)
                  : (zh ? '未投递（无缩略图，需主人帧披露）' : 'not delivered (no thumbnail; needs owner frame disclosure)')}
              </span>
            </p>
            {status?.last_refusal ? (
              <p className="shell-card-text" role="alert" data-testid="shell-execution-desktop-refusal">
                {zh ? '最近拒绝：' : 'Last refusal: '}{status.last_refusal}
              </p>
            ) : null}
          </div>
          <div className="shell-execution-desktop-actions" data-testid="shell-execution-desktop-actions">
            <button
              type="button"
              className="shell-btn"
              data-testid="shell-execution-desktop-takeover"
              disabled={!canControl || busy || humanOperating}
              onClick={onTakeover}
            >{zh ? '接管' : 'Take over'}</button>
            <button
              type="button"
              className="shell-btn"
              data-testid="shell-execution-desktop-handback"
              disabled={!canControl || busy || !humanOperating}
              onClick={onHandBack}
            >{zh ? '完成并交还' : 'Done and hand back'}</button>
            <button
              type="button"
              className="shell-btn"
              data-testid="shell-execution-desktop-refresh"
              disabled={!canControl || busy}
              onClick={onRefresh}
            >{zh ? '刷新状态' : 'Refresh'}</button>
          </div>
        </>
      )}
      {error && <p className="shell-card-text" role="alert" data-testid="shell-execution-desktop-error">{error}</p>}
    </div>
  )
}
