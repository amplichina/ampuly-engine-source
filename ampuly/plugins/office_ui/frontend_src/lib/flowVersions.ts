// 流程版本面板 in 验收准入: the flow's versions and which project runs which, as the console reads them
// (workflow_versions.py: the manifest, the snapshots publishing leaves, each project's adoption). Read-only: upgrading
// is the project's own step in 项目中心.
import { DIFF_FIELD_LABELS } from '../workspace/workflowGraph'

/** What changed from version `from` (workflow_versions.manifest_diff): steps and field names only, never values. */
export interface FlowVersionDiff {
  from: string
  /** False when either side's snapshot cannot be read: never shown as "nothing changed". */
  readable: boolean
  nodes?: {
    added: Array<{ id: string; name: string }>
    removed: Array<{ id: string; name: string }>
    changed: Array<{ id: string; name: string; fields: string[] }>
  }
  package?: string[]
  same_version?: boolean
  edited_in_place?: boolean
}

export interface FlowVersionRelease {
  version: string
  snapshot: string
  change_note: string
  updated_at: string
  published_at: string
  published_by: string
  unreadable?: boolean
  /** Against the version published before this one; null for the oldest. */
  changes_from_previous?: FlowVersionDiff | null
}

export interface FlowVersionAdoption {
  project_id: string
  version: string
  status: string
  adopted_at: string
  updated_at: string
  behind: boolean
  /** 升级影响: what moving to the latest published version would bring this project. */
  upgrade_diff?: FlowVersionDiff | null
}

export interface FlowVersions {
  workflow_id: string
  current: { version: string; status: string; updated_at: string; updated_by: string; change_note: string }
  published: FlowVersionRelease[]
  latest_published: string
  adoptions: FlowVersionAdoption[]
  /** The saved manifest against the latest published version; null when nothing is published. */
  current_diff?: FlowVersionDiff | null
  /** Skill files whose content differs from what the latest publication signed; null when nothing was signed. */
  skills_changed_since_publication?: string[] | null
}

/** Flow-level keys of a workflow package, as a version difference names them. Others are shown by their key. */
export const PACKAGE_FIELD_LABELS: Record<string, [string, string]> = {
  name: ['名称', 'name'],
  summary: ['简介', 'summary'],
  category: ['分类', 'category'],
  applies_to: ['适用范围', 'applies to'],
  intake: ['开单字段', 'intake fields'],
  acceptance: ['验收标准', 'acceptance'],
  skills: ['技能清单', 'skills'],
  skill_versions: ['技能版本锁', 'skill versions'],
  skills_root: ['技能目录', 'skills folder'],
  exec: ['执行方式', 'execution'],
  org_config: ['团队', 'team'],
  org_id: ['团队', 'team'],
  staffing: ['人员配置', 'staffing'],
  tool_grants: ['工具授权', 'tool grants'],
  directories: ['目录', 'folders'],
  schema_version: ['格式版本', 'schema version'],
}

const SHOWN_NAMES = 3

function names(items: Array<{ name: string; id: string }>, zh: boolean): string {
  const shown = items.slice(0, SHOWN_NAMES).map(item => item.name || item.id).join(zh ? '、' : ', ')
  const rest = items.length - SHOWN_NAMES
  return rest > 0 ? (zh ? `${shown} 等 ${items.length} 步` : `${shown} and ${rest} more`) : shown
}

/** One line for a difference: what was added, removed and changed (by field), and which flow-level keys moved. */
export function diffLine(diff: FlowVersionDiff, zh: boolean): string {
  if (!diff.readable) return zh ? `v${diff.from} 的快照读不出，无法比较` : `v${diff.from}'s snapshot cannot be read: not compared`
  const nodes = diff.nodes ?? { added: [], removed: [], changed: [] }
  const label = (key: string, table: Record<string, [string, string]>) => table[key]?.[zh ? 0 : 1] ?? key
  const parts: string[] = []
  if (nodes.added.length) parts.push(zh ? `新增 ${nodes.added.length} 步（${names(nodes.added, zh)}）` : `${nodes.added.length} step(s) added (${names(nodes.added, zh)})`)
  if (nodes.removed.length) parts.push(zh ? `删除 ${nodes.removed.length} 步（${names(nodes.removed, zh)}）` : `${nodes.removed.length} step(s) removed (${names(nodes.removed, zh)})`)
  if (nodes.changed.length) {
    const each = nodes.changed.slice(0, SHOWN_NAMES)
      .map(item => `${item.name || item.id}（${item.fields.map(field => label(field, DIFF_FIELD_LABELS)).join(zh ? '、' : ', ')}）`)
      .join(zh ? '；' : '; ')
    const rest = nodes.changed.length - SHOWN_NAMES
    parts.push((zh ? `改 ${nodes.changed.length} 步：` : `${nodes.changed.length} step(s) changed: `) + each + (rest > 0 ? (zh ? ' 等' : ' …') : ''))
  }
  if (diff.package?.length) parts.push((zh ? '流程级：' : 'flow level: ') + diff.package.map(key => label(key, PACKAGE_FIELD_LABELS)).join(zh ? '、' : ', '))
  if (!parts.length) return zh ? `步骤和流程合同与 v${diff.from} 相同` : `Steps and flow contract as in v${diff.from}`
  const head = diff.edited_in_place
    ? (zh ? `同一版本号 v${diff.from} 在发布后被改过：` : `v${diff.from} was edited after it was published: `)
    : (zh ? `相对 v${diff.from}：` : `Against v${diff.from}: `)
  return head + parts.join(' · ')
}

const STATUS: Record<string, readonly [string, string]> = {
  draft: ['草案', 'draft'],
  testing: ['测试中', 'testing'],
  published: ['已发布', 'published'],
  retired: ['已停用', 'retired'],
}

const ADOPTION: Record<string, readonly [string, string]> = {
  active: ['接单中', 'taking orders'],
  paused: ['暂停接单', 'paused'],
  withdrawn: ['已撤下', 'withdrawn'],
}

/** "2026-09-06T17:53:32" -> "2026-09-06 17:53"; anything else as it is. */
function when(iso: string): string {
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(iso) ? iso.slice(0, 16).replace('T', ' ') : iso
}

export interface FlowVersionsView {
  current: string
  /** The saved manifest against the latest published version; warn when the same version was edited in place. */
  currentDiff: { text: string; warn: boolean } | null
  published: string[]
  /** Parallel to `published`: what each changed against the version before it ('' for the oldest). */
  publishedDiffs: string[]
  adoptions: Array<{ text: string; behind: boolean; upgrade: string }>
  /** Said once under the adoptions when any project is on an older version. */
  behindNote: string
  /** Whether the skill files still are what the latest publication signed ('' when nothing is published). */
  skillsNote: string
}

export function versionsView(record: FlowVersions, zh: boolean, projectNameOf: (projectId: string) => string = id => id): FlowVersionsView {
  const pick = (table: Record<string, readonly [string, string]>, key: string) => table[key]?.[zh ? 0 : 1] ?? key
  const { current } = record
  const saved = current.updated_at
    ? (zh ? ` · ${when(current.updated_at)}${current.updated_by ? ` 由 ${current.updated_by}` : ''} 保存` : ` · saved ${when(current.updated_at)}${current.updated_by ? ` by ${current.updated_by}` : ''}`)
    : ''
  const note = (text: string) => (text ? (zh ? `：${text}` : `: ${text}`) : '')
  const behind = record.adoptions.filter(item => item.behind).length
  return {
    current: `${zh ? '当前' : 'Now'} v${current.version} · ${pick(STATUS, current.status)}${saved}${note(current.change_note)}`,
    currentDiff: record.latest_published && record.current_diff
      ? {
          text: (zh ? '已保存的版本对比最新发布：' : 'Saved version against the latest published one: ') + diffLine(record.current_diff, zh),
          warn: Boolean(record.current_diff.edited_in_place) || !record.current_diff.readable,
        }
      : null,
    publishedDiffs: record.published.map(item => (item.changes_from_previous ? diffLine(item.changes_from_previous, zh) : '')),
    published: record.published.map(item => {
      const at = item.published_at
        ? (zh ? ` · ${when(item.published_at)} 发布${item.published_by ? `（${item.published_by}）` : ''}` : ` · published ${when(item.published_at)}${item.published_by ? ` by ${item.published_by}` : ''}`)
        : ''
      const unreadable = item.unreadable ? (zh ? ' · 快照读不出' : ' · snapshot unreadable') : ''
      return `v${item.version}${at}${note(item.change_note)}${unreadable}`
    }),
    adoptions: record.adoptions.map(item => ({
      behind: item.behind,
      upgrade: item.behind && item.upgrade_diff
        ? (zh ? `升级到 v${record.latest_published}：` : `Upgrading to v${record.latest_published}: `) + diffLine(item.upgrade_diff, zh)
        : '',
      text: `${projectNameOf(item.project_id) || item.project_id} · ${zh ? '用' : 'on'} v${item.version} · ${pick(ADOPTION, item.status)}`
        + (item.behind ? (zh ? ` · 落后于 v${record.latest_published}` : ` · behind v${record.latest_published}`) : ''),
    })),
    behindNote: behind
      ? (zh ? `有 ${behind} 个项目还在用旧版本。升级由项目自己在项目中心的「业务流」里做，在途工单仍按下单时的版本跑完。`
        : `${behind} project(s) still run an older version. A project upgrades itself under 项目中心 → 业务流; orders already placed finish on the version they started with.`)
      : '',
    skillsNote: !record.latest_published ? ''
      : record.skills_changed_since_publication === undefined || record.skills_changed_since_publication === null
        ? (zh ? '发布时没有记下技能文件的指纹：技能内容这里不比较。' : 'The publication recorded no skill fingerprints: skill content is not compared here.')
        : record.skills_changed_since_publication.length
          ? (zh ? `发布后内容变过的技能文件：${record.skills_changed_since_publication.join('、')}（与发布时签的不同，含已不存在的）。`
            : `Skill files changed since publication: ${record.skills_changed_since_publication.join(', ')} (differ from what it signed, or gone).`)
          : (zh ? '技能文件与发布时签的一致。' : 'Skill files are what the publication signed.'),
  }
}
