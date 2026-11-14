import { actorStateLabel, type ActorKind, type ActorState, type DutyCard } from './model'

function toneOfStatus(status: string): ActorState {
  const value = status.toLowerCase()
  if (/block|fail|error/.test(value)) return 'blocked'
  if (/wait|review|pending|await|gate/.test(value)) return 'waiting'
  if (/run|active|doing|progress|work|execut/.test(value)) return 'working'
  if (/done|complet|success|accept/.test(value)) return 'idle'
  return 'unknown'
}

function kindLabel(kind: ActorKind): string {
  if (kind === 'human') return '人'
  if (kind === 'supervised_ai') return '受监督'
  return 'AI'
}

export function DutyBoard({
  cards, emptyText, onSelectActor, onOpenTask,
}: {
  cards: readonly DutyCard[]
  emptyText: string
  onSelectActor?: (id: string) => void
  onOpenTask?: (id: string) => void
}) {
  if (!cards.length) {
    return (
      <div className="collab-duty-empty">
        <span aria-hidden="true">◇</span>
        <h3>{emptyText}</h3>
        <p>没有数据时不补造项目、工单或负责人。</p>
      </div>
    )
  }
  return (
    <div className={`collab-duty-grid${cards.length === 1 ? ' is-single' : ''}`}>
      {cards.map(card => (
        <article className={`collab-duty-card is-${card.kind}`} key={card.id}>
          <header className="collab-duty-head">
            <span className={`collab-dot ${toneOfStatus(card.status)}`} aria-hidden="true" />
            <span className="collab-duty-kind">{card.kind === 'project' ? '项目' : '业务'}</span>
            <b>{card.title}</b>
            <span className={`collab-status-pill tone-${toneOfStatus(card.status)}`}>{card.status || '未提供'}</span>
          </header>
          <p className="collab-duty-sub">{card.subtitle}</p>
          {card.actors.length > 0 && (
            <div className="collab-duty-actors">
              {card.actors.slice(0, 5).map(actor => (
                <button key={actor.id} type="button" title={`${actor.name} · ${kindLabel(actor.kind)}`}
                  onClick={() => onSelectActor?.(actor.id)}>
                  <span className={`collab-mini-avatar kind-${actor.kind}`}>{actor.name.slice(0, 1)}</span>
                  <span>{actor.name}</span>
                </button>
              ))}
              {card.actors.length > 5 && <em>+{card.actors.length - 5}</em>}
            </div>
          )}
          <div className="collab-duty-items">
            {card.items.slice(0, 4).map(item => (
              <div className="collab-duty-item" key={item.id} title={`${item.title} · ${item.meta}`}>
                <i className={`collab-dot ${item.state}`} aria-hidden="true" />
                <b>{item.title}</b>
                <span>{item.meta || actorStateLabel(item.state)}</span>
              </div>
            ))}
            {!card.items.length && <div className="collab-duty-item is-empty">尚无已采集的在办项</div>}
          </div>
          <footer className="collab-duty-foot">
            <span>{card.metric}</span>
            {card.taskId && onOpenTask && (
              <button type="button" onClick={() => onOpenTask(card.taskId as string)}>打开任务 ↗</button>
            )}
          </footer>
        </article>
      ))}
    </div>
  )
}
