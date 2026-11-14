// What still stops this draft, tied to the step and the field it is about. The card's 待补 row and the canvas's
// blocker strip both read this list, so they cannot disagree. No view code here.
import type { WorkflowNode } from '../components/WorkflowsPage'
import { missingForSave, type MissingField } from './workflowGraph'

/** One finding as the server states it (workflow_packages.node_findings). */
export interface NodeBlocker {
  node_id: string
  field: string
  code: string
  message: string
  nodes?: string[]
  dependency?: string
  /** input_not_delivered: the input this step needs that no upstream step delivers. */
  input?: string
}

export interface HealthItem {
  /** '' for a finding about the graph as a whole. */
  nodeId: string
  nodeName: string
  /** The editor field to put the cursor in. */
  field: string
  code: string
  /** 'draft': computed from the draft as it is now. 'server': what the last save or preflight said, kept only for
   *  findings the page cannot recompute itself. */
  source: 'draft' | 'server'
  /** The server's own sentence, when there is one. */
  message: string
  dependency?: string
  nodes?: string[]
  input?: string
}

const FROM_MISSING: Record<MissingField, { field: string; code: string }> = {
  role: { field: 'role', code: 'role_missing' },
  roleNotInOrg: { field: 'role', code: 'role_not_in_org' },
  responsibility: { field: 'responsibility', code: 'responsibility_missing' },
  deliverables: { field: 'deliverables', code: 'deliverables_missing' },
  assignee: { field: 'assignee', code: 'assignee_missing' },
  invokeTarget: { field: 'invoke', code: 'invoke_target_missing' },
}

/** Codes the page recomputes from the draft on every edit; a server finding with one of these is never shown from
 *  memory -- it would still be on screen after the person fixed it. */
const LIVE_CODES = new Set([...Object.values(FROM_MISSING).map(item => item.code), 'unknown_dependency'])

const LABELS: Record<string, [zh: string, en: string]> = {
  role_missing: ['缺角色', 'no role'],
  role_not_in_org: ['角色不在组织里', 'role not in the org'],
  responsibility_missing: ['缺职责', 'no responsibility'],
  deliverables_missing: ['缺交付物', 'no deliverable'],
  assignee_missing: ['缺负责人', 'no assignee'],
  invoke_target_missing: ['没选调用哪条流程', 'no flow to call'],
  unknown_dependency: ['依赖了不存在的步骤', 'depends on a missing step'],
  dependency_cycle: ['步骤依赖成环', 'steps depend on each other in a loop'],
  input_not_delivered: ['需要的输入没有上游步骤交付', 'needs an input no upstream step delivers'],
  id_missing: ['有步骤没有 id', 'a step has no id'],
  id_duplicate: ['步骤 id 重复', 'duplicate step ids'],
  type_invalid: ['类型无效', 'invalid type'],
  gate_invalid: ['审核方式无效', 'invalid gate'],
  tool_name_empty: ['有空的工具名', 'an empty tool name'],
  canvas_invalid: ['画布位置无效', 'invalid canvas position'],
  model_invalid: ['模型要填模型编号，或留空用默认', 'model must be a model id, or empty for the default'],
  model_not_ai_step: ['只有 AI 步骤才选模型', 'only an AI step runs on a model'],
  model_unknown: ['选的模型不在控制台的列表里', 'model not in the console list'],
  tier_invalid: ['档位只能是轻量、标准、加强，或留空', 'tier must be light, standard or strong, or empty'],
  tier_not_ai_step: ['只有 AI 步骤才选档位', 'only an AI step runs on a tier'],
  // 4.7-A07: where a review's 打回 sends the work back -- the same words as workflow_packages._FINDING_PROBLEM_ZH.
  rework_to_invalid: ['「打回时回到」只能选这一步之前的步骤', 'send-back must name a step this one comes after'],
  rework_to_not_review: ['只有人审步骤才设「打回时回到」', 'only a person’s review sends work back'],
  // The browser connector's own findings (browser_connector.connector_findings), by the field each is about, so the
  // strip never shows the server's English sentence for them. Same words as workflow_packages._FINDING_*_ZH.
  connector_domains_missing: ['连接器还没填允许的网站', 'connector: no sites named'],
  connector_goal_missing: ['连接器还没填要在网站上完成什么', 'connector: no goal'],
  connector_postconditions_missing: ['连接器还没填完成条件', 'connector: no postconditions'],
  connector_output_missing: ['连接器还没填输出文件', 'connector: no output file'],
  connector_output_not_deliverable: ['连接器的输出文件不在这一步的交付物里', 'connector: output is not one of the deliverables'],
  connector_domain_invalid: ['允许的网站里有不是网址的写法', 'connector: not a site name'],
  connector_not_ai_step: ['浏览器连接器只能放在 AI 步骤上', 'connector: only on an AI step'],
  connector_twice: ['一步只能带一个浏览器连接器', 'connector: one per step'],
  connector_unknown: ['这个连接器不是本控制台有的', 'connector: not one this console has'],
  connector_version: ['浏览器连接器的版本不是本控制台提供的', 'connector: version not available'],
  connector_postcondition_invalid: ['连接器的完成条件写得不对', 'connector: a postcondition is malformed'],
  connector_file_type_invalid: ['下载文件要按类型写（pdf、xlsx、csv……）', 'connector: name a download by its type'],
  connector_handoff_invalid: ['交给人处理的设置不对', 'connector: hand-off settings are malformed'],
  connector_max_steps_invalid: ['最多步数要是 1 到上限之间的整数', 'connector: max_steps out of range'],
  connector_login_not_enabled: ['按客户保留登录还没启用', 'connector: kept login is not enabled'],
  connector_login_invalid: ['登录只能交给人处理', 'connector: login can only be handed to a person'],
  connector_decider_invalid: ['「由谁决定下一步」的设置写得不对', 'connector: decider settings are malformed'],
  connector_inputs_invalid:['输入项要写成「名字 → 订单里的哪一项」', 'connector: inputs are malformed'],
  connector_input_not_reference: ['输入项只能引用订单里的信息', 'connector: an input must reference the order'],
  connector_controls_invalid: ['可点的按钮要按页面上的字样一个个列', 'connector: controls are malformed'],
  connector_controls_empty: ['可点的按钮列表是空的', 'connector: controls list is empty'],
  connector_controls_long: ['可点的按钮写的是按钮名，不是一句话', 'connector: a control is a name, not a sentence'],
  connector_paths_invalid: ['限定网址要写成「网站/路径」', 'connector: a path entry is malformed'],
  connector_paths_unlisted: ['限定的网址不属于这一步列出的网站', 'connector: a path narrows a site not named'],
  connector_verify_invalid: ['核对方式的设置写得不对', 'connector: verify settings are malformed'],
  connector_item_outside_batch: ['只有批量步骤的条件里才能写 {item.…}', 'connector: {item.…} outside a batch step'],
  connector_key_outside_lookup: ['只有查询步骤的条件里才能写 {key}', 'connector: {key} outside a lookup step'],
  connector_lookup_key_missing: ['查询步骤还没填查询用的钥匙', 'connector: lookup key missing'],
  connector_lookup_key_not_shown: ['查询步骤至少要有一条页面条件包含 {key}', 'connector: no condition shows {key}'],
  connector_lookup_answer_missing: ['查询步骤还没填要取回的答案字段', 'connector: lookup answer fields missing'],
  connector_lookup_no_downloads: ['查询步骤不以下载为证据', 'connector: a lookup has no downloads'],
  connector_lookup_no_result_invalid: ['「没查到」的提示语数量或长度超了', 'connector: no_result phrases out of bounds'],
  connector_lookup_extract_invalid: ['答案字段与页面上标签的对应关系写得不对', 'connector: extract map is malformed'],
  connector_batch_items_missing: ['批量步骤还没填清单来自订单里的哪一项', 'connector: batch items missing'],
  connector_batch_item_key_missing: ['批量步骤还没填每一行的身份字段', 'connector: batch item key missing'],
  connector_batch_item_not_shown: ['批量步骤至少要有一条页面条件写出这一行自己的字', 'connector: no condition shows the row'],
  connector_batch_lookup_keys: ['查询用的钥匙、「没查到」的提示语、答案对应关系只属于查询步骤', 'connector: lookup keys on a batch step'],
  connector_distribution_action_invalid: ['「这一步做什么」只能选上传、发布或回查', 'connector: batch action is not upload, publish or look back'],
}

export function healthLabel(item: Pick<HealthItem, 'code' | 'message' | 'dependency' | 'input'>, zh: boolean): string {
  const label = LABELS[item.code]
  if (!label) return item.message || item.code
  if (item.code === 'input_not_delivered' && item.input) {
    // Same words as workflow_packages.refused_save_text: the input by name, and what is missing about it.
    return zh ? `需要「${item.input}」，但没有上游步骤交付它` : `needs "${item.input}", which no upstream step delivers`
  }
  const text = label[zh ? 0 : 1]
  return item.code === 'unknown_dependency' && item.dependency ? `${text}（${item.dependency}）` : text
}

/** The card's 待补 row names the field, as it always has ("角色 · 职责"); the strip says what is wrong with it. */
const CARD_WORDS: Record<string, [zh: string, en: string]> = {
  role_missing: ['角色', 'role'],
  role_not_in_org: ['角色不在组织里', 'role not in the org'],
  responsibility_missing: ['职责', 'responsibility'],
  deliverables_missing: ['交付', 'deliverables'],
  assignee_missing: ['负责人', 'assignee'],
  invoke_target_missing: ['调用哪条流程', 'which flow to call'],
}

export function healthCardWord(item: Pick<HealthItem, 'code' | 'message' | 'dependency' | 'input'>, zh: boolean): string {
  return CARD_WORDS[item.code]?.[zh ? 0 : 1] ?? healthLabel(item, zh)
}

export function healthItems(
  nodes: readonly WorkflowNode[],
  orgRoleIds?: ReadonlySet<string>,
  serverBlockers: readonly NodeBlocker[] = [],
): HealthItem[] {
  const names = new Map(nodes.map(node => [node.id, node.name || node.id]))
  const items: HealthItem[] = []
  for (const node of nodes) {
    for (const missing of missingForSave(node, orgRoleIds)) {
      const { field, code } = FROM_MISSING[missing]
      items.push({ nodeId: node.id, nodeName: names.get(node.id) ?? node.id, field, code, source: 'draft', message: '' })
    }
    for (const dependency of node.depends_on ?? []) {
      if (!names.has(dependency)) {
        items.push({ nodeId: node.id, nodeName: names.get(node.id) ?? node.id, field: 'depends_on', code: 'unknown_dependency', source: 'draft', message: '', dependency })
      }
    }
  }
  const seen = new Set(items.map(item => `${item.nodeId}|${item.field}|${item.code}`))
  for (const blocker of serverBlockers) {
    if (LIVE_CODES.has(blocker.code)) continue
    // A finding about a step that is no longer in the draft has nothing left to point at.
    if (blocker.node_id && !names.has(blocker.node_id)) continue
    const key = `${blocker.node_id}|${blocker.field}|${blocker.code}`
    if (seen.has(key)) continue
    seen.add(key)
    items.push({
      nodeId: blocker.node_id, nodeName: blocker.node_id ? (names.get(blocker.node_id) ?? blocker.node_id) : '',
      field: blocker.field, code: blocker.code, source: 'server', message: blocker.message,
      dependency: blocker.dependency, nodes: blocker.nodes, input: blocker.input,
    })
  }
  return items
}

/** Editor fields a finding can put the cursor in. Anything else (a wire, a loop, an id) is fixed on the canvas,
 *  and clicking it says so instead of opening an editor with nowhere to type. `connector` is the connector's own
 *  switch; the controls inside it are `connector.<part>` (see editorFieldFor). */
export const EDITOR_FIELDS = ['role', 'invoke', 'responsibility', 'assignee', 'gate', 'deliverables', 'inputs', 'tools', 'rework_to', 'model', 'tier', 'connector'] as const

export function isEditorField(field: string): boolean {
  return (EDITOR_FIELDS as readonly string[]).includes(field) || field.startsWith('connector.')
}

/** The connector control each connector finding is about. The server files them all under `components` (they are
 *  about the step's connector instance), which is no control of the editor; without this the strip said "not in the
 *  step editor" about a field that is right there. A code not listed goes to the connector's switch. */
const CONNECTOR_FIELDS: Record<string, string> = {
  connector_domains_missing: 'connector.allowed_domains',
  connector_domain_invalid: 'connector.allowed_domains',
  connector_paths_invalid: 'connector.allowed_paths',
  connector_paths_unlisted: 'connector.allowed_paths',
  connector_controls_invalid: 'connector.controls',
  connector_controls_empty: 'connector.controls',
  connector_controls_long: 'connector.controls',
  connector_goal_missing: 'connector.goal',
  connector_inputs_invalid: 'connector.inputs',
  connector_input_not_reference: 'connector.inputs',
  connector_postconditions_missing: 'connector.postconditions',
  connector_postcondition_invalid: 'connector.postconditions',
  connector_file_type_invalid: 'connector.postconditions',
  connector_item_outside_batch: 'connector.postconditions',
  connector_key_outside_lookup: 'connector.postconditions',
  connector_lookup_key_not_shown: 'connector.postconditions',
  connector_lookup_no_downloads: 'connector.postconditions',
  connector_batch_item_not_shown: 'connector.postconditions',
  connector_output_missing: 'connector.output.path',
  connector_output_not_deliverable: 'connector.output.path',
  connector_handoff_invalid: 'connector.handoff.max_steps',
  connector_max_steps_invalid: 'connector.handoff.max_steps',
  connector_decider_invalid: 'connector.decider.jev',
  connector_verify_invalid: 'connector.verify.mode',
  connector_batch_lookup_keys: 'connector.verify.mode',
  connector_lookup_key_missing: 'connector.verify.key',
  connector_lookup_answer_missing: 'connector.output.fields',
  connector_lookup_extract_invalid: 'connector.output.fields',
  connector_lookup_no_result_invalid: 'connector.verify.no_result',
  connector_batch_items_missing: 'connector.verify.items',
  connector_batch_item_key_missing: 'connector.verify.item_key',
  connector_distribution_action_invalid: 'connector.distribution_action',
}

/** Where 跳转编辑 puts the cursor for one finding: the editor control it is about. The finding's own `field` for
 *  everything but the connector's findings, which name their control by code. */
export function editorFieldFor(item: Pick<HealthItem, 'field' | 'code'>): string {
  if (item.code.startsWith('connector_')) return CONNECTOR_FIELDS[item.code] ?? 'connector'
  return item.field
}

/** The controls to try, nearest first: `connector.verify.key`, then `connector.verify`, then `connector`. A control
 *  that is not on screen right now (a lookup's key while the step is set to 操作) still lands on its section. */
export function fieldFallbacks(field: string): string[] {
  const parts = field.split('.')
  return parts.map((_, index) => parts.slice(0, parts.length - index).join('.')).filter(Boolean)
}

/** Where the fix is, for a finding with no editor field. */
export function fixHint(item: Pick<HealthItem, 'field' | 'code'>, zh: boolean): string {
  if (item.field === 'depends_on') {
    return zh ? '依赖是画布上的连线：选中那条线按 Delete 去掉，或从端口重新拖一条。' : 'Dependencies are the wires on the canvas: select the wire and press Delete, or drag a new one from a port.'
  }
  return zh ? '这一项不在步骤编辑框里；看提示内容，在画布上改。' : 'This one is not in the step editor; see the note and fix it on the canvas.'
}

export interface RefusedSave { workflowId: string; blockers: NodeBlocker[] }

/**
 * Which server findings one workflow's canvas is given.
 *
 * A refused save describes the draft that was sent: once that draft changes or is dropped (`draftUnchanged` false)
 * it describes nothing, and showing it would keep "1 thing to fix" on screen after the person fixed it. A preflight
 * describes the saved version, so it speaks only while no draft is open. Returns the same empty array when there is
 * nothing, so a caller's memo does not see a new value on every render.
 */
const NONE: readonly NodeBlocker[] = Object.freeze([])
export function serverBlockersFor(input: {
  workflowId: string
  refusedSave: RefusedSave | null
  draftUnchanged: boolean
  draftOpen: boolean
  preflight: { workflow_id: string; node_blockers?: NodeBlocker[] } | null | undefined
}): readonly NodeBlocker[] {
  if (input.refusedSave && input.refusedSave.workflowId === input.workflowId && input.draftUnchanged && input.refusedSave.blockers.length) {
    return input.refusedSave.blockers
  }
  if (!input.draftOpen && input.preflight && input.preflight.workflow_id === input.workflowId && input.preflight.node_blockers?.length) {
    return input.preflight.node_blockers
  }
  return NONE
}

export function healthByNode(items: readonly HealthItem[]): Map<string, HealthItem[]> {
  const byNode = new Map<string, HealthItem[]>()
  for (const item of items) {
    const targets = item.nodeId ? [item.nodeId] : (item.nodes ?? [])
    for (const target of targets) byNode.set(target, [...(byNode.get(target) ?? []), item])
  }
  return byNode
}
