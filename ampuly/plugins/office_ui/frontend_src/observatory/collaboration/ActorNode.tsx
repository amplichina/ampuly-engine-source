import { Handle, Position } from '@xyflow/react'

import type { ActorNodeData } from './layout'
import { actorStateLabel, type CollaborationActor } from './model'

function initials(actor: CollaborationActor): string {
  const cleaned = actor.name.replace(/[（(].*$/, '').trim()
  if (actor.kind === 'human' && !cleaned) return '人'
  return [...cleaned].slice(0, 2).join('') || '—'
}

function kindLabel(actor: CollaborationActor): string {
  if (actor.vacant) return '未配置'
  if (actor.kind === 'human') return '人类'
  if (actor.kind === 'supervised_ai') return '受监督 AI'
  return 'AI'
}

export function ActorNode({ data }: { data: ActorNodeData }) {
  const actor = data.actor
  return (
    <div
      className={`collab-node tone-${actor.tone} kind-${actor.kind}${data.selected ? ' is-selected' : ''}${actor.vacant ? ' is-vacant' : ''}${actor.id === 'owner' ? ' is-owner' : ''}`}
      data-actor-id={actor.id}
      aria-label={`${actor.name}，${actor.roleName}，${actor.stateLabel}`}
    >
      <Handle type="target" position={Position.Top} className="collab-handle" />
      <div className="collab-node-head">
        <span className="collab-node-avatar">{initials(actor)}</span>
        <div className="collab-node-title">
          <b>{actor.name}</b>
          <small>{actor.roleName}{actor.vacant ? ' · 席位空缺' : ''}</small>
        </div>
        <span className={`collab-kind kind-${actor.kind}`}>{kindLabel(actor)}</span>
      </div>

      <div className="collab-node-state">
        <i className={`collab-dot ${actor.state}`} aria-hidden="true" />
        <span>{actor.vacant ? '未配置' : actor.stateLabel}</span>
        {actor.activeWorkCount > 0 && <em>{actor.activeWorkCount} 项在办</em>}
        {actor.waitingCount > 0 && <em className="is-waiting">{actor.waitingCount} 等待</em>}
        {actor.blockedCount > 0 && <em className="is-blocked">{actor.blockedCount} 阻塞</em>}
      </div>

      <div className="collab-node-work">
        {actor.workItems[0] ? (
          <div className="collab-node-work-row" title={actor.workItems[0].title}>
            <i className={`collab-dot ${actor.workItems[0].state}`} aria-hidden="true" />
            <span>{actor.workItems[0].title}</span>
            <em>{actorStateLabel(actor.workItems[0].state)}</em>
          </div>
        ) : (
          <div className="collab-node-work-empty">{actor.vacant ? '还没有员工' : '暂无在办工作'}</div>
        )}
        {actor.workItems.length > 1 && <span className="collab-node-more">+{actor.workItems.length - 1} 项</span>}
      </div>

      {(actor.currentTool || actor.currentTaskId) && (
        <div className="collab-node-foot" title={actor.currentTool || actor.currentTaskId}>
          {actor.currentTool ? `工具 · ${actor.currentTool}` : `任务 · ${actor.currentTaskId}`}
        </div>
      )}
      <Handle type="source" position={Position.Bottom} className="collab-handle" />
    </div>
  )
}
