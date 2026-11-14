import type { CSSProperties } from 'react'
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react'
import type { KanbanPhase } from '../types/kanban'

/** Visual family a phase belongs to; drives node colour and the minimap. */
export type RunGraphTone = 'pending' | 'run' | 'wait' | 'review' | 'attention' | 'ok' | 'bad'

export function toneForPhase(phase: KanbanPhase | string): RunGraphTone {
  switch (phase) {
    case 'approved':
      return 'ok'
    case 'failed':
    case 'cancelled':
      return 'bad'
    case 'running':
      return 'run'
    case 'awaiting_manager_review':
    case 'awaiting_human':
      return 'review'
    case 'needs_attention':
      return 'attention'
    case 'waiting_dependencies':
    case 'waiting_for_peer':
    case 'waiting_for_children':
    case 'paused':
      return 'wait'
    default:
      return 'pending'
  }
}

export interface RunGraphRowData {
  label: string
  value: string
  tone?: RunGraphTone
  /** Builder canvas only: the node slot this row shows, so a component can be dropped onto it. */
  slot?: string
  /** Hover text when the shown value is a shortened summary; defaults to the value itself. */
  title?: string
}

export interface RunGraphNodeData extends Record<string, unknown> {
  title: string
  kindLabel: string
  roleName: string
  roleColor: string
  phase: KanbanPhase
  tone: RunGraphTone
  rows: RunGraphRowData[]
  /** Checkpoints waiting on a person in this item's session. */
  pendingCount: number
  /** True while the session behind this item is actually executing. */
  live: boolean
  /** False for queued items that have no runtime task yet — nothing to open. */
  openable: boolean
  selected: boolean
}

export type RunGraphNodeType = Node<RunGraphNodeData, 'runNode'>

/** Height the layout reserves for a card with this many rows. */
export const NODE_HEADER_HEIGHT = 34
export const NODE_ROW_HEIGHT = 22
export const NODE_PADDING = 10

export function nodeHeightForRows(rowCount: number): number {
  return NODE_HEADER_HEIGHT + NODE_PADDING * 2 + rowCount * NODE_ROW_HEIGHT
}

export function RunGraphNode({ data }: NodeProps<RunGraphNodeType>) {
  const classes = ['rg-node', `rg-tone-${data.tone}`]
  if (data.selected) classes.push('is-selected')
  if (!data.openable) classes.push('is-queued')
  if (data.live) classes.push('is-live')
  return (
    <div className={classes.join(' ')} style={{ '--role': data.roleColor } as CSSProperties}>
      <Handle type="target" position={Position.Left} className="rg-port rg-port-in" />
      <div className="rg-node-header">
        <span className="rg-node-kind">{data.kindLabel}</span>
        <span className="rg-node-title" title={data.title}>{data.title}</span>
        {data.live && <span className="rg-node-live-dot" aria-hidden="true" />}
      </div>
      <div className="rg-node-body">
        {data.rows.map(row => (
          <div className="rg-row" key={row.label} data-slot={row.slot}>
            <span className="rg-row-label">{row.label}</span>
            <span className={`rg-row-value${row.tone ? ` rg-tone-${row.tone}` : ''}`} title={row.title ?? row.value}>
              {row.value}
            </span>
          </div>
        ))}
      </div>
      {data.pendingCount > 0 && (
        <span className="rg-node-badge" title={String(data.pendingCount)}>
          {data.pendingCount}
        </span>
      )}
      <Handle type="source" position={Position.Right} className="rg-port rg-port-out" />
    </div>
  )
}

export interface RunGraphGroupData extends Record<string, unknown> {
  name: string
  color: string
  count: number
}

export type RunGraphGroupType = Node<RunGraphGroupData, 'runGroup'>

export function RunGraphGroup({ data }: NodeProps<RunGraphGroupType>) {
  return (
    <div className="rg-group" style={{ '--role': data.color } as CSSProperties}>
      <div className="rg-group-title">
        <span className="rg-group-name">{data.name}</span>
        <span className="rg-group-count">{data.count}</span>
      </div>
    </div>
  )
}
