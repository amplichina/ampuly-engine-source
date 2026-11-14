import type { TraceEvent } from '../traceModel'
import { actorStateLabel, type CollaborationActor, type CollaborationLink } from './model'

function time(value: number | undefined): string {
  if (!value || !Number.isFinite(value)) return '—'
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value))
}

function kindLabel(actor: CollaborationActor): string {
  if (actor.kind === 'human') return '人类员工'
  if (actor.kind === 'supervised_ai') return '受监督 AI'
  return 'AI 员工'
}

export function Inspector({
  actor, actors, links, events, onOpenTask, onOpenTrace, onSelectActor,
}: {
  actor?: CollaborationActor
  actors: readonly CollaborationActor[]
  links: readonly CollaborationLink[]
  events: readonly TraceEvent[]
  onOpenTask?: (id: string) => void
  onOpenTrace?: () => void
  onSelectActor?: (id: string) => void
}) {
  if (!actor) {
    return (
      <section className="collab-inspector" aria-label="员工详情">
        <div className="collab-empty-inspector">
          <span aria-hidden="true">◎</span>
          <h3>选择一个人</h3>
          <p>点左侧名册或组织图里的员工，查看当前工作、协作关系和最近执行。</p>
          <dl>
            <div><dt>组织成员</dt><dd>{actors.filter(row => row.id !== 'owner').length}</dd></div>
            <div><dt>有实时状态</dt><dd>{actors.filter(row => row.state !== 'unknown' && !row.vacant).length}</dd></div>
            <div><dt>协作关系</dt><dd>{links.length}</dd></div>
          </dl>
        </div>
      </section>
    )
  }

  const manager = actor.managerId ? actors.find(row => row.id === actor.managerId) : undefined
  const relations = links
    .filter(link => link.source === actor.id || link.target === actor.id)
    .map(link => {
      const otherId = link.source === actor.id ? link.target : link.source
      const other = actors.find(row => row.id === otherId)
      return other ? { id: link.id, other, kind: link.kind, label: link.label } : null
    })
    .filter((row): row is { id: string; other: CollaborationActor; kind: CollaborationLink['kind']; label: string } => row !== null)
    .slice(0, 6)
  const actorIds = new Set([actor.id, actor.agentId, actor.employeeId].filter(Boolean) as string[])
  const recent = events
    .filter(event => actorIds.has(event.actorId) || (actor.currentTaskId ? event.taskId === actor.currentTaskId : false))
    .slice(-6)
    .reverse()
  const taskId = actor.currentTaskId || actor.workItems.find(item => item.taskId)?.taskId

  return (
    <section className="collab-inspector" aria-label={`${actor.name} 的详情`}>
      <div className="collab-inspector-kicker">SELECTED / 当前选中</div>
      <header className="collab-inspector-person">
        <span className={`collab-inspector-avatar kind-${actor.kind}`}>{actor.name.slice(0, 1)}</span>
        <div>
          <b>{actor.name} <span className={`collab-kind kind-${actor.kind}`}>{kindLabel(actor)}</span></b>
          <small>{actor.roleName}{manager ? ` · 向 ${manager.name} 汇报` : ''}</small>
        </div>
      </header>
      <div className={`collab-inspector-state state-${actor.state}`}>
        <i className={`collab-dot ${actor.state}`} aria-hidden="true" />
        {actor.vacant ? '席位空缺' : actor.stateLabel}
        {actor.currentTool && <span>工具 · {actor.currentTool}</span>}
        {actor.lastActive && <span>最近 {time(actor.lastActive)}</span>}
      </div>

      <div className="collab-inspector-card">
        <span className="collab-inspector-label">当前工作</span>
        {actor.workItems.length ? (
          <ul className="collab-inspector-work">
            {actor.workItems.slice(0, 5).map(item => (
              <li key={item.id}>
                <i className={`collab-dot ${item.state}`} aria-hidden="true" />
                <span title={item.title}>{item.title}</span>
                <em>{actorStateLabel(item.state)}</em>
              </li>
            ))}
          </ul>
        ) : (
          <p>{actor.currentWorkTitle || '暂无在办工作；没有实时任务时不补造状态。'}</p>
        )}
        <div className="collab-inspector-card-foot">
          <span>{actor.activeWorkCount} 在办 · {actor.waitingCount} 等待 · {actor.blockedCount} 阻塞</span>
          {taskId && onOpenTask && <button type="button" onClick={() => onOpenTask(taskId)}>打开任务 ↗</button>}
        </div>
      </div>

      <div className="collab-inspector-section">
        <h4>协作关系 <span>{relations.length}</span></h4>
        {relations.length ? relations.map(row => (
          <button type="button" className="collab-relation" key={row.id} onClick={() => onSelectActor?.(row.other.id)}>
            <span className={`collab-mini-avatar kind-${row.other.kind}`}>{row.other.name.slice(0, 1)}</span>
            <b>{row.other.name}</b>
            <span className="collab-relation-line" aria-hidden="true" />
            <span>{row.label}</span>
          </button>
        )) : <p className="collab-inspector-muted">尚未观测到汇报或协作关系。</p>}
      </div>

      <div className="collab-inspector-section">
        <h4>最近执行 <span>{recent.length}</span></h4>
        {recent.length ? recent.map(event => (
          <div className="collab-event" key={event.id}>
            <time>{time(event.time)}</time>
            <div><b>{event.title}</b><span>{event.summary || '未提供公开摘要'}</span></div>
          </div>
        )) : <p className="collab-inspector-muted">还没有属于这个人的已采集事件。</p>}
        <button type="button" className="collab-text-button" onClick={onOpenTrace}>查看全部执行记录 →</button>
      </div>
    </section>
  )
}
