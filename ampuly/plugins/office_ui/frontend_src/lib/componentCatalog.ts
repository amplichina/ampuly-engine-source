// 组件库目录: what the console's component_catalog_list returns, and the
// filters the page applies to it. Kept free of React so the contract tests can
// hold the filter rules without a DOM.
//
// Three parts (policy): 内部库 holds what passed admission and can be
// used on a step; 外部库 holds candidates (the local libraries today, the online
// markets later); 业务流 lists the workflows themselves as the larger components
// they are, with what they take in, deliver and call.

import type { WorkflowSummary } from '../components/WorkflowsPage'

export type AccessMode = 'existing_mcp' | 'fastmcp_wrap' | 'browser' | 'commercial'
export type RiskLevel = 'low' | 'medium' | 'high'

export const ACCESS_MODES: AccessMode[] = ['existing_mcp', 'fastmcp_wrap', 'browser', 'commercial']
export const RISK_LEVELS: RiskLevel[] = ['high', 'medium', 'low']

export interface CatalogDomain {
  id: string
  name: string
  name_en?: string
}

export interface CatalogConfigKey {
  key: string
  secret: boolean
  required: boolean
}

export interface ConnectorAdmissionPlan {
  catalog_fingerprint: string
  route: 'inspect_existing_mcp' | 'generate_declarative_candidate' | 'computer_use_candidate' | 'vendor_connector_review' | 'unsupported'
  state: 'ready_for_candidate' | 'blocked'
  blockers: string[]
  required_evidence: string[]
  enforced: {
    credential_values_in_registry: false
    workspace_scope_required: true
    audit_required: true
    budget_required: true
    human_review_required: boolean
    activation_after_registration: false
  }
}

export interface CatalogConnector {
  id: string
  name: string
  name_en: string
  domain: string
  group: string
  access_mode: AccessMode
  install: { type?: string; command?: string; url?: string; where?: string; note?: string }
  config_schema: CatalogConfigKey[]
  // null: the source list does not say, admission has to find out.
  capability: { summary: string; readonly: boolean | null; side_effects: string[] | null }
  governance: {
    risk_level: RiskLevel
    reasons: string[]
    credential_isolation: boolean
    audit_required: boolean
    budget_guard: boolean
    extra_human_review: boolean
    rules: 'api' | 'browser'
  }
  pricing: { model?: string; hint?: string }
  source: { local_path?: string; repo?: string; list?: string }
  inferred: string[]
  notes: string[]
  admission: string
  admission_plan: ConnectorAdmissionPlan
  lifecycle?: {
    stage: string
    candidate: null | { candidate_id?: string; version?: string; status?: string; created_at?: string }
    registration: null | { version?: string; status?: string; enabled?: boolean; registered_at?: string }
  }
}

export interface CatalogSkill {
  id: string
  name: string
  name_en: string
  version: string
  category: string
  summary: string
  source: { path: string; marketplace_id: string; skill_md_sha256: string }
  frontmatter: boolean
  agent_created: boolean
  requires: { tools: string[]; bins: string[]; env: string[] }
  bundles: string[]
  code_files: number
  dependency_manifests: string[]
  license: { declared: string | null; file: boolean; class: 'permissive' | 'restricted' | 'unknown' }
  pricing: { paid: boolean; price_hint?: string; protocol?: string }
  governance: { risk_level: RiskLevel; reasons: string[]; admission_blockers: string[] }
  admission: string
}

export interface CatalogAside {
  id: string
  name: string
  purpose?: string
  note?: string
}

/** A shared skill the workflow packages can declare: admitted, versioned, held to a release contract. */
export interface InternalSkill {
  id: string
  name: string
  summary: string
  version: string
  contract_complete: boolean
  problems: string[]
  used_by: Array<{ id: string; name: string; status: string }>
  /** A category key shared with the online market's (design-media, business-ops ...); empty when unfiled. */
  category?: string
  /** Where a skill admitted from the market came from, and who let it in; null for our own. */
  admission?: { market: string; slug: string; version: string; admitted_by: string; admitted_at: string } | null
}

export interface CatalogCategory {
  key: string
  name: string
  name_en?: string
}

/** A connector that went through the candidate pipeline and was registered (enabling it is a separate step). */
/** A component the console carries itself -- nothing to register or install (Q05: the browser universal connector). */
export interface BuiltInComponent {
  id: string
  kind: string
  name_zh: string
  name_en: string
  version: string
  access_mode: string
  built_in: boolean
  summary_zh: string
  summary_en: string
  tools: string[]
}

export interface InternalConnector {
  connector: string
  version: string
  status: string
  enabled: boolean
  registered_at?: string
}

export interface ComponentCatalog {
  domains: CatalogDomain[]
  connectors: CatalogConnector[]
  tooling: CatalogAside[]
  planned: CatalogAside[]
  skills: CatalogSkill[]
  skill_categories: string[]
  problems: string[]
  internal?: { skills: InternalSkill[]; connectors: InternalConnector[]; categories?: CatalogCategory[]; built_in?: BuiltInComponent[] }
  /** Whether the local catalog's skills can be imported for 调用测试 on this machine (the owner names the library). */
  library?: { ready: boolean; reason: '' | 'library_not_configured' | 'library_unknown' | 'library_missing' | 'library_mismatch'; library: string }
}

/** A workflow seen as a component: what it needs, what it delivers, whom it calls and who calls it. */
export interface FlowComponent {
  id: string
  name: string
  version: string
  status: string
  summary: string
  steps: number
  humanGates: number
  inputs: string[]
  outputs: string[]
  calls: string[]
  calledBy: string[]
  invokable: boolean
}

const FLOW_STATUS_ORDER: Record<string, number> = { published: 0, testing: 1, draft: 2 }

export function flowComponents(workflows: readonly WorkflowSummary[]): FlowComponent[] {
  const live = workflows.filter(flow => flow.status !== 'retired')
  const callsOf = (flow: WorkflowSummary) => [...new Set(flow.nodes
    .filter(node => node.type === 'invoke_workflow')
    .map(node => String(node.invoke?.workflow ?? '').trim())
    .filter(Boolean))]
  return live
    .map(flow => {
      const upstream = new Set(flow.nodes.flatMap(node => node.depends_on))
      // What the flow hands over at the end: the deliverables of the steps nothing else waits on.
      const outputs = [...new Set(flow.nodes
        .filter(node => node.type !== 'invoke_workflow' && !upstream.has(node.id))
        .flatMap(node => node.deliverables))]
      return {
        id: flow.id,
        name: flow.name,
        version: flow.version,
        status: flow.status,
        summary: flow.summary,
        steps: flow.nodes.length,
        humanGates: flow.gate_count,
        inputs: flow.intake_fields.map(field => field.label || field.id),
        outputs,
        calls: callsOf(flow),
        calledBy: live.filter(other => other.id !== flow.id && callsOf(other).includes(flow.id)).map(other => other.id),
        invokable: flow.invokable,
      }
    })
    .sort((a, b) => (FLOW_STATUS_ORDER[a.status] ?? 3) - (FLOW_STATUS_ORDER[b.status] ?? 3) || a.name.localeCompare(b.name))
}

export interface ConnectorFilter {
  query: string
  domain: string
  access: string
  risk: string
}

export type SkillFlag = '' | 'ready' | 'paid' | 'code' | 'license'
export const SKILL_FLAGS: Exclude<SkillFlag, ''>[] = ['ready', 'paid', 'code', 'license']

export interface SkillFilter {
  query: string
  category: string
  risk: string
  flag: SkillFlag
}

function matchesQuery(query: string, fields: Array<string | undefined>): boolean {
  const needle = query.trim().toLowerCase()
  if (!needle) return true
  return fields.some(field => String(field ?? '').toLowerCase().includes(needle))
}

export function filterConnectors(connectors: CatalogConnector[], filter: ConnectorFilter): CatalogConnector[] {
  return connectors.filter(item => (
    (!filter.domain || item.domain === filter.domain)
    && (!filter.access || item.access_mode === filter.access)
    && (!filter.risk || item.governance.risk_level === filter.risk)
    && matchesQuery(filter.query, [item.name, item.name_en, item.id, item.group, item.capability.summary])
  ))
}

export function skillHasFlag(skill: CatalogSkill, flag: SkillFlag): boolean {
  const { reasons, admission_blockers: blockers } = skill.governance
  switch (flag) {
    case '': return true
    // Nothing the owner has to decide first: the license is known to allow it,
    // it costs nothing per use, and it carries its own metadata.
    case 'ready': return blockers.length === 0
    case 'paid': return skill.pricing.paid
    case 'code': return reasons.includes('bundled_code') || reasons.includes('shell_access')
    case 'license': return blockers.includes('license_unconfirmed') || blockers.includes('license_restricts_redistribution')
  }
}

export function filterSkills(skills: CatalogSkill[], filter: SkillFilter): CatalogSkill[] {
  return skills.filter(item => (
    (!filter.category || item.category === filter.category)
    && (!filter.risk || item.governance.risk_level === filter.risk)
    && skillHasFlag(item, filter.flag)
    && matchesQuery(filter.query, [item.name, item.name_en, item.id, item.category, item.summary])
  ))
}

export type InternalUsage = '' | 'used' | 'unused' | 'incomplete'
export const INTERNAL_USAGES: Exclude<InternalUsage, ''>[] = ['used', 'unused', 'incomplete']

export interface InternalSkillFilter {
  query: string
  category: string
  usage: InternalUsage
}

/** 内部库 search: name, id, summary and category, narrowed by category and by whether a flow uses it. */
export function filterInternalSkills(skills: readonly InternalSkill[], filter: InternalSkillFilter): InternalSkill[] {
  return skills.filter(skill => (
    (!filter.category || (skill.category ?? '') === filter.category)
    && (filter.usage !== 'used' || skill.used_by.length > 0)
    && (filter.usage !== 'unused' || skill.used_by.length === 0)
    && (filter.usage !== 'incomplete' || !skill.contract_complete)
    && matchesQuery(filter.query, [skill.name, skill.id, skill.summary, skill.category])
  ))
}

/** The categories the internal skills are filed under, in the order the catalog lists them; unknown keys last. */
export function internalCategoryOptions(skills: readonly InternalSkill[], categories: readonly CatalogCategory[] = []): CatalogCategory[] {
  const used = new Set(skills.map(skill => skill.category ?? '').filter(Boolean))
  const known = categories.filter(category => used.has(category.key))
  const unknown = [...used].filter(key => !categories.some(category => category.key === key)).sort().map(key => ({ key, name: key }))
  return [...known, ...unknown]
}

/**
 * Whether a package can take a skill from the internal library on a step, and so declare it when saved.
 * A draft can; a testing or published package locks what it declares, and a skill without a complete release
 * contract cannot be locked -- the save would refuse it, so the builder says so before anyone drags it in.
 */
export function libraryDeclarable(skill: Pick<InternalSkill, 'contract_complete'>, status: string): boolean {
  return status === 'draft' || skill.contract_complete
}

export type FlowTargetState = 'ok' | 'published' | 'contract_incomplete' | 'no_steps' | 'own_skills'

export interface FlowTarget {
  id: string
  name: string
  status: string
  version: string
  /** The package already declares the skill or a step already names it. */
  uses: boolean
  state: FlowTargetState
}

/**
 * The 业务流 an internal skill can be brought to with 调用, each with whether the builder can take it there now.
 * The builder edits drafts and testing packages; a published one needs a draft first.
 */
export function flowTargetsFor(skill: InternalSkill, workflows: readonly WorkflowSummary[]): FlowTarget[] {
  return workflows
    .filter(flow => flow.status !== 'retired')
    .map(flow => {
      const declared = (flow.skill_contracts ?? []).some(contract => contract.id === skill.id)
      const uses = declared || flow.nodes.some(node => (node.skills ?? []).includes(skill.id))
      // The package payload says which library skills it can lock; the skill's own contract is the fallback.
      const lockable = flow.library_skills ? flow.library_skills.includes(skill.id) : libraryDeclarable(skill, flow.status)
      const state: FlowTargetState = flow.nodes.length === 0
        ? 'no_steps'
        : flow.status !== 'draft' && flow.status !== 'testing'
          ? 'published'
          : declared ? 'ok'
            // A draft cut into the runtime home carries its own skills; one that copies library skills in at the save
            // (library_by_copy, policy) takes them like any other flow.
            : flow.shared_skill_library === false && !flow.library_by_copy ? 'own_skills'
              : !lockable ? 'contract_incomplete' : 'ok'
      return { id: flow.id, name: flow.name || flow.id, status: flow.status, version: flow.version, uses, state }
    })
    .sort((a, b) => Number(b.state === 'ok') - Number(a.state === 'ok') || Number(b.uses) - Number(a.uses) || a.name.localeCompare(b.name))
}

/**
 * The 业务流 a skill on trial can be taken to with 调用测试: any the builder edits that takes skills from the shared
 * library. Its contract does not matter yet -- a draft naming a trial skill cannot be saved until it is admitted.
 */
export function trialFlowTargets(slug: string, workflows: readonly WorkflowSummary[]): FlowTarget[] {
  return workflows
    .filter(flow => flow.status !== 'retired')
    .map(flow => {
      const uses = flow.nodes.some(node => (node.skills ?? []).includes(slug))
      const state: FlowTargetState = flow.nodes.length === 0
        ? 'no_steps'
        : flow.status !== 'draft' && flow.status !== 'testing' ? 'published'
          : flow.shared_skill_library === false && !flow.library_by_copy ? 'own_skills' : 'ok'
      return { id: flow.id, name: flow.name || flow.id, status: flow.status, version: flow.version, uses, state }
    })
    .sort((a, b) => Number(b.state === 'ok') - Number(a.state === 'ok') || a.name.localeCompare(b.name))
}

export function countBy<T>(items: T[], key: (item: T) => string): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const item of items) {
    const value = key(item)
    counts[value] = (counts[value] ?? 0) + 1
  }
  return counts
}

// ── 组件库一列 (frontend redesign S3-5, policy; FRONTEND-IA- 「组件库」 + 「第二轮定稿」) ──────────
// One row per component, grouped 能力 / 平台 / 行业 / 业务流 -- the catalog's own groups (the internal categories, the
// connector domains, the local catalog's skill categories, the flows) -- with ONE state word and ONE verb by that
// state. Every state word stands on a field the server sent (contract_complete, enabled, built_in, lifecycle,
// status, invokable): nothing here says 可用 or 已接通 on its own (capability honesty).

export type ComponentGroup = 'ability' | 'platform' | 'industry' | 'flows'
export const COMPONENT_GROUPS: ReadonlyArray<readonly [ComponentGroup, string, string]> = [
  ['ability', '能力', 'Abilities'], ['platform', '平台', 'Platforms'], ['industry', '行业', 'Industries'], ['flows', '业务流', 'Business flows'],
]

/** 可用 / 需配置 / 候选 / 未接通 -- the four words of the design, and nothing in between. */
export type ComponentState = 'ready' | 'needs_setup' | 'candidate' | 'unwired'
export const COMPONENT_STATES: readonly ComponentState[] = ['ready', 'needs_setup', 'candidate', 'unwired']
export const COMPONENT_STATE_WORDS: Readonly<Record<ComponentState, readonly [string, string]>> = {
  ready: ['可用', 'Ready'], needs_setup: ['需配置', 'Needs setup'], candidate: ['候选', 'Candidate'], unwired: ['未接通', 'Not wired'],
}

/** The one verb a row offers: 加进草稿 for what is ready and goes on a step, 去配置 for what needs setting up, 查看 for the rest. */
export type ComponentVerb = 'add_to_draft' | 'configure' | 'view'
export const COMPONENT_VERB_WORDS: Readonly<Record<ComponentVerb, readonly [string, string]>> = {
  add_to_draft: ['加进草稿', 'Add to draft'], configure: ['去配置', 'Set up'], view: ['查看', 'View'],
}

export type ComponentRowKind = 'skill' | 'built_in' | 'connector' | 'catalog_connector' | 'catalog_skill' | 'flow'
export const COMPONENT_KIND_WORDS: Readonly<Record<ComponentRowKind, readonly [string, string]>> = {
  skill: ['技能', 'Skill'], built_in: ['内置', 'Built in'], connector: ['连接器', 'Connector'],
  catalog_connector: ['连接器', 'Connector'], catalog_skill: ['技能', 'Skill'], flow: ['业务流', 'Business flow'],
}

export type ComponentRowSource =
  | { kind: 'skill'; item: InternalSkill }
  | { kind: 'built_in'; item: BuiltInComponent }
  | { kind: 'connector'; item: InternalConnector }
  | { kind: 'catalog_connector'; item: CatalogConnector; registered: InternalConnector | null }
  | { kind: 'catalog_skill'; item: CatalogSkill }
  | { kind: 'flow'; item: FlowComponent }

export interface ComponentRow {
  /** `${kind}:${id}`: stable across renders, what an open row and a picker are keyed by. */
  key: string
  kind: ComponentRowKind
  id: string
  /** The name in each language (a catalog name is the same in both when it carries no English one). */
  name: readonly [string, string]
  summary: string
  version: string
  group: ComponentGroup
  /** The subgroup key (a category key, a domain id; '' for none) and its words. */
  subgroup: string
  subgroupName: readonly [string, string]
  state: ComponentState
  verb: ComponentVerb
  source: ComponentRowSource
}

const UNFILED: readonly [string, string] = ['未分类', 'Unfiled']
const REGISTERED: readonly [string, string] = ['已注册', 'Registered']
const BUILT_IN: readonly [string, string] = ['内置', 'Built in']

function both(zh: string, en?: string): readonly [string, string] {
  return [zh, en || zh]
}

/** The state of a connector the catalog lists, from the registry first (enabled is the registry's word), then its lifecycle. */
export function catalogConnectorState(item: Pick<CatalogConnector, 'lifecycle'>, registered: Pick<InternalConnector, 'enabled'> | null): ComponentState {
  if (registered) return registered.enabled ? 'ready' : 'unwired'
  if (item.lifecycle?.stage === 'registered') return item.lifecycle.registration?.enabled ? 'ready' : 'unwired'
  return 'candidate'
}

/** A published flow that can be started is ready; a published one that cannot needs setting up on the builder; the rest are candidates. */
export function flowState(flow: Pick<FlowComponent, 'status' | 'invokable'>): ComponentState {
  if (flow.status !== 'published') return 'candidate'
  return flow.invokable ? 'ready' : 'needs_setup'
}

/**
 * The verb by state. 加进草稿 is for a ready skill (the one kind the builder's shelf takes); a ready connector, built-in
 * or flow is used from a step's own configuration, so its verb is 查看. 需配置 → 去配置; 候选 / 未接通 → 查看.
 */
export function componentVerb(kind: ComponentRowKind, state: ComponentState): ComponentVerb {
  if (state === 'needs_setup') return 'configure'
  if (state === 'ready' && kind === 'skill') return 'add_to_draft'
  return 'view'
}

/** Every component the console knows, as rows. The catalog may still be on its way (null): then only the flows. */
export function componentRows(catalog: ComponentCatalog | null, workflows: readonly WorkflowSummary[] | null): ComponentRow[] {
  const rows: ComponentRow[] = []
  const internal = catalog?.internal
  const categories = internal?.categories ?? []
  const categoryName = (key: string): readonly [string, string] => {
    if (!key) return UNFILED
    const found = categories.find(category => category.key === key)
    return found ? both(found.name, found.name_en) : both(key)
  }
  for (const item of internal?.skills ?? []) {
    const state: ComponentState = item.contract_complete ? 'ready' : 'needs_setup'
    rows.push({
      key: `skill:${item.id}`, kind: 'skill', id: item.id, name: both(item.name || item.id), summary: item.summary, version: item.version,
      group: 'ability', subgroup: item.category ?? '', subgroupName: categoryName(item.category ?? ''),
      state, verb: componentVerb('skill', state), source: { kind: 'skill', item },
    })
  }
  const builtIns = internal?.built_in ?? []
  for (const item of builtIns) {
    rows.push({
      key: `built_in:${item.id}`, kind: 'built_in', id: item.id, name: [item.name_zh || item.id, item.name_en || item.name_zh || item.id], summary: item.summary_zh, version: item.version,
      group: 'platform', subgroup: 'built_in', subgroupName: BUILT_IN,
      state: 'ready', verb: componentVerb('built_in', 'ready'), source: { kind: 'built_in', item },
    })
  }
  const registry = new Map((internal?.connectors ?? []).map(item => [item.connector, item]))
  const listed = new Set<string>()
  const domains = new Map((catalog?.domains ?? []).map(domain => [domain.id, both(domain.name, domain.name_en)]))
  for (const item of catalog?.connectors ?? []) {
    const registered = registry.get(item.id) ?? null
    if (registered) listed.add(item.id)
    const state = catalogConnectorState(item, registered)
    rows.push({
      key: `catalog_connector:${item.id}`, kind: 'catalog_connector', id: item.id, name: both(item.name || item.id, item.name_en), summary: item.capability.summary,
      version: registered?.version ?? item.lifecycle?.registration?.version ?? '',
      group: 'platform', subgroup: item.domain, subgroupName: domains.get(item.domain) ?? both(item.domain),
      state, verb: componentVerb('catalog_connector', state), source: { kind: 'catalog_connector', item, registered },
    })
  }
  for (const item of internal?.connectors ?? []) {
    if (listed.has(item.connector)) continue
    const state: ComponentState = item.enabled ? 'ready' : 'unwired'
    rows.push({
      key: `connector:${item.connector}`, kind: 'connector', id: item.connector, name: both(item.connector), summary: '', version: item.version,
      group: 'platform', subgroup: 'registered', subgroupName: REGISTERED,
      state, verb: componentVerb('connector', state), source: { kind: 'connector', item },
    })
  }
  for (const item of catalog?.skills ?? []) {
    rows.push({
      key: `catalog_skill:${item.id}`, kind: 'catalog_skill', id: item.id, name: both(item.name || item.id, item.name_en), summary: item.summary, version: item.version,
      group: 'industry', subgroup: item.category, subgroupName: both(item.category || UNFILED[0], item.category || UNFILED[1]),
      state: 'candidate', verb: componentVerb('catalog_skill', 'candidate'), source: { kind: 'catalog_skill', item },
    })
  }
  for (const item of flowComponents(workflows ?? [])) {
    const state = flowState(item)
    rows.push({
      key: `flow:${item.id}`, kind: 'flow', id: item.id, name: both(item.name || item.id), summary: item.summary, version: item.version,
      group: 'flows', subgroup: '', subgroupName: ['', ''],
      state, verb: componentVerb('flow', state), source: { kind: 'flow', item },
    })
  }
  return rows
}

export interface ComponentRowFilter {
  query: string
  group: ComponentGroup | ''
  state: ComponentState | ''
}

/** Search by name (both languages), id, purpose and subgroup; narrow by group and by state word. */
export function filterComponentRows(rows: readonly ComponentRow[], filter: ComponentRowFilter): ComponentRow[] {
  return rows.filter(row => (
    (!filter.group || row.group === filter.group)
    && (!filter.state || row.state === filter.state)
    && matchesQuery(filter.query, [row.name[0], row.name[1], row.id, row.summary, row.subgroupName[0], row.subgroupName[1]])
  ))
}

export interface ComponentSubgroup { key: string; name: readonly [string, string]; rows: ComponentRow[] }
export interface ComponentRowGroup { group: ComponentGroup; rows: ComponentRow[]; subgroups: ComponentSubgroup[] }

/** The four groups in the design's order (each present even when empty, so the page can say so), each split by subgroup in first-seen order. */
export function groupComponentRows(rows: readonly ComponentRow[]): ComponentRowGroup[] {
  return COMPONENT_GROUPS.map(([group]) => {
    const own = rows.filter(row => row.group === group)
    const subgroups: ComponentSubgroup[] = []
    for (const row of own) {
      let subgroup = subgroups.find(item => item.key === row.subgroup)
      if (!subgroup) { subgroup = { key: row.subgroup, name: row.subgroupName, rows: [] }; subgroups.push(subgroup) }
      subgroup.rows.push(row)
    }
    return { group, rows: own, subgroups }
  })
}

/** How many rows stand under each state word, for the filter. */
export function countComponentStates(rows: readonly ComponentRow[]): Record<ComponentState, number> {
  const counts: Record<ComponentState, number> = { ready: 0, needs_setup: 0, candidate: 0, unwired: 0 }
  for (const row of rows) counts[row.state] += 1
  return counts
}

export interface CurrentDraft { id: string; name: string; version: string; status: string }

/** The flow the builder holds, when 加进草稿 can go there: a draft or a testing package. A published flow or an unknown id is no draft. */
export function currentDraftOf(workflows: readonly WorkflowSummary[] | null | undefined, draftId: string | null | undefined): CurrentDraft | null {
  if (!draftId) return null
  const flow = (workflows ?? []).find(item => item.id === draftId)
  if (!flow || (flow.status !== 'draft' && flow.status !== 'testing')) return null
  return { id: flow.id, name: flow.name || flow.id, version: flow.version, status: flow.status }
}

/** 加进草稿 for one skill on the builder's draft: the target with its state (ok, or why not), null when there is no draft. */
export function draftTargetFor(skill: InternalSkill, workflows: readonly WorkflowSummary[] | null | undefined, draftId: string | null | undefined): FlowTarget | null {
  const draft = currentDraftOf(workflows, draftId)
  if (!draft) return null
  const flow = (workflows ?? []).find(item => item.id === draft.id)
  return flow ? flowTargetsFor(skill, [flow])[0] ?? null : null
}
