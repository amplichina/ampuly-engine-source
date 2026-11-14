import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { Background, Controls, ReactFlow, type ReactFlowInstance } from '@xyflow/react'

import type { AgentInfo, OrgInfoPayload, SocketStatus } from '../../types/visual'
import type { ExecutionTraceStore } from '../ExecutionTraceStore'
import type { TracePhase } from '../traceModel'
import { ActorNode } from './ActorNode'
import { CollaborationEdge } from './CollaborationEdge'
import { DutyBoard } from './DutyBoard'
import { FoldNode } from './FoldNode'
import { Inspector } from './Inspector'
import { layoutCollaboration } from './layout'
import {
  buildCollaborationView,
  type CollaborationActor,
  type CollaborationOrder,
  type DutyCard,
} from './model'
import { buildRelationGraph, foldInactiveBranches, tracePulses } from './relations'
import { RelationsStage } from './RelationsStage'

const NODE_TYPES = { actor: ActorNode, fold: FoldNode }
const EDGE_TYPES = { collab: CollaborationEdge }

const PHASE_LABEL: Record<TracePhase, string> = {
  idle: '等待', thinking: '分析', searching: '检索', executing: '执行', collaborating: '协作',
  review: '待审', verifying: '核验', completed: '完成', failed: '失败', cancelled: '取消', unknown: '状态未知',
}

type Lens = 'org' | 'project' | 'business'
type GraphMode = 'tree' | 'relations'
const GRAPH_MODE_LABEL: Record<GraphMode, string> = { tree: '组织树', relations: '关系' }
type RailFilter = 'all' | 'ai' | 'human' | 'active'

function defaultActorId(actors: readonly CollaborationActor[], taskId?: string): string {
  const onTask = taskId ? actors.find(actor => actor.id !== 'owner' && actor.currentTaskId === taskId) : undefined
  if (onTask) return onTask.id
  const manager = actors.find(actor => actor.id !== 'owner' && actor.managerId === 'owner' && !actor.vacant && actor.kind !== 'human')
  return manager?.id || actors.find(actor => actor.id === 'owner')?.id || actors[0]?.id || ''
}

function timeText(value: number | undefined): string {
  if (!value || !Number.isFinite(value)) return '—'
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value))
}

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false)
  useEffect(() => {
    const media = window.matchMedia?.('(prefers-reduced-motion: reduce)')
    if (!media) return
    setReduced(media.matches)
    const update = () => setReduced(media.matches)
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])
  return reduced
}

export interface CollaborationPageProps {
  store: ExecutionTraceStore
  tasks: readonly { id: string; title: string; status?: string }[]
  orders?: readonly CollaborationOrder[]
  org?: OrgInfoPayload | null
  agents?: readonly AgentInfo[]
  projectName?: string
  taskId?: string
  onClose: () => void
  onOpenTask?: (id: string) => void
  onLoadHistory?: (orderId: string) => void
  historyLoading?: boolean
  historyError?: string | null
  historyOrderId?: string
  onTaskChange?: (id: string) => void
  connection?: SocketStatus
  /** Test / deep-link: start in 关系 so the stage fills the atlas body. */
  initialGraphMode?: GraphMode
}

export function CollaborationPage({
  store, tasks, orders = [], org, agents = [], projectName, taskId,
  onClose, onOpenTask, onLoadHistory, historyLoading = false, historyError, historyOrderId, onTaskChange,
  initialGraphMode = 'tree',
}: CollaborationPageProps) {
  const subscribe = useCallback((listener: () => void) => store.subscribe(listener), [store])
  const getSnapshot = useCallback(() => store.getSnapshot(), [store])
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  const reducedMotion = useReducedMotion()
  const dialogRef = useRef<HTMLDivElement>(null)
  const [flow, setFlow] = useState<ReactFlowInstance | null>(null)
  const userSelected = useRef(false)

  const view = useMemo(() => buildCollaborationView({
    org: org ?? null,
    agents,
    tasks: tasks.map(task => ({ id: task.id, title: task.title, status: task.status || '未提供' })),
    orders,
    traceEvents: snapshot.events,
  }), [org, agents, tasks, orders, snapshot.events])

  const [selectedId, setSelectedId] = useState('')
  const [lens, setLens] = useState<Lens>('org')
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<RailFilter>('all')
  const [traceOpen, setTraceOpen] = useState(false)
  const [traceTask, setTraceTask] = useState(taskId || '')
  const [tracePhase, setTracePhase] = useState<TracePhase | 'all'>('all')
  const [traceQuery, setTraceQuery] = useState('')
  const [traceEventId, setTraceEventId] = useState('')
  const [traceCursor, setTraceCursor] = useState<number | null>(null)
  const [tracePlaying, setTracePlaying] = useState(false)
  const [orderId, setOrderId] = useState(historyOrderId || '')
  const [graphMode, setGraphMode] = useState<GraphMode>(initialGraphMode)
  const [expandedFolds, setExpandedFolds] = useState<ReadonlySet<string>>(() => new Set())
  const expandFold = useCallback((managerId: string) => {
    setExpandedFolds(previous => new Set(previous).add(managerId))
  }, [])
  const selectActor = useCallback((id: string) => { userSelected.current = true; setSelectedId(id) }, [])

  const selected = view.actors.find(actor => actor.id === selectedId)
  const connected = snapshot.connection === 'connected'
  const folded = useMemo(() => foldInactiveBranches(view.actors, view.links, {
    expanded: expandedFolds,
    keepVisible: new Set(selectedId ? [selectedId] : []),
  }), [view.actors, view.links, expandedFolds, selectedId])
  const graph = useMemo(() => layoutCollaboration(folded.actors, folded.links, folded.folds), [folded])
  const nodes = useMemo(() => graph.nodes.map(node => (node.type === 'actor'
    ? { ...node, data: { ...node.data, selected: node.id === selectedId } }
    : node)), [graph.nodes, selectedId])
  const relationData = useMemo(
    () => (graphMode !== 'tree' ? buildRelationGraph(folded.actors, folded.links, folded.folds) : null),
    [graphMode, folded],
  )
  const pulses = useMemo(
    () => (graphMode !== 'tree' ? tracePulses(view.actors, snapshot.events) : []),
    [graphMode, view.actors, snapshot.events],
  )
  const taskTitles = useMemo(
    () => Object.fromEntries(tasks.map(task => [task.id, task.title || task.id])),
    [tasks],
  )
  const foldedPeople = folded.folds.reduce((total, fold) => total + fold.count, 0)
  const openGraphNode = useCallback((id: string) => {
    if (id.startsWith('fold:')) expandFold(id.slice('fold:'.length))
    else selectActor(id)
  }, [expandFold, selectActor])

  const railActors = useMemo(() => {
    const query = search.trim().toLocaleLowerCase()
    return view.actors.filter(actor => {
      if (filter === 'ai' && actor.kind === 'human') return false
      if (filter === 'human' && actor.kind !== 'human') return false
      if (filter === 'active' && actor.state !== 'working' && actor.state !== 'waiting' && actor.state !== 'blocked') return false
      if (!query) return true
      return `${actor.name} ${actor.roleName} ${actor.roleId}`.toLocaleLowerCase().includes(query)
    })
  }, [view.actors, search, filter])

  const filteredTrace = useMemo(() => {
    const query = traceQuery.trim().toLocaleLowerCase()
    return snapshot.events.filter(event => (!traceTask || event.taskId === traceTask)
      && (tracePhase === 'all' || event.phase === tracePhase)
      && (!query || `${event.title} ${event.summary ?? ''} ${event.actorId} ${event.type}`.toLocaleLowerCase().includes(query)))
  }, [snapshot.events, traceTask, tracePhase, traceQuery])
  const visibleTraceCount = traceCursor === null ? filteredTrace.length : Math.min(traceCursor + 1, filteredTrace.length)
  const traceEvents = useMemo(
    () => filteredTrace.slice(Math.max(0, visibleTraceCount - 160), visibleTraceCount).reverse(),
    [filteredTrace, visibleTraceCount],
  )
  const traceEvent = traceEvents.find(event => event.id === traceEventId) ?? traceEvents[0]
  const workItemCount = view.actors.reduce((total, actor) => total + actor.workItems.length, 0)
  const attention = view.attention.slice(0, 4)

  useEffect(() => {
    setSelectedId(current => {
      if (current && view.actors.some(actor => actor.id === current) && (userSelected.current || current !== 'owner')) return current
      return defaultActorId(view.actors, taskId)
    })
  }, [view.actors, taskId])

  useEffect(() => {
    if (!flow || !nodes.length) return
    const frame = requestAnimationFrame(() => flow.fitView({ padding: 0.06, minZoom: 0.25, maxZoom: 1.05 }))
    return () => cancelAnimationFrame(frame)
  }, [flow, nodes.length, lens, graphMode])

  useEffect(() => {
    if (taskId !== undefined) setTraceTask(taskId)
  }, [taskId])

  useEffect(() => {
    setTraceCursor(null); setTracePlaying(false); setTraceEventId('')
  }, [traceTask, tracePhase, traceQuery])

  useEffect(() => {
    if (!tracePlaying || filteredTrace.length < 2) return
    const timer = window.setInterval(() => {
      setTraceCursor(previous => {
        const next = (previous ?? -1) + 1
        if (next >= filteredTrace.length - 1) { setTracePlaying(false); return filteredTrace.length - 1 }
        return next
      })
    }, 800)
    return () => window.clearInterval(timer)
  }, [tracePlaying, filteredTrace.length])

  useEffect(() => {
    if (historyOrderId !== undefined) setOrderId(historyOrderId)
  }, [historyOrderId])

  useEffect(() => {
    if (!traceOpen) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); setTraceOpen(false) }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [traceOpen])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !traceOpen) { event.preventDefault(); onClose() }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose, traceOpen])

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    dialogRef.current?.querySelector<HTMLElement>('[data-collab-close]')?.focus()
    return () => { if (previous?.isConnected) previous.focus() }
  }, [])

  const togglePlayback = () => {
    if (tracePlaying) { setTracePlaying(false); return }
    if (filteredTrace.length < 2) return
    if (traceCursor === null || traceCursor >= filteredTrace.length - 1) setTraceCursor(0)
    setTracePlaying(true)
  }
  const switchLive = () => { setTracePlaying(false); setTraceCursor(null) }

  const orgCards: readonly DutyCard[] = useMemo(() => [
    ...view.projectCards.slice(0, 2),
    ...view.businessCards.slice(0, 1),
  ], [view.projectCards, view.businessCards])

  const dutyCards = lens === 'project' ? view.projectCards : lens === 'business' ? view.businessCards : orgCards
  const dutyEmpty = lens === 'project' ? '还没有项目或会话' : lens === 'business' ? '还没有业务工单' : '还没有在办项目或业务工单'
  const dutyLabel = lens === 'project' ? '项目与任务' : lens === 'business' ? '业务工单' : '在办与业务'
  // 关系 mode: RelationsStage fills the atlas body (no rail / Inspector / duty chrome).
  const relationsStageFill = lens === 'org' && graphMode === 'relations'

  return (
    <div ref={dialogRef} className={`collab-atlas${reducedMotion ? ' is-reduced' : ''}${relationsStageFill ? ' is-relations-stage' : ''}`} role="dialog" aria-modal="true" aria-labelledby="collab-title">
      <header className="collab-header">
        <div className="collab-brand">
          <img className="collab-brand-on-light" src="./brand/ampuly-lockup.png" alt="安普里 Ampuly" />
          <img className="collab-brand-on-dark" src="./brand/ampuly-lockup-light.png" alt="安普里 Ampuly" />
          <span className="collab-brand-divider" aria-hidden="true">/</span>
          <div>
            <span className="collab-eyebrow">COLLABORATION ATLAS</span>
            <h1 id="collab-title">协作图谱</h1>
          </div>
        </div>

        <nav className="collab-lens" aria-label="协作视角">
          {(['org', 'project', 'business'] as const).map(value => (
            <button key={value} type="button" className={lens === value ? 'is-active' : ''}
              aria-pressed={lens === value} onClick={() => setLens(value)}>
              {value === 'org' ? '组织' : value === 'project' ? '项目' : '业务'}
            </button>
          ))}
        </nav>

        <div className="collab-header-right">
          <span className={`collab-connection ${connected ? 'is-connected' : ''}`} role="status">
            <i aria-hidden="true" />{connected ? '已连接' : snapshot.connection === 'connecting' ? '连接中' : '已断开'}
          </span>
          <button type="button" className="collab-header-button" onClick={() => setTraceOpen(true)}>执行记录</button>
          <button type="button" className="collab-header-button collab-close" data-collab-close onClick={onClose}>返回主应用 <kbd>Esc</kbd></button>
        </div>
      </header>

      <div className="collab-body">
        {!relationsStageFill && (
          <aside className="collab-rail" aria-label="组织名册">
            <div className="collab-rail-head"><b>组织</b><span>{view.actors.filter(actor => actor.id !== 'owner').length} 人</span></div>
            <label className="collab-search">
              <span className="collab-sr-only">搜索人或角色</span>
              <input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索人或角色" />
            </label>
            <div className="collab-filters" role="group" aria-label="筛选成员">
              {(['all', 'ai', 'human', 'active'] as const).map(value => (
                <button key={value} type="button" className={filter === value ? 'is-active' : ''}
                  aria-pressed={filter === value} onClick={() => setFilter(value)}>
                  {value === 'all' ? '全部' : value === 'ai' ? 'AI' : value === 'human' ? '人类' : '在办'}
                </button>
              ))}
            </div>
            <div className="collab-rail-list">
              {railActors.map(actor => (
                <button key={actor.id} type="button" className={`collab-rail-row${actor.id === selectedId ? ' is-selected' : ''}`}
                  aria-pressed={actor.id === selectedId} onClick={() => selectActor(actor.id)}>
                  <span className={`collab-rail-avatar kind-${actor.kind}`}>{actor.name.slice(0, 1)}</span>
                  <span className="collab-rail-text"><b>{actor.name}</b><small>{actor.roleName}</small></span>
                  <i className={`collab-dot ${actor.state}`} title={actor.stateLabel} aria-label={actor.stateLabel} />
                </button>
              ))}
              {!railActors.length && <p className="collab-rail-empty">没有符合筛选的人。</p>}
            </div>

            {attention.length > 0 && (
              <div className="collab-attention">
                <h2>等待 <span>{view.attention.length}</span></h2>
                {attention.map(item => (
                  <button key={item.id} type="button" onClick={() => {
                    if (item.actorId) selectActor(item.actorId)
                    if (item.taskId && onOpenTask) onOpenTask(item.taskId)
                  }}>
                    <i className="collab-dot waiting" aria-hidden="true" />
                    <span><b>{item.label}</b><small>{item.detail}</small></span>
                  </button>
                ))}
              </div>
            )}
            <p className="collab-rail-note">状态来自 org_info / runtime / 已采集事件；没有数据时不补造。</p>
          </aside>
        )}

        <main className="collab-main">
          {!relationsStageFill && (
            <div className="collab-main-head">
              <div>
                <span className="collab-eyebrow">{lens === 'org' ? 'ORGANIZATION / 组织' : lens === 'project' ? 'PROJECTS / 项目' : 'BUSINESS / 业务'}</span>
                <h2>{projectName || '当前项目'}</h2>
              </div>
              <div className="collab-kpis" aria-label="协作概览">
                <span><b>{view.kpis.available}/{view.kpis.total}</b> 在岗</span>
                <span><b>{view.kpis.working}</b> 协作中</span>
                <span><b>{view.kpis.waiting}</b> 等待</span>
                <span><b>{workItemCount}</b> 工作项</span>
                <span><b>{view.kpis.orders}</b> 业务</span>
              </div>
            </div>
          )}

          {lens === 'org' ? (
            <>
              <div className="collab-graph" aria-label="组织协作图">
                <div className="collab-graph-toolbar">
                  <div className="collab-graph-mode" role="group" aria-label="图谱视图">
                    {(['tree', 'relations'] as const).map(value => (
                      <button key={value} type="button" className={graphMode === value ? 'is-active' : ''}
                        aria-pressed={graphMode === value} onClick={() => setGraphMode(value)}>
                        {GRAPH_MODE_LABEL[value]}
                      </button>
                    ))}
                  </div>
                  {relationData && relationData.activeLinks > 0 && (
                    <span className="collab-graph-chip is-flow">{relationData.activeLinks} 条关系正在协作</span>
                  )}
                  {foldedPeople > 0 && (
                    <span className="collab-graph-chip" role="status">已折叠 {foldedPeople} 位未在办成员 · 点击折叠卡展开</span>
                  )}
                  {view.truncatedLinks > 0 && (
                    <span className="collab-graph-chip is-warn" role="status">还有 {view.truncatedLinks} 条关系未显示</span>
                  )}
                </div>
                {graphMode === 'relations' && relationData ? (
                  <RelationsStage data={relationData} pulses={pulses} taskTitles={taskTitles} selectedId={selectedId}
                    reducedMotion={reducedMotion} onSelect={selectActor} onExpandFold={expandFold} />
                ) : (
                  <ReactFlow
                    nodes={nodes}
                    edges={graph.edges}
                    nodeTypes={NODE_TYPES}
                    edgeTypes={EDGE_TYPES}
                    onInit={setFlow}
                    onNodeClick={(_, node) => openGraphNode(node.id)}
                    fitView
                    fitViewOptions={{ padding: 0.06, minZoom: 0.25, maxZoom: 1.05 }}
                    nodesDraggable={false}
                    nodesConnectable={false}
                    elementsSelectable={false}
                    minZoom={0.2}
                    maxZoom={1.4}
                  >
                    <Background gap={24} size={1} color="var(--border)" />
                    <Controls showInteractive={false} position="top-right" />
                  </ReactFlow>
                )}
                {!view.actors.some(actor => actor.id !== 'owner') && (
                  <div className="collab-graph-empty">
                    <h3>还没有组织成员</h3>
                    <p>去「组织与团队」创建 AI 员工或人类员工后，这里会自动出现。</p>
                  </div>
                )}
              </div>
              {!relationsStageFill && (
                <section className="collab-duty" aria-label="在办与业务">
                  <header><h3>{dutyLabel}</h3><span>{dutyCards.length} 条</span></header>
                  <DutyBoard cards={dutyCards} emptyText={dutyEmpty} onSelectActor={selectActor} onOpenTask={onOpenTask} />
                </section>
              )}
            </>
          ) : (
            <section className="collab-duty is-full" aria-label={dutyLabel}>
              <header><h3>{dutyLabel}</h3><span>{dutyCards.length} 条</span></header>
              <DutyBoard cards={dutyCards} emptyText={dutyEmpty} onSelectActor={selectActor} onOpenTask={onOpenTask} />
              <p className="collab-duty-hint">更多{lens === 'project' ? '会话/任务' : '业务工单'}会出现在这里；没有数据时不补造。</p>
            </section>
          )}
        </main>

        {!relationsStageFill && (
          <Inspector actor={selected} actors={view.actors} links={view.links} events={snapshot.events}
            onOpenTask={onOpenTask} onOpenTrace={() => setTraceOpen(true)} onSelectActor={selectActor} />
        )}
      </div>

      {traceOpen && (
        <div className="collab-drawer-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) setTraceOpen(false) }}>
          <aside className="collab-drawer" role="dialog" aria-modal="true" aria-label="执行记录">
            <header className="collab-drawer-head">
              <div><span className="collab-eyebrow">EVIDENCE / 已采集事件</span><h2>执行记录</h2></div>
              <button type="button" className="collab-header-button" onClick={() => setTraceOpen(false)}>关闭</button>
            </header>
            <div className="collab-drawer-filters">
              <label><span>任务</span><select value={traceTask} onChange={event => { setTraceTask(event.target.value); onTaskChange?.(event.target.value) }}>
                <option value="">全部任务</option>
                {tasks.map(task => <option key={task.id} value={task.id}>{task.title || task.id}</option>)}
              </select></label>
              <label><span>阶段</span><select value={tracePhase} onChange={event => setTracePhase(event.target.value as TracePhase | 'all')}>
                <option value="all">全部阶段</option>
                {Object.entries(PHASE_LABEL).map(([phase, label]) => <option key={phase} value={phase}>{label}</option>)}
              </select></label>
              <label className="collab-drawer-search"><span>搜索</span><input type="search" value={traceQuery} onChange={event => setTraceQuery(event.target.value)} placeholder="事件、摘要或执行者" /></label>
            </div>
            <div className="collab-drawer-playback">
              <button type="button" className="collab-play" onClick={togglePlayback} disabled={filteredTrace.length < 2} aria-label={tracePlaying ? '暂停回放' : '播放事件回放'}>{tracePlaying ? 'Ⅱ' : '▶'}</button>
              <button type="button" className={`collab-live${traceCursor === null ? ' is-live' : ''}`} onClick={switchLive} aria-pressed={traceCursor === null}>实时</button>
              <label>
                <span>{traceCursor === null ? '跟随最新事件' : `回看第 ${visibleTraceCount} 条`}</span>
                <input type="range" min={0} max={Math.max(0, filteredTrace.length - 1)} value={Math.max(0, traceCursor ?? filteredTrace.length - 1)}
                  disabled={!filteredTrace.length} onChange={event => { setTracePlaying(false); setTraceCursor(Number(event.target.value)) }}
                  aria-label="事件回放位置" aria-valuetext={filteredTrace.length ? `第 ${visibleTraceCount} 条，共 ${filteredTrace.length} 条` : '暂无事件'} />
              </label>
              <span className="collab-playback-count">{filteredTrace.length ? `${visibleTraceCount} / ${filteredTrace.length}` : '暂无事件'}</span>
            </div>
            <div className="collab-drawer-body">
              <ol className="collab-trace-list">
                {traceEvents.map(event => (
                  <li key={event.id}>
                    <button type="button" className={event.id === traceEvent?.id ? 'is-selected' : ''} onClick={() => setTraceEventId(event.id)}>
                      <time>{timeText(event.time)}</time>
                      <i className={`collab-dot ${event.phase === 'failed' ? 'blocked' : event.phase === 'review' ? 'waiting' : event.phase === 'completed' ? 'idle' : 'working'}`} aria-hidden="true" />
                      <span><b>{event.title}</b><small>{event.actorId} · {PHASE_LABEL[event.phase]}</small></span>
                    </button>
                  </li>
                ))}
                {!traceEvents.length && <li className="collab-trace-empty">还没有符合条件的事件。</li>}
              </ol>
              <div className="collab-trace-detail">
                {traceEvent ? (
                  <>
                    <time>{timeText(traceEvent.time)}</time>
                    <h3>{traceEvent.title}</h3>
                    <p>{traceEvent.summary || '未提供可公开摘要。'}</p>
                    <dl>
                      <div><dt>执行者</dt><dd>{traceEvent.actorId || '未提供'}</dd></div>
                      <div><dt>类型</dt><dd>{traceEvent.type || '未提供'}</dd></div>
                      <div><dt>任务</dt><dd><code>{traceEvent.taskId}</code></dd></div>
                    </dl>
                    {traceEvent.references.length > 0 && (
                      <ul className="collab-reference-list">
                        {traceEvent.references.map(reference => (
                          <li key={`${reference.kind}:${reference.id}`}><span>{reference.kind}</span><b>{reference.label}</b></li>
                        ))}
                      </ul>
                    )}
                    {onOpenTask && <button type="button" className="collab-primary" onClick={() => onOpenTask(traceEvent.taskId)}>打开任务 ↗</button>}
                  </>
                ) : <p className="collab-inspector-muted">选择一条事件查看已采集的说明与引用。</p>}
              </div>
            </div>
            <footer className="collab-drawer-history">
              <div><b>工单历史</b><span>只读取已留存事件，不补写未采集的过程。</span></div>
              <div className="collab-history-row">
                <select value={orderId} onChange={event => setOrderId(event.target.value)} disabled={historyLoading || !orders.length} aria-label="选择工单">
                  <option value="">{orders.length ? '选择工单' : '暂无可读取工单'}</option>
                  {orders.map(order => <option key={order.id} value={order.id}>{order.title || order.id}</option>)}
                </select>
                <button type="button" className="collab-header-button" disabled={!connected || !orderId || !onLoadHistory || historyLoading}
                  onClick={() => onLoadHistory?.(orderId)}>{historyLoading ? '正在读取…' : '读取历史'}</button>
              </div>
              {historyError && <p className="collab-history-error" role="alert">{historyError}</p>}
            </footer>
          </aside>
        </div>
      )}
    </div>
  )
}

export default CollaborationPage

