import type { ReactNode } from 'react'

/** Host-private read projection. Not a WebSocket DTO, permission or readiness assertion. */
export interface AIEmployeeProjection {
  connected: boolean
  state: 'not_connected' | 'loading' | 'ready' | 'error' | 'stale'
  source: 'server' | 'unconnected'
  requested_project_id: string | null
  observed_project_id: string | null
  requested_key: string
  observed_key: string | null
  /** The host's read generation, not an ordered revision. Opaque revisions are compared only for equality. */
  requested_token: string
  observed_token: string | null
  revision: string | null
  message?: string
}

export interface AIEmployeeProjectionBoundaryProps {
  projection: AIEmployeeProjection
  title: string
  children: ReactNode
  /** Fetch/reconcile only. This never retries a write or discards the host's unsaved draft. */
  onReadLatest?: (target: { project_id: string | null; key: string; preserve_draft: true }) => void
}

const nonempty = (value: string | null): boolean => typeof value === 'string' && value.trim().length > 0

export function aiEmployeeProjectionReason(value: AIEmployeeProjection): string {
  if (!value.connected) return '连接已断开，保留草稿；重新连接后先核实当前状态。'
  if (value.state === 'loading') return '正在读取员工信息。'
  if (value.state === 'not_connected' || value.source !== 'server') return '员工数据源尚未接入；不会用演示数据代替实际员工。'
  if (value.state === 'error') return value.message || '员工信息读取失败，可重新读取核实。'
  if (value.state === 'stale') return '员工信息已过期，先读取当前项目与版本。'
  if (value.state !== 'ready') return '员工读取状态未识别，先核实服务端结果。'
  if (value.requested_project_id !== value.observed_project_id) return '这份信息属于另一个项目，不展示或套用到当前项目。'
  if (value.requested_project_id !== null && !nonempty(value.requested_project_id)) return '当前项目标识尚未确定。'
  if (!nonempty(value.requested_key) || value.requested_key !== value.observed_key) return '这份信息不是当前员工、节点或运行的读取结果。'
  if (!nonempty(value.requested_token) || value.requested_token !== value.observed_token) return '收到旧读取结果，等待当前请求的结果。'
  if (!nonempty(value.revision)) return '当前数据修订尚未加载，暂不展示可操作入口。'
  return ''
}

export function aiEmployeeProjectionReadTarget(props: AIEmployeeProjectionBoundaryProps): { project_id: string | null; key: string; preserve_draft: true } | null {
  const value = props.projection
  if (!value.connected || value.state === 'loading' || !nonempty(value.requested_key) || !props.onReadLatest) return null
  if (value.requested_project_id !== null && !nonempty(value.requested_project_id)) return null
  return { project_id: value.requested_project_id, key: value.requested_key, preserve_draft: true }
}

export function AIEmployeeProjectionBoundary(props: AIEmployeeProjectionBoundaryProps) {
  const reason = aiEmployeeProjectionReason(props.projection)
  if (!reason) return <>{props.children}</>
  const target = aiEmployeeProjectionReadTarget(props)
  return <section className="ai-employee-projection" aria-label={props.title} data-state={props.projection.state}>
    <h3>{props.title}</h3>
    <p role={props.projection.state === 'error' ? 'alert' : 'status'}>{reason}</p>
    {props.projection.message && props.projection.state !== 'error' && <p>{props.projection.message}</p>}
    <button type="button" disabled={!target} data-action="read-current" onClick={() => {
      const current = aiEmployeeProjectionReadTarget(props)
      if (current) props.onReadLatest?.(current)
    }}>读取当前状态</button>
    <small>只核对状态，不重新派工，不覆盖未保存内容。</small>
  </section>
}
