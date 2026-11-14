import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import type { AgentInfo, OrgInfoPayload, SocketStatus } from '../types/visual'
import type { ExecutionTraceStore } from './ExecutionTraceStore'
import type { ObservatoryHistoryReader } from './historyReader'
import type { CollaborationOrder } from './collaboration/model'
import './observatory.css'
import './collaboration.css'
import '@xyflow/react/dist/style.css'

const Page = lazy(() => import('./collaboration/CollaborationPage').then(module => ({ default: module.CollaborationPage })))
type Task = { id: string; title: string; status?: string }
export interface ExecutionObservatoryHostProps {
  store: ExecutionTraceStore; historyReader: ObservatoryHistoryReader; projectId: string
  connection: SocketStatus; pendingProject: boolean; openSequence: number
  tasks: readonly Task[]; orders: readonly CollaborationOrder[]
  org?: OrgInfoPayload | null; agents?: readonly AgentInfo[]; projectName?: string
  onRequestData?: () => void
  onOpenTask: (id: string) => void
}
const requested = () => typeof window !== 'undefined' && new URLSearchParams(window.location.search).get('view') === 'observatory'
function changeAddress(open: boolean, taskId = '') {
  const url = new URL(window.location.href)
  if (open) { url.searchParams.set('view', 'observatory'); if (taskId) url.searchParams.set('observe_task', taskId); else url.searchParams.delete('observe_task') }
  else { url.searchParams.delete('view'); url.searchParams.delete('observe_task') }
  window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`)
}

/** Mounted only on the owner console. Opening this view sends no execution command. */
export function ExecutionObservatoryHost({ store, historyReader, projectId, connection, pendingProject, openSequence, tasks, orders, org, agents, projectName, onRequestData, onOpenTask }: ExecutionObservatoryHostProps) {
  const [open, setOpen] = useState(requested)
  const [taskId, setTaskId] = useState(() => typeof window !== 'undefined' ? new URLSearchParams(window.location.search).get('observe_task') || '' : '')
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [historyOrderId, setHistoryOrderId] = useState('')
  const sequence = useRef(openSequence)
  const project = useRef(projectId)
  const readGeneration = useRef(0)
  const requestData = useRef(onRequestData)
  requestData.current = onRequestData
  useEffect(() => { if (open) requestData.current?.() }, [open, openSequence])
  useEffect(() => {
    if (sequence.current === openSequence) return
    sequence.current = openSequence; setOpen(true); changeAddress(true, taskId)
  }, [openSequence, taskId])
  useEffect(() => {
    if (project.current === projectId && !pendingProject) return
    project.current = projectId; readGeneration.current += 1; historyReader.cancel()
    setHistoryError(null); setHistoryLoading(false); setHistoryOrderId(''); setTaskId('')
    if (open) changeAddress(true)
  }, [projectId, pendingProject, historyReader, open])
  useEffect(() => {
    if (connection === 'connected') return
    readGeneration.current += 1; historyReader.cancel(); setHistoryLoading(false)
  }, [connection, historyReader])
  useEffect(() => () => { readGeneration.current += 1; historyReader.cancel() }, [historyReader])
  useEffect(() => {
    const onPop = () => { setOpen(requested()); setTaskId(new URLSearchParams(window.location.search).get('observe_task') || '') }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])
  const close = useCallback(() => { readGeneration.current += 1; historyReader.cancel(); setHistoryLoading(false); setOpen(false); changeAddress(false) }, [historyReader])
  const load = useCallback((orderId: string) => {
    if (pendingProject || connection !== 'connected' || historyLoading) return
    const order = orders.find(row => row.id === orderId)
    if (!order) { setHistoryError('这张工单不在当前项目的可读列表中。'); return }
    const generation = ++readGeneration.current
    setHistoryLoading(true); setHistoryError(null); setHistoryOrderId(orderId)
    void historyReader.read(projectId, orderId, order.taskId).then(events => {
      if (generation !== readGeneration.current) return
      store.appendProjected(events, projectId)
      const nextTask = order.taskId || `order:${orderId}`
      setTaskId(nextTask); changeAddress(true, nextTask)
      if (!events.length) setHistoryError('这张工单暂时没有可展示的历史事件。')
    }).catch(() => {
      if (generation === readGeneration.current) setHistoryError('历史记录未读取成功，请确认连接后手动重试。')
    }).finally(() => { if (generation === readGeneration.current) setHistoryLoading(false) })
  }, [connection, historyLoading, historyReader, orders, pendingProject, projectId, store])
  const openTask = useCallback((id: string) => {
    if (pendingProject || !tasks.some(task => task.id === id)) {
      setHistoryError('对应执行任务尚未在当前项目加载，请回到业务工单查看。')
      return
    }
    close(); onOpenTask(id)
  }, [close, onOpenTask, pendingProject, tasks])
  if (!open) return null
  return <Suspense fallback={<section className="ao-loading" role="dialog" aria-label="协作图谱"><p role="status">正在打开协作图谱…</p><button type="button" onClick={close}>返回引擎</button></section>}>
    <Page store={store} taskId={taskId} tasks={pendingProject ? [] : tasks} orders={pendingProject ? [] : orders}
      org={pendingProject ? null : org} agents={pendingProject ? [] : agents} projectName={projectName}
      onClose={close} onTaskChange={id => { setTaskId(id); changeAddress(true, id) }}
      onOpenTask={openTask} onLoadHistory={load} historyLoading={historyLoading}
      historyError={historyError} historyOrderId={historyOrderId} />
  </Suspense>
}
