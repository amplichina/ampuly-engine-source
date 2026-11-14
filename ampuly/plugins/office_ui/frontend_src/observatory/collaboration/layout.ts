import type { Edge, Node } from '@xyflow/react'

import { computeDagreLayout } from '../../org/dagreLayout'
import type { CollaborationActor, CollaborationLink } from './model'
import type { CollaborationFold } from './relations'

export const ACTOR_NODE_WIDTH = 152
export const ACTOR_NODE_HEIGHT = 106

export interface ActorNodeData extends Record<string, unknown> {
  actor: CollaborationActor
  selected: boolean
}

export interface FoldNodeData extends Record<string, unknown> {
  fold: CollaborationFold
}

export interface CollaborationEdgeData extends Record<string, unknown> {
  kind: CollaborationLink['kind']
  label: string
}

export function layoutCollaboration(
  actors: readonly CollaborationActor[],
  links: readonly CollaborationLink[],
  folds: readonly CollaborationFold[] = [],
): { nodes: Node<ActorNodeData | FoldNodeData>[]; edges: Edge<CollaborationEdgeData>[] } {
  const nodes: Node<ActorNodeData | FoldNodeData>[] = actors.map(actor => ({
    id: actor.id,
    type: 'actor',
    position: { x: 0, y: 0 },
    data: { actor, selected: false },
    draggable: false,
    selectable: true,
  }))
  for (const fold of folds) {
    nodes.push({
      id: fold.id,
      type: 'fold',
      position: { x: 0, y: 0 },
      data: { fold },
      draggable: false,
      selectable: true,
    })
  }
  const edges: Edge<CollaborationEdgeData>[] = links.map(link => ({
    id: link.id,
    source: link.source,
    target: link.target,
    type: 'collab',
    data: { kind: link.kind, label: link.label },
    selectable: false,
  }))
  const laidOut = computeDagreLayout(nodes as Node[], edges as Edge[], {
    nodeWidth: ACTOR_NODE_WIDTH,
    nodeHeight: ACTOR_NODE_HEIGHT,
    rankdir: 'TB',
    nodesep: 18,
    ranksep: 36,
  }) as Node<ActorNodeData | FoldNodeData>[]
  return { nodes: laidOut, edges }
}
