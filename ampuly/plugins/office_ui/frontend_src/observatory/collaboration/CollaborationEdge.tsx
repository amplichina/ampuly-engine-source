import { BaseEdge, EdgeLabelRenderer, getSmoothStepPath, type EdgeProps } from '@xyflow/react'

import type { CollaborationEdgeData } from './layout'

export function CollaborationEdge(props: EdgeProps) {
  const data = props.data as CollaborationEdgeData | undefined
  const kind = data?.kind ?? 'reports'
  const [path, labelX, labelY] = getSmoothStepPath({
    sourceX: props.sourceX,
    sourceY: props.sourceY,
    targetX: props.targetX,
    targetY: props.targetY,
    sourcePosition: props.sourcePosition,
    targetPosition: props.targetPosition,
    borderRadius: 14,
  })
  return (
    <>
      <BaseEdge id={props.id} path={path} className={`collab-edge edge-${kind}`} />
      {data?.label && kind !== 'reports' && (
        <EdgeLabelRenderer>
          <div
            className={`collab-edge-label edge-${kind}`}
            style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
          >
            {data.label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  )
}
