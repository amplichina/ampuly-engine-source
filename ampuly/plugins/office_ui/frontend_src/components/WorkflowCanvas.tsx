import { NodeTrialSection, type NodeTrialRecord, type TrialArtifactState } from './NodeTrialSection'
import { WorkflowBlockerStrip } from './WorkflowBlockerStrip'
import { LinesField } from './LinesField'
import { DEFAULT_INSPECTOR_TAB, INSPECTOR_TABS, INSPECTOR_TAB_LABELS, tabForField, upstreamSteps, type InspectorTab } from '../workspace/inspectorTabs'
import { canvasShortcut, changed, emptyHistory, recorded, redone, snapshotted, undone, type DraftHistory } from '../workspace/draftHistory'
import { duplicateNode } from '../workspace/duplicateStep'
import { TIERS, suggestTier, tierLabel } from '../workspace/modelTiers'
import { absoluteCanvas, alignAll, boardLayout, branchCondition, branchRowText, componentLabel, deleteImpact, insertAfter, insertBefore, insertOnWire, laneSpots, moveComponent, pinnedToBoard, placeNewStep, removeNodeBridging, wireId, wireOf, type AlignFrame, type DeleteImpact, type NewStepPlace } from '../workspace/graphEdits'
import { edgeCarries, undeliveredInputs, wireTargets } from '../workspace/neededDeliverables'
import { fieldFallbacks, fixHint, healthByNode, healthCardWord, healthItems, isEditorField, type HealthItem, type NodeBlocker } from '../workspace/workflowHealth'
import type { NodeTrialPin, NodeTrialPinning } from '../lib/nodeTrialPins'
import { appliedNodes } from '../lib/nodeProposal'
import { type MouseEvent as ReactMouseEvent, type ReactNode, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  Background,
  BackgroundVariant,
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  type EdgeProps,
  MiniMap,
  NodeToolbar,
  Position,
  ReactFlow,
  ReactFlowProvider,
  getViewportForBounds,
  useEdgesState,
  useNodesInitialized,
  useNodesState,
  useReactFlow,
  useStore,
  type Connection,
  type Edge,
  type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import {
  RunGraphGroup,
  type RunGraphGroupData,
  type RunGraphNodeData,
  type RunGraphRowData,
} from '../workspace/RunGraphNode'
import { CATALOG_MODES, CATALOG_MODE_LABELS, catalogProgress, catalogTree, payloadKey, rememberedGroups, toggledKey, withRecent, type CatalogMode } from '../workspace/builderCatalog'

// The shelf's 我的常用 / 最近使用, per viewer. The browser may refuse storage (private window, cleared data): then the
// two groups are simply absent -- nothing depends on them.
const SHELF_FAVOURITES = 'ampuly.builder.shelf.favourites'
const SHELF_RECENT = 'ampuly.builder.shelf.recent'
function readShelfList(key: string): string[] {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(key) ?? '[]')
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string').slice(0, 50) : []
  } catch {
    return []
  }
}
function writeShelfList(key: string, list: readonly string[]): void {
  try { window.localStorage.setItem(key, JSON.stringify(list.slice(0, 50))) } catch { /* storage refused: remembered for this page only */ }
}
import { FlowPreviewBoard } from '../workspace/FlowPreviewBoard'
import { previewGroups, viewAfter, type BuilderView } from '../workspace/flowPreview'
import { STEP_SEMANTIC_LABELS, stepSemantics } from '../workspace/stepSemantics'
import { carryMeasuredAndSelected, carrySelected, keepUnchanged, seedMemory, type SeedMemory } from '../workspace/seedReuse'
import { flowShape, shapeWords } from '../workspace/flowShape'
import { applyCardMarks } from '../workspace/cardMarks'
import { modelRefusedText, stepModelChoices, stepModelRow, type ModelProvider } from '../workspace/stepModels'
import { POSTCONDITION_HINTS, POSTCONDITION_KINDS, POSTCONDITION_LABELS, declaredFromText, declaredText, itemKeyName, itemsInputName, linesFromText, withItemKey, withItemsInput, type VerifyMode, lookupKeyName, namesFromText, verifyMode, withLookupKey, withVerifyMode, clampSteps, connectorRow, domainsFromText, domainsText, inputsFromText, inputsText, newConnector, type ConnectorParams, type PostconditionKind, DISTRIBUTION_ACTION_OPTIONS, distributionAction, withDistributionAction } from '../workspace/browserConnector'
import { desktopRow, isDesktopConnector, newDesktopConnector } from '../workspace/desktopConnector'
import { DesktopConnectorFieldset } from './DesktopConnectorFieldset'
import { bringIntoView } from '../workspace/bringIntoView'
import { readableStart } from '../workspace/readableStart'
import { BuilderNode, builderCardHeight, cardCaption, type CardEdit } from '../workspace/BuilderNode'
import { GROUP_PADDING, GROUP_TITLE_HEIGHT, NODE_WIDTH, type LayoutItem } from '../workspace/runGraphLayout'
import '../workspace/runGraph.css'
import type { WorkflowNode } from './WorkflowsPage'
import { AIEmployeeShelfHost } from './aiEmployees/AIEmployeeShelfHost'
import './aiEmployees/employeeLibrary.css'
import './aiEmployees/employeeNodeBinding.css'
import './aiEmployees/canvasEmployeeDesign.css'
import { AI_EMPLOYEE_CANDIDATE_MIME, parseAIEmployeeCandidate } from './aiEmployees/AIEmployeeShelf'
import type { AIEmployeeInstanceRef } from './aiEmployees/AIEmployeeLibrary'
import { bindingForCanvasEmployee, currentCanvasEmployee, type CanvasEmployeeDesign } from '../lib/canvasEmployeeDesign'
import type { OrgEmployee } from '../types/visual'
import { ContactHint } from './PeopleContacts'
import type { BookContact } from '../lib/contactBook'
import { reworkTargets, showsReworkTo } from '../workspace/reworkTo'
import { useI18n } from '../i18n'
import {
  ADDABLE_NODE_KINDS,
  retypePatch,
  type NodeKind,
  COMPONENT_MIME,
  type ComponentPayload,
  type NodeSlot,
  type StepInput,
  inputsFor,
  componentKindMime,
  connectPatch,
  connectRejection,
  DIFF_FIELD_LABELS,
  diffNodeTables,
  conflictFieldsText,
  proposalConflicts,
  proposalWidens,
  wideningText,
  disconnectPatches,
  effectiveSigner,
  flowCallRejection,
  newInvokeNode,
  newNode,
  patchForComponent,
  isStagedStep,
  layoutWithStaging,
  rosterFor,
  toggleSkill,
  canvasLayoutFor,
  skillDeclaration,
  undeclaredSkills,
} from '../workspace/workflowGraph'

/** What 前加 / 后加 put in: a kind of step, one of this flow's skills, or a call to another flow. */
type InsertChoice =
  | { kind: 'type'; type: 'ai_do' | 'human_do' | 'human_review' }
  | { kind: 'skill'; id: string }
  | { kind: 'flow'; id: string; name: string }

// 4.4 §2.4/§2.5: the 搭建台 shows a workflow's business steps on the same canvas the company runtime
// already uses in the workspace (@xyflow/react + computeRunGraphLayout + RunGraphNode). Nodes drag
// smoothly (拖拽为主). §2.3 万物皆组件: a node is a container of components -- drag an employee (and,
// next, MCP / Skill / model / prompt) from the 组件库 onto a node to fill its slots. No shadow data
// model: it reads WorkflowNode[] as-is and patches the draft through onNodePatch.

// The builder's own card under the run view's type key: selectors, hit tests and saved positions name 'runNode'.
const NODE_TYPES = { runNode: BuilderNode, runGroup: RunGraphGroup }
/** From this many steps React Flow renders only the cards and wires in view (4.7 §353: every drag step otherwise runs
 *  a selector per card, handle, wire and minimap point). Below it every card stays mounted, exactly as before; measured
 *  per move about 7.5 ms + 0.085 ms a step, it crosses one 60 fps frame near 110 steps. */
export const VISIBLE_ONLY_FROM = 120

type BuilderEdgeData = {
  removable?: boolean; removeLabel?: string; onRemove?: (source: string, target: string) => void
  /** The target's declared inputs that this source promises; empty when the target declares none. */
  carries?: string[]
  /** 4.7 A02: the target is a branch (it has a 分支条件): the word marked beside the source's end of the wire, and the
   *  condition for the hover. */
  branch?: { label: string; title: string }
}

// A dependency wire on the builder canvas. Selected (and in an editable draft) it shows a ✕ at its midpoint,
// so a wire can be removed without knowing that Backspace deletes the selected edge.
function BuilderEdge({ id, source, target, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, style, markerEnd, selected, data }: EdgeProps) {
  const [path, labelX, labelY] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition })
  const edgeData = (data ?? {}) as BuilderEdgeData
  // 分支 sits on the upstream card's side of the wire, so each branch that leaves one step is marked where it leaves.
  const branchX = sourceX + (labelX - sourceX) * 0.45
  const branchY = sourceY + (labelY - sourceY) * 0.45
  return (
    <>
      <BaseEdge id={id} path={path} style={style} markerEnd={markerEnd} />
      {edgeData.branch && (
        <EdgeLabelRenderer>
          <span className="wf-edge-carries wf-edge-branch nodrag nopan" data-branch-mark="" title={edgeData.branch.title} aria-label={edgeData.branch.title}
            style={{ transform: `translate(${branchX}px, ${branchY}px) translate(-50%, -110%)`, color: 'var(--accent)', borderColor: 'var(--accent)' }}>
            {edgeData.branch.label}
          </span>
        </EdgeLabelRenderer>
      )}
      {(edgeData.carries ?? []).length > 0 && !selected && (
        <EdgeLabelRenderer>
          <span className="wf-edge-carries nodrag nopan" style={{ transform: `translate(${labelX}px, ${labelY}px) translate(-50%, -50%)` }} title={(edgeData.carries ?? []).join('\n')}>
            {(edgeData.carries ?? [])[0]}{(edgeData.carries ?? []).length > 1 ? ` +${(edgeData.carries ?? []).length - 1}` : ''}
          </span>
        </EdgeLabelRenderer>
      )}
      {selected && edgeData.removable && edgeData.onRemove && (
        <EdgeLabelRenderer>
          <EdgeRemoveButton x={labelX} y={labelY} label={edgeData.removeLabel ?? ''} onClick={() => edgeData.onRemove?.(source, target)} />
        </EdgeLabelRenderer>
      )}
    </>
  )
}

// The label layer lives inside the zoomed viewport, so a plain button shrinks with the graph (12px at a
// typical fit). Counter-scale by the zoom to keep it a 22px target. Only the selected wire renders this,
// so only one component follows the zoom.
function EdgeRemoveButton({ x, y, label, onClick }: { x: number; y: number; label: string; onClick: () => void }) {
  const zoom = useStore(state => state.transform[2])
  return (
    <button
      type="button"
      className="wf-edge-remove nodrag nopan"
      style={{ transform: `translate(${x}px, ${y}px) translate(-50%, -50%) scale(${1 / (zoom || 1)})` }}
      title={label}
      aria-label={label}
      onClick={onClick}
    >
      ✕
    </button>
  )
}

const EDGE_TYPES = { builder: BuilderEdge }

// The 组件库 drop plumbing (COMPONENT_MIME / ComponentPayload / patchForComponent) and the wire rules
// (connectPatch / disconnectPatches) are pure and live in ../workspace/workflowGraph so they can be
// unit-tested without React Flow. This file wires them to the canvas.

// A node is a container of components (4.4 §2.1/§2.3: prompt / Skill / MCP / model / responsible person are
// what a node configures -- 万物皆组件). This card surfaces the slots the definition already carries; the
// 负责 slot shows the assigned employee's name (assignee holds the employee_id) and the 审核 slot shows
// the gate type plus who signs it off (gate_to, empty = owner). Slot rows carry `slot` so a component
// can be dropped onto that exact row.
// The node fields a proposal can change, as a person reads them on the card.

function EmployeeViewportCapture({ capture }: { capture: (value: { screenToFlowPosition: (point: { x: number; y: number }) => { x: number; y: number } }) => void }) {
  const viewport = useReactFlow()
  useEffect(() => { capture(viewport) }, [capture, viewport])
  return null
}

function rowsFor(
  node: WorkflowNode,
  zh: boolean,
  empName: Map<string, string>,
  gateText: (gate: string) => string,
  roleName: Map<string, string>,
  inputs: readonly StepInput[],
  packageSkills?: readonly string[],
  librarySkills: readonly string[] = [],
  libraryUnlockable: readonly string[] = [],
  trialSkills: readonly string[] = [],
  candidates: readonly string[] = [],
  health: readonly HealthItem[] = [],
  undelivered: readonly string[] = [],
): RunGraphRowData[] {
  const rows: RunGraphRowData[] = []
  // What a save would still refuse (a freshly added step starts here), shown first so it cannot be missed. The
  // same list feeds the blocker strip above the canvas, so the card and the strip cannot disagree.
  if (health.length) {
    rows.push({ label: zh ? '待补' : 'todo', value: [...new Set(health.map(item => healthCardWord(item, zh)))].join(' · '), tone: 'attention' })
  }
  // 4.7 A02: a branch says on its card when it is taken -- the first 40 characters, the whole condition on hover.
  const branch = branchRowText(node)
  if (branch) rows.push({ label: zh ? '分支条件' : 'branch if', value: branch.value, title: zh ? `分支条件：${branch.title}` : `Branch if: ${branch.title}` })
  // Business first (4.4 §2.5): who does it, what it needs, what it produces, whether a person signs it off;
  // the technical components (Skill / tools / model) come after.
  const assignedName = node.assignee ? (empName.get(node.assignee) ?? node.assignee) : ''
  const who = assignedName || (node.role ? (roleName.get(node.role) ?? node.role) : '') || (node.invoke?.workflow ? `→ ${String(node.invoke.workflow)}` : '')
  if (who) rows.push({ label: zh ? '负责' : 'who', value: who, slot: 'assignee' })
  const declared = (node.inputs ?? []).map(item => item.trim()).filter(Boolean)
  if (declared.length) {
    // The step says what it needs, by name. One nobody upstream promises is marked, not hidden among the rest.
    rows.push({
      label: zh ? '需要' : 'needs',
      value: (undelivered.length ? (zh ? `上游没人交付：${undelivered.join(' · ')}` : `nobody upstream delivers: ${undelivered.join(' · ')}`)
        : declared.length <= 2 ? declared.join(' · ') : (zh ? `${declared[0]} 等 ${declared.length} 项` : `${declared[0]} +${declared.length - 1}`)),
      title: declared.join('\n'),
      tone: undelivered.length ? 'attention' : undefined,
    })
  } else if (inputs.length) {
    // what the wires bring in: each upstream step's deliverables, or the step's name if it has none yet
    const needed = inputs.flatMap(input => (input.deliverables.length ? input.deliverables : [zh ? `（${input.name}）` : `(${input.name})`]))
    rows.push({
      label: zh ? '需要' : 'needs',
      value: needed.length <= 2 ? needed.join(' · ') : (zh ? `${needed[0]} 等 ${needed.length} 项` : `${needed[0]} +${needed.length - 1}`),
      title: inputs.map(input => `${input.name}: ${input.deliverables.join(zh ? '、' : ', ') || '—'}`).join('\n'),
    })
  }
  if (node.deliverables?.length) rows.push({ label: zh ? '交付' : 'out', value: node.deliverables.join(' · ') })
  if (node.gate && node.gate !== 'none') {
    const signer = effectiveSigner(node.gate_to)
    rows.push({ label: zh ? '审核' : 'gate', value: `✋ ${gateText(node.gate)} · ${empName.get(signer) ?? signer}`, tone: 'review', slot: 'gate' })
  }
  if (node.skills?.length) {
    // The save accepts a skill the package does not declare, but it would not be installed or version-locked;
    // one from the internal library is declared and locked by the save, and says so.
    // Everything the save would not declare: on trial, not lockable, or unknown -- all flagged on the card.
    const stray = undeclaredSkills(node, packageSkills, librarySkills)
    rows.push({
      label: 'Skill',
      value: node.skills.map(skill => {
        const state = skillDeclaration(skill, packageSkills, librarySkills, libraryUnlockable, trialSkills, candidates)
        if (state === 'candidate') return `${skill}${zh ? '（总控建议的外部组件：调用测试并录入后才能保存）' : ' (suggested from outside: try it and admit it to save)'}`
        if (state === 'trial') return `${skill}${zh ? '（试用技能，录入内部库后才能保存）' : ' (trial skill: saved once admitted)'}`
        if (state === 'undeclared') return `${skill}${zh ? '（未声明）' : ' (undeclared)'}`
        if (state === 'refused') return `${skill}${zh ? '（契约不完整，保存会被拒）' : ' (incomplete contract: the save refuses it)'}`
        return state === 'on_save' ? `${skill}${zh ? '（保存时声明）' : ' (declared on save)'}` : skill
      }).join(' · '),
      ...(stray.length ? { tone: 'attention' as const, title: zh ? '流程包没有声明带「未声明」的技能：它们不会随流程包安装和锁版本' : 'Skills marked undeclared are not installed or version-locked with the package' } : {}),
    })
  }
  if (node.employee_binding) rows.push({ label: zh ? 'AI 员工' : 'AI employee',
    value: `${node.employee_binding.package_id} · ${node.employee_binding.version_constraint.version ?? (zh ? '项目采用版' : 'adopted')}` })
  if (node.tools?.length) rows.push({ label: zh ? '工具/MCP' : 'MCP', value: node.tools.join(' · ') })
  const model = stepModelRow(node.model)
  if (model) rows.push({ label: zh ? '模型' : 'model', value: model })
  const browser = connectorRow(node.connector, zh)
  if (browser) rows.push({ label: zh ? '浏览器' : 'browser', value: browser })
  const desktop = desktopRow(node.connector, zh)
  if (desktop) rows.push({ label: zh ? '桌面应用' : 'desktop', value: desktop })
  return rows.length ? rows : [{ label: zh ? '类型' : 'type', value: node.type }]
}

// What a component drop at this point would fill: the workflow node under the pointer and, when the
// pointer is on a slot row (审核), that slot. DOM hit-testing (elementFromPoint -> the item-node wrapper)
// is robust to zoom/pan and node nesting, so it beats translating client coords into flow space and
// searching by geometry. `highlight` is the element to outline while hovering: the slot row itself, or
// the whole node when the drop fills the default slot.
function dropTargetAt(clientX: number, clientY: number): { nodeId: string; slot: NodeSlot; highlight: HTMLElement | null; nodeEl: HTMLElement | null } {
  const hit = document.elementFromPoint(clientX, clientY)
  const nodeEl = hit?.closest('.react-flow__node-runNode') as HTMLElement | null
  const nodeId = nodeEl?.getAttribute('data-id') ?? ''
  if (!nodeEl || !nodeId) return { nodeId: '', slot: 'assignee', highlight: null, nodeEl: null }
  const rowEl = hit?.closest('[data-slot]') as HTMLElement | null
  if (rowEl && nodeEl.contains(rowEl) && rowEl.getAttribute('data-slot') === 'gate') {
    return { nodeId, slot: 'gate', highlight: rowEl, nodeEl }
  }
  return { nodeId, slot: 'assignee', highlight: nodeEl, nodeEl }
}

const DROP_TARGET_CLASS = 'wf-drop-target'
const DROP_SLOT_CLASS = 'wf-drop-slot'
const DROP_WIRE_CLASS = 'wf-drop-wire'

/** The wire under a point, when there is one and no step is there (4.7 Q02: 拖到连线提供插入预览). React Flow gives each
 *  wire a wider invisible path to hit; its group names the wire (graphEdits.wireId, read back by wireOf). */
function wireAt(clientX: number, clientY: number): { source: string; target: string; el: Element } | null {
  for (const hit of document.elementsFromPoint(clientX, clientY)) {
    if (hit.closest('.react-flow__node')) return null
    const edge = hit.closest('.react-flow__edge')
    const id = edge?.getAttribute('data-id') ?? edge?.getAttribute('data-testid')?.replace(/^rf__edge-/, '') ?? ''
    const wire = wireOf(id)
    if (edge && wire) return { ...wire, el: edge }
  }
  return null
}

// Re-fit the graph when the canvas changes size (expand/collapse); `fitView` on <ReactFlow> only runs at
// init. This reads the pane's new size straight from the DOM and applies the viewport with no animation,
// before paint. The queued fitView() is not used here: it and its zoom transition wait on animation
// frames and on the ResizeObserver-fed store size, so a background tab (verified: 0 frames while hidden)
// could leave the graph fitted to the old size.
/** A real flow fitted whole is cards nobody can read: then start readable, on the flow's beginning. Once, and only
 *  when the cards have been measured -- at the canvas's own init they may not be yet, and the fit that follows the
 *  measuring would undo it (it did, on some loads). */
/** The canvas follows the step whose inspector is open. It moves only when that card is not already fully in the
 *  usable part of the pane -- the pane minus what floats over it -- and it never changes the zoom: the person's own
 *  view stays theirs, it just makes sure they can see what they opened. */
function FollowsTheOpenStep({ nodeId, wrapRef }: { nodeId: string; wrapRef: { current: HTMLDivElement | null } }) {
  const { getNode, getNodesBounds, getViewport, setViewport } = useReactFlow()
  const measured = useNodesInitialized()
  useEffect(() => {
    if (!nodeId || !measured) return
    // Measured a tick later, not in this commit: the inspector that docks over the canvas is rendering right now, and
    // reading the pane before it has settled made the very first step opened after a load miss (seen on the console).
    const timer = window.setTimeout(() => {
    const wrap = wrapRef.current
    const node = getNode(nodeId)
    if (!wrap || !node) return
    // The absolute box, not node.position: the steps sit inside lane nodes, so their own position is relative to the
    // lane and doing the arithmetic with it moved the canvas the wrong way (seen on the real console).
    const box = getNodesBounds([node])
    const pane = wrap.getBoundingClientRect()
    if (!pane.width || !pane.height) return
    // What covers the pane, measured rather than guessed: the inspector docks right on a wide canvas and along the
    // bottom on a narrow one, and the view switch and the panel's reopen button sit at the top left.
    const cover = (selector: string) => {
      const element = wrap.querySelector<HTMLElement>(selector) ?? wrap.parentElement?.querySelector<HTMLElement>(selector)
      return element?.getBoundingClientRect() ?? null
    }
    const inspector = cover('.wf-node-edit')
    const overlay = cover('.wf-view-switch')
    const next = bringIntoView(
      { x: box.x, y: box.y, width: box.width || 220, height: box.height || 140 },
      {
        width: pane.width,
        height: pane.height,
        right: inspector && inspector.right >= pane.right - 2 ? inspector.width : 0,
        bottom: inspector && inspector.bottom >= pane.bottom - 2 && inspector.right < pane.right - 2 ? inspector.height : 0,
        left: overlay ? Math.max(0, overlay.right - pane.left) : 0,
        top: overlay ? Math.max(0, overlay.bottom - pane.top) : 0,
      },
      getViewport(),
    )
    if (next) void setViewport(next, { duration: 180 })
    }, 60)
    return () => window.clearTimeout(timer)
    // Only when another step is opened: panning away from the one you are editing is allowed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId, measured])
  return null
}

/** What zoom the canvas is at, and one click back to 100%. It opens at 70% on a real flow (readableStart), so
 *  "why are these cards small" needs an answer on screen rather than in the controls' tooltips. */
/** What deleting a step costs, read before it happens (4.7 Q02): who waited for it and where they are rejoined, the
 *  inputs only it delivered, the sign-off it carried. The numbers come from workspace/graphEdits.deleteImpact. */
function DeleteImpactNote({ impact, nodes, zh }: { impact: DeleteImpact | null; nodes: readonly WorkflowNode[]; zh: boolean }) {
  if (!impact) return null
  const name = (id: string) => nodes.find(node => node.id === id)?.name || id
  const lines: string[] = []
  if (impact.dependents.length) {
    const joined = impact.bridgedTo.length ? impact.bridgedTo.map(name).join('、') : (zh ? '（无，改为起点）' : '(nothing; they become starting steps)')
    lines.push(zh
      ? `等它的步骤：${impact.dependents.map(name).join('、')}，删除后改接到：${joined}`
      : `Waiting for it: ${impact.dependents.map(name).join(', ')}; rejoined to: ${joined}`)
  }
  if (impact.lostInputs.length) {
    lines.push(zh
      ? `只有它交付、之后就没人交付的输入：${impact.lostInputs.map(item => `${name(item.nodeId)} 需要的 ${item.input}`).join('；')}`
      : `Inputs only it delivered: ${impact.lostInputs.map(item => `${item.input} for ${name(item.nodeId)}`).join('; ')}`)
  }
  if (impact.gate !== 'none') lines.push(zh ? '它带着的人工签字也会一起去掉' : 'Its sign-off goes with it')
  if (impact.invokes) lines.push(zh ? `它调用的流程「${impact.invokes}」不再被调用` : `The flow it calls (${impact.invokes}) is no longer called`)
  return (
    <div className="wf-delete-impact">
      <span>{zh ? '删除这一步？' : 'Remove this step?'}</span>
      {lines.length ? <ul>{lines.map(line => <li key={line}>{line}</li>)}</ul>
        : <span className="wf-delete-impact-none">{zh ? '没有别的步骤依赖它。' : 'No other step depends on it.'}</span>}
    </div>
  )
}

function ZoomReadout({ zh, onAlign }: { zh: boolean; onAlign?: () => void }) {
  const { zoomTo, zoomIn, zoomOut, fitView } = useReactFlow()
  const zoom = useStore(state => state.transform[2])
  const percent = Math.round((zoom || 0) * 100)
  if (!percent) return null
  return (
    <div className="wf-zoom-readout wf-zoom-bar" data-testid="zoom-readout" role="group" aria-label={zh ? '缩放' : 'Zoom'}>
      <button type="button" data-zoom="out" onClick={() => void zoomOut({ duration: 150 })} aria-label={zh ? '缩小' : 'Zoom out'}>−</button>
      <span>{percent}%</span>
      <button type="button" data-zoom="in" onClick={() => void zoomIn({ duration: 150 })} aria-label={zh ? '放大' : 'Zoom in'}>+</button>
      <button type="button" data-zoom="fit" onClick={() => void fitView({ duration: 200, padding: 0.12 })}>{zh ? '适应视图' : 'Fit'}</button>
      {onAlign && (
        <button type="button" data-zoom="align" title={zh ? '按步骤先后从左到右分层排好，分出去的几步围绕上一步上下居中；不改连线，可撤销' : 'Lay the steps out by dependency, left to right, with branches centred on the step they leave; wires unchanged, undoable'}
          onClick={() => { onAlign(); window.setTimeout(() => void fitView({ duration: 200, padding: 0.12 }), 50) }}>{zh ? '一键对齐' : 'Align'}</button>
      )}
      <button type="button" data-zoom="reset" onClick={() => void zoomTo(1, { duration: 150 })} disabled={percent === 100}
        title={zh ? '回到 100%' : 'Back to 100%'}>100%</button>
    </div>
  )
}

function ReadableStartOnce() {
  const { getNodes, getNodesBounds, setViewport } = useReactFlow()
  const domNode = useStore(state => state.domNode)
  const measured = useNodesInitialized()
  const done = useRef(false)
  useEffect(() => {
    if (done.current || !measured) return
    const rect = domNode?.getBoundingClientRect()
    if (!rect || !rect.width || !rect.height) return
    done.current = true
    const start = readableStart(getNodesBounds(getNodes()), { width: rect.width, height: rect.height })
    if (start) void setViewport(start, { duration: 0 })
  }, [measured, domNode, getNodes, getNodesBounds, setViewport])
  return null
}

function FitViewOn({ signal }: { signal: unknown }) {
  const { getNodes, getNodesBounds, setViewport } = useReactFlow()
  const domNode = useStore(state => state.domNode)
  const minZoom = useStore(state => state.minZoom)
  const maxZoom = useStore(state => state.maxZoom)
  const first = useRef(true)
  useLayoutEffect(() => {
    if (first.current) { first.current = false; return }
    const rect = domNode?.getBoundingClientRect()
    if (!rect || !rect.width || !rect.height) return
    const flowNodes = getNodes()
    if (!flowNodes.length) return
    const viewport = getViewportForBounds(getNodesBounds(flowNodes), rect.width, rect.height, minZoom, maxZoom, 0.1)
    void setViewport(viewport, { duration: 0 })
  }, [signal, domNode, minZoom, maxZoom, getNodes, getNodesBounds, setViewport])
  return null
}

export function WorkflowCanvas({
  nodes,
  locale = 'zh-CN',
  workflowId = '',
  employeeDesign,
  employeeSavedRevision = '',
  employees = [],
  employeesOrgId = '',
  contacts = null,
  onOpenPeople,
  workflowOrgId = '',
  orgRoles,
  packageSkills: packageSkillsProp,
  librarySkills: librarySkillsProp,
  libraryUnlockable: libraryUnlockableProp,
  desktopChannelOpen = false,
  trialSkills: trialSkillsProp,
  broughtExtra,
  candidates: candidatesProp,
  candidateActions,
  brought = null,
  onDismissBrought,
  onOpenLibrary,
  intakeFields = [],
  flows = [],
  proposal = null,
  onApplyProposal,
  onDiscardProposal,
  onNodePatch: onNodePatchProp,
  onNodesReplace: onNodesReplaceProp,
  onFocusStep,
  onNodePreflight,
  nodePreflight = null,
  nodePreflightBusy = false,
  nodePreflightBlocked = false,
  onNodeTrial,
  nodeTrials = [],
  nodeTrialBusy = false,
  nodeTrialBlocked = false,
  serverBlockers,
  draftOpen = false,
  savedVersion = '',
  nodeTrialPinning = null,
  onNodeTrialPlan,
  trialArtifact,
  onOpenTrialArtifact,
  onCloseTrialArtifact,
  onAnswerTrial,
  onAbandonTrial,
  saveBar,
  packageHostRef,
  openCanvasSignal = 0,
  toolbarActions,
  builderRequest = null,
  onViewSkill,
  onEditSkill,
  defaultModel = '',
  modelProviders,
  skillNames,
  toolNames,
}: {
  nodes: WorkflowNode[]
  locale?: string
  workflowId?: string
  employeeDesign?: CanvasEmployeeDesign
  employeeSavedRevision?: string
  employees?: OrgEmployee[]
  /** The org the `employees` roster belongs to (the console's active org). */
  employeesOrgId?: string
  /** The contact book as last read (null = not read): beside 负责 / 签字人, whether that person can be reached. */
  contacts?: BookContact[] | null
  /** 去绑定: to 人员与职责, where a member's Feishu or email is bound. Absent = the hint has no link. */
  onOpenPeople?: () => void
  /** The org this workflow's orders run on; only that org's employees may be named on its nodes. */
  workflowOrgId?: string
  /** The workflow org's roles (id + display name); when present the role field is a list, not free text. */
  orgRoles?: { id: string; name: string }[]
  /** The package's declared skills: the only ones the 组件架 offers and the editor lists (absent = unknown, free text). */
  packageSkills?: string[]
  /** Internal-library skills this package can take: a step may name one and the save declares and locks it. */
  librarySkills?: string[]
  /** Internal-library skills this package cannot lock (incomplete release contract): a save naming one is refused. */
  libraryUnlockable?: string[]
  /** 4.7-A06: whether the owner opened the desktop channel (the package payload's desktop_channel_open). */
  desktopChannelOpen?: boolean
  /** Skills on trial (调用测试): a step may name one to see how it fits; the draft is not saved while it does. */
  trialSkills?: string[]
  /** Extra controls under the brought component (for a skill on trial: its test notes and verdict). */
  broughtExtra?: ReactNode
  /** Components master control suggested from outside the internal library for this workflow (policy). */
  candidates?: Array<{ id: string; source: 'skillhub' | 'catalog'; name: string; summary: string; node_ids: string[]; can_try?: boolean }>
  /** What the shelf offers for one suggested component (调用测试, or where to find it). */
  candidateActions?: (candidate: { id: string; source: 'skillhub' | 'catalog'; name: string; can_try?: boolean }) => ReactNode
  /** A component brought from 组件库 with 调用: offered first on the shelf until dismissed. */
  brought?: { kind: 'skill'; id: string; name: string } | null
  onDismissBrought?: () => void
  /** Take the person to 组件库, where a component that is not on the shelf goes through admission first. */
  onOpenLibrary?: () => void
  /** What placing an order on this flow asks the customer for. A step with no upstream starts from these. */
  intakeFields?: Array<{ id: string; label: string; required?: boolean }>
  /** Live workflows as larger components (业务流): what each calls, so a call that loops back can be refused. */
  flows?: Array<{ id: string; name: string; status: string; calls: string[] }>
  /**
   * A master-control proposal to preview on this canvas: its table is shown read-only with added and changed
   * steps marked, until the person applies it to their draft (undoable) or discards it. Nothing is saved.
   */
  proposal?: {
    nodes: WorkflowNode[]
    /** The table the proposal was checked against: its marks are what it changes from this. */
    baseNodes: WorkflowNode[]
    baseVersion: string
    newVersion: string
    changeNote: string
    liveVersion: string
    /** The saved workflow moved on since the proposal was made (version, or the table at the same version). */
    stale: boolean
    /** Why it cannot be applied right now (an open draft, a workflow that is no longer editable), if so. */
    blockedReason?: string | null
    testing: boolean
    /** Components it suggests from outside the internal library, each still to be tried and admitted. */
    candidates?: Array<{ id: string; source: 'skillhub' | 'catalog'; name: string; summary: string; node_ids: string[]; can_try?: boolean }>
  } | null
  onApplyProposal?: () => void
  onDiscardProposal?: () => void
  onNodePatch?: (nodeId: string, patch: Partial<WorkflowNode>) => void
  /** Replace the whole draft table -- how a step is added or removed (the save sends the full table). */
  onNodesReplace?: (nodes: WorkflowNode[]) => void
  /** The step whose editor is open ('' = none): 安安 offers it as the thing a message is about (4.7 Q04 引用). */
  onFocusStep?: (nodeId: string) => void
  /** Read-only, side-effect-free validation of one saved node through the authoritative workflow preflight. */
  onNodePreflight?: (nodeId: string) => void
  nodePreflight?: {
    focus_node_id?: string
    ready: boolean
    blockers: string[]
    warnings?: string[]
    steps: Array<{ node_id: string; name: string; type: string; status: string; tools: string[]; skills: string[]; gate: string; depends_on?: string[]; inputs?: string[]; deliverables?: string[] }>
    simulated_effects: Array<{ tool: string; mode: string; reason: string }>
  } | null
  nodePreflightBusy?: boolean
  /** A saved-version preflight must never be presented as proof for an unsaved draft. */
  nodePreflightBlocked?: boolean
  /** Real, budgeted Provider run of one saved AI node in an isolated workspace; a node with upstream steps runs
   *  on the succeeded trials a person pinned for them. */
  onNodeTrial?: (nodeId: string, pins: NodeTrialPin[], imageLimit?: number) => void
  nodeTrials?: NodeTrialRecord[]
  nodeTrialBusy?: boolean
  nodeTrialBlocked?: boolean
  /** Whether an unsaved draft of this workflow is open, and which version is saved: the undo history belongs to
   *  one draft of one saved version and ends with it. */
  draftOpen?: boolean
  savedVersion?: string
  /** What the last refused save or preflight said about the nodes, each tied to its step and field. */
  serverBlockers?: readonly NodeBlocker[]
  /** The server's plan for the node being edited: what has to be pinned, and what can be. */
  nodeTrialPinning?: NodeTrialPinning | null
  onNodeTrialPlan?: (nodeId: string) => void
  trialArtifact?: TrialArtifactState
  onOpenTrialArtifact?: (specId: string, path: string) => void
  onCloseTrialArtifact?: () => void
  /** The step asked a person inside a trial: answer it (continues the trial) or abandon the trial. */
  onAnswerTrial?: (specId: string, reply: string) => void
  onAbandonTrial?: (specId: string) => void
  /** The draft's save controls, shown in the expanded canvas while a draft is open. */
  saveBar?: ReactNode
  /** Mount point for the selected package. The parent portals the existing package controls into this view. */
  packageHostRef?: (node: HTMLDivElement | null) => void
  /** Incremented when a package action asks to return to the infinite canvas. */
  openCanvasSignal?: number
  /** 搭建台's top actions besides undo and save (4.7 A2-2): 安安协助 first, 测试 before the save, 验收准入 after it. */
  toolbarActions?: { assist?: ReactNode; test?: ReactNode; admit?: ReactNode }
  /** Asked from outside the canvas (测试's 去试跑, 记录试跑结果): show this view, and open this step on this page. */
  builderRequest?: { view: BuilderView; nodeId?: string; tab?: InspectorTab; seq: number } | null
  /** Open a skill's component detail over the canvas (read-only); closing it returns to the open step. */
  onViewSkill?: (skillId: string) => void
  /** 4.7-A16: edit a step's skill in 组件库, remembering this step so the library can bring the owner back to it. */
  onEditSkill?: (skillId: string, nodeId: string) => void
  /** The configured default model, when known: what an AI step runs on unless it names one. */
  defaultModel?: string
  /** Which providers have a key here, and their models: the choices for a step's own model. */
  modelProviders?: ModelProvider[]
  /** 4.7 AQ: the display names the component catalogue gives skills (and tools), so the step's editor says 「品牌简报」
   *  and not the id it is stored under. A component without one is shown by its id. */
  skillNames?: ReadonlyArray<{ id: string; name: string }>
  toolNames?: ReadonlyArray<{ id: string; name: string }>
}) {
  const zh = locale.startsWith('zh')
  // Gate type names come from the shared dictionary, so the canvas and the list editor say the same thing.
  const { t } = useI18n()
  const gateText = useCallback((gate: string) => {
    const key = `workflows.gate.${gate}` as Parameters<typeof t>[0]
    const text = t(key)
    return text === key ? gate : text
  }, [t])
  const nodeTypeText = useCallback((kind: string) => {
    const key = `workflows.node.${kind}` as Parameters<typeof t>[0]
    const text = t(key)
    return text === key ? kind : text
  }, [t])
  // Names resolve from every known employee (so an existing value still reads as a person), but only the
  // workflow org's roster is offered for assignment -- see rosterFor.
  const empName = useMemo(() => new Map(employees.map(emp => [emp.employee_id, emp.name || emp.employee_id])), [employees])
  const { roster, sameOrg } = useMemo(() => rosterFor(workflowOrgId, employeesOrgId, employees), [workflowOrgId, employeesOrgId, employees])
  // While a proposal is previewed the canvas shows its table and takes no edits: what is on screen is not the
  // draft, so an edit there would land on something the person has not chosen yet.
  const shownNodes = proposal ? proposal.nodes : nodes
  const visibleOnly = shownNodes.length >= VISIBLE_ONLY_FROM
  // Marked is what the proposal changes from the table it was checked against, not from whatever is open now.
  const proposalDiff = useMemo(() => (proposal ? diffNodeTables(proposal.baseNodes, proposal.nodes) : null), [proposal])
  // 4.7 A04: what applying would overwrite (the table open now -- a draft, or a later save -- against the one the
  // proposal was checked against), and what it lets a step do that it could not before. Either needs its own yes.
  const proposalConflictList = useMemo(() => (proposal ? proposalConflicts(proposal.baseNodes, nodes) : []), [proposal, nodes])
  const proposalWidenList = useMemo(() => (proposal ? proposalWidens(proposal.baseNodes, proposal.nodes) : []), [proposal])
  // The yeses belong to one proposal: another one (or the same one opened again) starts unconfirmed.
  const proposalKey = proposal ? JSON.stringify([proposal.baseVersion, proposal.newVersion, proposal.changeNote, proposal.nodes.map(node => node.id)]) : ''
  const [proposalYes, setProposalYes] = useState<{ key: string; overwrite: boolean; widen: boolean }>({ key: '', overwrite: false, widen: false })
  const overwriteConfirmed = proposalYes.key === proposalKey && proposalYes.overwrite
  const widenConfirmed = proposalYes.key === proposalKey && proposalYes.widen
  const confirmProposal = (patch: { overwrite?: boolean; widen?: boolean }) => setProposalYes(prev => (
    prev.key === proposalKey ? { ...prev, ...patch } : { key: proposalKey, overwrite: false, widen: false, ...patch }
  ))
  // 4.7 AQ: on an arranged flow (a step dragged or 一键对齐) every change first writes each card down where it is shown,
  // as an absolute position (graphEdits.pinnedToBoard): an older lane-relative position is converted once, and no
  // card moves because a lane it is no longer drawn in moved. `board` is the last drawn layout; undo / redo put a
  // table back as it was and go to the parent directly (onNodesReplaceProp).
  const board = useRef<{ free: boolean; settled: boolean; shownAt: ReadonlyMap<string, { x: number; y: number }> }>({ free: false, settled: true, shownAt: new Map() })
  const pinnedFor = useRef<WorkflowNode[] | null>(null)
  const onNodesReplace = onNodesReplaceProp
    ? (next: WorkflowNode[]) => onNodesReplaceProp(board.current.free ? pinnedToBoard(next, board.current.shownAt) : next)
    : undefined
  const onNodePatch = onNodePatchProp
    ? (nodeId: string, patch: Partial<WorkflowNode>) => {
      // Once per table: several patches in one event (a multi-wire delete) must not each put the table back.
      if (board.current.free && !board.current.settled && onNodesReplaceProp && !proposal && pinnedFor.current !== nodes) {
        pinnedFor.current = nodes
        onNodesReplaceProp(pinnedToBoard(nodes, board.current.shownAt))
      }
      onNodePatchProp(nodeId, patch)
    }
    : undefined
  const componentNames = useMemo(() => ({
    skill: new Map((skillNames ?? []).map(item => [item.id, item.name])),
    tool: new Map((toolNames ?? []).map(item => [item.id, item.name])),
  }), [skillNames, toolNames])
  const editable = Boolean(onNodePatch) && !proposal
  const canAddNodes = editable && Boolean(onNodesReplace)
  const [employeePlacement, setEmployeePlacement] = useState<{
    reference: AIEmployeeInstanceRef; name: string; nodeId: string | null; point: { x: number; y: number } | null
  } | null>(null)
  const employeeViewport = useRef<{ screenToFlowPosition: (point: { x: number; y: number }) => { x: number; y: number } } | null>(null)
  const captureEmployeeViewport = useCallback((value: { screenToFlowPosition: (point: { x: number; y: number }) => { x: number; y: number } }) => { employeeViewport.current = value }, [])
  useEffect(() => { setEmployeePlacement(null) }, [workflowId, employeeDesign?.project_id])
  // Role suggestions: the roles this table already uses. The org's full role list is not in the workflow
  // payload, so the field stays free text and the save still checks the role against the org config.
  const usedRoles = useMemo(() => [...new Set(nodes.map(node => node.role).filter(Boolean))].sort(), [nodes])
  // The org's role list, when the payload carries it: role ids for the save check, names for display.
  const roleName = useMemo(() => new Map((orgRoles ?? []).map(role => [role.id, role.name || role.id])), [orgRoles])
  const orgRoleIds = useMemo(() => new Set((orgRoles ?? []).map(role => role.id)), [orgRoles])
  // Callers derive the list from the payload on every render; key it by content so the graph memo does not rebuild.
  const packageSkillsKey = packageSkillsProp ? packageSkillsProp.join('\n') : null
  const packageSkills = useMemo(() => (packageSkillsKey === null ? undefined : packageSkillsKey.split('\n').filter(Boolean)), [packageSkillsKey])
  const librarySkillsKey = (librarySkillsProp ?? []).join('\n')
  const librarySkills = useMemo(() => librarySkillsKey.split('\n').filter(Boolean), [librarySkillsKey])
  const libraryUnlockableKey = (libraryUnlockableProp ?? []).join('\n')
  const libraryUnlockable = useMemo(() => libraryUnlockableKey.split('\n').filter(Boolean), [libraryUnlockableKey])
  const trialSkillsKey = (trialSkillsProp ?? []).join('\n')
  const trialSkills = useMemo(() => trialSkillsKey.split('\n').filter(Boolean), [trialSkillsKey])
  // A proposal on preview names its own suggestions; an applied one's are passed in.
  const shownCandidates = proposal?.candidates ?? candidatesProp ?? []
  const candidateIdsKey = shownCandidates.map(candidate => candidate.id).join('\n')
  const candidateIds = useMemo(() => candidateIdsKey.split('\n').filter(Boolean), [candidateIdsKey])
  const broughtState = brought ? skillDeclaration(brought.id, packageSkills, librarySkills, libraryUnlockable, trialSkills) : null
  const [confirmDeleteId, setConfirmDeleteId] = useState('')
  // 前加 / 后加 open a short list of what to put there (InsertChoice); null while it is closed.
  const [insertMenu, setInsertMenu] = useState<'before' | 'after' | null>(null)
  // Undo: snapshots of the draft table taken before each canvas action (drop, wire, unwire, add or remove
  // a step) and once per focus in the node editor -- never per keystroke. Restored via onNodesReplace; a
  // snapshot identical to the current table is skipped, so an edit session that changed nothing costs no
  // extra press. Hand-placed positions live outside the draft and are not part of it.
  // Redo goes forward again after an undo, until the next change (workspace/draftHistory holds the rules).
  const historyRef = useRef<DraftHistory<WorkflowNode[]>>(emptyHistory())
  const [historySize, setHistorySize] = useState(0)
  const [futureSize, setFutureSize] = useState(0)
  const latestNodes = useRef(nodes)
  latestNodes.current = nodes
  const setHistory = (next: DraftHistory<WorkflowNode[]>) => {
    historyRef.current = next
    setHistorySize(next.past.length)
    setFutureSize(next.future.length)
  }
  const record = () => {
    if (!onNodesReplace) return
    setHistory(recorded(historyRef.current, latestNodes.current))
  }
  // The step editor takes a snapshot when it gets the focus, before any keystroke. Looking is not changing: the
  // redo line stays until the draft really becomes something undo/redo did not put there (the effect below).
  const recordOnFocus = () => {
    if (!onNodesReplace) return
    setHistory(snapshotted(historyRef.current, latestNodes.current))
  }
  // The cards' in-place fields reach the draft through these two. They are created once and read the latest
  // handlers from refs: a card's data must not change identity on every render, or every card would re-seed.
  const cardEditHandlers = useRef({ begin: recordOnFocus, patch: onNodePatch })
  cardEditHandlers.current = { begin: recordOnFocus, patch: onNodePatch }
  const cardEditBegin = useCallback(() => cardEditHandlers.current.begin(), [])
  const cardEditCommit = useCallback((nodeId: string, field: 'name' | 'responsibility', value: string) => {
    cardEditHandlers.current.patch?.(nodeId, { [field]: value })
  }, [])
  const restoredRef = useRef<WorkflowNode[] | null>(null)
  const undo = () => {
    const step = undone(historyRef.current, latestNodes.current)
    setHistory(step.history)
    if (step.value) { restoredRef.current = step.value; onNodesReplaceProp?.(step.value) }
  }
  const redo = () => {
    const step = redone(historyRef.current, latestNodes.current)
    setHistory(step.history)
    if (step.value) { restoredRef.current = step.value; onNodesReplaceProp?.(step.value) }
  }
  useEffect(() => {
    if (nodes === restoredRef.current) return
    restoredRef.current = null
    if (historyRef.current.future.length) setHistory(changed(historyRef.current))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes])
  const undoRef = useRef(undo)
  undoRef.current = undo
  const redoRef = useRef(redo)
  redoRef.current = redo
  const duplicateRef = useRef<() => void>(() => {})
  const editingIdRef = useRef('')
  // A short-lived note on the canvas for things that were refused (e.g. a wire that would form a cycle).
  const [notice, setNotice] = useState('')
  const noticeTimer = useRef<number | undefined>(undefined)
  const showNotice = (text: string) => {
    setNotice(text)
    window.clearTimeout(noticeTimer.current)
    noticeTimer.current = window.setTimeout(() => setNotice(''), 3200)
  }
  useEffect(() => () => window.clearTimeout(noticeTimer.current), [])
  useEffect(() => { historyRef.current = emptyHistory(); setHistorySize(0); setFutureSize(0) }, [workflowId, savedVersion])
  // Discarding or saving a draft ends its history: a Redo afterwards would quietly rebuild a draft out of what was
  // just thrown away, on top of a version it never belonged to. Opening a draft does not reset (that is the first
  // recorded action itself).
  const draftWasOpen = useRef(draftOpen)
  useEffect(() => {
    if (draftWasOpen.current && !draftOpen) { historyRef.current = emptyHistory(); setHistorySize(0); setFutureSize(0) }
    draftWasOpen.current = draftOpen
  }, [draftOpen])
  const [paletteOpen, setPaletteOpen] = useState(true)
  // 流程预览 / 无限画布: two ways of looking at the one draft. The canvas stays mounted underneath the board, so
  // coming back finds the pan and zoom where they were left.
  const [builderView, setBuilderView] = useState<BuilderView>('canvas')
  useEffect(() => { if (openCanvasSignal > 0) setBuilderView('canvas') }, [openCanvasSignal])
  // Finding a thing on the panel.
  const [shelfQuery, setShelfQuery] = useState('')
  // A component brought with 调用 is the reason the owner is here: open the shelf that shows it.
  const broughtId = brought?.id ?? ''
  useEffect(() => { if (broughtId) setPaletteOpen(true) }, [broughtId])
  // 4.4 §2.4: on a narrow canvas the shelf and the node editor fold into drawers and the canvas keeps the main space.
  const canvasRef = useRef<HTMLDivElement>(null)
  const [canvasWidth, setCanvasWidth] = useState(0)
  const hasNodes = nodes.length > 0
  useLayoutEffect(() => {
    const element = canvasRef.current
    if (!element) return
    const measure = () => setCanvasWidth(Math.round(element.getBoundingClientRect().width))
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    // The observer follows every size change, the expanded layer included.
    return () => observer.disconnect()
  }, [hasNodes])
  const layout = canvasLayoutFor(canvasWidth)
  // Becoming narrow folds the shelf once (unless a brought component is what the owner came for). Widening again
  // puts back what the narrowing took -- and only that: a shelf the person folded themselves stays folded. A window
  // dragged small and large again must end where it started.
  const wasNarrow = useRef(false)
  const foldedByNarrowing = useRef(false)
  useEffect(() => {
    if (layout.narrow && !wasNarrow.current && !broughtId) {
      setPaletteOpen(open => { foldedByNarrowing.current = open; return false })
    } else if (!layout.narrow && wasNarrow.current && foldedByNarrowing.current) {
      foldedByNarrowing.current = false
      setPaletteOpen(true)
    }
    wasNarrow.current = layout.narrow
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layout.narrow])
  // While a chip is dragged out of the drawer, the drawer lets the canvas underneath take the drop.
  const [shelfDragging, setShelfDragging] = useState(false)
  const shelfDragLive = useRef(false)
  const endShelfDrag = () => { shelfDragLive.current = false; setShelfDragging(false) }
  // ④ in-place edit: click (or double-click) a node to open a small field editor anchored at the node.
  const flowWrapRef = useRef<HTMLDivElement>(null)
  // `top` or `bottom` (px within the canvas): the editor opens downward from a point in the upper half and
  // upward from one in the lower half, so it is never pushed past the canvas edge and clipped.
  const [editing, setEditing] = useState<{ id: string; x: number; top?: number; bottom?: number } | null>(null)

  // The ✕ on a selected wire calls this. It is stable (the real work is swapped in each render below) so
  // the edge data stays memoised and a re-render does not rebuild the edges and drop the selection.
  const removeWireImpl = useRef<(source: string, target: string) => void>(() => {})
  const removeWire = useCallback((source: string, target: string) => removeWireImpl.current(source, target), [])
  // React Flow hands the node click handler to every card's (memoised) wrapper: a new function on each render made
  // every card draw again on every drag step, click and keystroke -- ~0.12 ms per card per update, 40 ms at 300.
  const openEditorImpl = useRef<(event: ReactMouseEvent, node: Node) => void>(() => {})
  const openEditorFromClick = useCallback((event: ReactMouseEvent, node: Node) => openEditorImpl.current(event, node), [])

  // What a save would still refuse, from the draft as it is now plus what the server last said that the page
  // cannot recompute. One list: the cards' 待补 rows and the strip above the canvas both read it.
  // Keyed by content, not identity: a parent that hands over an equal list in a new array must not rebuild the
  // cards (that re-seeds React Flow and drops a drag, the selection and a selected wire).
  const serverBlockersKey = JSON.stringify(serverBlockers ?? [])
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const health = useMemo(() => healthItems(nodes, orgRoleIds, serverBlockers ?? []), [nodes, orgRoleIds, serverBlockersKey])
  const healthOfNode = useMemo(() => healthByNode(health), [health])

  const { seedNodes, seedEdges, alignFrame, parentAt, drawn } = useMemo(() => {
    const nodes = shownNodes
    const ids = new Set(nodes.map(node => node.id))
    const ungrouped = zh ? '未分组' : 'ungrouped'
    const added = new Set(proposalDiff?.added ?? [])
    const changed = new Map((proposalDiff?.changed ?? []).map(item => [item.id, item.fields]))
    const rowsByNode = new Map(nodes.map(node => {
      const rows = rowsFor(node, zh, empName, gateText, roleName, inputsFor(nodes, node), packageSkills, librarySkills, libraryUnlockable, trialSkills, candidateIds, editable ? (healthOfNode.get(node.id) ?? []) : [], undeliveredInputs(nodes, node))
      // A proposal marks what it would change, first on the card so it cannot be missed.
      if (added.has(node.id)) rows.unshift({ label: zh ? '提议' : 'proposed', value: zh ? '新增的步骤' : 'new step', tone: 'attention' })
      const fields = changed.get(node.id)
      if (fields) rows.unshift({ label: zh ? '提议' : 'proposed', value: (zh ? '改：' : 'changes: ') + fields.map(field => DIFF_FIELD_LABELS[field]?.[zh ? 0 : 1] ?? field).join(zh ? '、' : ', '), tone: 'attention' })
      return [node.id, rows]
    }))
    const layoutItems: LayoutItem[] = nodes.map((node, index) => ({
      id: node.id,
      groupKey: node.role || ungrouped,
      groupName: node.role ? (roleName.get(node.role) ?? node.role) : ungrouped,
      createdAt: index,
      deps: (node.depends_on ?? []).filter(dep => ids.has(dep) && dep !== node.id),
      height: builderCardHeight(rowsByNode.get(node.id)?.length ?? 1, editable),
    }))
    // Role-less new steps sit in a staging lane after the role lanes, so adding one never moves the graph.
    const stagedIds = new Set(nodes.filter(isStagedStep).map(node => node.id))
    const layout = layoutWithStaging(layoutItems, stagedIds, zh ? '新步骤 · 待选角色' : 'New steps · no role yet')
    const groupColor = new Map(layout.groups.map(group => [group.key, group.color]))
    // 4.7 AQ: a flow nobody arranged is drawn in its role lanes. Once a step has a saved position (a drag, 一键对齐) the
    // flow is drawn as arranged, with no lane box -- a box drawn around cards laid out by dependency lands on the other
    // lanes -- and the role stays on each card (its tint and caption). Positions are absolute then (graphEdits.boardLayout).
    const heightOf = new Map(layoutItems.map(item => [item.id, item.height]))
    const lanes = laneSpots(layout, nodes.map(node => node.id))
    const drawn = boardLayout(nodes, lanes, heightOf)
    // Where each card's parent sits: a card in a lane is positioned inside it (the drag stop adds it back).
    const parentAt = new Map<string, { x: number; y: number }>(drawn.free ? [] : layout.groups.map(group => [`group:${group.key}`, { x: group.x, y: group.y }]))

    const groupNodes: Node[] = drawn.free ? [] : layout.groups.map(group => ({
      id: `group:${group.key}`,
      type: 'runGroup',
      position: { x: group.x, y: group.y },
      style: { width: group.width, height: group.height },
      draggable: false,
      selectable: false,
      deletable: false,
      data: { name: group.name, color: group.color, count: group.count } satisfies RunGraphGroupData,
    }))
    const semantics = stepSemantics(nodes)
    const shapes = flowShape(nodes)
    const nameOf = new Map(nodes.map(node => [node.id, node.name || node.id]))
    const itemNodes: Node[] = nodes.map(node => {
      const shape = shapes.get(node.id)
      const laid = layout.positions.get(node.id) ?? { x: 0, y: 0, groupKey: node.role || ungrouped }
      const data: RunGraphNodeData = {
        title: node.name || node.id,
        kindLabel: nodeTypeText(node.type),
        roleName: node.role,
        roleColor: groupColor.get(laid.groupKey) ?? '#3d7ddb',
        phase: 'queued',
        // A step a save would refuse wears it on the card, not only in a row of text.
        tone: editable && healthOfNode.has(node.id) ? 'attention' : 'pending',
        rows: rowsByNode.get(node.id) ?? [],
        pendingCount: 0,
        live: false,
        openable: true,
        selected: false,
        semantic: semantics.get(node.id),
        semanticLabel: STEP_SEMANTIC_LABELS[semantics.get(node.id) ?? 'concept'][zh ? 0 : 1],
        // 分支 / 汇合 (flowShape): marked on the card's lower corners, beside the port each is about.
        joinMark: shape?.joinFrom.length ? shapeWords('join', shape.joinFrom.map(id => nameOf.get(id) ?? id), zh) : undefined,
        splitMark: shape?.splitTo.length ? shapeWords('split', shape.splitTo.map(id => nameOf.get(id) ?? id), zh) : undefined,
        caption: cardCaption({
          roleLabel: node.role ? (roleName.get(node.role) ?? node.role) : '',
          gated: Boolean(node.gate && node.gate !== 'none'),
          signer: empName.get(effectiveSigner(node.gate_to)) ?? effectiveSigner(node.gate_to),
        }, zh),
        // The two things changed most often, changed where they are read. Same draft, same undo history.
        edit: editable ? {
          name: node.name ?? '',
          responsibility: node.responsibility ?? '',
          begin: cardEditBegin,
          commit: (field, value) => cardEditCommit(node.id, field, value),
          labels: zh
            ? { name: '步骤名称', responsibility: '职责', placeholder: '这一步要做成什么' }
            : { name: 'Step name', responsibility: 'Responsibility', placeholder: 'what this step must get done' },
        } satisfies CardEdit : undefined,
      }
      // As arranged: the card's own place on the canvas. In lanes: its place inside its lane.
      const at = drawn.positions.get(node.id) ?? lanes.get(node.id)?.spot ?? { x: 0, y: 0 }
      return {
        id: node.id,
        type: 'runNode',
        ...(drawn.free ? { position: { x: at.x, y: at.y } } : { parentId: `group:${laid.groupKey}`, position: { x: laid.x, y: laid.y } }),
        style: { width: NODE_WIDTH },
        deletable: false,
        className: added.has(node.id) ? 'wf-proposal-added' : changed.has(node.id) ? 'wf-proposal-changed' : undefined,
        data,
      }
    })

    const edges: Edge[] = []
    for (const node of nodes) {
      // 4.7 A02: every wire into a branch is marked 分支 where it leaves the step before it.
      const condition = branchCondition(node)
      const branch = condition ? { label: zh ? '分支' : 'branch', title: zh ? `分支条件：${condition}` : `Branch if: ${condition}` } : undefined
      for (const dep of (node.depends_on ?? []).filter(dep => ids.has(dep) && dep !== node.id)) {
        edges.push({
          id: wireId(dep, node.id),
          source: dep,
          target: node.id,
          type: 'builder',
          className: 'rg-wire rg-wire-waiting',
          style: { strokeWidth: 2 },
          deletable: editable,
          data: { removable: editable, removeLabel: zh ? '删除这条连线' : 'Remove this wire', onRemove: removeWire, carries: edgeCarries(nodes, dep, node.id), ...(branch ? { branch } : {}) } satisfies BuilderEdgeData,
        })
      }
    }
    // 一键对齐 lays the flow out from where the first lane's first card sits, every position saved absolute.
    const alignFrame: AlignFrame = {
      heights: heightOf,
      start: { x: GROUP_PADDING, y: GROUP_TITLE_HEIGHT + GROUP_PADDING },
    }
    return { seedNodes: [...groupNodes, ...itemNodes], seedEdges: edges, alignFrame, parentAt, drawn }
  }, [shownNodes, proposalDiff, zh, empName, gateText, nodeTypeText, roleName, orgRoleIds, packageSkills, librarySkills, libraryUnlockable, trialSkills, candidateIds, workflowId, editable, removeWire, healthOfNode])
  board.current = { free: drawn.free, settled: drawn.settled, shownAt: drawn.positions }
  // 4.7 AQ: a step added next to another on an arranged flow goes where it belongs in the graph (graphEdits.placeNewStep),
  // saved absolute; on a flow drawn in lanes it is left to the lane layout, as before.
  const newStepCanvas = (place: NewStepPlace, anchorId: string, height: number): WorkflowNode['canvas'] => {
    if (!drawn.free) return null
    const at = placeNewStep(place, anchorId, drawn.positions, alignFrame.heights, height)
    return at ? absoluteCanvas(at) : null
  }
  const newCardHeight = builderCardHeight(2, editable)

  // Drive React Flow from local state so a drag sticks; re-seed when the workflow data (seedNodes) changes.
  const [flowNodes, setFlowNodes, onNodesChange] = useNodesState(seedNodes)
  const [flowEdges, setFlowEdges, onEdgesChange] = useEdgesState(seedEdges)
  // A card or wire whose seed did not change keeps the object React Flow holds (seedReuse): a keystroke in one step
  // draws that step again, not all of them, and the rest keep their measured size and selection.
  const nodesSeeded = useRef<SeedMemory<Node> | null>(null)
  const edgesSeeded = useRef<SeedMemory<Edge> | null>(null)
  useEffect(() => {
    const before = nodesSeeded.current
    nodesSeeded.current = seedMemory(workflowId, seedNodes)
    setFlowNodes(current => keepUnchanged(before, workflowId, seedNodes, current, carryMeasuredAndSelected))
  }, [seedNodes, setFlowNodes, workflowId])
  useEffect(() => {
    const before = edgesSeeded.current
    edgesSeeded.current = seedMemory(workflowId, seedEdges)
    setFlowEdges(current => keepUnchanged(before, workflowId, seedEdges, current, carrySelected))
  }, [seedEdges, setFlowEdges, workflowId])

  // The element outlined under a component drag: a slot row (审核) or the whole node. We toggle a class
  // on the DOM directly (not through React state) so hover feedback stays smooth and never re-renders.
  const dropTargetRef = useRef<HTMLElement | null>(null)
  const clearDropTarget = () => {
    dropTargetRef.current?.classList.remove(DROP_TARGET_CLASS, DROP_SLOT_CLASS, DROP_WIRE_CLASS)
    dropTargetRef.current = null
  }

  const onPaletteDragStart = (event: React.DragEvent, payload: ComponentPayload) => {
    if (layout.shelf === 'drawer') {
      // Chromium cancels a drag whose source stops being hit-testable before dragstart returns, and React commits this
      // update in a microtask; so the drawer fades and lets pointers through only once the native drag has begun.
      // A timer, not an animation frame: frames do not run while the pane is hidden.
      shelfDragLive.current = true
      window.setTimeout(() => { if (shelfDragLive.current) setShelfDragging(true) }, 0)
    }
    event.dataTransfer.setData(COMPONENT_MIME, JSON.stringify(payload))
    event.dataTransfer.setData(componentKindMime(payload.kind), '1')
    event.dataTransfer.setData('text/plain', payload.name)
    event.dataTransfer.effectAllowed = 'copy'
  }

  // Anchor the node editor at a client point, clamped so it stays inside the canvas.
  const editorAt = (clientX: number, clientY: number): { x: number; top?: number; bottom?: number } | null => {
    const rect = flowWrapRef.current?.getBoundingClientRect()
    if (!rect) return null
    const x = Math.min(Math.max(8, clientX - rect.left), Math.max(8, rect.width - 248))
    const y = Math.min(Math.max(8, clientY - rect.top), rect.height - 8)
    return y > rect.height / 2 ? { x, bottom: Math.max(8, rect.height - y) } : { x, top: y }
  }

  const offerEmployee = (reference: AIEmployeeInstanceRef, nodeId: string | null, point: { x: number; y: number } | null = null) => {
    const item = currentCanvasEmployee(employeeDesign, reference)
    if (!editable || !item) { showNotice(zh ? '项目员工信息已变化，请刷新后重新选择。' : 'Refresh the project employee before choosing it.'); return }
    setEmployeePlacement({ reference, name: item.name || item.package_id, nodeId, point })
  }
  const placeEmployee = (replaceExecutor: boolean) => {
    if (!employeePlacement || !editable || !currentCanvasEmployee(employeeDesign, employeePlacement.reference)) return
    const target = replaceExecutor ? nodes.find(node => node.id === employeePlacement.nodeId) : null
    if (replaceExecutor && (!target || target.type !== 'ai_do')) return
    if (!replaceExecutor && !canAddNodes) return
    const binding = bindingForCanvasEmployee(employeePlacement.reference, target?.employee_binding)
    if (!binding) { showNotice(zh ? '员工绑定内容无效，修改未应用。' : 'Invalid employee binding.'); return }
    record()
    let id: string
    if (target) { id = target.id; onNodePatch?.(id, { employee_binding: binding }) }
    else {
      const created = { ...newNode(nodes, 'ai_do', employeePlacement.name), employee_binding: binding,
        canvas: employeePlacement.point ? absoluteCanvas(employeePlacement.point) : null }
      id = created.id
      onNodesReplace?.([...nodes, created])
    }
    setBuilderView('canvas')
    const rect = flowWrapRef.current?.getBoundingClientRect()
    const at = rect ? editorAt(rect.left + rect.width / 2, rect.top + rect.height / 3) : null
    if (at) setEditing({ id, ...at })
    setEmployeePlacement(null)
    showNotice(zh ? '已加入草稿。保存后，在左侧确认本步骤使用的项目员工，再配置并测试。' : 'Added to the draft. Save, select the project instance, then configure and test.')
  }

  const onCanvasDragOver = (event: React.DragEvent) => {
    if (builderView !== 'canvas') return // the board is not a drop surface: a step dropped there could not be seen
    if (editable && employeeDesign && event.dataTransfer.types.includes(AI_EMPLOYEE_CANDIDATE_MIME)) {
      event.preventDefault()
      event.dataTransfer.dropEffect = 'copy'
      const target = dropTargetAt(event.clientX, event.clientY).nodeEl
      if (target !== dropTargetRef.current) { clearDropTarget(); target?.classList.add(DROP_TARGET_CLASS); dropTargetRef.current = target }
      return
    }
    if (!editable || !event.dataTransfer.types.includes(COMPONENT_MIME)) return
    // a flow lands as a new (call) step, exactly like a step chip
    const addingNode = event.dataTransfer.types.includes(componentKindMime('node')) || event.dataTransfer.types.includes(componentKindMime('flow'))
    if (addingNode && !canAddNodes) return
    event.preventDefault()
    event.dataTransfer.dropEffect = 'copy'
    const target = dropTargetAt(event.clientX, event.clientY)
    // A new step dropped on a wire goes in between: the wire it would split is shown before the drop.
    const wire = addingNode && !target.nodeEl ? wireAt(event.clientX, event.clientY) : null
    if (wire) {
      const el = wire.el as HTMLElement
      if (el !== dropTargetRef.current) { clearDropTarget(); el.classList.add(DROP_WIRE_CLASS); dropTargetRef.current = el }
      return
    }
    // A new step dropped on a node chains after that whole node, and a skill joins the whole node;
    // a person dropped on a row fills that slot.
    const wholeNode = addingNode || event.dataTransfer.types.includes(componentKindMime('skill'))
    const highlight = wholeNode ? target.nodeEl : target.highlight
    if (highlight !== dropTargetRef.current) {
      clearDropTarget()
      highlight?.classList.add(!wholeNode && target.slot === 'gate' ? DROP_SLOT_CLASS : DROP_TARGET_CLASS)
      dropTargetRef.current = highlight
    }
  }

  const onCanvasDrop = (event: React.DragEvent) => {
    endShelfDrag()
    if (!editable || builderView !== 'canvas') return
    const employeeRaw = event.dataTransfer.getData(AI_EMPLOYEE_CANDIDATE_MIME)
    if (employeeRaw) {
      event.preventDefault()
      clearDropTarget()
      const reference = parseAIEmployeeCandidate(employeeRaw, employeeDesign?.project_id ?? '')
      if (!reference) { showNotice(zh ? '员工来源或项目不一致，请从左侧重新拖入。' : 'Choose an employee from this project.'); return }
      if (layout.shelf === 'drawer') setPaletteOpen(false)
      const nodeId = dropTargetAt(event.clientX, event.clientY).nodeId || null
      offerEmployee(reference, nodeId, employeeViewport.current?.screenToFlowPosition({ x: event.clientX, y: event.clientY }) ?? null)
      return
    }
    const raw = event.dataTransfer.getData(COMPONENT_MIME)
    clearDropTarget()
    if (!raw) return
    event.preventDefault()
    // On a narrow canvas the drawer would come back over the result, the new step's editor and any refusal notice.
    if (layout.shelf === 'drawer') setPaletteOpen(false)
    let payload: ComponentPayload
    try { payload = JSON.parse(raw) as ComponentPayload } catch { return }
    const { nodeId, slot } = dropTargetAt(event.clientX, event.clientY)
    // On a wire: the new step goes in between its two steps (graphEdits.insertOnWire), the rest of the graph as it was.
    const wire = !nodeId && (payload.kind === 'node' || payload.kind === 'flow') && canAddNodes ? wireAt(event.clientX, event.clientY) : null
    if (wire) {
      if (payload.kind === 'flow') {
        const rejection = flowCallRejection(workflowId, payload.id, flows)
        if (rejection) {
          showNotice(rejection === 'self'
            ? (zh ? '流程不能调用它自己' : 'A workflow cannot call itself')
            : (zh ? `「${payload.name}」已经会（间接）调用本流程，再调用它会循环下单` : `"${payload.name}" already calls this workflow, so calling it would loop`))
          return
        }
      }
      let created: WorkflowNode
      if (payload.kind === 'flow') created = newInvokeNode(nodes, payload.id, zh ? `调用：${payload.name}` : `Call: ${payload.name}`, '')
      else if (payload.kind === 'node') created = newNode(nodes, payload.id, zh ? '新步骤' : 'New step')
      else return
      created = { ...created, canvas: newStepCanvas('after', wire.source, newCardHeight) }
      const next = insertOnWire(nodes, wire.source, wire.target, created)
      if (!next) return
      record()
      onNodesReplace?.(next)
      const at = editorAt(event.clientX, event.clientY)
      if (at) setEditing({ id: created.id, ...at })
      return
    }
    if (payload.kind === 'flow') {
      if (!canAddNodes) return
      const rejection = flowCallRejection(workflowId, payload.id, flows)
      if (rejection) {
        showNotice(rejection === 'self'
          ? (zh ? '流程不能调用它自己' : 'A workflow cannot call itself')
          : (zh ? `「${payload.name}」已经会（间接）调用本流程，再调用它会循环下单` : `"${payload.name}" already calls this workflow, so calling it would loop`))
        return
      }
      const created = { ...newInvokeNode(nodes, payload.id, zh ? `调用：${payload.name}` : `Call: ${payload.name}`, nodeId), canvas: nodeId ? newStepCanvas('after', nodeId, newCardHeight) : null }
      record()
      onNodesReplace?.([...nodes, created])
      const at = editorAt(event.clientX, event.clientY)
      if (at) setEditing({ id: created.id, ...at })
      return
    }
    if (payload.kind === 'node') {
      if (!canAddNodes) return
      // On empty canvas: a standalone step. On a node: a step that runs after it.
      const created = { ...newNode(nodes, payload.id, zh ? '新步骤' : 'New step', nodeId), canvas: nodeId ? newStepCanvas('after', nodeId, newCardHeight) : null }
      record()
      onNodesReplace?.([...nodes, created])
      const at = editorAt(event.clientX, event.clientY)
      if (at) setEditing({ id: created.id, ...at })
      return
    }
    if (!nodeId) return
    const patch = patchForComponent(payload, slot, nodes.find(node => node.id === nodeId))
    if (patch) {
      record()
      onNodePatch?.(nodeId, patch)
    }
  }

  // The same two additions a drop makes, without the drag: a click or Enter on a shelf chip. The new step runs
  // after the step being edited (the person's current place), or stands alone; its editor opens in the canvas so
  // the keyboard lands where the step is filled in. Dragging needs a mouse; this does not.
  const editorAtCentre = () => {
    const rect = flowWrapRef.current?.getBoundingClientRect()
    return rect ? editorAt(rect.left + rect.width / 2, rect.top + rect.height / 3) : null
  }
  const addFromShelf = (payload: ComponentPayload) => {
    if (!canAddNodes) return
    setBuilderView(view => viewAfter(view, 'add_step'))
    const after = editing?.id ?? ''
    let created: WorkflowNode
    if (payload.kind === 'flow') {
      const rejection = flowCallRejection(workflowId, payload.id, flows)
      if (rejection) {
        showNotice(rejection === 'self'
          ? (zh ? '流程不能调用它自己' : 'A workflow cannot call itself')
          : (zh ? `「${payload.name}」已经会（间接）调用本流程，再调用它会循环下单` : `"${payload.name}" already calls this workflow, so calling it would loop`))
        return
      }
      created = newInvokeNode(nodes, payload.id, zh ? `调用：${payload.name}` : `Call: ${payload.name}`, after)
    } else if (payload.kind === 'node') {
      created = newNode(nodes, payload.id, zh ? '新步骤' : 'New step', after)
    } else {
      return
    }
    if (after) created = { ...created, canvas: newStepCanvas('after', after, newCardHeight) }
    if (layout.shelf === 'drawer') setPaletteOpen(false)
    record()
    onNodesReplace?.([...nodes, created])
    const at = editorAtCentre()
    if (at) { pendingFocusField.current = 'role'; setFocusTick(tick => tick + 1); setEditing({ id: created.id, ...at }) }
  }
  const shelfKey = (payload: ComponentPayload) => (event: React.KeyboardEvent) => {
    if (event.key !== 'Enter' && event.key !== ' ') return
    event.preventDefault()
    addFromShelf(payload)
  }

  // Ctrl/⌘+D and the editor's button: a copy of the step being edited, right after it, with its editor open.
  const duplicateEditing = () => {
    if (!canAddNodes || !editing) return
    const result = duplicateNode(nodes, editing.id, zh)
    if (!result) return
    // On an arranged flow the copy goes a row under the original (not on top of it, nor back into a lane).
    const canvas = newStepCanvas('copy', editing.id, alignFrame.heights?.get(editing.id) ?? newCardHeight)
    record()
    onNodesReplace?.(canvas ? result.nodes.map(node => (node.id === result.created.id ? { ...node, canvas } : node)) : result.nodes)
    setEditing(current => (current ? { ...current, id: result.created.id } : current))
  }
  duplicateRef.current = duplicateEditing
  editingIdRef.current = editing?.id ?? ''

  // 前加 / 后加 (4.7 Q02): a new step between the one being edited and its neighbours -- what waited for it now waits
  // for the new step (后加), or the new step takes over what it waited for (前加). Not a branch beside it. The graph
  // rules are workspace/graphEdits (unit-tested); the save re-validates dependencies and refuses a cycle.
  // What goes in is chosen first, as the prototype asks (节点能力): a kind of step, one of this flow's own skills, or a
  // call to another flow. Everything else about it is filled in on the editor that opens on it.
  const insertBeside = (where: 'before' | 'after', choice: InsertChoice = { kind: 'type', type: 'ai_do' }) => {
    if (!canAddNodes || !editing) return
    setInsertMenu(null)
    let created: WorkflowNode
    if (choice.kind === 'flow') {
      const rejection = flowCallRejection(workflowId, choice.id, flows)
      if (rejection) { showNotice(rejection === 'self' ? (zh ? '流程不能调用它自己' : 'A workflow cannot call itself') : (zh ? `「${choice.name}」已经会（间接）调用本流程，再调用它会循环下单` : `"${choice.name}" already calls this workflow, so calling it would loop`)); return }
      created = newInvokeNode(nodes, choice.id, zh ? `调用：${choice.name}` : `Call: ${choice.name}`, '')
    } else if (choice.kind === 'skill') {
      created = { ...newNode(nodes, 'ai_do', choice.id), skills: [choice.id] }
    } else {
      created = newNode(nodes, choice.type, zh ? '新步骤' : 'New step')
    }
    // 4.7 AQ: on an arranged flow, 后加 one layer right of this step and 前加 one layer left of it, moved down off any
    // card already there -- never on top of one.
    created = { ...created, canvas: newStepCanvas(where, editing.id, newCardHeight) }
    const next = where === 'after' ? insertAfter(nodes, editing.id, created) : insertBefore(nodes, editing.id, created)
    if (!next) return
    record()
    onNodesReplace?.(next)
    pendingFocusField.current = 'role'
    setFocusTick(tick => tick + 1)
    setEditing(current => (current ? { ...current, id: created.id } : current))
  }

  // 一键对齐 (4.7 A02): every step laid out by dependency layer, left to right, branches centred on the step they leave;
  // only positions change, dependencies are untouched (graphEdits.alignAll). One undo step.
  const alignSteps = () => {
    if (!editable || !onNodesReplace) return
    const { nodes: aligned, moved } = alignAll(nodes, alignFrame)
    if (!moved.length) {
      showNotice(zh ? '步骤已经按先后排好了，不用再对齐' : 'The steps are already laid out by dependency')
      return
    }
    record()
    onNodesReplace(aligned)
    showNotice(zh ? `已按先后排好 ${moved.length} 个步骤；保存草稿后保留，可撤销` : `${moved.length} steps laid out by dependency; kept when the draft is saved, and undoable`)
  }

  // ③ connections: drag from a node's output port (right) to another's input (left) to add a
  // depends_on edge; delete a selected edge to drop it. Edges are derived from depends_on (single
  // source of truth), so patching the draft re-renders them -- no separate edge state to reconcile.
  // The self/dup/cycle guard and per-target delete aggregation live in workflowGraph (unit-tested).
  const onConnect = (conn: Connection) => {
    const rejection = connectRejection(nodes, conn.source, conn.target)
    if (rejection) {
      // A refused wire just snaps back, which reads as "nothing happened" -- say why instead.
      if (rejection !== 'missing') {
        showNotice({
          self: zh ? '不能把步骤连到它自己' : 'A step cannot depend on itself',
          duplicate: zh ? '这两步已经连着了' : 'These steps are already wired',
          cycle: zh ? '这条连线会让步骤互相等待（成环），没有加上' : 'That wire would make the steps wait on each other (a cycle); not added',
        }[rejection])
      }
      return
    }
    const result = connectPatch(nodes, conn.source, conn.target)
    if (!result) return
    record()
    onNodePatch?.(result.nodeId, result.patch)
  }

  const wireVerdictsRef = useRef<ReturnType<typeof wireTargets> | null>(null)
  // While a wire is being dragged, every step says whether it may land there: a step it cannot reach (already
  // wired, or a loop) goes dim before the drop instead of refusing after it. Written straight onto the card
  // elements: re-seeding React Flow's nodes in the middle of a connection would cancel the drag.
  const clearWireTargets = () => {
    wireVerdictsRef.current = null
    flowWrapRef.current?.querySelectorAll<HTMLElement>('.react-flow__node[data-wire]').forEach(element => element.removeAttribute('data-wire'))
    flowWrapRef.current?.removeAttribute('data-wiring')
  }
  const markWireTargets = (fromId: string | null, handle: 'source' | 'target' | null) => {
    clearWireTargets()
    const wrap = flowWrapRef.current
    if (!wrap || !fromId || !handle || !editable) return
    const verdicts = wireTargets(nodes, fromId, handle)
    // Kept for the cards that come into view mid-drag on a big flow (the observer below marks them as they mount).
    wireVerdictsRef.current = verdicts
    wrap.setAttribute('data-wiring', 'true')
    wrap.querySelectorAll<HTMLElement>('.react-flow__node').forEach(element => {
      const verdict = verdicts.get(element.getAttribute('data-id') ?? '')
      if (verdict) element.setAttribute('data-wire', verdict === 'ok' ? 'ok' : 'refused')
    })
  }

  // Removing wires, from Backspace on a selected edge or the ✕ on it: one undo step, one patch per target.
  const removeWires = (wires: readonly { source?: string | null; target?: string | null }[]) => {
    const patches = disconnectPatches(nodes, wires)
    if (patches.length) record()
    for (const { nodeId, patch } of patches) onNodePatch?.(nodeId, patch)
  }
  const onEdgesDelete = (deleted: Edge[]) => removeWires(deleted)
  removeWireImpl.current = (source, target) => removeWires([{ source, target }])

  // 4.4 §2.5 点击即可快改: a click on a step opens its editor (React Flow does not report a click after a
  // drag). A click on a port is left to wiring, which can also start from a click.
  openEditorImpl.current = (event: ReactMouseEvent, node: Node) => {
    if (!editable || node.type !== 'runNode') return
    if ((event.target as HTMLElement | null)?.closest('.react-flow__handle')) return
    // Ctrl/⌘/Shift-click is React Flow's selection gesture (pick several steps to move together), not an edit.
    if (event.ctrlKey || event.metaKey || event.shiftKey) return
    const at = editorAt(event.clientX, event.clientY)
    if (at) setEditing({ id: node.id, ...at })
  }

  // From the blocker strip: open that step's editor next to its card and put the cursor in the field in question.
  const pendingFocusField = useRef('')
  // The step a field's page was just opened for: opening that step must not put the page back to the default.
  const keepTabForRef = useRef('')
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>(DEFAULT_INSPECTOR_TAB)
  const [focusTick, setFocusTick] = useState(0)
  const focusNodeField = (nodeId: string, field: string) => {
    const wrap = flowWrapRef.current
    if (!wrap || !nodes.some(node => node.id === nodeId)) return
    // A wire, a loop or an id is not something the step editor can change: say where the fix is.
    if (!isEditorField(field)) { showNotice(fixHint({ field, code: '' }, zh)); return }
    const card = wrap.querySelector<HTMLElement>(`.react-flow__node[data-id="${CSS.escape(nodeId)}"]`)
    const wrapRect = wrap.getBoundingClientRect()
    const rect = card?.getBoundingClientRect()
    // A card scrolled or zoomed out of view still gets its editor: in the middle of the canvas.
    const visible = rect && rect.right > wrapRect.left && rect.left < wrapRect.right && rect.bottom > wrapRect.top && rect.top < wrapRect.bottom
    const at = visible && rect
      ? editorAt(rect.right + 8, rect.top)
      : editorAt(wrapRect.left + wrapRect.width / 2, wrapRect.top + wrapRect.height / 3)
    if (!at) return
    pendingFocusField.current = field
    keepTabForRef.current = nodeId
    setInspectorTab(tabForField(field))
    // The editor may already be open on this very step: the tick makes the focus effect run again anyway.
    setFocusTick(tick => tick + 1)
    setEditing(current => (current?.id === nodeId ? current : { id: nodeId, ...at }))
  }
  // 定位当前流程节点: the flow's own steps, each a way to that step's editor (and into view). Built from the node table,
  // not from the cards being dragged, so a drag step does not rebuild one button per step.
  const focusNodeFieldRef = useRef(focusNodeField)
  focusNodeFieldRef.current = focusNodeField
  const shelfOutline = useMemo(() => (nodes.length > 0 ? (
    <details className="wf-shelf-outline" data-testid="shelf-outline">
      <summary>{zh ? '定位当前流程节点' : 'Find a step of this flow'}<small>{nodes.length}</small></summary>
      {nodes.map(node => (
        <button key={node.id} type="button" data-node={node.id} onClick={() => { focusNodeFieldRef.current(node.id, 'role'); if (layout.shelf === 'drawer') setPaletteOpen(false) }}>
          <span>{node.name || node.id}</span><small>{nodeTypeText(node.type)}</small>
        </button>
      ))}
    </details>
  ) : null), [nodes, zh, nodeTypeText, layout.shelf])
  // 测试's 去试跑 / 记录试跑结果: this view, and this step opened on this page (a trial starts on its 交付 page).
  const builderRequestSeq = builderRequest?.seq ?? 0
  useEffect(() => {
    if (!builderRequest) return
    setBuilderView(builderRequest.view)
    const stepId = builderRequest.nodeId ?? ''
    if (!stepId || !editable || !nodes.some(node => node.id === stepId)) return
    keepTabForRef.current = stepId
    if (builderRequest.tab) setInspectorTab(builderRequest.tab)
    // After the view switch has rendered: the editor is placed against the canvas, which may have been covered.
    const timer = window.setTimeout(() => {
      const at = editorAtCentre()
      if (at) setEditing({ id: stepId, ...at })
    }, 30)
    return () => window.clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [builderRequestSeq])
  useEffect(() => {
    const field = pendingFocusField.current
    if (!editing || !field) return
    pendingFocusField.current = ''
    // The control itself, or -- when it is not on screen in the step's present set-up -- the nearest section it lives in.
    const target = fieldFallbacks(field)
      .map(candidate => flowWrapRef.current?.querySelector<HTMLElement>(`.wf-node-edit [data-field="${CSS.escape(candidate)}"]`) ?? null)
      .find(Boolean)
    target?.focus()
    target?.scrollIntoView({ block: 'nearest' })
  }, [editing, focusTick])

  // Esc closes the in-place editor; if its node disappears from the draft, close too.
  useEffect(() => {
    if (!editing) return
    if (!nodes.some(node => node.id === editing.id)) { setEditing(null); return }
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setEditing(null) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [editing, nodes])

  const editingNode = editing ? nodes.find(node => node.id === editing.id) ?? null : null
  // A pending "remove this step?" belongs to the node it was asked about; switching nodes drops it.
  const editingId = editing?.id ?? ''
  useEffect(() => { setConfirmDeleteId(''); setInsertMenu(null) }, [editingId])
  const focusStepRef = useRef(onFocusStep)
  focusStepRef.current = onFocusStep
  useEffect(() => { focusStepRef.current?.(editingId) }, [editingId])
  // The step whose inspector is open wears a mark on its card. A data attribute set on the DOM, as the wire targets
  // are: every patch re-seeds the nodes and React rewrites their class names, so a class would be gone after the first
  // keystroke in the inspector. Re-applied whenever the nodes change (a freshly dropped step has no element before).
  useEffect(() => {
    const wrap = flowWrapRef.current
    if (!wrap) return
    for (const element of wrap.querySelectorAll<HTMLElement>('.react-flow__node[data-inspected]')) element.removeAttribute('data-inspected')
    if (!editingId) return
    const card = wrap.querySelector<HTMLElement>(`.react-flow__node[data-id="${CSS.escape(editingId)}"]`)
    card?.setAttribute('data-inspected', '')
  }, [editingId, flowNodes])
  // On a big flow (visibleOnly) a card that comes back into view is a new element: give it its marks as it mounts --
  // the open step's, and a dragged wire's verdict -- before it is painted (mutation callbacks run before the frame).
  useEffect(() => {
    const container = flowWrapRef.current?.querySelector('.react-flow__nodes')
    if (!visibleOnly || !container) return
    const observer = new MutationObserver(records => {
      for (const record of records) {
        for (const added of record.addedNodes) {
          if (added instanceof HTMLElement && added.classList.contains('react-flow__node')) {
            applyCardMarks(added, { inspected: editingIdRef.current, wire: wireVerdictsRef.current })
          }
        }
      }
    })
    observer.observe(container, { childList: true })
    return () => observer.disconnect()
  }, [visibleOnly])
  // Another step opens on its set-up, not on the page the last one was left on -- unless it was opened to fix a field.
  useEffect(() => {
    const kept = keepTabForRef.current === editingId
    keepTabForRef.current = ''
    if (!kept) setInspectorTab(DEFAULT_INSPECTOR_TAB)
  }, [editingId])

  // Expanded mode: the canvas lifts out of the workflow card into a near-full-window layer, so building
  // is not squeezed into the card's column. Esc collapses it (after first closing an open node editor).
  const [expanded, setExpanded] = useState(false)
  useEffect(() => {
    if (!expanded || editing) return
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setExpanded(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [expanded, editing])

  // The canvas owns these keys while it is expanded or while the keyboard is inside it: Ctrl/⌘+Z undoes a canvas
  // step, Ctrl/⌘+Shift+Z or Ctrl+Y redoes it, Ctrl/⌘+D copies the step being edited. Inside a text field or select
  // the browser's own undo is left alone -- and Ctrl+D never reaches the browser's bookmark dialog from the canvas.
  useEffect(() => {
    if (!canAddNodes) return
    const onKey = (event: KeyboardEvent) => {
      // A key event can be aimed at the window itself, which is not an element and has no closest().
      const target = event.target instanceof HTMLElement ? event.target : null
      const shortcut = canvasShortcut({
        key: event.key, ctrl: event.ctrlKey, meta: event.metaKey, shift: event.shiftKey, alt: event.altKey,
        inField: Boolean(target?.closest('input, textarea, select, [contenteditable="true"]')),
        insideCanvas: Boolean(target && canvasRef.current?.contains(target)),
        expanded,
      })
      if (!shortcut) return
      // Copying needs a step to copy; without one the key is the browser's.
      if (shortcut === 'duplicate' && !editingIdRef.current) return
      event.preventDefault()
      if (shortcut === 'duplicate') duplicateRef.current()
      else if (shortcut === 'redo') redoRef.current()
      else undoRef.current()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [expanded, canAddNodes])

  // The panel's tabs are the kinds of thing a flow can really take. A search looks through all of them at once.
  // (Both hooks stay above the early return below: an empty draft that becomes editable -- a flow draft opened from
  // 安安, a recovered draft -- would otherwise call more hooks than the render before it and crash the page.)
  const [shelfMode, setShelfMode] = useState<CatalogMode>('capability')
  // Which groups of the tree are open, per mode. The group of what is already wired starts open.
  const [openCatalogGroups, setOpenCatalogGroups] = useState<ReadonlySet<string>>(() => new Set(['capability:本流程已接通的技能', 'capability:人工参与']))
  // 我的常用 ★ / 最近使用: this viewer's own, in the browser (never shared, never needed for anything to work).
  const [shelfFavourites, setShelfFavourites] = useState<string[]>(() => readShelfList(SHELF_FAVOURITES))
  const [shelfRecent, setShelfRecent] = useState<string[]>(() => readShelfList(SHELF_RECENT))
  // Read-only and empty: nothing to show. Editable and empty: the shelf and the toolbar are how the first step is
  // added and how removing the last one is undone, so the canvas stays.
  if (!nodes.length && !canAddNodes) return null
  const showPalette = editable
  const shelfSearching = shelfQuery.trim() !== ''
  const catalogReal = {
    skills: packageSkills ?? [],
    flows: flows.filter(flow => flow.id !== workflowId).map(flow => ({ id: flow.id, name: flow.name || flow.id, blocked: flowCallRejection(workflowId, flow.id, flows) === 'cycle' })),
    canAddNodes: Boolean(canAddNodes),
  }
  // A search looks through all three ways of sorting at once; otherwise the chosen one.
  const catalog: Array<ReturnType<typeof catalogTree>[number] & { mode: CatalogMode | 'remembered' }> = shelfSearching
    ? CATALOG_MODES.flatMap(mode => catalogTree(mode, catalogReal, shelfQuery).map(item => ({ ...item, mode })))
    : [
      ...rememberedGroups(catalogReal, shelfFavourites, shelfRecent, zh).map(item => ({ ...item, mode: 'remembered' as const })),
      ...catalogTree(shelfMode, catalogReal).map(item => ({ ...item, mode: shelfMode })),
    ]
  const rememberUse = (payload: ComponentPayload) => setShelfRecent(current => {
    const next = withRecent(current, payloadKey(payload))
    writeShelfList(SHELF_RECENT, next)
    return next
  })
  const toggleFavourite = (payload: ComponentPayload) => setShelfFavourites(current => {
    const next = toggledKey(current, payloadKey(payload))
    writeShelfList(SHELF_FAVOURITES, next)
    return next
  })
  const shelfFound = catalog.reduce((sum, item) => sum + item.rows.length, 0)
  const catalogDone = catalogProgress(catalogReal)
  return (
    <>
    {expanded && <div className="wf-canvas-backdrop" onClick={() => setExpanded(false)} aria-hidden="true" />}
    <div ref={canvasRef} className={`wf-canvas${expanded ? ' is-expanded' : ''}${layout.narrow ? ' is-narrow' : ''}${builderView === 'package' ? ' is-package-view' : ''}`} data-testid="workflow-canvas" data-layout={layout.narrow ? 'narrow' : 'wide'}>
      {builderView !== 'package' && showPalette && paletteOpen && layout.shelf === 'drawer' && !shelfDragging && (
        <div className="wf-palette-scrim" onClick={() => setPaletteOpen(false)} aria-hidden="true" />
      )}
      {builderView !== 'package' && showPalette && paletteOpen && (
        // 组件架 (v4.4 §2.4): the builder's side shelf -- a drawer over the canvas when it is narrow.
        <aside className={`wf-palette${layout.shelf === 'drawer' ? ' is-drawer' : ''}${shelfDragging ? ' is-dragging' : ''}`} aria-label={zh ? '组件' : 'Components'} data-searching={shelfSearching || undefined}
          onDragEnd={endShelfDrag} onDragStartCapture={event => {
            if (layout.shelf !== 'drawer' || !(event.target instanceof HTMLElement) || !event.target.closest('.ai-employee-shelf__card')) return
            shelfDragLive.current = true
            window.setTimeout(() => { if (shelfDragLive.current) setShelfDragging(true) }, 0)
          }}>
          <div className="wf-palette-head">
            <span>{zh ? '组件' : 'Components'}</span>
            <button type="button" className="wf-palette-toggle" onClick={() => { foldedByNarrowing.current = false; setPaletteOpen(false) }} aria-label={zh ? '收起组件栏' : 'Collapse'}>‹</button>
          </div>
          {employeeDesign && <AIEmployeeShelfHost design={employeeDesign} workflowId={workflowId}
            savedRevision={employeeSavedRevision} draftOpen={draftOpen} node={editingNode} editable={editable}
            onCandidate={reference => offerEmployee(reference, editing?.id ?? null)}
            onBinding={(nodeId, value) => { if (editable) { record(); onNodePatch?.(nodeId, { employee_binding: value }) } }} />}
          <input type="search" className="wf-palette-search" value={shelfQuery} onChange={event => setShelfQuery(event.target.value)}
            onKeyDown={event => { event.stopPropagation(); if (event.key === 'Escape') setShelfQuery('') }}
            placeholder={zh ? '搜索组件、平台、用途' : 'Search components, platforms, uses'} aria-label={zh ? '搜索组件' : 'Search components'} />
          {shelfSearching && shelfFound === 0 && onOpenLibrary && (
            // Finding nothing here is exactly the moment to go and admit something, so say it with a way there.
            <button type="button" className="wf-palette-library wf-palette-library--empty" data-testid="shelf-library-empty" onClick={onOpenLibrary}>
              {zh ? '去「组件库」找并准入 ↗' : 'Find and admit one in Components ↗'}
            </button>
          )}
          {shelfSearching && shelfFound === 0 && (
            <div className="wf-palette-empty" role="status">{zh ? '这里没有匹配的组件。更多组件在「组件库」里准入后才会出现在这里。' : 'Nothing here matches. More components appear here once admitted in the library.'}</div>
          )}
          {brought && broughtState && (
            // 调用 from 组件库: the component the owner came here with, first on the shelf until dismissed.
            <div className="wf-palette-group wf-palette-group--brought" data-testid="palette-brought" data-state={broughtState}>
              <div className="wf-palette-group-title">
                {zh ? '从组件库带来' : 'From Components'}
                {onDismissBrought && (
                  <button type="button" className="wf-palette-dismiss" onClick={onDismissBrought} aria-label={zh ? '不用了，移出组件架' : 'Remove from the shelf'}>✕</button>
                )}
              </div>
              <div className="wf-palette-hint">
                {broughtState === 'declared'
                  ? (zh ? '本流程包已声明这个技能：拖到节点上使用' : 'This package declares it already: drop it on a step')
                  : broughtState === 'on_save'
                    ? (zh ? '拖到节点上使用；保存时声明到流程包并锁定版本' : 'Drop it on a step; the save declares it in the package and locks its version')
                    : broughtState === 'trial'
                      ? (zh ? '试用中：拖到步骤上看搭配，在下面写测试结论；录入内部库后这份草稿才能保存' : 'On trial: drop it on a step to see how it fits and record the test below; the draft saves once it is admitted')
                    : broughtState === 'refused'
                      ? (zh ? '这个流程包现在不能锁定它（发布契约不完整）：先补齐契约再来' : 'This package cannot lock it now (incomplete release contract): complete the contract first')
                      : (zh ? '这个流程包不从内部库取技能：放上去也不会安装' : 'This package does not take skills from the internal library: it would not be installed')}
              </div>
              <div
                className={`wf-chip wf-chip--skill is-brought${broughtState === 'undeclared' || broughtState === 'refused' ? ' is-disabled' : ''}`}
                draggable={broughtState === 'declared' || broughtState === 'on_save' || broughtState === 'trial'}
                aria-disabled={broughtState === 'undeclared' || broughtState === 'refused' || undefined}
                onDragStart={broughtState === 'declared' || broughtState === 'on_save' || broughtState === 'trial' ? event => onPaletteDragStart(event, { kind: 'skill', id: brought.id, name: brought.name }) : undefined}
                title={brought.id}
              >
                <span className="wf-chip-name">{brought.name}</span>
                {brought.name !== brought.id && <span className="wf-chip-sub">{brought.id}</span>}
              </div>
              {broughtExtra}
            </div>
          )}
          {editable && candidatesProp && candidatesProp.length > 0 && (
            // Master control's suggestions from outside the library: each is tried (调用测试) and admitted before the draft saves.
            <div className="wf-palette-group wf-palette-group--candidates" data-testid="palette-candidates">
              <div className="wf-palette-group-title">{zh ? '总控建议的外部组件' : 'Suggested from outside'}</div>
              <div className="wf-palette-hint">{zh ? '已在步骤上。逐个调用测试、录入内部库，或从步骤上去掉，草稿才能保存' : 'Already on steps. Try and admit each, or take it off the steps, before the draft saves'}</div>
              {candidatesProp.map(candidate => (
                <div key={candidate.id} className="wf-candidate" data-testid="palette-candidate" data-id={candidate.id} data-source={candidate.source}>
                  <span className="wf-chip-name" title={candidate.summary || candidate.id}>{candidate.name}</span>
                  <span className="wf-chip-sub">{candidate.source === 'skillhub' ? 'SkillHub' : (zh ? '本地资源库' : 'Local catalog')} · {candidate.node_ids.join('、')}</span>
                  {candidateActions?.(candidate)}
                </div>
              ))}
            </div>
          )}
          {canAddNodes && !shelfSearching && (
            // One way to add a step. What kind of step it is -- AI 做 / 人做 / 人审 -- is chosen in the step's own popup,
            // which opens on the new step straight away.
            <div className="wf-shelf-add" role="button" tabIndex={0} draggable data-testid="shelf-add-step"
              aria-label={zh ? '添加步骤' : 'Add a step'}
              onClick={() => addFromShelf({ kind: 'node', id: 'ai_do', name: nodeTypeText('ai_do') })}
              onKeyDown={shelfKey({ kind: 'node', id: 'ai_do', name: nodeTypeText('ai_do') })}
              onDragStart={event => onPaletteDragStart(event, { kind: 'node', id: 'ai_do', name: nodeTypeText('ai_do') })}>
              <strong>{zh ? '＋ 添加步骤' : '+ Add a step'}</strong>
              <small>{zh ? '点一下接在正在编辑的步骤后面，也可以拖到画布上。AI 做 / 人做 / 人审在步骤的小窗里选。' : 'Click to add after the step being edited, or drag onto the canvas. AI / person / review is chosen in the step’s popup.'}</small>
            </div>
          )}
          <div className="wf-shelf-tabs" role="group" aria-label={zh ? '组件分类方式' : 'Ways of sorting'} hidden={shelfSearching}>
            {CATALOG_MODES.map(mode => (
              <button key={mode} type="button" data-shelf-tab={mode} aria-pressed={shelfMode === mode} className={shelfMode === mode ? 'is-active' : ''} onClick={() => setShelfMode(mode)}>{CATALOG_MODE_LABELS[mode][zh ? 0 : 1]}</button>
            ))}
          </div>
          {!shelfSearching && (
            <div className="wf-palette-hint">{shelfMode === 'capability'
              ? (zh ? '按要完成的动作找组件。标「待接入」的已经设计、还没接通，放不进流程。' : 'Find a component by what it does. “Planned” ones are designed, not built, and cannot be placed.')
              : shelfMode === 'platform'
                ? (zh ? '企业常用平台的接口。目前都还没接通，逐个接入并验证后才会变成可用。' : 'Interfaces of the platforms companies use. None is wired yet; each becomes available once connected and verified.')
                : (zh ? '把前两类按行业归好，方便查找。' : 'The first two, sorted by trade.')}</div>
          )}
          {catalog.map(item => {
            const groupKey = `${item.mode}:${item.group}`
            const open = shelfSearching || item.mode === 'remembered' || openCatalogGroups.has(groupKey)
            return (
              <div key={groupKey} className="wf-shelf-group" data-open={open} data-group={item.group}>
                <button type="button" className="wf-shelf-group-title" aria-expanded={open} disabled={shelfSearching}
                  onClick={() => setOpenCatalogGroups(current => { const next = new Set(current); if (next.has(groupKey)) next.delete(groupKey); else next.add(groupKey); return next })}>
                  <span>{item.group}</span>
                  <small>{item.available > 0 ? (zh ? `${item.available} 可用 · ` : `${item.available} ready · `) : ''}{item.rows.length}</small>
                  <i aria-hidden="true">›</i>
                </button>
                {open && item.rows.map(row => {
                  const payload = row.payload
                  if (!payload) {
                    // Designed, not built: listed and searchable, never placeable.
                    return (
                      <div key={row.key} className="wf-chip is-planned" aria-disabled="true" data-status="planned" title={zh ? `${row.name} · 已设计，待接入` : `${row.name} · designed, not built`}>
                        <span className="wf-chip-name">{row.name}</span>
                        <span className="wf-chip-tag">{zh ? '待接入' : 'planned'}</span>
                      </div>
                    )
                  }
                  // A skill goes onto a step (drag); a flow to call or a person's step is a new step (click or drag).
                  const adds = payload.kind !== 'skill'
                  return (
                    <div key={row.key} className={`wf-chip wf-chip--${payload.kind === 'skill' ? 'skill' : payload.kind === 'flow' ? 'flow' : 'step'}`} data-status="available" draggable
                      role={adds ? 'button' : undefined} tabIndex={adds ? 0 : undefined} title={row.sub || row.name}
                      onClick={adds ? () => { rememberUse(payload); addFromShelf(payload) } : undefined}
                      onKeyDown={adds ? event => { if (event.key === 'Enter' || event.key === ' ') rememberUse(payload); shelfKey(payload)(event) } : undefined}
                      onDragStart={event => { rememberUse(payload); onPaletteDragStart(event, payload) }}>
                      <span className="wf-chip-name">{row.name}</span>
                      {row.sub && <span className="wf-chip-sub">{row.sub}</span>}
                      {(() => {
                        const starred = shelfFavourites.includes(payloadKey(payload))
                        return (
                          <button type="button" className={`wf-chip-star${starred ? ' is-on' : ''}`} data-testid="shelf-star" draggable={false} aria-pressed={starred}
                            title={starred ? (zh ? '从我的常用里去掉' : 'Remove from favourites') : (zh ? '加到我的常用' : 'Add to favourites')}
                            onClick={event => { event.stopPropagation(); toggleFavourite(payload) }}>{starred ? '★' : '☆'}</button>
                        )
                      })()}
                      {/* ⓘ: the skill's own read-only detail, over the canvas (the prototype's row ⓘ). */}
                      {payload.kind === 'skill' && onViewSkill && (
                        <button type="button" className="wf-chip-info" data-testid="shelf-skill-info" draggable={false}
                          title={zh ? `查看 ${row.name}` : `Open ${row.name}`} aria-label={zh ? `查看 ${row.name}` : `Open ${row.name}`}
                          onClick={event => { event.stopPropagation(); onViewSkill(payload.id) }}>ⓘ</button>
                      )}
                      <span className="wf-chip-grip" aria-hidden="true">⠿</span>
                    </div>
                  )
                })}
              </div>
            )
          })}
          {!shelfSearching && shelfOutline}
          {onOpenLibrary && (
            <button type="button" className="wf-palette-library" data-testid="shelf-library" onClick={onOpenLibrary}>
              {zh ? '发现更多组件 ↗' : 'Find more components ↗'}
              <small data-testid="shelf-progress">{zh ? `目录里已接通 ${catalogDone.available} / ${catalogDone.total}；新技能在组件库准入后出现在「能力」里` : `${catalogDone.available} of ${catalogDone.total} in the catalogue are wired; a new skill appears under Capabilities once admitted`}</small>
            </button>
          )}
        </aside>
      )}
      <div ref={flowWrapRef} className="wf-canvas-flow run-graph" onDragOver={onCanvasDragOver} onDrop={onCanvasDrop} onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as HTMLElement | null)) clearDropTarget() }}>
        {builderView !== 'package' && showPalette && !paletteOpen && (
          <button type="button" className="wf-palette-reopen" onClick={() => setPaletteOpen(true)}>{zh ? '组件 ›' : 'Components ›'}</button>
        )}
        {notice && <div className="wf-canvas-notice" role="status">{notice}</div>}
        {editable && <WorkflowBlockerStrip items={health} zh={zh} onFocus={focusNodeField} />}
        {nodes.length === 0 && canAddNodes && (
          <div className="wf-canvas-empty" role="note">{zh ? '还没有步骤：打开左侧「组件」，点「＋ 添加步骤」加第一步；是 AI 做、人做还是人审，在步骤的小窗里选。' : 'No steps yet: open Components and click “+ Add a step”; choose AI, person or review in the step’s popup.'}</div>
        )}
        {proposal && proposalDiff && (
          <div className="wf-proposal" role="region" aria-label={t('workflows.proposal.title')} data-testid="canvas-proposal">
            <div className="wf-proposal-head">
              <strong>{t('workflows.proposal.title')}</strong>
              <span>v{proposal.baseVersion} → v{proposal.newVersion}{proposal.changeNote ? `「${proposal.changeNote}」` : ''}</span>
            </div>
            <div className="wf-proposal-counts">
              {t('workflows.proposal.counts', { added: proposalDiff.added.length, removed: proposalDiff.removed.length, changed: proposalDiff.changed.length })}
              {proposalDiff.moved.length > 0 && ` · ${t('workflows.proposal.moved', { moved: proposalDiff.moved.length })}`}
            </div>
            {proposalDiff.removed.length > 0 && (
              <div className="wf-proposal-removed">
                {t('workflows.proposal.removedList', { names: proposalDiff.removed.map(id => proposal.baseNodes.find(node => node.id === id)?.name || id).join(zh ? '、' : ', ') })}
              </div>
            )}
            {proposal.stale && <div className="wf-proposal-warn" role="alert">{t('workflows.proposal.stale', { base: proposal.baseVersion, live: proposal.liveVersion })}</div>}
            {!proposal.stale && proposal.blockedReason && <div className="wf-proposal-warn">{proposal.blockedReason}</div>}
            {proposal.testing && <div className="wf-proposal-note">{t('workflows.proposal.testing')}</div>}
            {proposalConflictList.length > 0 && (
              // A later save cannot be applied over at all (stale); an open draft of the person's own can, once they
              // say so -- applying replaces the whole table, so every step they changed is named.
              <div className="wf-proposal-warn" role="alert" data-testid="proposal-conflicts">
                <ul>
                  {proposalConflictList.map(item => (
                    <li key={item.id} data-step={item.id}>
                      {t(proposal.stale ? 'workflows.proposal.changedSince' : 'workflows.proposal.conflict', { step: item.name, fields: conflictFieldsText(item.fields, zh) })}
                    </li>
                  ))}
                </ul>
                {!proposal.stale && !proposal.blockedReason && (
                  <label className="wf-proposal-confirm">
                    <input type="checkbox" data-testid="proposal-confirm-overwrite" checked={overwriteConfirmed} onChange={event => confirmProposal({ overwrite: event.target.checked })} />
                    {t('workflows.proposal.confirmOverwrite')}
                  </label>
                )}
              </div>
            )}
            {proposalWidenList.length > 0 && (
              <div className="wf-proposal-warn" role="alert" data-testid="proposal-widens">
                <ul>
                  {proposalWidenList.map(item => (
                    <li key={item.id} data-step={item.id}>{t('workflows.proposal.widen', { step: item.name, what: wideningText(item, zh, componentNames.tool) })}</li>
                  ))}
                </ul>
                {!proposal.stale && !proposal.blockedReason && (
                  <label className="wf-proposal-confirm">
                    <input type="checkbox" data-testid="proposal-confirm-widen" checked={widenConfirmed} onChange={event => confirmProposal({ widen: event.target.checked })} />
                    {t('workflows.proposal.confirmWiden')}
                  </label>
                )}
              </div>
            )}
            <div className="wf-proposal-actions">
              <button
                type="button"
                className="workflows-invoke"
                data-testid="proposal-apply"
                disabled={!onNodesReplace || proposal.stale || Boolean(proposal.blockedReason)
                  || (proposalConflictList.length > 0 && !overwriteConfirmed) || (proposalWidenList.length > 0 && !widenConfirmed)}
                onClick={() => {
                  // The applied table starts a fresh draft; to take it back the person discards that draft (the
                  // save bar's 放弃草稿), which restores the saved table and its version fields together. The
                  // canvas undo history is cleared so an undo cannot restore the nodes and leave the version behind.
                  historyRef.current = emptyHistory()
                  setHistorySize(0)
                  setFutureSize(0)
                  onNodesReplace?.(appliedNodes(proposal.nodes))
                  onApplyProposal?.()
                }}
              >
                {t('workflows.proposal.apply')}
              </button>
              <button type="button" className="workflows-cancel" onClick={onDiscardProposal}>{t('workflows.proposal.discard')}</button>
            </div>
          </div>
        )}
        <div className="wf-view-switch" role="group" aria-label={zh ? '查看方式' : 'View'}>
          {(['preview', 'canvas', 'package'] as const).map(view => (
            <button key={view} type="button" data-view={view} aria-pressed={builderView === view} className={builderView === view ? 'is-active' : ''}
              onClick={() => setBuilderView(view)}>{view === 'preview' ? (zh ? '流程预览' : 'Flow preview') : view === 'canvas' ? (zh ? '无限画布' : 'Canvas') : (zh ? '工作流包' : 'Package')}</button>
          ))}
        </div>
        {builderView === 'preview' && (
          <FlowPreviewBoard zh={zh} selectedId={editing?.id ?? ''} kindLabel={nodeTypeText}
            groups={previewGroups(shownNodes, role => roleName.get(role) ?? role, new Set(editable ? healthOfNode.keys() : []), nodeTrials, zh ? '待选角色' : 'No role yet')}
            onOpen={stepId => { const at = editorAtCentre(); if (editable && at) setEditing({ id: stepId, ...at }) }} />
        )}
        <div ref={packageHostRef} className={`wf-package-view${builderView === 'package' ? ' is-active' : ''}`} aria-hidden={builderView !== 'package'} />
        {builderView !== 'package' && <div className="wf-canvas-toolbar" data-testid="builder-toolbar">
          {/* 搭建台's top actions, in the order the plan fixes: 安安协助 / 撤销 / 测试 / 保存草稿 / 验收准入. Saving, testing
              and admitting are three different transitions and three different buttons. */}
          {toolbarActions?.assist}
          {canAddNodes && (
            <>
              <button type="button" className="wf-canvas-expand wf-canvas-undo" disabled={historySize === 0} title={expanded ? (zh ? '撤销（Ctrl/⌘+Z）' : 'Undo (Ctrl/⌘+Z)') : (zh ? '撤销' : 'Undo')} onClick={undo}>
                {zh ? '↶ 撤销' : '↶ Undo'}
              </button>
              <button type="button" className="wf-canvas-expand wf-canvas-redo" disabled={futureSize === 0} title={zh ? '重做（Ctrl/⌘+Shift+Z）' : 'Redo (Ctrl/⌘+Shift+Z)'} onClick={redo}>
                {zh ? '↷ 重做' : '↷ Redo'}
              </button>
            </>
          )}
          {toolbarActions?.test}
          {editable && (saveBar ?? (expanded ? <span className="wf-canvas-toolbar-hint">{zh ? '开始编辑后可在这里保存' : 'Edit something to save it here'}</span> : null))}
          {toolbarActions?.admit}
          <button type="button" className="wf-canvas-expand wf-canvas-size" aria-pressed={expanded} title={expanded ? (zh ? '收起（Esc）' : 'Collapse (Esc)') : (zh ? '展开画布' : 'Expand canvas')} onClick={() => setExpanded(value => !value)}>
            {expanded ? (zh ? '⤡ 收起' : '⤡ Collapse') : (zh ? '⤢ 展开' : '⤢ Expand')}
          </button>
        </div>}
        {employeePlacement && <div className="wf-employee-placement" role="dialog" aria-label={zh ? '放置 AI 员工' : 'Place AI employee'}>
          <h4>{employeePlacement.name}</h4>
          <p>{employeePlacement.nodeId ? (zh ? '选择怎样加入当前步骤。原有连线、真人负责人和人审保留。' : 'Choose placement; keep wiring and human review.') : (zh ? '新增一个 AI 步骤，保存后配置项目实例并测试。' : 'Add an AI step, then save, configure and test.')}</p>
          {employeePlacement.nodeId && <button type="button" disabled={!editable || nodes.find(node => node.id === employeePlacement.nodeId)?.type !== 'ai_do'}
            onClick={() => placeEmployee(true)}>{zh ? '替换执行员工' : 'Replace executor'}</button>}
          <button type="button" disabled={!canAddNodes} onClick={() => placeEmployee(false)}>{employeePlacement.nodeId ? (zh ? '另加一步' : 'Add another step') : (zh ? '确认新增步骤' : 'Add step')}</button>
          <button type="button" onClick={() => setEmployeePlacement(null)}>{zh ? '取消' : 'Cancel'}</button>
        </div>}
        <ReactFlowProvider>
          <EmployeeViewportCapture capture={captureEmployeeViewport} />
          <ReactFlow
            nodes={flowNodes}
            edges={flowEdges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onConnectStart={(_event, params) => markWireTargets(params.nodeId, params.handleType)}
            onConnectEnd={clearWireTargets}
            onEdgesDelete={onEdgesDelete}
            onNodeClick={openEditorFromClick}
            onNodeDoubleClick={openEditorFromClick}
            onPaneClick={() => setEditing(null)}
            onNodeDragStop={(_event, _node, dragged) => {
              // Arranging steps is part of building: in an editable draft the new positions go into the draft and
              // are saved with it into the 流程包. A read-only workflow can be rearranged to look, but not kept.
              const moved = dragged.filter(item => item.type === 'runNode')
              // `editable` is false while a proposal is previewed: a drag there must not open or change a draft.
              if (!editable || !onNodePatch || !moved.length) return
              record()
              // 4.7 AQ: saved as absolute canvas positions (a card in a lane is positioned inside it: parentAt adds the
              // lane back). The first drag writes every other card down where it is shown as well, so the lanes can
              // go without a card jumping; after that each change keeps them where they are.
              const placed = new Map(moved.map(item => {
                const parent = parentAt.get(item.parentId ?? '') ?? { x: 0, y: 0 }
                return [item.id, absoluteCanvas({ x: item.position.x + parent.x, y: item.position.y + parent.y })] as const
              }))
              if (onNodesReplaceProp) {
                onNodesReplaceProp(pinnedToBoard(nodes, drawn.positions).map(node => (placed.has(node.id) ? { ...node, canvas: placed.get(node.id) } : node)))
                return
              }
              for (const [id, canvas] of placed) onNodePatch(id, { canvas })
            }}
            nodeTypes={NODE_TYPES}
            edgeTypes={EDGE_TYPES}
            fitView
            onlyRenderVisibleElements={visibleOnly}
            proOptions={{ hideAttribution: true }}
            nodesDraggable
            nodesConnectable={editable}
            elementsSelectable
            deleteKeyCode={editable ? ['Backspace', 'Delete'] : null}
            minZoom={0.2}
            maxZoom={1.5}
          >
            <Background variant={BackgroundVariant.Dots} gap={18} size={1} />
            <MiniMap pannable zoomable />
            <ZoomReadout zh={zh} onAlign={editable && onNodesReplace ? alignSteps : undefined} />
            <FitViewOn signal={expanded} />
            <FollowsTheOpenStep nodeId={editingId} wrapRef={flowWrapRef} />
            <ReadableStartOnce />
            {/* The inspected step's own bar, above its card, in the prototype's order: 前加 · the inspector's pages ·
                预演 · 复制 · 删除 · 后加. 预演 only opens the 交付 page: the paid trial is started there, where its
                ceilings are shown -- never in one click from here. 删除 opens the impact below before anything goes. */}
            {editingNode && (
              <NodeToolbar nodeId={editingNode.id} isVisible position={Position.Top} offset={8} className="wf-card-bar">
                {canAddNodes && (
                  <button type="button" data-card-action="insert-before" aria-expanded={insertMenu === 'before'} title={zh ? '在这一步前面插入一步，它原来等的改由新步骤等' : 'Insert a step before this one'} onClick={() => setInsertMenu(open => (open === 'before' ? null : 'before'))}>{zh ? '前加' : 'Before'}</button>
                )}
                {INSPECTOR_TABS.map(tab => (
                  <button key={tab} type="button" aria-pressed={inspectorTab === tab} className={inspectorTab === tab ? 'is-active' : ''}
                    onClick={() => setInspectorTab(tab)}>{INSPECTOR_TAB_LABELS[tab][zh ? 0 : 1]}</button>
                ))}
                {onNodeTrial && editingNode.type === 'ai_do' && (
                  <button type="button" data-card-action="trial" onClick={() => setInspectorTab('output')}>{zh ? '预演…' : 'Trial…'}</button>
                )}
                {canAddNodes && <button type="button" data-card-action="copy" onClick={duplicateEditing}>{zh ? '复制' : 'Copy'}</button>}
                {canAddNodes && (
                  <button type="button" data-card-action="delete" onClick={() => setConfirmDeleteId(editingNode.id)}>{zh ? '删除' : 'Delete'}</button>
                )}
                {canAddNodes && (
                  <button type="button" data-card-action="insert-after" aria-expanded={insertMenu === 'after'} title={zh ? '在这一步后面插入一步，原来接在它后面的步骤改接到新步骤' : 'Insert a step after this one; what followed it now follows the new step'} onClick={() => setInsertMenu(open => (open === 'after' ? null : 'after'))}>{zh ? '后加' : 'After'}</button>
                )}
                {canAddNodes && insertMenu && (
                  <div className="wf-insert-menu" role="menu" data-testid="insert-menu" data-where={insertMenu}
                    onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); setInsertMenu(null) } }}>
                    <small>{insertMenu === 'before' ? (zh ? '在前面加：' : 'Insert before:') : (zh ? '在后面加：' : 'Insert after:')}</small>
                    {(['ai_do', 'human_do', 'human_review'] as const).map(type => (
                      <button key={type} type="button" role="menuitem" data-insert-kind={type} onClick={() => insertBeside(insertMenu, { kind: 'type', type })}>{nodeTypeText(type)}</button>
                    ))}
                    {(packageSkills ?? []).slice(0, 8).map(id => (
                      <button key={`skill:${id}`} type="button" role="menuitem" data-insert-kind="skill" onClick={() => insertBeside(insertMenu, { kind: 'skill', id })}>{zh ? `技能：${id}` : `Skill: ${id}`}</button>
                    ))}
                    {(flows ?? []).filter(flow => flow.id !== workflowId).slice(0, 6).map(flow => (
                      <button key={`flow:${flow.id}`} type="button" role="menuitem" data-insert-kind="flow" onClick={() => insertBeside(insertMenu, { kind: 'flow', id: flow.id, name: flow.name || flow.id })}>{zh ? `调用：${flow.name || flow.id}` : `Call: ${flow.name || flow.id}`}</button>
                    ))}
                  </div>
                )}
              </NodeToolbar>
            )}
          </ReactFlow>
        </ReactFlowProvider>
        {editingNode && editing && (
          <div className="wf-node-edit"
            // A bottom sheet on a narrow canvas: its place and height are the stylesheet's, not the tapped node's.
            style={layout.editor === 'sheet' ? undefined : { left: editing.x, top: editing.top, bottom: editing.bottom, maxHeight: `calc(100% - ${(editing.top ?? editing.bottom ?? 0) + 8}px)` }} role="dialog" aria-label={zh ? '编辑节点' : 'Edit node'} data-inspector-tab={inspectorTab} onFocus={recordOnFocus}>
            <div className="wf-node-edit-head">
              <span className="wf-node-edit-title" title={editingNode.name || editingNode.id}>{editingNode.name || editingNode.id}</span>
              <button type="button" className="wf-node-edit-close" onClick={() => setEditing(null)} aria-label={zh ? '关闭' : 'Close'}>✕</button>
            </div>
            {employeeDesign && editingNode.employee_binding && <div className="wf-palette-hint">
              <span>{zh ? 'AI 员工：' : 'AI employee: '}{editingNode.employee_binding.package_id}</span>
              <button type="button" onClick={() => setPaletteOpen(true)}>{zh ? '配置 / 更换员工' : 'Configure employee'}</button>
            </div>}
            <div className="wf-node-edit-tabs" role="group" aria-label={zh ? '检查器分页' : 'Inspector pages'}>
              {INSPECTOR_TABS.map(tab => (
                <button key={tab} type="button" data-tab-button={tab} aria-pressed={inspectorTab === tab} className={inspectorTab === tab ? 'is-active' : ''}
                  onClick={() => setInspectorTab(tab)}>{INSPECTOR_TAB_LABELS[tab][zh ? 0 : 1]}</button>
              ))}
            </div>
            <div className="wf-node-edit-page" data-tab="inputs" hidden={inspectorTab !== 'inputs'}>
              <div className="wf-node-edit-upstream" role="group" aria-label={zh ? '上游步骤' : 'Upstream steps'}>
                <span>{zh ? '上游步骤' : 'Upstream steps'}</span>
                {upstreamSteps(nodes, editingNode).length === 0 && (
                  <>
                    <em className="wf-node-edit-none">{zh ? '这一步不等任何上游：它从订单资料开始。' : 'This step waits for nothing upstream: it starts from the order.'}</em>
                    {/* Which fields the order actually carries, so "from the order" is a list and not a promise.
                        The flow's own declared intake: nothing invented, and nothing asked for. */}
                    {intakeFields.length > 0 ? (
                      <ul className="wf-node-edit-intake" data-testid="inspector-intake">
                        {intakeFields.map(field => (
                          <li key={field.id}>
                            <span>{field.label || field.id}</span>
                            <small>{field.id}{field.required ? (zh ? ' · 必填' : ' · required') : ''}</small>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <em className="wf-node-edit-none" data-testid="inspector-intake-empty">{zh ? '这条流程还没有声明开单时要填的字段。' : 'This flow declares no intake fields yet.'}</em>
                    )}
                  </>
                )}
                {upstreamSteps(nodes, editingNode).map(step => (
                  <div key={step.id} className={`wf-node-edit-upstream-step${step.missing ? ' is-missing' : ''}`}>
                    <button type="button" disabled={step.missing} title={zh ? '查看这一步' : 'Open this step'}
                      onClick={() => setEditing(current => (current ? { ...current, id: step.id } : current))}>{step.name}</button>
                    <small>{step.missing
                      ? (zh ? '草稿里没有这一步——去掉这条依赖，或把它加回来。' : 'Not in the draft -- drop the dependency or add the step back.')
                      : (step.deliverables.join(' · ') || (zh ? '没有声明交付物' : 'declares no deliverables'))}</small>
                  </div>
                ))}
              </div>
              <label className="wf-node-edit-field">
                <span>{zh ? '需要' : 'Needs'}</span>
                <LinesField key={`inputs:${editingNode.id}`} field="inputs" value={editingNode.inputs ?? []}
                  placeholder={zh ? '一行一个：上游交付物的路径，如 brand/brand-spec.json（可留空）' : 'one per line: a path upstream delivers, e.g. brand/brand-spec.json (optional)'}
                  onChange={next => onNodePatch?.(editingNode.id, { inputs: next })} />
              </label>
              {undeliveredInputs(nodes, editingNode).length > 0 && (
                <div className="wf-node-edit-hint" role="status">{zh
                  ? `上游没有哪一步承诺交付：${undeliveredInputs(nodes, editingNode).join('、')}。连上会交付它的步骤，或确认它来自订单资料（这只是提醒，不拦保存）。`
                  : `No upstream step promises: ${undeliveredInputs(nodes, editingNode).join(', ')}. Wire the step that delivers it, or make sure it comes with the order (a reminder; it does not block a save).`}</div>
              )}
            </div>
            <div className="wf-node-edit-page" data-tab="config" hidden={inspectorTab !== 'config'}>
              {ADDABLE_NODE_KINDS.includes(editingNode.type as NodeKind) && (
                // What kind of step this is. It used to be chosen on the shelf, before the step existed; here it can
                // also be changed later. A step that calls another flow is not one of these and shows no choice.
                <div className="wf-node-edit-kind" role="group" aria-label={zh ? '步骤类型' : 'Kind of step'} data-testid="node-kind">
                  <span>{zh ? '步骤类型' : 'Kind of step'}</span>
                  <div className="wf-node-edit-kind-options">
                    {ADDABLE_NODE_KINDS.map(kind => (
                      <button key={kind} type="button" data-kind={kind} aria-pressed={editingNode.type === kind} className={editingNode.type === kind ? 'is-active' : ''}
                        disabled={!onNodePatch}
                        onClick={() => { const patch = retypePatch(editingNode, kind); if (patch) onNodePatch?.(editingNode.id, patch) }}>{nodeTypeText(kind)}</button>
                    ))}
                  </div>
                </div>
              )}
              <label className="wf-node-edit-field">
                <span>{zh ? '名称' : 'Name'}</span>
                <input value={editingNode.name} onChange={event => onNodePatch?.(editingNode.id, { name: event.target.value })} />
              </label>
              <label className="wf-node-edit-field">
                <span>{zh ? '角色' : 'Role'}</span>
                {/* 4.7 AQ: a placed step keeps its place when its role changes -- positions are absolute, not lane-relative
                    (on an arranged flow onNodePatch writes an older lane-relative one down first) */}
                {orgRoleIds.size > 0 ? (
                  <select data-field="role" value={editingNode.role} onChange={event => onNodePatch?.(editingNode.id, { role: event.target.value })}>
                    <option value="">{zh ? '（选择角色）' : '(choose a role)'}</option>
                    {editingNode.role && !orgRoleIds.has(editingNode.role) && (
                      <option value={editingNode.role}>{zh ? `${editingNode.role}（不在组织里）` : `${editingNode.role} (not in the org)`}</option>
                    )}
                    {(orgRoles ?? []).map(role => (
                      <option key={role.id} value={role.id}>{role.name && role.name !== role.id ? `${role.name} · ${role.id}` : role.id}</option>
                    ))}
                  </select>
                ) : (
                  <>
                    <input data-field="role" list={`wf-roles-${workflowId}`} value={editingNode.role} placeholder={zh ? '选择或输入组织里的角色' : 'a role in the org'} onChange={event => onNodePatch?.(editingNode.id, { role: event.target.value })} />
                    <datalist id={`wf-roles-${workflowId}`}>
                      {usedRoles.map(role => <option key={role} value={role} />)}
                    </datalist>
                  </>
                )}
              </label>
              {editingNode.type === 'invoke_workflow' && (
                <label className="wf-node-edit-field">
                  <span>{zh ? '调用流程' : 'Calls'}</span>
                  <select data-field="invoke" value={String(editingNode.invoke?.workflow ?? '')} onChange={event => onNodePatch?.(editingNode.id, { invoke: { ...editingNode.invoke, workflow: event.target.value } })}>
                    <option value="">{zh ? '（选择流程）' : '(choose a workflow)'}</option>
                    {String(editingNode.invoke?.workflow ?? '') && !flows.some(flow => flow.id === editingNode.invoke?.workflow) && (
                      <option value={String(editingNode.invoke?.workflow)}>{String(editingNode.invoke?.workflow)}</option>
                    )}
                    {flows.filter(flow => !flowCallRejection(workflowId, flow.id, flows) || flow.id === editingNode.invoke?.workflow).map(flow => (
                      <option key={flow.id} value={flow.id}>{flow.name || flow.id}</option>
                    ))}
                  </select>
                </label>
              )}
              <label className="wf-node-edit-field">
                <span>{zh ? '职责' : 'Responsibility'}</span>
                <textarea data-field="responsibility" rows={2} value={editingNode.responsibility ?? ''} placeholder={zh ? '这一步要做成什么' : 'what this step must get done'} onChange={event => onNodePatch?.(editingNode.id, { responsibility: event.target.value })} />
              </label>
              <label className="wf-node-edit-field">
                <span>{zh ? '负责' : 'Who'}</span>
                <select data-field="assignee" value={editingNode.assignee} onChange={event => onNodePatch?.(editingNode.id, { assignee: event.target.value })}>
                  <option value="">{zh ? '（未指派）' : '(unassigned)'}</option>
                  {/* the backend accepts owner as a person node's assignee, and a new person step starts with it */}
                  <option value="owner">owner</option>
                  {editingNode.assignee && editingNode.assignee !== 'owner' && !roster.some(emp => emp.employee_id === editingNode.assignee) && (
                    <option value={editingNode.assignee}>{empName.get(editingNode.assignee) ?? editingNode.assignee}</option>
                  )}
                  {roster.map(emp => <option key={emp.employee_id} value={emp.employee_id}>{emp.name || emp.employee_id}</option>)}
                </select>
                {roster.length === 0 && (
                  <small className="wf-node-edit-roster-note" data-testid="roster-note">{sameOrg
                    ? (zh ? '该组织还没有员工，先用 owner。成员在项目中心的「协作人员与组织」里管理。' : 'This org has no employees yet; use owner. People are managed under People & organisation in the project centre.')
                    : (zh
                      ? `这条流程运行在组织「${workflowOrgId || '未指定'}」上，当前控制台组织的员工不在它的名册里，不能指派；签字人可用 owner。`
                      : `This workflow runs on org "${workflowOrgId || 'unspecified'}"; the console org's employees are not on its roster, so they cannot be assigned. Use owner as the signer.`)}</small>
                )}
              </label>
              {/* 4.7-A07: a person's step has to reach its person -- say whether they have a channel bound, and where
                  to bind one. Outside the <label>, so the link's click is not handed to the select. */}
              {(editingNode.type === 'human_do' || editingNode.type === 'human_review') && (
                <ContactHint personId={editingNode.assignee} employees={employees} contacts={contacts} zh={zh} onOpenPeople={onOpenPeople} />
              )}
              <label className="wf-node-edit-field">
                <span>{zh ? '审核' : 'Gate'}</span>
                <select data-field="gate" value={editingNode.gate} onChange={event => onNodePatch?.(editingNode.id, { gate: event.target.value })}>
                  <option value="none">{gateText('none')}</option>
                  <option value="human_required">{gateText('human_required')}</option>
                  <option value="ai_first_human_spot_check">{gateText('ai_first_human_spot_check')}</option>
                </select>
              </label>
              {editingNode.gate && editingNode.gate !== 'none' && (
                <label className="wf-node-edit-field">
                  <span>{zh ? '签字人' : 'Signer'}</span>
                  {/* gate_to: who signs this gate off. Empty means the owner, as the backend resolves it. */}
                  <select value={effectiveSigner(editingNode.gate_to)} onChange={event => onNodePatch?.(editingNode.id, { gate_to: event.target.value })}>
                    <option value="owner">{zh ? 'owner（默认）' : 'owner (default)'}</option>
                    {effectiveSigner(editingNode.gate_to) !== 'owner' && !roster.some(emp => emp.employee_id === editingNode.gate_to) && (
                      <option value={editingNode.gate_to}>{empName.get(editingNode.gate_to) ?? editingNode.gate_to}</option>
                    )}
                    {roster.map(emp => <option key={emp.employee_id} value={emp.employee_id}>{emp.name || emp.employee_id}</option>)}
                  </select>
                </label>
              )}
              {editingNode.gate && editingNode.gate !== 'none' && (
                <ContactHint personId={effectiveSigner(editingNode.gate_to)} employees={employees} contacts={contacts} zh={zh} onOpenPeople={onOpenPeople} />
              )}
              {showsReworkTo(editingNode) && (
                // 4.7-A07: where this review's 打回 sends the work back. Only the steps it comes after are offered --
                // the rule the save applies -- and a stored value outside them is shown as it is, so it can be fixed.
                <label className="wf-node-edit-field">
                  <span>{zh ? '打回时回到' : 'On send-back, return to'}</span>
                  <select data-field="rework_to" data-testid="node-rework-to" value={editingNode.rework_to ?? ''} onChange={event => onNodePatch?.(editingNode.id, { rework_to: event.target.value })}>
                    <option value="">{zh ? '本步（默认）' : 'This step (default)'}</option>
                    {editingNode.rework_to && !reworkTargets(nodes, editingNode).some(step => step.id === editingNode.rework_to) && (
                      <option value={editingNode.rework_to}>{zh ? `${editingNode.rework_to}（不在这一步之前）` : `${editingNode.rework_to} (not before this step)`}</option>
                    )}
                    {reworkTargets(nodes, editingNode).map(step => <option key={step.id} value={step.id}>{step.name}</option>)}
                  </select>
                  {editingNode.type !== 'human_review' && (
                    <small className="wf-node-edit-roster-note">{zh ? '只有人审步骤用得上这一项，请改回「本步」。' : 'Only a person’s review uses this; set it back to this step.'}</small>
                  )}
                </label>
              )}
              {packageSkills ? (
                // a group, not a <label>: a label would pass a click anywhere inside it on to its first button
                <div className="wf-node-edit-field" role="group" aria-label="Skill">
                  <span>Skill</span>
                  <div className="wf-node-edit-skills">
                    {packageSkills.length === 0 && <em className="wf-node-edit-none">{zh ? '流程包还没有声明技能' : 'The package declares no skills'}</em>}
                    {packageSkills.map(id => {
                      const on = (editingNode.skills ?? []).includes(id)
                      return (
                        <button type="button" key={id} className={`wf-skill-toggle${on ? ' is-on' : ''}`} aria-pressed={on} data-skill={id}
                          onClick={() => onNodePatch?.(editingNode.id, { skills: toggleSkill(editingNode.skills, id) })}>{componentLabel(id, componentNames.skill)}</button>
                      )
                    })}
                    {/* Internal-library skills the save would declare: the one brought here, and any a step already names. */}
                    {[...new Set([
                      ...(brought && broughtState === 'on_save' ? [brought.id] : []),
                      ...(editingNode.skills ?? []).filter(id => skillDeclaration(id, packageSkills, librarySkills) === 'on_save'),
                    ])].map(id => {
                      const on = (editingNode.skills ?? []).includes(id)
                      return (
                        <button type="button" key={id} className={`wf-skill-toggle is-library${on ? ' is-on' : ''}`} aria-pressed={on}
                          title={zh ? '内部库技能：保存时声明到流程包并锁定版本' : 'Internal library: the save declares and locks it'}
                          onClick={() => onNodePatch?.(editingNode.id, { skills: toggleSkill(editingNode.skills, id) })}>{componentLabel(id, componentNames.skill)} ＋</button>
                      )
                    })}
                    {undeclaredSkills(editingNode, packageSkills, librarySkills).map(id => (
                      <button type="button" key={id} className="wf-skill-toggle is-on is-stray" aria-pressed
                        title={zh ? '流程包没有声明这个技能：不会随流程包安装和锁版本。点一下去掉' : 'Not declared by the package: it is not installed or version-locked. Click to remove'}
                        onClick={() => onNodePatch?.(editingNode.id, { skills: toggleSkill(editingNode.skills, id) })}>{componentLabel(id, componentNames.skill)} ✕</button>
                    ))}
                  </div>
                </div>
              ) : (
                <label className="wf-node-edit-field">
                  <span>Skill</span>
                  <input value={(editingNode.skills ?? []).join(', ')} onChange={event => onNodePatch?.(editingNode.id, { skills: event.target.value.split(/[,，]/).map(item => item.trim()).filter(Boolean) })} />
                </label>
              )}
              {/* 4.7 A2-3: a step's skill opens its component detail over the canvas and closing it comes back here,
                  with the step and its unsaved edits as they were. Read-only for now; editing a component is A2-4. */}
              {onViewSkill && (editingNode.skills ?? []).length > 0 && (
                <div className="wf-node-edit-field wf-node-edit-skill-links" data-testid="node-skill-links">
                  <span>{onEditSkill ? (zh ? '查看 / 编辑技能' : 'Open or edit skill') : (zh ? '查看技能' : 'Open skill')}</span>
                  <div>
                    {(editingNode.skills ?? []).map(id => (
                      <span key={id} className="wf-skill-link-pair">
                        <button type="button" className="wf-skill-link" onClick={() => onViewSkill(id)}>{componentLabel(id, componentNames.skill)} ↗</button>
                        {/* 4.7-A16: 编辑 goes to 组件库 with this step remembered; the library offers the way back to it. */}
                        {onEditSkill && (
                          <button type="button" className="wf-skill-link" data-skill-edit={id}
                            title={zh ? '到组件库里编辑这个技能，改完可以回到这一步' : 'Edit this skill in the library, then come back to this step'}
                            onClick={() => onEditSkill(id, editingNode.id)}>{zh ? '编辑' : 'Edit'}</button>
                        )}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {/* 4.7 A02 节点内组件重排: a step's skills, and its tools, each in their own order. ↑ / ↓ are buttons, so
                  the keyboard reaches them; each move is one undo step, written into the draft (graphEdits.moveComponent)
                  and saved with it. A button at the end of the list stays focusable and does nothing. */}
              {editable && onNodePatch && (['skill', 'tool'] as const).map(kind => {
                const list = (kind === 'skill' ? editingNode.skills : editingNode.tools) ?? []
                if (list.length < 2) return null
                const what = kind === 'skill' ? (zh ? '技能' : 'skill') : (zh ? '工具' : 'tool')
                // 4.7 AQ: listed by the name a person reads (the catalogue's display name), the id only when it has none.
                const label = (ref: string) => componentLabel(ref, componentNames[kind])
                const move = (index: number, step: -1 | 1) => {
                  const patch = moveComponent(editingNode, kind, index, step)
                  if (!patch) return
                  record()
                  onNodePatch(editingNode.id, patch)
                }
                return (
                  <div key={`order:${kind}`} className="wf-node-edit-field" role="group" data-testid={`component-order-${kind}`}
                    aria-label={zh ? `${what}的先后顺序` : `Order of the ${what}s`}>
                    <span>{zh ? `${what}顺序` : `${what === 'skill' ? 'Skill' : 'Tool'} order`}</span>
                    <ol style={{ margin: 0, paddingLeft: 20, display: 'grid', gap: 2 }}>
                      {list.map((ref, index) => (
                        <li key={list.indexOf(ref) === index ? ref : `${ref}:${index}`} data-component={ref} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                          <span title={label(ref)} style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{label(ref)}</span>
                          <button type="button" className="wf-skill-link" data-move="up" aria-disabled={index === 0} style={index === 0 ? { opacity: 0.4 } : undefined}
                            aria-label={zh ? `把${what}「${label(ref)}」上移一位` : `Move the ${what} ${label(ref)} up`} title={zh ? '上移' : 'Move up'}
                            onClick={() => move(index, -1)}>↑</button>
                          <button type="button" className="wf-skill-link" data-move="down" aria-disabled={index === list.length - 1} style={index === list.length - 1 ? { opacity: 0.4 } : undefined}
                            aria-label={zh ? `把${what}「${label(ref)}」下移一位` : `Move the ${what} ${label(ref)} down`} title={zh ? '下移' : 'Move down'}
                            onClick={() => move(index, 1)}>↓</button>
                        </li>
                      ))}
                    </ol>
                  </div>
                )
              })}
              {/* A step's own model (workspace/stepModels.ts): the default, or a model of a provider whose key is set here
                  that the catalogue lets a step newly choose. The save checks it against the console's list; a trial or a
                  run refuses one whose key is missing, or that the catalogue refuses, before any call, and the trial
                  record says which model the step's calls were sent with. A retired or refused model is kept and marked. */}
              {editingNode.type === 'ai_do' && (() => {
                const choices = stepModelChoices(modelProviders, editingNode.model ?? '')
                const refused = modelRefusedText(choices, t)
                const kept = choices.retired?.model || choices.blocked?.model || ''
                // The current default is never asked: a step on it runs, whatever the catalogue says of new choices.
                const keptDefault = Boolean(kept) && kept === defaultModel
                return (
                  <label className="wf-node-edit-field wf-node-edit-model" data-testid="node-model">
                    <span>{zh ? '模型' : 'Model'}</span>
                    <select data-field="model" value={editingNode.model ?? ''} onChange={event => onNodePatch?.(editingNode.id, { model: event.target.value })}>
                      <option value="">{zh ? `跟随默认${defaultModel ? `（${defaultModel}）` : ''}` : `Default${defaultModel ? ` (${defaultModel})` : ''}`}</option>
                      {choices.groups.map(group => (
                        <optgroup key={group.provider} label={group.provider}>
                          {group.models.map(model => <option key={model} value={model}>{model}</option>)}
                        </optgroup>
                      ))}
                      {choices.unavailable && (
                        <option value={choices.unavailable}>{zh ? `${choices.unavailable}（密钥未配置：试跑与运行会被拒）` : `${choices.unavailable} (no key here: trials and runs are refused)`}</option>
                      )}
                      {kept && <option value={kept}>{zh ? `${kept}（${refused}）` : `${kept} (${refused})`}</option>}
                      {choices.unknown && editingNode.model && <option value={editingNode.model}>{editingNode.model}</option>}
                    </select>
                    <small data-testid="node-model-note">{choices.unknown
                      ? (zh ? '还没读到模型厂商清单：这里不判断密钥是否已配置。' : 'The provider list is not loaded: whether a key is set is not judged here.')
                      : keptDefault
                      ? t('models.defaultKept', { reason: refused })
                      : kept
                      ? `${zh ? `这一步选的模型：${refused}。` : `This step's model: ${refused}. `}${t('models.stepRefused')}`
                      : choices.unavailable
                      ? (zh ? '这一步选的模型在这台机器上没有密钥：试跑和正式运行都会在发出调用前被拒。' : 'No key for this step\'s model here: trials and runs are refused before any call.')
                      : `${choices.alias ? `${t('models.alias', { model: choices.alias.successor })}${zh ? '。' : '. '}` : ''}${zh ? '只列已配置密钥的模型。没有项目级默认：顺序是这一步，然后是全局默认。' : 'Only models with a key here. No project level yet: this step, then the global default.'}`}</small>
                  </label>
                )
              })()}
              {editingNode.type === 'ai_do' && (() => {
                // 档位: 轻 / 标准 / 强, which the order's frozen plan turns into a model. A step that also names
                // a model runs on the model -- said here rather than left to be discovered.
                const suggestion = suggestTier(editingNode as never)
                const pinned = Boolean(String(editingNode.model ?? '').trim())
                return (
                  <label className="wf-node-edit-field wf-node-edit-tier" data-testid="node-tier">
                    <span>{zh ? '档位' : 'Tier'}</span>
                    <select data-field="tier" value={editingNode.tier ?? ''}
                      onChange={event => onNodePatch?.(editingNode.id, { tier: event.target.value })}>
                      <option value="">{zh ? '跟随建议（标准）' : 'Follow the suggestion (standard)'}</option>
                      {TIERS.map(tier => (
                        <option key={tier} value={tier}>{tierLabel(tier, zh)}</option>
                      ))}
                    </select>
                    <small data-testid="node-tier-note">{pinned
                      ? (zh ? '这一步钉了模型，档位不生效（把关仍按这个模型的档位来）'
                            : 'This step names a model, so the tier does not choose one (gatekeeping still uses that model’s tier)')
                      : (zh ? suggestion.zh : suggestion.en)}</small>
                  </label>
                )
              })()}
              {editingNode.type === 'ai_do' && (() => {
                // Q05 B4: the browser universal connector -- one field on the step, its parameters or null.
                // 4.7-A06: the same field holds the desktop connector (kind: 'desktop'); the kind picker says which.
                const current = editingNode.connector ?? null
                const kindPicker = (
                  <label className="wf-node-edit-field" data-testid="node-connector-kind">
                    <span>{zh ? '连接器类别' : 'Connector kind'}</span>
                    <select data-field="connector.kind" value={isDesktopConnector(current) ? 'desktop' : current ? 'browser' : ''}
                      onChange={event => onNodePatch?.(editingNode.id, {
                        connector: event.target.value === 'desktop' ? newDesktopConnector(editingNode.deliverables)
                          : event.target.value === 'browser' ? newConnector(editingNode.deliverables) : null,
                      })}>
                      <option value="">{zh ? '不用连接器' : 'None'}</option>
                      <option value="browser">{zh ? '内置浏览器（网站）' : 'Built-in browser (websites)'}</option>
                      <option value="desktop">{zh ? '本机桌面应用' : 'A program on this computer'}</option>
                    </select>
                  </label>
                )
                if (isDesktopConnector(current)) {
                  return (
                    <>
                      {kindPicker}
                      <DesktopConnectorFieldset nodeId={editingNode.id} connector={current} deliverables={editingNode.deliverables}
                        zh={zh} channelOpen={desktopChannelOpen} onChange={next => onNodePatch?.(editingNode.id, { connector: next })} />
                    </>
                  )
                }
                const connector = current
                const set = (patch: Partial<ConnectorParams>) => connector && onNodePatch?.(editingNode.id, { connector: { ...connector, ...patch } })
                const posts = connector?.postconditions ?? []
                return (
                  <>
                  {kindPicker}
                  <fieldset className="wf-node-edit-connector" data-testid="node-connector">
                    <legend>{zh ? '浏览器万能连接器' : 'Browser universal connector'}</legend>
                    <label className="wf-node-edit-toggle">
                      <input type="checkbox" data-field="connector" checked={Boolean(connector)}
                        onChange={event => onNodePatch?.(editingNode.id, { connector: event.target.checked ? newConnector(editingNode.deliverables) : null })} />
                      <span>{zh ? '这一步在内置浏览器里做（没有 API、MCP 组件可用时）' : 'This step works in the built-in browser (when no API or MCP component fits)'}</span>
                    </label>
                    {connector && (
                      <>
                        <label className="wf-node-edit-field">
                          <span>{zh ? '网站' : 'Sites'}</span>
                          <textarea key={`${editingNode.id}-domains`} data-field="connector.allowed_domains" rows={2}
                            defaultValue={domainsText(connector.allowed_domains)} placeholder={zh ? '每行一个，如 portal.example.com' : 'one per line, e.g. portal.example.com'}
                            onBlur={event => set({ allowed_domains: domainsFromText(event.target.value) })} />
                        </label>
                        <label className="wf-node-edit-field">
                          <span>{zh ? '只准进这些网址（可不填）' : 'Only these addresses (optional)'}</span>
                          <textarea key={`${editingNode.id}-paths`} data-field="connector.allowed_paths" rows={2}
                            defaultValue={declaredText(connector.allowed_paths)}
                            placeholder={zh ? '每行一个，如 portal.example.com/orders；不填就是整个网站'
                                            : 'one per line, e.g. portal.example.com/orders; empty means the whole site'}
                            onBlur={event => set({ allowed_paths: declaredFromText(event.target.value) })} />
                        </label>
                        <label className="wf-node-edit-field">
                          <span>{zh ? '只准点这些按钮（可不填）' : 'Only these buttons (optional)'}</span>
                          <textarea key={`${editingNode.id}-controls`} data-field="connector.controls" rows={2}
                            defaultValue={declaredText(connector.controls)}
                            placeholder={zh ? '每行一个，按页面上的原字写，如 查询；不填就不限'
                                            : 'one per line, exactly as the page reads it; empty means no limit'}
                            onBlur={event => set({ controls: declaredFromText(event.target.value) })} />
                        </label>
                        <label className="wf-node-edit-field">
                          <span>{zh ? '要做成什么' : 'Goal'}</span>
                          <textarea key={`${editingNode.id}-goal`} data-field="connector.goal" rows={2} defaultValue={connector.goal}
                            onBlur={event => set({ goal: event.target.value.trim() })} />
                        </label>
                        <label className="wf-node-edit-field">
                          <span>{zh ? '用到的订单资料' : 'Order inputs used'}</span>
                          <input key={`${editingNode.id}-inputs`} data-field="connector.inputs" defaultValue={inputsText(connector.inputs)}
                            placeholder={zh ? '资料名，逗号分隔，如 order_no' : 'input names, comma separated'}
                            onBlur={event => set({ inputs: inputsFromText(event.target.value) })} />
                        </label>
                        <div className="wf-node-edit-field" data-testid="node-connector-posts">
                          <span>{zh ? '怎样算做完（由代码在页面上核对）' : 'Done when (checked by code on the page)'}</span>
                          {posts.map((post, index) => (
                            <div className="wf-node-edit-post" key={`${editingNode.id}-post-${index}`}>
                              <select data-field={`connector.postconditions.${index}.kind`} value={post.kind}
                                onChange={event => set({ postconditions: posts.map((p, i) => i === index ? { ...p, kind: event.target.value as PostconditionKind } : p) })}>
                                {POSTCONDITION_KINDS.map(kind => <option key={kind} value={kind}>{POSTCONDITION_LABELS[kind][zh ? 0 : 1]}</option>)}
                              </select>
                              <input data-field={`connector.postconditions.${index}.value`} defaultValue={post.value} key={`${editingNode.id}-post-${index}-${post.kind}`}
                                placeholder={POSTCONDITION_HINTS[post.kind]?.[zh ? 0 : 1] ?? ''}
                                onBlur={event => set({ postconditions: posts.map((p, i) => i === index ? { ...p, value: event.target.value.trim() } : p) })} />
                              <button type="button" onClick={() => set({ postconditions: posts.filter((_, i) => i !== index) })}>{zh ? '删去' : 'Remove'}</button>
                            </div>
                          ))}
                          <button type="button" data-testid="node-connector-add-post" data-field="connector.postconditions"
                            onClick={() => set({ postconditions: [...posts, { kind: 'text_contains', value: '' }] })}>{zh ? '＋ 加一条' : '+ Add'}</button>
                        </div>
                        <label className="wf-node-edit-field">
                          <span>{zh ? '怎样证明做完' : 'How done is proven'}</span>
                          <select data-field="connector.verify.mode" value={verifyMode(connector)}
                            onChange={event => onNodePatch?.(editingNode.id, { connector: withVerifyMode(connector, event.target.value as VerifyMode) })}>
                            <option value="operation">{zh ? '操作：它做了什么，让上面的条件成立（默认）' : 'Operation: something it did made the conditions true (default)'}</option>
                            <option value="lookup">{zh ? '查询：只读网站；页面同一块里出现本单钥匙和答案就算完成' : 'Lookup: read only; done when the page shows this order’s key and the answer together'}</option>
                            <option value="batch">{zh ? '批量：按订单里的清单一条一条做，每条单独核验' : 'Batch: work the order’s list row by row; each row is judged on its own'}</option>
                          </select>
                        </label>
                        {verifyMode(connector) === 'batch' && (
                          <>
                            <label className="wf-node-edit-field">
                              <span>{zh ? '清单（本单的哪项资料，要是一份列表）' : 'The list (which of the order’s inputs)'}</span>
                              <select data-field="connector.verify.items" value={itemsInputName(connector)}
                                onChange={event => onNodePatch?.(editingNode.id, { connector: withItemsInput(connector, event.target.value) })}>
                                <option value="">{zh ? '（先在上面「用到的订单资料」里写上）' : '(add it to the order inputs above first)'}</option>
                                {Object.keys(connector.inputs ?? {}).map(name => <option key={name} value={name}>{name}</option>)}
                              </select>
                            </label>
                            <label className="wf-node-edit-field">
                              <span>{zh ? '哪个字段是这一条的身份（清单每一行里的字段名）' : 'Which field of a row is its identity'}</span>
                              <input key={`${editingNode.id}-itemkey`} data-field="connector.verify.item_key"
                                defaultValue={itemKeyName(connector)} placeholder={zh ? '如 剧集编号' : 'e.g. Episode no.'}
                                onBlur={event => onNodePatch?.(editingNode.id, { connector: withItemKey(connector, event.target.value.trim()) })} />
                            </label>
                            <label className="wf-node-edit-field">
                              <span>{zh ? '这一步做什么：上传 / 发布 / 回查' : 'What this step does: upload / publish / look back'}</span>
                              <select data-field="connector.distribution_action" value={distributionAction(connector)}
                                onChange={event => onNodePatch?.(editingNode.id, { connector: withDistributionAction(connector, event.target.value) })}>
                                <option value="">{zh ? '（还没选：分发授权单没法核对，发起时会被拦下）' : '(not chosen: the distribution authorization cannot be checked; launch is refused)'}</option>
                                {DISTRIBUTION_ACTION_OPTIONS.map(([code, zhName, enName]) => <option key={code} value={code}>{zh ? zhName : enName}</option>)}
                              </select>
                            </label>
                            <p className="wf-node-edit-hint">
                              {zh ? '完成条件里可以写 {item.字段名}，由代码按每一行代入；清单由订单决定，模型不能增删改。'
                                  : 'A condition may say {item.<field>}, filled by code from each row; the list is the order’s and the model cannot change it.'}
                            </p>
                          </>
                        )}
                        {verifyMode(connector) === 'lookup' && (
                          <>
                            <label className="wf-node-edit-field">
                              <span>{zh ? '查询钥匙（本单的哪项资料，如客户订单号）' : 'Lookup key (which of the order’s inputs)'}</span>
                              <select data-field="connector.verify.key" value={lookupKeyName(connector)}
                                onChange={event => onNodePatch?.(editingNode.id, { connector: withLookupKey(connector, event.target.value) })}>
                                <option value="">{zh ? '（先在上面「用到的订单资料」里写上）' : '(add it to the order inputs above first)'}</option>
                                {Object.keys(connector.inputs ?? {}).map(name => <option key={name} value={name}>{name}</option>)}
                              </select>
                            </label>
                            <label className="wf-node-edit-field">
                              <span>{zh ? '要摘取的答案（字段名，逗号分隔；页面上答案前面的标签要与字段名一致）' : 'Answer to take off the page (field names; the page labels the answer with the same words)'}</span>
                              <input key={`${editingNode.id}-fields`} data-field="connector.output.fields" defaultValue={(connector.output?.fields ?? []).join(', ')}
                                placeholder={zh ? '如 物流单号' : 'e.g. Tracking number'}
                                onBlur={event => set({ output: { ...(connector.output ?? { path: '' }), fields: namesFromText(event.target.value) } })} />
                            </label>
                            <label className="wf-node-edit-field">
                              <span>{zh ? '「查无结果」的说法（每行一句，可不填；常见说法已内置）' : 'How the site says “nothing found” (one per line, optional; common ones are built in)'}</span>
                              <textarea key={`${editingNode.id}-noresult`} data-field="connector.verify.no_result" rows={2}
                                defaultValue={(connector.verify?.no_result ?? []).join('\n')}
                                onBlur={event => set({ verify: { ...(connector.verify ?? { mode: 'lookup' }), mode: 'lookup', no_result: linesFromText(event.target.value) } })} />
                            </label>
                          </>
                        )}
                        <label className="wf-node-edit-field">
                          <span>{zh ? '结果写到' : 'Result goes to'}</span>
                          <select data-field="connector.output.path" value={connector.output?.path ?? ''}
                            onChange={event => set({ output: { ...(connector.output ?? {}), path: event.target.value } })}>
                            <option value="">{zh ? '（选这一步的一项交付）' : '(pick one of the step\'s deliverables)'}</option>
                            {editingNode.deliverables.map(path => <option key={path} value={path}>{path}</option>)}
                          </select>
                        </label>
                        <label className="wf-node-edit-field">
                          <span>{zh ? '最多步数' : 'Most steps'}</span>
                          <input type="number" min={1} max={40} data-field="connector.handoff.max_steps" value={connector.handoff?.max_steps ?? 20}
                            onChange={event => set({ handoff: { ...(connector.handoff ?? {}), max_steps: clampSteps(Number(event.target.value)) } })} />
                        </label>
                        <label className="wf-node-edit-toggle">
                          <input type="checkbox" data-field="connector.decider.jev" checked={connector.decider?.jev !== false}
                            onChange={event => set({ decider: { ...(connector.decider ?? {}), jev: event.target.checked } })} />
                          <span>{zh ? '由 PULY（JEV）决定下一步点哪里' : 'PULY (JEV) picks the next step'}</span>
                        </label>
                        {connector.decider?.jev !== false && (
                          <label className="wf-node-edit-field">
                            <span>{zh ? 'JEV 的方式' : 'JEV mode'}</span>
                            <select data-field="connector.decider.mode" value={connector.decider?.mode ?? 'act'}
                              onChange={event => set({ decider: { ...(connector.decider ?? {}), mode: event.target.value as 'act' | 'suggest' } })}>
                              <option value="act">{zh ? '直接做低风险动作（打开、点击、选择、滚动）' : 'Do low-risk steps (open, click, choose, scroll)'}</option>
                              <option value="suggest">{zh ? '只给建议，由这一步自己决定' : 'Only suggest; the step decides'}</option>
                            </select>
                          </label>
                        )}
                        <small data-testid="node-connector-note">{zh
                          ? '验证码、上传、登录交给人；提交、发送、支付、删除仍要你审批。网站还要在发单时委托给这张订单，这里只点名、不授权。'
                          : 'Captchas, uploads and logins go to a person; submits, sends, payments and deletions still need your approval. The sites must also be delegated to the order: this names them, it grants nothing.'}</small>
                      </>
                    )}
                  </fieldset>
                  </>
                )
              })()}
              <label className="wf-node-edit-field">
                <span>{zh ? '工具/MCP' : 'MCP'}</span>
                <input data-field="tools" value={(editingNode.tools ?? []).join(', ')} onChange={event => onNodePatch?.(editingNode.id, { tools: event.target.value.split(/[,，]/).map(item => item.trim()).filter(Boolean) })} />
              </label>
              {onNodePreflight && (
                <div className="wf-node-preflight">
                  <button type="button" className="wf-node-preflight-run"
                    disabled={nodePreflightBlocked || nodePreflightBusy}
                    onClick={() => onNodePreflight(editingNode.id)}>
                    {nodePreflightBusy
                      ? (zh ? '预检中…' : 'Checking…')
                      : (zh ? '模拟预检这一步' : 'Simulate this step')}
                  </button>
                  {nodePreflightBlocked && <span role="status">{zh ? '草稿尚未保存；先保存版本，避免拿旧节点结果冒充当前结果。' : 'Save this draft first so an old node result is not shown as current.'}</span>}
                  {nodePreflight?.focus_node_id === editingNode.id && (
                    <div className={`wf-node-preflight-result${nodePreflight.ready ? ' is-ready' : ' is-blocked'}`} role="status">
                      <strong>{nodePreflight.ready ? (zh ? '合同通过（仅模拟）' : 'Contract ready (simulation only)') : (zh ? '这一步被阻止' : 'Step blocked')}</strong>
                      <span>{zh ? `${nodePreflight.steps.length} 个步骤 · ${nodePreflight.simulated_effects.length} 个副作用均未执行` : `${nodePreflight.steps.length} step · ${nodePreflight.simulated_effects.length} effects not executed`}</span>
                      {nodePreflight.steps[0] && (
                        <>
                          <span><b>{zh ? '计划输入' : 'Planned input'}：</b>{nodePreflight.steps[0].inputs?.join(' · ') || (zh ? '无上游输入' : 'no upstream input')}</span>
                          <span><b>{zh ? '计划产物' : 'Planned output'}：</b>{nodePreflight.steps[0].deliverables?.join(' · ') || (zh ? '未声明' : 'not declared')}</span>
                          <span><b>Skill / MCP：</b>{[...nodePreflight.steps[0].skills, ...nodePreflight.steps[0].tools].join(' · ') || (zh ? '未声明' : 'not declared')}</span>
                        </>
                      )}
                      {nodePreflight.blockers.map(item => <span key={item}>{item}</span>)}
                      {(nodePreflight.warnings ?? []).map(item => <span key={item}>{item}</span>)}
                    </div>
                  )}
                </div>
              )}
            </div>
            <div className="wf-node-edit-page" data-tab="output" hidden={inspectorTab !== 'output'}>
              <label className="wf-node-edit-field">
                <span>{zh ? '交付' : 'Out'}</span>
                <LinesField key={`deliverables:${editingNode.id}`} field="deliverables" value={editingNode.deliverables}
                  placeholder={zh ? '一行一个：这一步交出的文件路径' : 'one per line: a file this step hands over'}
                  onChange={next => onNodePatch?.(editingNode.id, { deliverables: next })} />
              </label>
              {/* What this step is judged by, and where it may write. From the package, read-only: a person standing
                  at this step's gate needs to see what it promised, and until now that meant opening workflow.yaml.
                  Not editable here and never sent back -- the server drops both from any draft that carries them. */}
              {(editingNode.standard?.length || editingNode.writes?.length) ? (
                <div className="wf-node-edit-promise" data-testid="node-promise">
                  {editingNode.standard && editingNode.standard.length > 0 && (
                    <div data-promise="standard">
                      <strong>{zh ? '质量标准（人工闸门照此判）' : 'Quality standard (what a gate judges by)'}</strong>
                      <ul>{editingNode.standard.map(line => <li key={line}>{line}</li>)}</ul>
                    </div>
                  )}
                  {editingNode.writes && editingNode.writes.length > 0 && (
                    <div data-promise="writes">
                      <strong>{zh ? '写入目录' : 'May write into'}</strong>
                      <ul>{editingNode.writes.map(line => <li key={line}><code>{line}</code></li>)}</ul>
                    </div>
                  )}
                  <small>{zh ? '来自流程包，这里只读；要改请改流程包。' : 'From the package, read-only here; change it in the package.'}</small>
                </div>
              ) : null}
              {onNodeTrial && editingNode.type === 'ai_do' && (
                <NodeTrialSection
                  key={editingNode.id /* another step starts from the default image ceiling, not the last step's */}
                  zh={zh} node={editingNode} draftOpen={nodeTrialBlocked} busy={nodeTrialBusy} trials={nodeTrials}
                  pinning={nodeTrialPinning} onPlan={onNodeTrialPlan} onRun={onNodeTrial}
                  artifact={trialArtifact} onOpenArtifact={onOpenTrialArtifact} onCloseArtifact={onCloseTrialArtifact}
                  onAnswer={onAnswerTrial} onAbandon={onAbandonTrial}
                />
              )}
            </div>
            <div className="wf-node-edit-foot">
              <div className="wf-node-edit-hint">{zh ? '改动进入草稿，下方保存后生效' : 'Edits go to the draft; save below'}</div>
              {canAddNodes && (
                <button type="button" className="wf-node-edit-duplicate" title={zh ? '复制这一步（Ctrl/⌘+D）' : 'Copy this step (Ctrl/⌘+D)'} onClick={duplicateEditing}>{zh ? '复制这一步' : 'Copy this step'}</button>
              )}
              {canAddNodes && (
                confirmDeleteId === editingNode.id ? (
                  <div className="wf-node-edit-danger" data-testid="delete-impact">
                    <DeleteImpactNote impact={deleteImpact(nodes, editingNode.id)} nodes={nodes} zh={zh} />
                    <div>
                      <button type="button" className="wf-node-edit-delete" onClick={() => { record(); onNodesReplace?.(removeNodeBridging(nodes, editingNode.id)); setConfirmDeleteId(''); setEditing(null) }}>{zh ? '确认删除' : 'Remove'}</button>
                      <button type="button" className="wf-node-edit-cancel" onClick={() => setConfirmDeleteId('')}>{zh ? '取消' : 'Cancel'}</button>
                    </div>
                  </div>
                ) : (
                  <button type="button" className="wf-node-edit-delete-open" onClick={() => setConfirmDeleteId(editingNode.id)}>{zh ? '删除这一步' : 'Remove step'}</button>
                )
              )}
            </div>
          </div>
        )}
      </div>
    </div>
    </>
  )
}
