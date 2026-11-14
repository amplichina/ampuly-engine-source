// 万物皆组件 -- the builder's component catalogue (policy).
//
// 能力 is what a step can do (a skill, a tool, a control, a person's part); 平台 is the interfaces companies use every
// day; 行业 is the first two sorted by trade so they are easy to find. The whole catalogue is listed now and becomes
// real one entry at a time, each wired and verified before its status changes. So every row carries a status:
//
//   available -- it exists in this console today and can be put on the canvas (a skill this flow package declares,
//                another business flow to call, a person's step);
//   planned   -- designed, not built: it is shown, searchable, and cannot be placed. A flow must never hold a step
//                that would run nothing.
//
// Pure. The catalogue below is the owner's design (names and grouping); what is `available` is decided only from
// what the canvas is really given -- nothing here can promote a planned entry by itself.

export type CatalogMode = 'capability' | 'platform' | 'industry'
export const CATALOG_MODES: readonly CatalogMode[] = ['capability', 'platform', 'industry']
export const CATALOG_MODE_LABELS: Record<CatalogMode, readonly [string, string]> = {
  capability: ['能力', 'Capabilities'],
  platform: ['平台', 'Platforms'],
  industry: ['行业', 'Industries'],
}

export type CatalogKind = 'Skill' | '工具' | '流程' | '人工' | '连接器'
type Planned = readonly [name: string, kind: CatalogKind]

const CAPABILITIES: ReadonlyArray<readonly [group: string, items: readonly Planned[]]> = [
  ['输入与触发', [['表单输入', '流程'], ['文件上传', '流程'], ['消息接收', '流程'], ['Webhook 接收', '流程'], ['定时触发', '流程'], ['手动启动', '流程']]],
  ['AI 理解与生成', [['意图识别', 'Skill'], ['信息提取', 'Skill'], ['内容分类', 'Skill'], ['文本摘要', 'Skill'], ['文案生成', 'Skill'], ['多语言翻译', 'Skill'], ['图像理解', 'Skill'], ['结构化输出', 'Skill']]],
  ['客服与客户经营', [['AI 客服', 'Skill'], ['人工客服转接', 'Skill'], ['需求整理', 'Skill'], ['客户信息查询', 'Skill'], ['线索分配', 'Skill'], ['售后工单', 'Skill']]],
  ['数据与系统操作', [['数据库查询', '工具'], ['数据写入', '工具'], ['表格处理', '工具'], ['字段映射', '工具'], ['数据校验', '工具'], ['去重与合并', '工具'], ['HTTP 请求', '工具']]],
  ['文档与知识', [['文档解析', 'Skill'], ['OCR 文字识别', 'Skill'], ['知识检索', 'Skill'], ['知识库问答', 'Skill'], ['文档生成', 'Skill'], ['PDF 导出', 'Skill'], ['资料归档', 'Skill']]],
  ['图像与多媒体', [['图像生成', 'Skill'], ['图片处理', 'Skill'], ['抠图与去背景', 'Skill'], ['海报制作', 'Skill'], ['商品主图', 'Skill'], ['商品详情页', 'Skill'], ['样机合成', 'Skill'], ['视频合成', 'Skill'], ['语音转文字', 'Skill'], ['语音合成', 'Skill']]],
  ['订单与经营', [['ERP 订单', 'Skill'], ['库存查询', 'Skill'], ['物流查询', 'Skill'], ['报价计算', 'Skill'], ['对账校验', 'Skill'], ['经营报表', 'Skill']]],
  ['消息与协作', [['群消息通知', 'Skill'], ['邮件通知', 'Skill'], ['审批通知', 'Skill'], ['日程安排', 'Skill'], ['工单同步', 'Skill']]],
  ['合同与签署', [['合同生成', 'Skill'], ['合同复核', 'Skill'], ['电子合同签署', 'Skill'], ['签署状态查询', 'Skill'], ['签署回调', 'Skill']]],
  ['人工参与', [['人工审核', '人工'], ['客户确认', '人工'], ['人工录入', '人工'], ['现场执行', '人工'], ['异常接管', '人工'], ['多人会签', '人工']]],
  ['流程控制', [['条件路由', '流程'], ['并行执行', '流程'], ['汇合等待', '流程'], ['循环与批量', '流程'], ['延时等待', '流程'], ['失败重试', '流程'], ['子流程调用', '流程']]],
  ['质量与运行保障', [['结果校验', '流程'], ['预算检查', '流程'], ['敏感信息脱敏', '流程'], ['权限检查', '流程'], ['审计记录', '流程'], ['失败告警', '流程']]],
]

const PLATFORMS: ReadonlyArray<readonly [group: string, items: readonly string[]]> = [
  ['协作办公', ['飞书 · 发送群消息', '飞书 · 读写多维表格', '飞书 · 创建审批', '企业微信 · 发送应用消息', '企业微信 · 客户联系同步', '钉钉 · 发送工作通知', '钉钉 · 发起审批', '邮件服务 · 读取邮件', '邮件服务 · 发送邮件']],
  ['电商与内容', ['淘宝 / 天猫 · 查询订单', '淘宝 / 天猫 · 同步商品', '京东 · 查询订单', '拼多多 · 查询订单', '抖音电商 · 查询订单', '小红书 · 内容准备与人工发布', '微信公众号 · 准备文章草稿']],
  ['ERP 与业务系统', ['金蝶 ERP · 查询库存', '金蝶 ERP · 同步销售订单', '用友 ERP · 查询库存', '用友 ERP · 同步销售订单', '通用 CRM · 查询客户', '通用 CRM · 更新销售线索']],
  ['数据库与存储', ['MySQL · 查询数据', 'PostgreSQL · 查询数据', 'SQL Server · 查询数据', '对象存储 · 上传交付文件', '对象存储 · 生成访问链接']],
  ['云与模型', ['阿里云百炼 · 模型调用', '腾讯云 · 模型调用', '百度智能云 · 模型调用', '火山引擎 · 模型调用']],
  ['签署与支付', ['e签宝 · 发起签署', 'e签宝 · 查询签署状态', '法大大 · 发起签署', '法大大 · 查询签署状态', '微信支付 · 查询支付状态', '支付宝 · 查询支付状态']],
]

// 行业 sorts the first two: every name here is an entry of 能力 (the test holds it to that).
const INDUSTRIES: ReadonlyArray<readonly [group: string, names: readonly string[]]> = [
  ['电商零售', ['AI 客服', '售后工单', '商品主图', '商品详情页', 'ERP 订单', '库存查询', '物流查询', '对账校验', '人工审核']],
  ['品牌与创意', ['需求整理', '图像生成', '样机合成', '海报制作', '客户确认', '资料归档']],
  ['内容与营销', ['信息提取', '文案生成', '海报制作', '视频合成', '人工审核', '群消息通知']],
  ['餐饮与门店', ['表单输入', 'ERP 订单', '库存查询', '经营报表', '现场执行', '异常接管']],
  ['制造与供应链', ['ERP 订单', '库存查询', '数据校验', '物流查询', '现场执行', '结果校验', '异常接管']],
  ['专业服务', ['需求整理', '报价计算', '合同生成', '合同复核', '电子合同签署', '客户确认', '资料归档']],
  ['企业内部运营', ['线索分配', '审批通知', '多人会签', '日程安排', '对账校验', '经营报表']],
  ['教育与培训', ['表单输入', '知识库问答', '文档生成', '结果校验', '人工审核', '邮件通知']],
]

/** The two entries of 人工参与 the engine already has: a person's step is a real kind of step. */
const PERSON_STEPS: Record<string, 'human_review' | 'human_do'> = { 人工审核: 'human_review', 人工录入: 'human_do' }

/** What placing a row does, in the canvas's own payload vocabulary. */
export type CatalogPayload =
  | { kind: 'skill'; id: string; name: string }
  | { kind: 'flow'; id: string; name: string }
  | { kind: 'node'; id: 'human_review' | 'human_do'; name: string }

export type CatalogRow = {
  key: string
  name: string
  /** What sort of thing it is, in the catalogue's words. */
  kind: CatalogKind
  status: 'available' | 'planned'
  /** Present exactly when `status` is `available`. */
  payload?: CatalogPayload
  /** A real id worth showing under the name (a skill's id, a flow's id). */
  sub?: string
}
export type CatalogGroup = { group: string; rows: CatalogRow[]; available: number }

export type CatalogReal = {
  /** The skills this flow package declares (version-locked): the only skills that can be placed. */
  skills: readonly string[]
  /** The other business flows this one may call. */
  flows: ReadonlyArray<{ id: string; name: string; blocked?: boolean }>
  /** Whether steps can be added at all (a read-only canvas places nothing). */
  canAddNodes: boolean
}

const REAL_SKILLS_GROUP = '本流程已接通的技能'
const planned = (group: string, name: string, kind: CatalogKind): CatalogRow => ({ key: `${group}/${name}`, name, kind, status: 'planned' })

function capabilityGroups(real: CatalogReal): CatalogGroup[] {
  const groups: CatalogGroup[] = []
  if (real.skills.length > 0) {
    groups.push({
      group: REAL_SKILLS_GROUP,
      rows: real.skills.map(id => ({ key: `skill/${id}`, name: id, kind: 'Skill' as const, status: 'available' as const, payload: { kind: 'skill' as const, id, name: id } })),
      available: real.skills.length,
    })
  }
  for (const [group, items] of CAPABILITIES) {
    const rows: CatalogRow[] = []
    if (group === '流程控制' && real.canAddNodes) {
      // A business flow is a component: calling one is a real step today.
      for (const flow of real.flows) {
        if (flow.blocked) continue
        rows.push({ key: `flow/${flow.id}`, name: `调用：${flow.name || flow.id}`, kind: '流程', status: 'available', sub: flow.id, payload: { kind: 'flow', id: flow.id, name: flow.name || flow.id } })
      }
    }
    for (const [name, kind] of items) {
      const step = PERSON_STEPS[name]
      rows.push(step && real.canAddNodes
        ? { key: `${group}/${name}`, name, kind, status: 'available', payload: { kind: 'node', id: step, name } }
        : planned(group, name, kind))
    }
    groups.push({ group, rows, available: rows.filter(row => row.status === 'available').length })
  }
  return groups
}

/** The catalogue as one mode shows it, narrowed by a search (every word typed must be in the row's name, kind, id or
 *  group). Groups a search leaves empty are dropped. */
export function catalogTree(mode: CatalogMode, real: CatalogReal, query = ''): CatalogGroup[] {
  const capability = capabilityGroups(real)
  let groups: CatalogGroup[]
  if (mode === 'capability') {
    groups = capability
  } else if (mode === 'platform') {
    groups = PLATFORMS.map(([group, names]) => ({ group, rows: names.map(name => planned(group, name, '连接器')), available: 0 }))
  } else {
    const byName = new Map(capability.flatMap(item => item.rows).filter(row => !row.key.startsWith('skill/') && !row.key.startsWith('flow/')).map(row => [row.name, row]))
    groups = INDUSTRIES.map(([group, names]) => {
      const rows = names.map(name => byName.get(name)).filter((row): row is CatalogRow => Boolean(row)).map(row => ({ ...row, key: `${group}/${row.name}` }))
      return { group, rows, available: rows.filter(row => row.status === 'available').length }
    })
  }
  const words = query.toLowerCase().split(/\s+/).filter(Boolean)
  if (words.length === 0) return groups
  return groups
    .map(item => {
      const rows = item.rows.filter(row => words.every(word => [row.name, row.kind, row.sub ?? '', item.group].join('\n').toLowerCase().includes(word)))
      return { group: item.group, rows, available: rows.filter(row => row.status === 'available').length }
    })
    .filter(item => item.rows.length > 0)
}

// 我的常用 ★ / 最近使用 (the prototype's shelf groups). What is remembered is a placeable row's identity (its payload
// kind and id), kept per viewer in the browser; what is shown is only what this flow can place now -- a skill no longer
// declared or a flow no longer callable simply does not appear, so a remembered row can never be placed by mistake.
export const payloadKey = (payload: { kind: string; id: string }): string => `${payload.kind}:${payload.id}`
export const RECENT_LIMIT = 6

/** A list with `key` first and no repeat, at most `limit` long (最近使用). */
export function withRecent(list: readonly string[], key: string, limit = RECENT_LIMIT): string[] {
  return [key, ...list.filter(item => item !== key)].slice(0, limit)
}

/** `key` added when absent, removed when present (★). */
export function toggledKey(list: readonly string[], key: string): string[] {
  return list.includes(key) ? list.filter(item => item !== key) : [...list, key]
}

export function rememberedGroups(real: CatalogReal, favourites: readonly string[], recent: readonly string[], zh: boolean): CatalogGroup[] {
  const placeable = new Map<string, CatalogRow>()
  for (const item of capabilityGroups(real)) {
    for (const row of item.rows) if (row.payload && !placeable.has(payloadKey(row.payload))) placeable.set(payloadKey(row.payload), row)
  }
  const pick = (keys: readonly string[], prefix: string) => keys
    .map(key => placeable.get(key))
    .filter((row): row is CatalogRow => Boolean(row))
    .map(row => ({ ...row, key: `${prefix}/${row.key}` }))
  const groups: CatalogGroup[] = []
  const fav = pick(favourites, 'fav')
  if (fav.length) groups.push({ group: zh ? '我的常用' : 'Favourites', rows: fav, available: fav.length })
  const last = pick(recent.slice(0, RECENT_LIMIT), 'recent')
  if (last.length) groups.push({ group: zh ? '最近使用' : 'Recent', rows: last, available: last.length })
  return groups
}

/** How much of the catalogue is real today, for the panel's foot: honest progress, not a promise. */
export function catalogProgress(real: CatalogReal): { available: number; total: number } {
  const rows = [...catalogTree('capability', real), ...catalogTree('platform', real)].flatMap(item => item.rows)
  return { available: rows.filter(row => row.status === 'available').length, total: rows.length }
}

export const CATALOG_NAMES = {
  capability: CAPABILITIES.flatMap(([, items]) => items.map(([name]) => name)),
  industry: INDUSTRIES.flatMap(([, names]) => [...names]),
}
