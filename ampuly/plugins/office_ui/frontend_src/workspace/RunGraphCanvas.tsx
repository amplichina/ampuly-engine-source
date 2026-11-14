import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  getNodesBounds,
  useReactFlow,
  type Edge,
  type Node,
  type NodeMouseHandler,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { KanbanPhase, KanbanTask, RoleWorkItemRow, RoleWorkItemSummary, Session } from '../types/kanban'
import { useI18n } from '../i18n'
import {
  RunGraphGroup,
  RunGraphNode,
  nodeHeightForRows,
  toneForPhase,
  type RunGraphGroupData,
  type RunGraphNodeData,
  type RunGraphRowData,
  type RunGraphTone,
} from './RunGraphNode'
import { actionsForSession, sessionForRow, type NodeSessionActions } from './runGraphModel'
import { NODE_WIDTH, computeRunGraphLayout, type LayoutItem } from './runGraphLayout'
import './runGraph.css'

const NODE_TYPES = { runNode: RunGraphNode, runGroup: RunGraphGroup }

const DONE_PHASES: ReadonlySet<KanbanPhase> = new Set(['approved', 'failed', 'cancelled'])
const REASSIGNABLE_PHASES: ReadonlySet<KanbanPhase> = new Set(['failed', 'cancelled'])

type I18nKey = Parameters<ReturnType<typeof useI18n>['t']>[0]

const PHASE_LABEL_KEYS: Record<KanbanPhase, I18nKey> = {
  queued: 'phase.queued',
  ready: 'phase.ready',
  ready_for_rework: 'phase.ready_for_rework',
  waiting_dependencies: 'phase.waiting_dependencies',
  running: 'phase.running',
  waiting_for_peer: 'phase.waiting_for_peer',
  waiting_for_children: 'phase.waiting_for_children',
  paused: 'phase.paused',
  needs_attention: 'phase.needs_attention',
  awaiting_manager_review: 'phase.awaiting_manager_review',
  awaiting_human: 'phase.awaiting_human',
  approved: 'phase.approved',
  failed: 'phase.failed',
  cancelled: 'phase.cancelled',
}

const KIND_LABEL_KEYS: Record<string, I18nKey> = {
  intake: 'kind.intake',
  execute: 'kind.execute',
  report: 'kind.report',
  review: 'kind.review',
  aggregate: 'kind.aggregate',
  deliver: 'kind.deliver',
}

/** The runtime's kinds are open-ended (the final wrap-up is not literally
 *  "deliver"); match on the word so the tag still says something. */
function kindLabelKey(kind: string): I18nKey {
  const exact = KIND_LABEL_KEYS[kind]
  if (exact) return exact
  const lower = kind.toLowerCase()
  if (lower.includes('deliver') || lower.includes('final')) return 'kind.deliver'
  if (lower.includes('review')) return 'kind.review'
  if (lower.includes('report')) return 'kind.report'
  if (lower.includes('aggregate') || lower.includes('synth')) return 'kind.aggregate'
  if (lower.includes('intake')) return 'kind.intake'
  return 'kind.execute'
}

/** Wire colour by the state of what flows along it. */
const EDGE_COLORS: Record<'done' | 'live' | 'waiting' | 'bad', string> = {
  done: '#4fcf7e',
  live: '#6f9bff',
  waiting: '#8a8a96',
  bad: '#ef6b6b',
}

const MINIMAP_COLORS: Record<RunGraphTone, string> = {
  ok: '#4fcf7e',
  bad: '#ef6b6b',
  run: '#6f9bff',
  review: '#f0c04a',
  attention: '#ef6b6b',
  wait: '#6b6b73',
  pending: '#4a4a52',
}

interface GraphRow {
  row: RoleWorkItemRow
  roleKey: string
  roleName: string
  roleSessionId?: string
}

/** Re-fit the viewport when stations are added or the board resizes: fitView
 *  on the canvas only runs at mount, a run grows one station at a time, and
 *  the board gains half the screen when the side panel closes. */
function FitOnChange({ count, container }: { count: number; container: HTMLElement | null }) {
  const { fitView, getNodes, getViewport, setViewport } = useReactFlow()
  useEffect(() => {
    if (count <= 0) return undefined
    // Never shrink below a readable size to fit everything; the minimap
    // and panning cover the rest of a long pipeline. When it does not fit,
    // start from the left — the pipeline's beginning — instead of centring
    // on the middle stations.
    const refit = () => {
      // fitView settles asynchronously; read the viewport only after it did.
      void fitView({ padding: 0.12, minZoom: 0.5, maxZoom: 1, duration: 0 }).then(() => {
        const width = container?.clientWidth ?? 0
        if (!width) return
        const bounds = getNodesBounds(getNodes())
        const { zoom, y } = getViewport()
        if (bounds.width * zoom > width - 24) {
          void setViewport({ x: 24 - bounds.x * zoom, y, zoom }, { duration: 200 })
        }
      })
    }
    let handle = window.setTimeout(refit, 50)
    let observer: ResizeObserver | undefined
    if (container && typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(() => {
        window.clearTimeout(handle)
        handle = window.setTimeout(refit, 150)
      })
      observer.observe(container)
    }
    return () => {
      window.clearTimeout(handle)
      observer?.disconnect()
    }
  }, [count, container, fitView])
  return null
}

function relativeTime(ms: number, t: ReturnType<typeof useI18n>['t']): string {
  if (!ms) return ''
  const minutes = Math.max(0, Math.round((Date.now() - ms) / 60000))
  if (minutes < 1) return t('workspace.graph.justNow')
  if (minutes < 60) return t('workspace.graph.minutesAgo').replace('{n}', String(minutes))
  return t('workspace.graph.hoursAgo').replace('{n}', String(Math.round(minutes / 60)))
}

interface RunGraphCanvasProps {
  /** Per-role rollups straight off the session snapshot. */
  roleWorkItems: Record<string, RoleWorkItemSummary> | undefined
  /** Board tasks, used to map a work item to the card the detail panel opens. */
  tasks: KanbanTask[]
  /** Every session the page knows; nodes find their own by execution turn. */
  sessions: Session[]
  selectedTaskId?: string | null
  onSelectTask: (task: { id: string }) => void
  /** Open the session's chat — where its pending checkpoint cards live. */
  onFocusSession?: (taskId: string) => void
  onStopSession?: (taskId: string) => void
  onResumeSession?: (taskId: string) => void
  /** Send a line to the run's manager (the order's main session). */
  onSendToManager?: (text: string) => void
  /** Open the role's settings (prompt, skills, tools) for the next order. */
  onEditRole?: (roleId: string) => void
  /** Re-run a failed work item as a fresh sibling (the failed phase has no exit). */
  onRetryWorkItem?: (workItemId: string, reason?: string) => void
}

/**
 * Run graph in the ComfyUI idiom: one panel per role, ordered left to right
 * by where the role enters the pipeline; the role's work items as cards
 * inside, each with a coloured header and a few labelled rows; wires only
 * for "waits on", coloured by the state of what flows along them. Clicking
 * a card opens the same detail panel the kanban card does — the graph is a
 * second view of the board, not a second source of truth. The action strip
 * under the canvas drives the selected card's session through the same
 * verbs the task header uses; nothing here is a new capability.
 */
export function RunGraphCanvas({
  roleWorkItems,
  tasks,
  sessions,
  selectedTaskId,
  onSelectTask,
  onFocusSession,
  onStopSession,
  onResumeSession,
  onSendToManager,
  onEditRole,
  onRetryWorkItem,
}: RunGraphCanvasProps) {
  const { t } = useI18n()
  const [pickedWorkItemId, setPickedWorkItemId] = useState<string | null>(null)
  const [canvasEl, setCanvasEl] = useState<HTMLDivElement | null>(null)

  const rows = useMemo(() => {
    const seen = new Map<string, GraphRow>()
    for (const [roleKey, summary] of Object.entries(roleWorkItems ?? {})) {
      for (const row of summary.workItems) {
        // A review-target row is the same work item shown under the reviewer;
        // the executor's copy carries the role that actually did the work.
        const existing = seen.get(row.workItemId)
        if (!existing || (existing.row.isReviewTarget && !row.isReviewTarget)) {
          const executorKey = row.executorRoleId && row.isReviewTarget ? row.executorRoleId : roleKey
          seen.set(row.workItemId, {
            row,
            roleKey: executorKey,
            roleName: row.executorRoleName || summary.roleName,
            roleSessionId: summary.roleSessionId,
          })
        }
      }
    }
    return Array.from(seen.values())
  }, [roleWorkItems])

  const taskByWorkItemId = useMemo(() => {
    const map = new Map<string, KanbanTask>()
    for (const task of tasks) if (task.workItemId) map.set(task.workItemId, task)
    return map
  }, [tasks])

  const actionsByWorkItemId = useMemo(() => {
    const map = new Map<string, NodeSessionActions>()
    for (const { row, roleSessionId } of rows) {
      map.set(row.workItemId, actionsForSession(sessionForRow(row, roleSessionId, sessions)))
    }
    return map
  }, [rows, sessions])

  const selectedWorkItemId = useMemo(() => {
    const fromTask = selectedTaskId ? tasks.find(task => task.id === selectedTaskId)?.workItemId : undefined
    return fromTask ?? pickedWorkItemId ?? undefined
  }, [tasks, selectedTaskId, pickedWorkItemId])

  const { nodes, edges } = useMemo(() => {
    const ids = new Set(rows.map(({ row }) => row.workItemId))
    const rowById = new Map(rows.map(entry => [entry.row.workItemId, entry]))
    const phaseLabel = (phase: KanbanPhase) => t(PHASE_LABEL_KEYS[phase] ?? 'phase.queued') || phase

    // Card rows first: the layout needs each card's height.
    const cardRows = new Map<string, RunGraphRowData[]>()
    for (const { row } of rows) {
      const actions = actionsByWorkItemId.get(row.workItemId)
      const list: RunGraphRowData[] = [
        { label: t('workspace.graph.row.status'), value: phaseLabel(row.phase), tone: toneForPhase(row.phase) },
      ]
      // "Waits for the stations I delegated" is the tree, not a dependency.
      const deps = (row.dependencyWorkItemIds ?? []).filter(
        dep => ids.has(dep) && dep !== row.workItemId && rowById.get(dep)?.row.parentWorkItemId !== row.workItemId,
      )
      const unmet = deps.filter(dep => !DONE_PHASES.has(rowById.get(dep)?.row.phase ?? 'queued'))
      if (deps.length > 0) {
        const names = (unmet.length ? unmet : deps).map(dep => rowById.get(dep)?.row.title || dep).join(' · ')
        list.push({
          label: unmet.length ? t('workspace.graph.row.waitingOn') : t('workspace.graph.row.after'),
          value: names,
          tone: unmet.length ? 'wait' : 'ok',
        })
      }
      if ((actions?.pendingCount ?? 0) > 0) {
        list.push({ label: t('workspace.graph.row.pending'), value: String(actions?.pendingCount), tone: 'review' })
      }
      if (row.blockedReason) {
        list.push({ label: t('workspace.graph.row.blocked'), value: row.blockedReason, tone: 'attention' })
      }
      list.push({ label: t('workspace.graph.row.updated'), value: relativeTime(row.updatedAt, t) })
      cardRows.set(row.workItemId, list)
    }

    const layoutItems: LayoutItem[] = rows.map(({ row, roleKey, roleName }) => ({
      id: row.workItemId,
      groupKey: roleKey,
      groupName: roleName,
      createdAt: row.createdAt || 0,
      deps: (row.dependencyWorkItemIds ?? []).filter(
        dep => ids.has(dep) && rowById.get(dep)?.row.parentWorkItemId !== row.workItemId,
      ),
      parent: row.parentWorkItemId && ids.has(row.parentWorkItemId) ? row.parentWorkItemId : undefined,
      height: nodeHeightForRows(cardRows.get(row.workItemId)?.length ?? 1),
    }))
    const layout = computeRunGraphLayout(layoutItems)
    const groupColor = new Map(layout.groups.map(group => [group.key, group.color]))

    const groupNodes: Node[] = layout.groups.map(group => ({
      id: `group:${group.key}`,
      type: 'runGroup',
      position: { x: group.x, y: group.y },
      style: { width: group.width, height: group.height },
      draggable: false,
      selectable: false,
      data: { name: group.name, color: group.color, count: group.count } satisfies RunGraphGroupData,
    }))
    const itemNodes: Node[] = rows.map(({ row, roleKey, roleName }) => {
      const actions = actionsByWorkItemId.get(row.workItemId)
      const position = layout.positions.get(row.workItemId) ?? { x: 0, y: 0, groupKey: roleKey }
      const data: RunGraphNodeData = {
        title: row.title || row.workItemId,
        kindLabel: t(kindLabelKey(String(row.kind || ''))),
        roleName,
        roleColor: groupColor.get(roleKey) ?? '#3d7ddb',
        phase: row.phase,
        tone: toneForPhase(row.phase),
        rows: cardRows.get(row.workItemId) ?? [],
        pendingCount: actions?.pendingCount ?? 0,
        live: actions?.live ?? false,
        openable: taskByWorkItemId.has(row.workItemId),
        selected: row.workItemId === selectedWorkItemId,
      }
      return {
        id: row.workItemId,
        type: 'runNode',
        parentId: `group:${position.groupKey}`,
        position: { x: position.x, y: position.y },
        style: { width: NODE_WIDTH },
        data,
      }
    })

    const rawEdges: Edge[] = []
    for (const { row } of rows) {
      const deps = (row.dependencyWorkItemIds ?? []).filter(
        dep => ids.has(dep) && dep !== row.workItemId && rowById.get(dep)?.row.parentWorkItemId !== row.workItemId,
      )
      const sources = deps.length > 0
        ? deps
        // A card with no dependency hangs off whoever delegated it, so the
        // pipeline still reads as one connected thing.
        : (row.parentWorkItemId && ids.has(row.parentWorkItemId) ? [row.parentWorkItemId] : [])
      for (const source of sources) {
        const sourcePhase = rowById.get(source)?.row.phase ?? 'queued'
        const state: keyof typeof EDGE_COLORS = sourcePhase === 'approved'
          ? 'done'
          : sourcePhase === 'failed' || sourcePhase === 'cancelled'
            ? 'bad'
            : sourcePhase === 'running'
              ? 'live'
              : 'waiting'
        rawEdges.push({
          id: `${deps.length > 0 ? 'dep' : 'tree'}:${source}->${row.workItemId}`,
          source,
          target: row.workItemId,
          type: 'default',
          animated: state === 'live',
          className: `rg-wire rg-wire-${state}${deps.length === 0 ? ' rg-wire-tree' : ''}`,
          style: { stroke: EDGE_COLORS[state], strokeWidth: state === 'waiting' ? 1.8 : 2.4 },
        })
      }
    }
    // Parents must precede their children in the node list.
    return { nodes: [...groupNodes, ...itemNodes], edges: rawEdges }
  }, [rows, taskByWorkItemId, actionsByWorkItemId, selectedWorkItemId, t])

  const handleNodeClick = useCallback<NodeMouseHandler>(
    (_event, node) => {
      if (node.type !== 'runNode') return
      setPickedWorkItemId(node.id)
      const task = taskByWorkItemId.get(node.id)
      if (task) onSelectTask({ id: task.id })
    },
    [taskByWorkItemId, onSelectTask],
  )

  const selected = useMemo(
    () => rows.find(({ row }) => row.workItemId === selectedWorkItemId),
    [rows, selectedWorkItemId],
  )
  const selectedActions = selected ? actionsByWorkItemId.get(selected.row.workItemId) : undefined
  const selectedSessionTaskId = selectedActions?.session?.taskId

  const handleReassign = useCallback(() => {
    if (!selected || !onSendToManager) return
    const title = selected.row.title || selected.row.workItemId
    onSendToManager(t('workspace.graph.reassignText').replace('{title}', title))
  }, [selected, onSendToManager, t])

  if (rows.length === 0) {
    return (
      <div className="kanban-empty-state kanban-empty-state-inline">
        <p>{t('workspace.graph.empty')}</p>
      </div>
    )
  }

  return (
    <div className="run-graph">
      <div className="run-graph-canvas" ref={setCanvasEl}>
        <ReactFlowProvider>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            onNodeClick={handleNodeClick}
            nodesDraggable={false}
            nodesConnectable={false}
            elementsSelectable={false}
            fitView
            fitViewOptions={{ padding: 0.12, minZoom: 0.5, maxZoom: 1 }}
            minZoom={0.2}
            proOptions={{ hideAttribution: true }}
          >
            <Background variant={BackgroundVariant.Dots} gap={22} size={1.2} color="#3a3a40" />
            <Controls showInteractive={false} />
            <MiniMap
              pannable
              zoomable
              nodeColor={(node: Node) => (
                node.type === 'runGroup'
                  ? `${String((node.data as RunGraphGroupData).color)}55`
                  : MINIMAP_COLORS[(node.data as RunGraphNodeData).tone] ?? MINIMAP_COLORS.pending
              )}
            />
          </ReactFlow>
          <FitOnChange count={nodes.length} container={canvasEl} />
        </ReactFlowProvider>
        <div className="rg-legend">
          <span><i style={{ background: EDGE_COLORS.done }} />{t('workspace.graph.legend.done')}</span>
          <span><i style={{ background: EDGE_COLORS.live }} />{t('workspace.graph.legend.live')}</span>
          <span><i style={{ background: EDGE_COLORS.waiting }} />{t('workspace.graph.legend.waiting')}</span>
        </div>
      </div>
      {selected && (
        <div className="rg-actions" data-testid="run-graph-actions">
          <div className="rg-actions-summary">
            <span className="rg-actions-role">{selected.roleName}</span>
            <span className="rg-actions-title">{selected.row.title || selected.row.workItemId}</span>
            <span className={`rg-actions-phase rg-tone-${toneForPhase(selected.row.phase)}`}>
              {t(PHASE_LABEL_KEYS[selected.row.phase] ?? 'phase.queued')}
            </span>
            {!selectedActions?.session && (
              <span className="rg-actions-note">{t('workspace.graph.noSession')}</span>
            )}
          </div>
          <div className="rg-actions-buttons">
            {selectedSessionTaskId && (selectedActions?.pendingCount ?? 0) > 0 && onFocusSession && (
              <button type="button" className="is-primary" onClick={() => onFocusSession(selectedSessionTaskId)}>
                {t('workspace.graph.handle')} · {selectedActions?.pendingCount}
              </button>
            )}
            {selectedSessionTaskId && selectedActions?.canStop && onStopSession && (
              <button type="button" onClick={() => onStopSession(selectedSessionTaskId)}>
                {t('pc.stop')}
              </button>
            )}
            {selectedSessionTaskId && selectedActions?.canResume && onResumeSession && (
              <button type="button" onClick={() => onResumeSession(selectedSessionTaskId)}>
                {t('ctx.continue')}
              </button>
            )}
            {REASSIGNABLE_PHASES.has(selected.row.phase) && onRetryWorkItem && (
              <button type="button" className="is-warn" onClick={() => onRetryWorkItem(selected.row.workItemId, selected.row.blockedReason || 'manual_retry')}>
                {t('workspace.graph.retry')}
              </button>
            )}
            {REASSIGNABLE_PHASES.has(selected.row.phase) && onSendToManager && (
              <button type="button" onClick={handleReassign}>
                {t('workspace.graph.reassign')}
              </button>
            )}
            {onEditRole && (
              <button type="button" onClick={() => onEditRole(selected.roleKey)}>
                {t('workspace.graph.editRole')}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
