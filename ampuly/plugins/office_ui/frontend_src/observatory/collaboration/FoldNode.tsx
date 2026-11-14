import { Handle, Position } from '@xyflow/react'

import type { FoldNodeData } from './layout'

/** A folded group of people whose whole branch is currently idle (>40-person orgs). */
export function FoldNode({ data }: { data: FoldNodeData }) {
  const fold = data.fold
  return (
    <div className="collab-node collab-fold-node" data-fold-id={fold.id} role="button" tabIndex={0}
      aria-label={`${fold.label}，点击展开`} title="点击展开">
      <Handle type="target" position={Position.Top} className="collab-handle" />
      <div className="collab-fold-count">{fold.count}</div>
      <div className="collab-fold-text">
        <b>{fold.label}</b>
        <small>已折叠 · 点击展开</small>
      </div>
      <Handle type="source" position={Position.Bottom} className="collab-handle" />
    </div>
  )
}
