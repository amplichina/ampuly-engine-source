import { useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from 'react'

import type { ExecutionTraceStore } from './ExecutionTraceStore'
import { OrbCanvas } from './OrbCanvas'
import { buildTraceRun, type TraceEvent, type TraceRun } from './traceModel'

// `observatory.css` is imported by `main.tsx`, not here: the page's contract
// test runs under plain Node, which refuses to load a .css module (2026-10-18).

type Phase = TraceEvent['phase']
type Metrics = NonNullable<TraceEvent['metrics']>
type Reference = TraceEvent['references'][number]

export interface ReasoningObservatoryPageProps {
  store: ExecutionTraceStore
  taskId?: string
  tasks: readonly { id: string; title: string; status?: string }[]
  orders?: readonly { id: string; title: string; taskId?: string }[]
  onClose: () => void
  onOpenTask?: (id: string) => void
  onLoadHistory?: (orderId: string) => void
  historyLoading?: boolean
  historyError?: string | null
  historyOrderId?: string
  onTaskChange?: (id: string) => void
}

const PHASE_LABELS: Record<Phase, string> = {
  idle: '等待事件', thinking: '分析任务', searching: '检索资料', executing: '执行任务',
  collaborating: '协作中', review: '等待审核', verifying: '校验结果', completed: '已完成',
  failed: '执行失败', cancelled: '已取消', unknown: '状态未明确',
}
const METRIC_FIELDS = ['durationMs', 'inputTokens', 'outputTokens', 'totalTokens', 'costUsd'] as const

/** Metrics are observed snapshots, never additive. Replay must not borrow a future run total. */
export function buildObservatoryView(run: TraceRun | undefined, cursor: number | null) {
  const allEvents = run?.events ?? []
  const isLive = cursor === null
  const position = allEvents.length ? (isLive ? allEvents.length - 1 : Math.max(0, Math.min(cursor, allEvents.length - 1))) : -1
  const events = allEvents.slice(0, position + 1)
  const prefix = events.length ? buildTraceRun(events) : undefined
  const metrics: Partial<Metrics> = {}
  const observedMetrics = isLive ? { ...run?.metrics, ...prefix?.metrics } : prefix?.metrics
  for (const key of METRIC_FIELDS) {
    const value = observedMetrics?.[key]
    if (typeof value === 'number' && Number.isFinite(value) && value >= 0) metrics[key] = value
  }
  const actorIds = [...new Set([...(isLive ? run?.actorIds ?? [] : []), ...events.map(event => event.actorId).filter(Boolean)])]
  const references = new Map<string, Reference>()
  for (const event of events) for (const reference of event.references) references.set(`${reference.kind}:${reference.id}`, reference)
  return {
    events, metrics, actorIds, references: [...references.values()], isLive, position,
    phase: isLive ? run?.phase ?? prefix?.phase ?? 'idle' : prefix?.phase ?? 'idle',
  }
}

function phaseLabel(phase: Phase) { return PHASE_LABELS[phase] ?? PHASE_LABELS.unknown }
function timestamp(value: string | number | undefined, full = false) {
  if (value === undefined || value === '') return '时间未提供'
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return '时间未提供'
  return new Intl.DateTimeFormat('zh-CN', full
    ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }
    : { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(date)
}
function metric(value: number | undefined, kind: 'duration' | 'tokens' | 'cost') {
  if (value === undefined || !Number.isFinite(value)) return '未提供'
  if (kind === 'cost') return `$${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 6 })}`
  if (kind === 'tokens') return value.toLocaleString('zh-CN')
  if (value < 1000) return `${Math.round(value)} ms`
  if (value < 60_000) return `${(value / 1000).toFixed(1)} 秒`
  return `${Math.floor(value / 60_000)} 分 ${Math.round(value % 60_000 / 1000)} 秒`
}
function useReducedMotion() {
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

function ReferenceList({ references, onOpenTask }: { references: readonly Reference[]; onOpenTask?: (id: string) => void }) {
  if (!references.length) return <p className="obs-muted obs-empty-line">尚未采集到产物或来源引用。</p>
  return <ul className="obs-reference-list">{references.map(reference => <li key={`${reference.kind}:${reference.id}`}>
    <span className="obs-reference-kind">{reference.kind === 'artifact' ? '产物' : reference.kind === 'task' ? '任务' : '来源'}</span>
    <div><strong>{reference.label || reference.id}</strong><code title={reference.id}>{reference.id}</code></div>
    {reference.kind === 'task' && onOpenTask && <button type="button" className="obs-text-button" onClick={() => onOpenTask(reference.id)}>打开任务 ↗</button>}
  </li>)}</ul>
}

export function ReasoningObservatoryPage({ store, taskId, tasks, orders = [], onClose, onOpenTask, onLoadHistory,
  historyLoading = false, historyError, historyOrderId, onTaskChange }: ReasoningObservatoryPageProps) {
  const subscribe = useCallback((listener: () => void) => store.subscribe(listener), [store])
  const getSnapshot = useCallback(() => store.getSnapshot(), [store])
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  const [selectedTaskId, setSelectedTaskId] = useState(taskId ?? '')
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null)
  const [cursor, setCursor] = useState<number | null>(null)
  const [replayRun, setReplayRun] = useState<TraceRun | null>(null)
  const [playing, setPlaying] = useState(false)
  const [search, setSearch] = useState('')
  const [phaseFilter, setPhaseFilter] = useState<Phase | 'all'>('all')
  const [timelinePage, setTimelinePage] = useState(0)
  const [orderId, setOrderId] = useState(historyOrderId ?? '')
  const [staticMode, setStaticMode] = useState(false)
  const reducedMotion = useReducedMotion()
  const dialogRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  const titleId = useId()
  const descriptionId = useId()
  const searchId = useId()
  const phaseId = useId()
  const orderSelectId = useId()
  const cursorId = useId()
  const allTasks = useMemo(() => {
    const result = new Map(tasks.map(task => [task.id, task]))
    for (const run of snapshot.runs) if (!result.has(run.taskId)) result.set(run.taskId, { id: run.taskId, title: run.taskId })
    return [...result.values()]
  }, [tasks, snapshot.runs])
  const activeTaskId = selectedTaskId || taskId || allTasks[0]?.id || ''
  const runs = useMemo(() => snapshot.runs.filter(run => run.taskId === activeTaskId), [snapshot.runs, activeTaskId])
  const run = runs.find(item => item.id === selectedRunId) ?? runs[0]
  // Freeze this bounded window during replay so ring-buffer eviction cannot move the cursor into future events.
  const replayMatchesScope = replayRun?.events[0]?.projectId === snapshot.projectId
    && replayRun?.taskId === activeTaskId && (!run || replayRun.id === run.id)
  const displayedRun = cursor === null ? run : replayMatchesScope ? replayRun ?? run : run
  const view = useMemo(() => buildObservatoryView(displayedRun, cursor), [displayedRun, cursor])
  const selectedEvent = view.events.find(event => event.id === selectedEventId) ?? view.events.at(-1)
  const selectedTask = allTasks.find(task => task.id === activeTaskId)
  const connected = snapshot.connection === 'connected'
  const connectionLabel = connected ? '已连接' : snapshot.connection === 'connecting' ? '正在连接' : '连接已断开'
  const filteredEvents = useMemo(() => {
    const query = search.trim().toLocaleLowerCase()
    return view.events.filter(event => (phaseFilter === 'all' || event.phase === phaseFilter)
      && (!query || `${event.title} ${event.summary ?? ''} ${event.actorId} ${event.type}`.toLocaleLowerCase().includes(query)))
  }, [view.events, search, phaseFilter])
  const pageCount = Math.max(1, Math.ceil(filteredEvents.length / 100))
  const actualPage = Math.min(timelinePage, pageCount - 1)
  const timelineEvents = filteredEvents.slice().reverse().slice(actualPage * 100, (actualPage + 1) * 100)
  const sequence = view.events.slice(-12)

  useEffect(() => { if (taskId !== undefined) { setSelectedTaskId(taskId); setSelectedRunId(null) } }, [taskId])
  useEffect(() => { if (historyOrderId !== undefined) setOrderId(historyOrderId) }, [historyOrderId])
  useEffect(() => { setCursor(null); setReplayRun(null); setPlaying(false); setSelectedEventId(null); setTimelinePage(0) }, [run?.id, snapshot.projectId, store])
  useEffect(() => { setTimelinePage(0) }, [search, phaseFilter])
  useEffect(() => {
    if (!playing || !displayedRun || displayedRun.events.length < 2) return
    const timer = window.setInterval(() => setCursor(previous => Math.min((previous ?? -1) + 1, displayedRun.events.length - 1)), 800)
    return () => window.clearInterval(timer)
  }, [playing, displayedRun?.id, displayedRun?.events.length])
  useEffect(() => {
    if (playing && cursor !== null && cursor >= (displayedRun?.events.length ?? 0) - 1) setPlaying(false)
  }, [cursor, playing, displayedRun?.events.length])
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const root = dialogRef.current
    root?.querySelector<HTMLElement>('[data-observatory-close]')?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeRef.current(); return }
      if (event.key !== 'Tab' || !root) return
      const focusable = [...root.querySelectorAll<HTMLElement>('button, a[href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
        .filter(element => !element.matches(':disabled') && !element.hidden && element.getAttribute('aria-hidden') !== 'true')
      const first = focusable[0], last = focusable.at(-1)
      if (!first) { event.preventDefault(); root.focus(); return }
      if (event.shiftKey && (document.activeElement === first || !root.contains(document.activeElement))) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && (document.activeElement === last || !root.contains(document.activeElement))) { event.preventDefault(); first.focus() }
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => { document.removeEventListener('keydown', onKeyDown, true); if (previous?.isConnected) previous.focus() }
  }, [])

  const chooseTask = (id: string) => { setSelectedTaskId(id); setSelectedRunId(null); onTaskChange?.(id) }
  const switchLive = () => { setPlaying(false); setCursor(null); setReplayRun(null) }
  const seekTo = (position: number) => {
    if (cursor === null) setReplayRun(run ?? null)
    setPlaying(false)
    setCursor(position)
  }
  const togglePlayback = () => {
    if (playing) { setPlaying(false); return }
    if (cursor === null) setReplayRun(run ?? null)
    if (cursor === null || cursor >= (displayedRun?.events.length ?? 0) - 1) setCursor(0)
    setPlaying(true)
  }

  return <div ref={dialogRef} className="reasoning-observatory" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={descriptionId} tabIndex={-1}>
    <header className="obs-header">
      <div className="obs-heading"><span className="obs-eyebrow">AMPULY ENGINE / OBSERVATORY</span><h1 id={titleId}>推理与执行</h1>
        <p id={descriptionId}>从真实事件，追踪任务如何推进。事件摘要不等同于模型完整思维过程。</p></div>
      <div className="obs-header-actions"><span className={`obs-connection ${connected ? 'is-connected' : ''}`} role="status"><i aria-hidden="true" />{connectionLabel}</span>
        <button type="button" className="obs-button" data-observatory-close onClick={onClose}>返回主应用 <kbd>Esc</kbd></button></div>
    </header>

    <div className="obs-layout">
      <aside className="obs-sidebar" aria-label="任务与运行选择">
        <div className="obs-section-heading"><h2>任务</h2><span>{allTasks.length}</span></div>
        {allTasks.length ? <ul className="obs-task-list">{allTasks.map(task => <li key={task.id}><button type="button" aria-pressed={task.id === activeTaskId} className={task.id === activeTaskId ? 'is-selected' : ''} onClick={() => chooseTask(task.id)}>
          <strong>{task.title || task.id}</strong><span>{task.id === activeTaskId && !view.isLive ? '回看中' : task.status || task.id}</span></button></li>)}</ul>
          : <p className="obs-muted obs-empty-line">尚无任务。主应用收到执行事件后，记录会出现在这里。</p>}
        <div className="obs-section-heading obs-run-heading"><h2>运行记录</h2><span>{runs.length}</span></div>
        {runs.length ? <ul className="obs-run-list">{runs.map(item => <li key={item.id}><button type="button" className={item.id === run?.id ? 'is-selected' : ''} aria-pressed={item.id === run?.id} onClick={() => setSelectedRunId(item.id)}>
          <span className="obs-run-title"><i data-phase={item.id === run?.id && !view.isLive ? view.phase : item.phase} aria-hidden="true" /><strong>{phaseLabel(item.id === run?.id && !view.isLive ? view.phase : item.phase)}</strong><time>{timestamp(item.startedAt)}</time></span>
          <code title={item.runId}>{item.runId}</code><small>{item.events.length} 条缓存事件</small></button></li>)}</ul>
          : <p className="obs-muted obs-empty-line">该任务尚无已采集的运行事件。</p>}
        <div className="obs-history"><h2>工单历史</h2><label htmlFor={orderSelectId}>读取已留存的工单事件</label>
          <select id={orderSelectId} value={orderId} onChange={event => setOrderId(event.target.value)} disabled={historyLoading || !orders.length}>
            <option value="">{orders.length ? '选择工单' : '暂无可读取工单'}</option>{orders.map(order => <option key={order.id} value={order.id}>{order.title || order.id}</option>)}</select>
          <button type="button" className="obs-button" disabled={!connected || !orderId || !onLoadHistory || historyLoading} onClick={() => onLoadHistory?.(orderId)}>{historyLoading ? '正在读取…' : '读取历史记录'}</button>
          {historyError && <p className="obs-history-error" role="alert">{historyError}</p>}
          <p className="obs-muted">历史只包含系统已留存的事件，不补写未采集的过程。</p></div>
      </aside>

      <main className="obs-main">
        {snapshot.droppedEvents > 0 && <div className="obs-cache-notice" role="note">当前是有界缓存窗口：{snapshot.droppedEvents.toLocaleString('zh-CN')} 条较早事件已移出，所见记录不代表完整历史。</div>}
        <section className="obs-stage" aria-label="当前执行状态">
          <div className="obs-stage-top"><div><span className="obs-eyebrow">{view.isLive ? 'LIVE / 实时观测' : 'REPLAY / 事件回看'}</span><h2>{selectedTask?.title || (run ? run.taskId : '等待一个真实任务')}</h2></div>
            <button type="button" className="obs-text-button" aria-pressed={staticMode || reducedMotion} disabled={reducedMotion} onClick={() => setStaticMode(value => !value)}>{staticMode || reducedMotion ? '静态画面' : '切为静态'}</button></div>
          <div className="obs-orb-wrap"><OrbCanvas phase={view.phase} connected={connected} actors={view.actorIds.length} staticMode={staticMode || reducedMotion} className="obs-orb" />
            <div className="obs-stage-status"><span className="obs-phase" data-phase={view.phase}>{phaseLabel(view.phase)}</span><h3>{view.events.at(-1)?.title || '等待执行事件'}</h3>
              <p>{view.events.at(-1)?.summary || '尚未提供可公开的执行摘要。'}</p>
              <div className="obs-stage-meta"><span>{view.actorIds.length ? `${view.actorIds.length} 位已观测执行者` : '执行者未提供'}</span><span>{view.events.length} 条可见事件</span>
                {!connected && <span>保留已采集记录</span>}</div></div></div>
          <div className="obs-playback"><div className="obs-playback-buttons"><button type="button" className="obs-play-button" onClick={togglePlayback} disabled={(displayedRun?.events.length ?? 0) < 2} aria-label={playing ? '暂停回放' : '播放事件回放'}>{playing ? 'Ⅱ' : '▶'}</button>
            <button type="button" className={`obs-live-button ${view.isLive ? 'is-live' : ''}`} onClick={switchLive} aria-pressed={view.isLive}>实时</button></div>
            <div className="obs-playback-slider"><label htmlFor={cursorId}>{view.isLive ? '跟随最新事件' : `回看第 ${view.position + 1} 条 · 固定采集窗口`}<span>{displayedRun?.events.length ? `${view.position + 1} / ${displayedRun.events.length}` : '暂无事件'}</span></label>
              <input id={cursorId} type="range" min={0} max={Math.max(0, (displayedRun?.events.length ?? 0) - 1)} value={Math.max(0, view.position)} disabled={!displayedRun?.events.length} onChange={event => seekTo(Number(event.target.value))} aria-valuetext={view.events.length ? `第 ${view.position + 1} 条：${view.events.at(-1)?.title}` : '暂无事件'} /></div></div>
        </section>

        <section className="obs-metrics" aria-label="采集到的运行指标"><div className="obs-metric-heading">观测指标<span>最后一次提供的快照</span></div><dl>
          <div><dt>执行耗时</dt><dd>{metric(view.metrics.durationMs, 'duration')}</dd></div><div><dt>输入 tokens</dt><dd>{metric(view.metrics.inputTokens, 'tokens')}</dd></div>
          <div><dt>输出 tokens</dt><dd>{metric(view.metrics.outputTokens, 'tokens')}</dd></div><div><dt>总 tokens</dt><dd>{metric(view.metrics.totalTokens, 'tokens')}</dd></div><div><dt>费用 USD</dt><dd>{metric(view.metrics.costUsd, 'cost')}</dd></div></dl></section>

        <section className="obs-sequence"><div className="obs-section-heading"><h2>执行序列</h2><span>{sequence.length < view.events.length ? '最近 12 步' : `${sequence.length} 步`}</span></div>
          <p className="obs-section-note">按采集顺序排列，可横向浏览；连线只表示先后，不推断业务依赖。</p>
          {sequence.length ? <ol className="obs-sequence-list">{sequence.map((event, index) => <li key={event.id}><button type="button" className={event.id === selectedEvent?.id ? 'is-selected' : ''} onClick={() => setSelectedEventId(event.id)} aria-pressed={event.id === selectedEvent?.id}>
            <span className="obs-sequence-number">{view.events.length - sequence.length + index + 1}</span><span className="obs-sequence-title">{event.title}</span><span className="obs-sequence-phase" data-phase={event.phase}>{phaseLabel(event.phase)}</span></button></li>)}</ol>
            : <div className="obs-empty"><span aria-hidden="true">↳</span><h3>事件到达后，路径才会出现。</h3><p>这里不会预填流程、执行者或完成结果。</p></div>}
        </section>

        <section className="obs-timeline"><div className="obs-section-heading"><h2>事件时间线</h2><span>{filteredEvents.length} 条</span></div>
          <div className="obs-timeline-filters"><label className="obs-search" htmlFor={searchId}><span className="obs-sr-only">搜索已显示的事件</span><input id={searchId} type="search" placeholder="搜索事件、摘要或执行者" value={search} onChange={event => setSearch(event.target.value)} /></label>
            <label className="obs-phase-select" htmlFor={phaseId}><span className="obs-sr-only">按阶段筛选</span><select id={phaseId} value={phaseFilter} onChange={event => setPhaseFilter(event.target.value as Phase | 'all')}><option value="all">全部阶段</option>{Object.entries(PHASE_LABELS).map(([phase, label]) => <option value={phase} key={phase}>{label}</option>)}</select></label></div>
          {timelineEvents.length ? <ol className="obs-event-list">{timelineEvents.map(event => <li key={event.id}><button type="button" className={selectedEvent?.id === event.id ? 'is-selected' : ''} onClick={() => setSelectedEventId(event.id)} aria-pressed={selectedEvent?.id === event.id}>
            <time>{timestamp(event.time)}</time><i className="obs-event-dot" data-phase={event.phase} aria-hidden="true" /><div><span className="obs-event-title">{event.title}</span><p>{event.summary || '未提供可公开摘要'}</p><span className="obs-event-actor">{event.actorId || '执行者未提供'} · {phaseLabel(event.phase)}</span></div></button></li>)}</ol>
            : <p className="obs-muted obs-empty-line">{view.events.length ? '没有符合筛选条件的事件。' : '还没有可显示的真实事件。'}</p>}
          {pageCount > 1 && <nav className="obs-pagination" aria-label="事件分页"><button type="button" className="obs-button" disabled={actualPage === 0} onClick={() => setTimelinePage(page => Math.max(0, page - 1))}>较新事件</button><span>{actualPage + 1} / {pageCount}</span><button type="button" className="obs-button" disabled={actualPage + 1 >= pageCount} onClick={() => setTimelinePage(page => page + 1)}>更早事件</button></nav>}
        </section>
      </main>

      <aside className="obs-detail-column" aria-label="事件详情与证据">
        <section className="obs-event-detail"><div className="obs-section-heading"><h2>事件详情</h2>{selectedEvent && <span data-phase={selectedEvent.phase}>{phaseLabel(selectedEvent.phase)}</span>}</div>
          {selectedEvent ? <><time className="obs-detail-time">{timestamp(selectedEvent.time, true)}</time><h3>{selectedEvent.title}</h3><p className="obs-detail-summary">{selectedEvent.summary || '未提供可公开摘要。该事件仅记录下方客观字段。'}</p>
            <dl className="obs-detail-fields"><div><dt>执行者</dt><dd>{selectedEvent.actorId || '未提供'}</dd></div><div><dt>事件类型</dt><dd>{selectedEvent.type || '未提供'}</dd></div><div><dt>运行</dt><dd><code>{selectedEvent.runId}</code></dd></div><div><dt>任务</dt><dd><code>{selectedEvent.taskId}</code></dd></div></dl>
            <h4>该事件的引用</h4><ReferenceList references={selectedEvent.references} onOpenTask={onOpenTask} />
            {onOpenTask && tasks.some(task => task.id === selectedEvent.taskId) && <button type="button" className="obs-button obs-open-task" onClick={() => onOpenTask(selectedEvent.taskId)}>打开任务，处理审核与执行 ↗</button>}</>
            : <p className="obs-muted obs-empty-line">选择一条事件，即可查看已采集的说明与引用。</p>}
        </section>
        <section className="obs-deliveries"><div className="obs-section-heading"><h2>产物与引用</h2><span>{view.references.length}</span></div><p className="obs-section-note">{view.isLive ? '来自当前缓存运行。' : '仅包含回放游标之前的记录。'}</p>
          <ReferenceList references={view.references.slice(-30)} onOpenTask={onOpenTask} />{view.references.length > 30 && <p className="obs-muted">显示最近 30 项，其余引用可在对应事件中查看。</p>}</section>
      </aside>
    </div>
  </div>
}

export default ReasoningObservatoryPage
