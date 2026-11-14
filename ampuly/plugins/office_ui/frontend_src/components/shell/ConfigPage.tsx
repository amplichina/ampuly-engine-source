// 配置 (frontend refactor U4): what the system runs on, laid out as the owner's prototype lays it out -- a menu on the
// left, one section at a time on the right -- and filled with what the server really reports (the same settings
// payload the models-and-keys dialog reads). This page shows state; it never takes a key: changing anything opens the
// existing dialog, where keys go straight to the machine's user-level environment and tests are run on purpose.
//
// "Configured" means a key is present. It does not mean "connected": only a test run in the dialog says that, and
// its result is not stored, so this page never claims it.
//
// Owner gating is "callback or undefined", as everywhere in the console: an entry whose callback is absent is not shown.

import { useEffect, useState, type ReactNode } from 'react'
import { TIERS, residencyLabel, tierLabel } from '../../workspace/modelTiers'

import { useI18n } from '../../i18n'
import type { SettingsData } from '../SettingsModal'
import type { CapabilityGroup } from '../CapabilityCenterPage'
import { dataTree, treeTotals } from '../../lib/dataTree'
import { ORDER_QUEUE_LABELS } from '../../lib/orderQueues'
import type { ProjectOverview } from '../../lib/projectOverview'
import { holdText, retentionText } from '../../lib/dataBrowser'
import { autoRunParts, commonRoot, locationsSummary, shortPath, taskProtectsText } from '../../lib/dataBrowser'
import type { BackupList, CleanupPreview, DataLocations } from '../../lib/dataBrowser'
import type { Project } from '../../types/kanban'

export const CONFIG_SECTIONS = ['models', 'routing', 'generation', 'channels', 'data', 'connect', 'browser', 'release', 'safety', 'org'] as const
export type ConfigSection = typeof CONFIG_SECTIONS[number]

/** The design's three tabs of 配置 (S3-7, policy): 服务与连接 holds the sections above (控制面's panels
 * among them), 组织与角色 is the organisation board, 偏好 is the way to 设置 -- a link, not a second copy of it. */
export const CONFIG_GROUPS = {
  services: ['models', 'routing', 'generation', 'channels', 'data', 'connect', 'browser', 'release', 'safety'],
  org: ['org'],
} as const satisfies Record<string, readonly ConfigSection[]>
export const CONFIG_GROUP_LABELS = {
  services: ['服务与连接', 'Services & connections'],
  org: ['组织与角色', 'Organisation & roles'],
  prefs: ['偏好', 'Preferences'],
} as const

// 「这些路径是怎么组织的」——解释性文档，不是状态。提成常量是因为它内联在 JSX 里时，代码和文案
// 两头都没法读；页面上它进折叠区，需要时才展开。
const LAYOUT_NOTE_ZH = '每张工单的文件——交付物和过程稿——都放在这个项目的资料区里，按工单号分开。每改一轮验收存一份，打包好的成品单独放一处。你上传的附件存在系统文件夹里，不和交付物混在一起。资料区具体在哪，看你怎么跑这套系统：装在这台电脑上就在本机，用 Docker 跑就在容器的数据盘上。订单、验收和审批记录是所有项目共用一份的，每一条都标着属于哪个项目；「导出 / 备份项目」会把属于这个项目的那些一起带走，删掉之后能从备份原样恢复（还没发出去的消息不会被恢复，恢复一个项目也不会真的往外发任何东西）。还没做的事这里也说清楚：每个项目一份独立的记录、接云存储、接代码仓库，都还没有——不装作已经有了。'
const LAYOUT_NOTE_EN = 'An order\'s files (deliverables and working files) are under <project>/orders/<order>/ in the project data area; its acceptance rounds are in <project>/delivery-history/<order>/round-N/, its packaged exports in <project>/delivery-exports/ and <project>/portfolio-exports/; the project\'s own material area will be <project>/project-materials/; uploaded attachments are in the system directory under projects/<project>/attachments/. Where that area is depends on the deployment: the local launcher keeps it in the system directory\'s workplace/, Docker in /data/workplace. Each project\'s task store is in the system directory under projects/<project>/. Orders, approvals and the audit trail live in one business store shared by the whole console, each row carrying its project; exporting or backing up a project takes its orders, event trails and gate cards along, and a deleted project comes back from its backup as it was (messages that were waiting to be sent are not restored) -- there is no database per project today, and no object storage or repository connection; this page does not pretend otherwise.'

const SECTION_LABELS: Record<ConfigSection, readonly [string, string]> = {
  models: ['模型服务', 'Model services'],
  routing: ['模型分配', 'Model assignment'],
  generation: ['生成工具', 'Generation tools'],
  channels: ['渠道连接', 'Channels'],
  data: ['数据与存储', 'Data & storage'],
  connect: ['连接器与凭据', 'Connectors & credentials'],
  browser: ['浏览器会话与演示候选', 'Browser sessions & candidates'],
  release: ['部署与流程工厂', 'Deployments & process factory'],
  safety: ['运行与安全', 'Runtime & safety'],
  org: ['组织与角色', 'Organisation & roles'],
}

/** The sections whose content is 控制面's panels, and the group of panels each shows. Since S3-7 retired 控制面 as a
 *  page the browser sessions' limits, audit and demonstration candidates (its `execution` group) live here too; the
 *  live view of a page itself is 执行区's 浏览器 tab and BrowserSidePane. */
export const CAPABILITY_SECTIONS: Partial<Record<ConfigSection, CapabilityGroup>> = {
  channels: 'channels', connect: 'connect', browser: 'execution', release: 'release', safety: 'safety',
}

/** The sections a console lists, in the page's order (callback or undefined, as everywhere): 控制面's panels need
 *  `capabilities`, 数据与存储 the overview, 组织与角色 the board; 渠道连接 and the model sections are always there. */
export function listedConfigSections({ capabilities, onAskProjectOverview, orgBoard }: {
  capabilities?: unknown; onAskProjectOverview?: unknown; orgBoard?: unknown
}): ConfigSection[] {
  return CONFIG_SECTIONS.filter(item => item === 'org' ? Boolean(orgBoard)
    : item === 'data' ? Boolean(onAskProjectOverview)
      : Boolean(capabilities) || !(item in CAPABILITY_SECTIONS) || item === 'channels')
}

/** A way in that names a section this console does not list -- a retired page's link (`?page=capabilities`,
 *  `?page=org`) opened on an employee's or a customer's console, where the panels and the board are not handed
 *  in -- lands on the first listed section: an old link never shows an empty page (S3-7, owner item 1). */
export function resolveConfigSection(asked: ConfigSection, listed: readonly ConfigSection[]): ConfigSection {
  return listed.includes(asked) ? asked : (listed[0] ?? 'models')
}

export function ConfigPage({ settings, settingsError, initialSection = 'models', capabilities, onCapabilitySection, onOpenModels, orgBoard, onOpenPreferences, projects, projectOverview = null, onAskProjectOverview, onOpenProject, dataLocations = null, dataBackups = null, cleanupPreview = null, onAskCleanupPreview, onAskDataLocations, onForgetBackupTask, wecomCard, customerServiceCard }: {
  /** 渠道连接's 「企业微信（微信客服）」 card (W3b, components/WecomChannelCard): the owner's only, so App hands it in on
   *  the owner's console and leaves it out elsewhere. It asks for its status itself when the section shows it. */
  wecomCard?: ReactNode
  /** 客服验收 (4.8): owner-only suite lifecycle card, handed in by App. */
  customerServiceCard?: ReactNode
  /** 数据与存储: the project index and, once asked for, where every project's orders stand. The section is listed
   *  only on a console that may ask (callback or undefined, as everywhere). */
  projects?: Project[]
  projectOverview?: ProjectOverview | null
  onAskProjectOverview?: () => void
  onOpenProject?: (projectId: string) => void
  /** Where this console's data really is on disk, for the owner who asked (data phase 2, D9). null until it
   *  arrives; the paths come from the server and are never composed here. */
  dataLocations?: DataLocations | null
  onAskDataLocations?: () => void
  dataBackups?: BackupList | null
  cleanupPreview?: CleanupPreview | null
  /** 清掉一个备份计划任务（policy：旧任务保留，但要能自己清）。 */
  onForgetBackupTask?: (name: string) => void
  onAskCleanupPreview?: () => void
  /** Which section to open on (a test, a link that names one, or a way in from another page); a change while the
   *  page is open moves to that section. */
  initialSection?: ConfigSection
  /** null while it has not arrived. */
  settings?: SettingsData | null
  settingsError?: string | null
  /** 控制面's panels, one group at a time. Absent on a console that may not see them: those sections are not listed. */
  capabilities?: (group: CapabilityGroup) => ReactNode
  /** A section made of those panels was opened: ask for the lists they read. */
  onCapabilitySection?: () => void
  /** Open 模型与密钥. With a provider id, opened scrolled to that service's row: this page is a read, and the
   *  one thing a person does from it is go and change the service they were just looking at. */
  onOpenModels?: (providerId?: string) => void
  /** 组织与角色 (S3-7): the organisation board -- the same element 项目中心's all-projects level shows, handed in, so
   *  nothing is copied. Absent on a console that may not see it: the tab is not listed. */
  orgBoard?: ReactNode
  /** 偏好: the way to 设置, which keeps this browser's preferences; nothing of it is drawn twice. */
  onOpenPreferences?: () => void
}) {
  const { t, locale } = useI18n()
  const zh = locale.startsWith('zh')
  const listed = listedConfigSections({ capabilities, onAskProjectOverview, orgBoard })
  const [askedSection, setSection] = useState<ConfigSection>(initialSection)
  // A way in that names a section while the page is already open (搭建台's 去打开, a deep link, 打开组织) moves there.
  useEffect(() => { setSection(initialSection) }, [initialSection])
  // ...and a section this console does not list gives way to the first listed one, so the menu always lights an
  // entry and the column is never empty -- whether the name came from a link or the listing changed under the page.
  const section = resolveConfigSection(askedSection, listed)
  // The real locations are read when this section is open -- from the section itself, so the menu's one click
  // still does exactly what it did; a section made of 控制面's panels asks for their lists the same way.
  useEffect(() => {
    if (section === 'data') onAskDataLocations?.()
    if (capabilities && section in CAPABILITY_SECTIONS) onCapabilitySection?.()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- opening the section, not a subscription to the callback
  }, [section])
  const change = onOpenModels
    ? <button type="button" className="shell-btn" onClick={() => onOpenModels()}>{zh ? '在「模型与密钥」里修改' : 'Change in Models & keys'}</button>
    : null
  const status = (configured: boolean) => (
    <span className={`shell-tag${configured ? ' shell-tag--ok' : ''}`}>{configured ? (zh ? '已配置密钥' : 'Key present') : (zh ? '未配置' : 'Not configured')}</span>
  )
  const waiting = settingsError
    ? <p className="shell-card-text" role="alert">{settingsError}</p>
    : <p className="shell-card-text" role="status">{zh ? '正在读取配置状态…' : 'Reading the configuration…'}</p>

  return (
    <main className="shell-page shell-config">
      <nav className="shell-config-menu" aria-label={t('app.page.config')}>
        <h1>{t('app.page.config')}</h1>
        <p>{t('config.subtitle')}</p>
        {/* 服务与连接: the sections a console may read (callback or undefined, as everywhere). */}
        <h2 className="shell-config-group" data-config-group="services">{CONFIG_GROUP_LABELS.services[zh ? 0 : 1]}</h2>
        {CONFIG_GROUPS.services.filter(item => listed.includes(item)).map(item => (
          <button key={item} type="button" data-config-section={item} aria-pressed={section === item} className={section === item ? 'is-active' : ''}
            onClick={() => { setSection(item); if (item === 'data') onAskProjectOverview?.() }}>{SECTION_LABELS[item][zh ? 0 : 1]}</button>
        ))}
        {/* 组织与角色: the board, for the console that may see it. */}
        {orgBoard && (
          <button type="button" className={`shell-config-group-btn${section === 'org' ? ' is-active' : ''}`} data-config-group="org" data-config-section="org"
            aria-pressed={section === 'org'} onClick={() => setSection('org')}>{CONFIG_GROUP_LABELS.org[zh ? 0 : 1]}</button>
        )}
        {/* 偏好: this browser's preferences live on 设置; the entry goes there rather than drawing them twice. */}
        {onOpenPreferences && (
          <button type="button" className="shell-config-group-btn" data-config-group="prefs" data-config-section="prefs" onClick={onOpenPreferences}>
            {`${CONFIG_GROUP_LABELS.prefs[zh ? 0 : 1]} · ${t('app.page.preferences')} →`}
          </button>
        )}
      </nav>
      <div className="shell-config-content">
        {section === 'models' && (
          <section aria-label={SECTION_LABELS.models[zh ? 0 : 1]}>
            <header className="shell-page-head"><h2>{SECTION_LABELS.models[zh ? 0 : 1]}</h2>
              <p>{zh ? '按服务独立管理。「已配置密钥」只表示这台机器上有这把密钥，不代表已经连通——连通要在对话框里点测试。' : 'Managed per service. "Key present" only means this machine has the key; whether it connects is what the test in the dialog tells.'}</p></header>
            {!settings ? waiting : (
              <ul className="shell-config-list">
                {/* PULY sits with the services it is billed like; it is a judgment model, never a default or a step's model. */}
                {[...settings.providers, ...(settings.judge_providers ?? [])].map(provider => (
                  <li key={provider.id} data-provider={provider.id}>
                    <span className="shell-config-badge" aria-hidden="true">{provider.name.slice(0, 2).toUpperCase()}</span>
                    <div><strong>{provider.name}</strong><small>{(zh ? provider.note_zh : provider.note_en) || provider.models.slice(0, 3).join(' · ')}</small></div>
                    {status(provider.configured)}
                    {onOpenModels && (
                      <button type="button" className="shell-config-open" data-testid="config-open-provider"
                        onClick={() => onOpenModels(provider.id)}>{zh ? `修改 ${provider.name}` : `Change ${provider.name}`}</button>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {change}
          </section>
        )}
        {section === 'routing' && (
          <section aria-label={SECTION_LABELS.routing[zh ? 0 : 1]}>
            <header className="shell-page-head"><h2>{SECTION_LABELS.routing[zh ? 0 : 1]}</h2>
              <p>{zh ? '系统现在有的分配位就是下面这些。原型里的「节点覆盖 → 项目默认 → 全局默认」三级，目前只有全局这一级；开单时会把这一份钉进订单，之后改设置不影响已经发起的单。' : 'These are the assignment slots that exist. Of the prototype’s three levels (node, project, global) only the global one exists today; it is locked into an order when the order is placed, so a later change does not reach it.'}</p></header>
            {!settings ? waiting : (
              <ul className="shell-config-list">
                <li><div><strong>{zh ? '默认模型' : 'Default model'}</strong><small>default</small></div><span className="shell-tag">{settings.default_model || (zh ? '未设置' : 'not set')}</span></li>
                {Object.entries(settings.routing ?? {}).filter(([slot]) => slot !== 'default').map(([slot, model]) => (
                  <li key={slot}><div><strong>{slot}</strong></div><span className="shell-tag">{model || (zh ? '跟随默认' : 'follows the default')}</span></li>
                ))}
              </ul>
            )}
            {settings && (
              <ul className="shell-config-list" data-testid="config-model-tiers">
                {TIERS.map(tier => {
                  const model = String((settings.tiers ?? {})[tier] ?? '')
                  const params = (settings.tier_params ?? {})[tier] ?? {}
                  const thinking = Boolean((params as Record<string, unknown>).thinking)
                  return (
                    <li key={tier}>
                      <div><strong>{tierLabel(tier, zh)}</strong><small>{zh ? '档位' : 'tier'}</small></div>
                      <span className="shell-tag">{model || (zh ? '未填' : 'not set')}
                        {model ? (thinking ? (zh ? ' · 开思考' : ' · thinking on') : (zh ? ' · 不开思考' : ' · thinking off')) : ''}</span>
                    </li>
                  )
                })}
                <li>
                  {/* PULY stays outside this setting (policy); said here, where someone reads "mainland only". */}
                  <div><strong>{zh ? '模型调用是否出境' : 'Where model calls may go'}</strong>
                    {(settings.judge_providers ?? []).length > 0 && <small data-testid="config-residency-not-puly">{t('settings.residencyNotPuly')}</small>}</div>
                  <span className="shell-tag">{residencyLabel(String(settings.residency ?? ''), zh)}</span>
                </li>
                <li>
                  <div><strong>{zh ? '工作窗口上限' : 'Work window cap'}</strong></div>
                  <span className="shell-tag">{Number(settings.work_window_cap ?? 0) > 0
                    ? `${Math.round(Number(settings.work_window_cap) / 1000)}k`
                    : (zh ? '不限' : 'none')}</span>
                </li>
                {Object.entries(settings.task_tiers ?? {}).map(([kind, tier]) => (
                  <li key={kind}>
                    <div><strong>{kind}</strong><small>{zh ? '按任务类型' : 'by task kind'}</small></div>
                    <span className="shell-tag">{tierLabel(String(tier), zh) || (zh ? '跟随默认' : 'follows the default')}</span>
                  </li>
                ))}
              </ul>
            )}
            {change}
          </section>
        )}
        {section === 'generation' && (
          <section aria-label={SECTION_LABELS.generation[zh ? 0 : 1]}>
            <header className="shell-page-head"><h2>{SECTION_LABELS.generation[zh ? 0 : 1]}</h2>
              <p>{zh ? '出图等生成服务。出图按张计费，试跑与订单都受各自的张数上限约束。' : 'Image and other generation services. Images are billed per image; trials and orders each run under an image ceiling.'}</p></header>
            {!settings ? waiting : (
              <ul className="shell-config-list">
                {settings.gen_providers.map(provider => (
                  <li key={provider.id} data-provider={provider.id}>
                    <span className="shell-config-badge" aria-hidden="true">{provider.name.slice(0, 2).toUpperCase()}</span>
                    <div><strong>{provider.name}</strong><small>{zh ? provider.note_zh : provider.note_en}</small></div>
                    {status(provider.configured)}
                  </li>
                ))}
                {settings.gen_tools_not_built.map(tool => (
                  <li key={tool.id} className="is-muted"><div><strong>{zh ? tool.name_zh : tool.name_en}</strong><small>{(zh ? tool.missing_zh : tool.missing_en) ?? ''}</small></div>
                    <span className="shell-tag">{zh ? '尚未接通' : 'Not built'}</span></li>
                ))}
              </ul>
            )}
            {change}
          </section>
        )}
        {section === 'channels' && (
          <section aria-label={SECTION_LABELS.channels[zh ? 0 : 1]}>
            <header className="shell-page-head"><h2>{SECTION_LABELS.channels[zh ? 0 : 1]}</h2>
              <p>{zh ? '给人发提醒的渠道。发送测试消息是真实外发，只在对话框里由人点击触发，这里不会自动发。' : 'Channels that page a person. A test message is a real send: it happens only when someone clicks it in the dialog, never from here.'}</p></header>
            {!settings ? waiting : (settings.notify?.channels ?? []).length === 0
              ? <p className="shell-card-text">{zh ? '服务端没有报告任何提醒渠道。' : 'The server reports no paging channel.'}</p>
              : (
                <ul className="shell-config-list">
                  {(settings.notify?.channels ?? []).map(channel => (
                    <li key={channel.id}><div><strong>{zh ? channel.name_zh : channel.name_en}</strong><small>{zh ? channel.note_zh : channel.note_en}</small></div>{status(channel.configured)}</li>
                  ))}
                  {settings.notify?.muted && <li className="is-muted"><div><strong>{zh ? '提醒已静音' : 'Paging is muted'}</strong></div></li>}
                </ul>
              )}
            {/* 企业微信（微信客服）(W3b): the owner's card, handed in on the owner's console only (callback or undefined). */}
            {wecomCard}
            {customerServiceCard}
            <p className="shell-card-text">{zh ? '钉钉入站、淘宝店铺接口需要各自平台的账号与应用审批，尚未接通。' : 'Inbound DingTalk and the Taobao shop API need their platforms’ accounts and app approval; not built.'}</p>
            {change}
            {capabilities && <div className="shell-config-capabilities" data-capability-group="channels">{capabilities('channels')}</div>}
            {/* What has no server support is said rather than drawn as a working card (it used to close the page's
                last section, which went with the three retired pages it led to). */}
            <section className="shell-card shell-card--muted">
              <h3>{t('config.notBuilt.title')}</h3>
              <p className="shell-card-text">{t('config.notBuilt.body')}</p>
            </section>
          </section>
        )}
        {capabilities && (section === 'connect' || section === 'browser' || section === 'release' || section === 'safety') && (
          <section aria-label={SECTION_LABELS[section][zh ? 0 : 1]}>
            <header className="shell-page-head"><h2>{SECTION_LABELS[section][zh ? 0 : 1]}</h2>
              <p>{section === 'connect'
                ? (zh ? '系统接到外部的每一条路：已注册的连接器、总控起草的候选、以及各自引用的凭据。凭据只显示引用与状态，不显示值。' : 'Every road out of the system: registered connectors, candidates drafted by master control, and the credentials each refers to. Credentials show a reference and a state, never a value.')
                : section === 'browser'
                  ? (zh ? '运行中的步骤打开的受控浏览器会话：它们的额度、审计与接管记录，以及由演示编成的连接器候选。看画面、接管在「执行区 → 浏览器」里做。' : 'The controlled browser sessions running steps opened: their limits, audit and hand-over records, and the connector candidates compiled from demonstrations. Watching a page and taking over happen in 执行区 → 浏览器.')
                : section === 'release'
                  ? (zh ? '已部署的方案包、灰度与回滚，以及流程工厂里的规格与试建。' : 'Deployed solution packages, rollouts and rollbacks, and the process factory\u2019s specs and trial builds.')
                  : (zh ? '一次运行靠什么保持安全：治理策略、权限证据、安全姿态、外部智能体提供方，以及还没结清的回合。' : 'What keeps a run safe: governance policy, permission evidence, security posture, external agent providers, and turns not yet settled.')}</p></header>
            <div className="shell-config-capabilities" data-capability-group={CAPABILITY_SECTIONS[section]}>{capabilities(CAPABILITY_SECTIONS[section] as CapabilityGroup)}</div>
          </section>
        )}
        {section === 'data' && onAskProjectOverview && (() => {
          const tree = dataTree(projects, projectOverview)
          const totals = treeTotals(tree)
          return (
            <section aria-label={SECTION_LABELS.data[zh ? 0 : 1]} data-testid="config-data">
              <header className="shell-page-head"><h2>{SECTION_LABELS.data[zh ? 0 : 1]}</h2>
                <p>{zh ? '你的东西都存在哪、有没有备份、什么会被自动清掉。这一页只看不改，不会新建、移动或删除任何东西。'
                  : 'Which project, flow and order every piece of business data belongs to. Read only: nothing is created, moved or migrated here.'}</p></header>
              <section className="shell-card">
                <h3>{zh ? `你的数据 · ${totals.projects} 个项目 · ${totals.flows} 条业务流 · ${totals.orders} 张工单` : `Ownership · ${totals.projects} projects · ${totals.flows} flows · ${totals.orders} orders`}</h3>
                {projectOverview === null && <p className="shell-card-text" role="status">{zh ? '正在读取各项目的工单…（绑定了客户的账号读不到这一项，树里就只有项目）' : 'Reading each project’s orders… (an account bound to customers is not given this, and sees projects only)'}</p>}
                <ul className="config-data-tree">
                  {tree.map(project => (
                    <li key={project.id} data-project={project.id}>
                      <div className="config-data-row config-data-row--project">
                        <strong>{project.name}</strong>
                        <small>{project.customer ? (zh ? `客户 ${project.customer}` : `customer ${project.customer}`) : (zh ? '未绑定客户' : 'no customer bound')} · {zh ? '资料区 ' : 'data area '}<code>{project.workspace}</code></small>
                        {onOpenProject && <button type="button" className="shell-btn" onClick={() => onOpenProject(project.id)}>{zh ? '到项目中心 →' : 'Open the project →'}</button>}
                      </div>
                      {project.known && project.flows.length === 0 && <p className="config-data-empty">{zh ? '还没有工单' : 'no orders yet'}</p>}
                      {project.flows.length > 0 && (
                        <ul>
                          {project.flows.map(flow => (
                            <li key={flow.flowId} data-flow={flow.flowId}>
                              <div className="config-data-row"><span>{zh ? '业务流' : 'Flow'} <code>{flow.flowId}</code></span><small>{zh ? `${flow.orders.length} 张工单` : `${flow.orders.length} orders`}</small></div>
                              <ul>
                                {flow.orders.map(order => (
                                  <li key={order.orderId} className="config-data-row config-data-row--order" data-order={order.orderId}>
                                    <code>{order.orderId}</code>
                                    <span className={`shell-tag${order.queue === 'needs_person' ? ' shell-tag--warn' : order.queue === 'ended' ? '' : ' shell-tag--ok'}`}>{ORDER_QUEUE_LABELS[order.queue][zh ? 0 : 1]}</span>
                                    <small><code>{project.workspace}orders/{order.orderId}/</code></small>
                                  </li>
                                ))}
                              </ul>
                            </li>
                          ))}
                        </ul>
                      )}
                      {project.truncated && <p className="config-data-empty">{zh ? '工单较多，只列了最近的一部分。' : 'Many orders; only the latest part is listed.'}</p>}
                    </li>
                  ))}
                </ul>
              </section>
              {/* ── 一、备份：先答「我的东西安不安全」 ──
                  以前这一块埋在「真实位置」那张卡的中间。而人打开这一页，第一个想知道的就是它。 */}
              <section className="shell-card" data-testid="config-data-backups">
                <h3>{zh ? '备份' : 'Backups'}</h3>
                {(dataBackups?.auto?.warnings ?? []).map(item => (
                  <p key={item.code} role="alert" data-auto-backup-warning={item.code}
                    className="shell-card-text" style={{ color: '#ef4444' }}>{item.why_zh}</p>
                ))}
                {dataBackups?.auto && (
                  <ul className="config-data-locations">
                    {([
                      ['daily', zh ? '每天备一次' : 'Daily', dataBackups.auto.daily],
                      ['weekly', zh ? '每周备一次（全部）' : 'Weekly (everything)', dataBackups.auto.weekly],
                    ] as const).map(([key, label, run]) => {
                      const part = autoRunParts(run, zh)
                      return (
                        <li key={key} data-auto-run={key}>
                          <span>{label}</span>
                          <span className={`shell-tag${part.ok ? ' shell-tag--ok' : ''}`}>{part.badge}</span>
                          <small style={part.wrongHome ? { color: '#ef4444' } : undefined}>{part.head}</small>
                          {part.detail && <small>{part.detail}</small>}
                        </li>
                      )
                    })}
                  </ul>
                )}
                {!dataBackups ? (
                  <p className="shell-card-text" role="status">{zh ? '正在读取备份…' : 'Reading the backups…'}</p>
                ) : dataBackups.backups.length === 0 ? (
                  <p className="shell-card-text" data-backup-total>{zh
                    ? `还没有备份文件。做出来的会放在 ${dataBackups.root}`
                    : `No backup files yet. They will go to ${dataBackups.root}`}</p>
                ) : (
                  <p className="shell-card-text" data-backup-total>{zh
                    ? `你有 ${dataBackups.total} 份备份，一共 ${Math.max(1, Math.round((dataBackups.bytes ?? 0) / 1048576))} MB。这些是你的东西，系统不会自己删——要腾地方，在下面自己删。`
                    : `${dataBackups.total} backups, ${Math.max(1, Math.round((dataBackups.bytes ?? 0) / 1048576))} MB. They are yours; nothing removes them automatically.`}</p>
                )}
                {/* 删单腾出来的空间没还给磁盘。够大够比例了才用醒目样式——为几 MB 吓人一跳不值得。 */}
                {dataLocations?.dead_space && dataLocations.dead_space.dead_bytes > 0 && (
                  <p className="shell-card-text" data-testid="config-dead-space"
                    role={dataLocations.dead_space.worth_reclaiming ? 'alert' : 'note'}
                    style={dataLocations.dead_space.worth_reclaiming ? { color: '#ef4444' } : undefined}>
                    {dataLocations.dead_space.why_zh}
                    {dataLocations.dead_space.worth_reclaiming && (
                      <> {zh ? '停掉控制台后执行：' : 'With the console stopped, run: '}<code>ampuly maintenance --apply</code></>
                    )}
                  </p>
                )}
                {/* 4.7-A19：有库还没有表结构版本号（或比程序旧）。系统只读不改，等你确认后自己运行这条命令。 */}
                {dataLocations?.schema_versions?.needs_owner && (
                  <p className="shell-card-text" data-testid="config-schema-versions" role="note">
                    {dataLocations.schema_versions.why_zh}
                    {' '}{zh ? '确认要改的话，先停掉控制台，再在命令行里原样执行（命令里已经写明是哪个数据目录）：' : 'To go ahead, stop the console, then run this exactly as shown (it names the data folder): '}<code>{dataLocations.schema_versions.command}</code>
                  </p>
                )}
                {dataBackups && (dataBackups.backups.length > 0 || (dataBackups.tasks?.length ?? 0) > 0) && (
                  <details data-testid="config-backup-detail">
                    <summary style={{ cursor: 'pointer', fontSize: 13 }}>{zh
                      ? `看看备份文件（${dataBackups.total} 份）和定时备份（${dataBackups.tasks?.length ?? 0} 个）`
                      : `${dataBackups.total} backup file(s) · ${dataBackups.tasks?.length ?? 0} scheduled task(s)`}</summary>
                    {dataBackups.backups.length > 0 && (
                      <ul className="config-data-locations">
                        {dataBackups.backups.map(row => (
                          <li key={row.name} data-backup={row.name} data-blocked={row.blocked ? '1' : undefined}>
                            <span>{row.taken_at || row.name}</span>
                            <code>{row.name}</code>
                            <span className={`shell-tag${row.blocked ? '' : ' shell-tag--ok'}`}>
                              {row.blocked ? (zh ? '读不出来' : 'unreadable')
                                : `${Math.max(1, Math.round(row.bytes / 1024))} KB`}
                            </span>
                            {row.holds.length > 0 && (
                              <small>{(zh ? '里面有：' : 'holds: ') + row.holds.map(hold => holdText(hold, zh)).join('、')}</small>
                            )}
                            {row.holds.some(hold => hold.restored === false) && (
                              <small>{zh ? '其中几项在备份里，但还原不会自动放回去' : 'some of it is carried but a restore does not put it back'}</small>
                            )}
                            {row.note && <small data-backup-note>{row.note}</small>}
                          </li>
                        ))}
                      </ul>
                    )}
                    {/* 计划任务显示的重点是「它保护的是哪儿」——2026-09-24 那个每晚成功却备着另一个
                        仓库的任务，名字是完全正确的。 */}
                    {(dataBackups.tasks?.length ?? 0) > 0 && (
                      <div className="config-data-tasks" data-testid="config-backup-tasks">
                        <ul className="config-data-locations">
                          {(dataBackups.tasks ?? []).map(task => (
                            <li key={task.name} data-backup-task={task.name}
                              data-foreign={task.this_install ? undefined : '1'}>
                              <span>{task.name}</span>
                              <span className={`shell-tag${task.this_install ? ' shell-tag--ok' : ''}`}>
                                {task.this_install ? (zh ? '本机' : 'this install') : (zh ? '别处' : 'elsewhere')}
                              </span>
                              <small>{taskProtectsText(task)}</small>
                              {task.next_run && <small>{(zh ? '下次：' : 'next: ') + task.next_run}</small>}
                              {onForgetBackupTask && !task.this_install && (
                                <button className="pill-btn" data-forget-task={task.name}
                                  style={{ fontSize: 11, padding: '2px 10px' }}
                                  onClick={() => onForgetBackupTask(task.name)}>
                                  {zh ? '清掉它' : 'clear it'}
                                </button>
                              )}
                            </li>
                          ))}
                        </ul>
                        {(dataBackups.tasks ?? []).some(task => !task.this_install) && (
                          <p className="shell-card-text">{zh
                            ? '标着「别处」的，备的是另一套安装的数据，不是这里的。它不会弄坏什么，但也保护不了你——留着或清掉都行。'
                            : 'A task marked “elsewhere” protects another installation, not this one.'}</p>
                        )}
                      </div>
                    )}
                  </details>
                )}
              </section>

              {/* ── 二、位置：东西在哪 ── */}
              <section className="shell-card" data-testid="config-data-locations">
                <h3>{zh ? '位置' : 'Where it is'}</h3>
                {!dataLocations ? (
                  <p className="shell-card-text" role="status">{zh ? '正在读取真实位置…' : 'Reading the real locations…'}</p>
                ) : (() => {
                  // 19 行里重复 19 遍同一个前缀是噪音。前缀在这里说一次，每一行只留真正不同的那段。
                  const root = commonRoot(dataLocations.locations.map(row => row.path))
                  return (
                    <>
                      {root && <p className="shell-card-text" data-locations-root>{
                        (zh ? '都在 ' : 'all under ')}<code>{root}</code></p>}
                      {dataLocations.data_area_inside_system_dir && (
                        <p className="shell-card-text" role="note">{zh
                          ? '这个文件夹在会自动同步到网盘的位置（OneDrive、文档、桌面之类）。你的交付文件会被复制到别人的服务器上。我们打算把它挪出去。'
                          : 'This folder is inside a directory that syncs to a cloud drive. Your delivery files are copied to someone else’s servers. We plan to move it out.'}</p>
                      )}
                      <details data-testid="config-locations-detail">
                        <summary style={{ cursor: 'pointer', fontSize: 13 }}>
                          {locationsSummary(dataLocations.locations, zh)}
                        </summary>
                        <ul className="config-data-locations">
                          {dataLocations.locations.map(row => (
                            <li key={row.key} data-location={row.key}>
                              <span>{row.label}</span>
                              <code title={row.path}>{shortPath(row.path, root, zh)}</code>
                              <span className={`shell-tag${row.exists ? ' shell-tag--ok' : ''}`}>{row.exists ? (zh ? '存在' : 'present') : (zh ? '还没有' : 'not there')}</span>
                              {row.retention && (
                                <span className={`shell-tag${row.retention === 'forever' ? ' shell-tag--ok' : ''}`}
                                  data-retention={row.retention}>{retentionText(row.retention, zh)}</span>
                              )}
                              {row.in_backup === false && (
                                <small data-out-of-backup title={row.reason}>{zh ? `不进备份：${row.reason ?? ''}` : `not in a backup: ${row.reason ?? ''}`}</small>
                              )}
                              {row.note && <small>{row.note}</small>}
                            </li>
                          ))}
                        </ul>
                      </details>
                      {/* 这段是解释，不是状态。它该在需要时展开，而不是每次都占掉半屏。 */}
                      <details data-testid="config-layout-note">
                        <summary style={{ cursor: 'pointer', fontSize: 13 }}>{zh ? '文件都放在哪、怎么分的' : 'How these paths are organised'}</summary>
                        <p className="shell-card-text">{zh
                          ? LAYOUT_NOTE_ZH
                          : LAYOUT_NOTE_EN}</p>
                      </details>
                    </>
                  )
                })()}
              </section>

              {/* ── 三、清理：什么会被删 ── */}
              <section className="shell-card" data-testid="config-data-cleanup">
                <h3>{zh ? '清理' : 'Cleanup'}</h3>
                <p className="shell-card-text">{zh
                  ? '你的订单历史和资料永久保留，系统不自动删除——删不删由你在「资料」里决定。只有产品自己的东西会自动清理，保留 30 天：搭建台试跑记录、系统运行日志、临时中转文件。'
                  : 'Your order history and material are kept for good and the system never removes them — you decide, from 「资料」. Only the product’s own things are cleaned up after 30 days: builder trial records, run logs and temporary hand-over files.'}</p>
                {onAskCleanupPreview && (
                  <button type="button" className="shell-button" data-action="cleanup-preview"
                    onClick={onAskCleanupPreview}>{zh ? '看看会删掉什么' : 'Show me what would go'}</button>
                )}
                {cleanupPreview && (
                  <>
                    <p className="shell-card-text" data-cleanup-total>{zh
                      ? `会清掉 ${cleanupPreview.totals.items} 项（${cleanupPreview.totals.files} 个文件）。没动的有：你的资料 ${cleanupPreview.kept.protected} 项、还不到 ${cleanupPreview.keep_days} 天的 ${cleanupPreview.kept.too_young} 项、认不出来的 ${cleanupPreview.kept.unknown} 项。`
                      : `${cleanupPreview.totals.items} items (${cleanupPreview.totals.files} files) would go. The gate stopped: ${cleanupPreview.kept.protected} of your material, ${cleanupPreview.kept.too_young} younger than ${cleanupPreview.keep_days} days, ${cleanupPreview.kept.unknown} the product does not recognise.`}</p>
                    <ul className="config-data-locations">
                      {cleanupPreview.families.map(family => (
                        <li key={family.key} data-cleanup-family={family.key}>
                          <span>{family.label_zh}</span>
                          <span className={`shell-tag${family.items ? '' : ' shell-tag--ok'}`}>
                            {family.items ? `${family.items} ${family.unit_zh}` : (zh ? '没有可清理的' : 'nothing to clean')}
                          </span>
                          {family.sample.length > 0 && (
                            <small>{family.sample.slice(0, 8).map(item => item.name).join('、')}
                              {family.sample.length > 8 ? '…' : ''}</small>
                          )}
                        </li>
                      ))}
                    </ul>
                    <p className="shell-card-text" role="note">{zh
                      ? '现在只是看看，不会真删。'
                      : 'This phase only looks: deleting is not built yet.'}</p>
                  </>
                )}
              </section>
            </section>
          )
        })()}
        {section === 'org' && orgBoard && (
          <section aria-label={SECTION_LABELS.org[zh ? 0 : 1]} className="shell-config-org" data-testid="config-org">
            <header className="shell-page-head"><h2>{SECTION_LABELS.org[zh ? 0 : 1]}</h2>
              <p>{zh ? '角色、名册、汇报结构、委派策略与已保存的组织。业务流的步骤按角色指派，执行时解析成具体的人；改动从下一单起生效。' : 'Roles, the roster, reporting structure, delegation and saved organisations. A flow’s steps name roles, resolved to people at run time; a change applies from the next order.'}</p></header>
            {orgBoard}
          </section>
        )}
      </div>
    </main>
  )
}
