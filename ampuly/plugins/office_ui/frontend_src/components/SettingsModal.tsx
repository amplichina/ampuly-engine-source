import { useEffect, useRef, useState, type ReactNode } from 'react'
import { TIERS, recommendedTiers, residencyLabel, tierLabel } from '../workspace/modelTiers'
import { useI18n } from '../i18n'
import { classifyPageError, pageErrorMessageKey } from '../lib/pageError'
import { modelRefusedText, modelStanding, offeredModels, type ModelEntry } from '../workspace/stepModels'

export interface SettingsProvider {
  id: string
  name: string
  api_key_env: string
  models: string[]
  note_zh: string
  note_en: string
  configured: boolean
  key_masked: string
  /** What each model is and whether it may be newly chosen (the console's model catalogue); absent from an older console. */
  entries?: ModelEntry[]
}

export interface SettingsRuntimeFlag {
  id: string
  env: string
  name_zh: string
  name_en: string
  note_zh: string
  note_en: string
  caution_zh: string
  caution_en: string
  enabled: boolean
}

export interface SettingsNotifyField {
  env: string
  label_zh: string
  label_en: string
  secret: boolean
  set: boolean
  value: string
}

export interface SettingsNotifyChannel {
  id: string
  name_zh: string
  name_en: string
  note_zh: string
  note_en: string
  configured: boolean
  fields: SettingsNotifyField[]
}

export interface GateStatus {
  configured: boolean
  muted?: boolean
  has_credentials: boolean
  chat: string
  owner_open_id: string
  listener: { status: string; last_error?: string; received?: number; sdk?: boolean }
  recent_senders: { at: string; open_id: string; chat_id: string; text: string }[]
}

export interface SettingsNotifyResult {
  ok: boolean
  error?: string
  results: Array<{ channel: string; ok: boolean; detail: string }>
}

export interface SettingsData {
  providers: SettingsProvider[]
  notify?: { channels: SettingsNotifyChannel[]; muted?: boolean }
  gen_providers: SettingsProvider[]
  /** Judgment models (PULY, answered by JEV): typed answers, never a default or a step's model. */
  judge_providers?: SettingsProvider[]
  /** Roadmap names with nothing behind them yet. Never rendered as work in progress. */
  gen_tools_not_built: Array<{ id: string; name_zh: string; name_en: string; state?: string; missing_zh?: string; missing_en?: string }>
  runtime_flags: SettingsRuntimeFlag[]
  flags_writable: boolean
  default_model: string
  routing: Record<string, string>
  /** For each model slot (default, quick_tasks) and each of the six task kinds, whether a call on it could be
   *  sent now, and if not why. A dry run that sends nothing. */
  route_health?: Record<string, { usable: boolean; reason: string }>
  // 模型分配 (routing phase 1). What 轻 / 标准 / 强 mean, and the rules around them. Read here; written through
  // settings_set_model_tiers, which has its own audit profile -- a tier is not a routing slot.
  tiers?: Record<string, string>
  tier_params?: Record<string, Record<string, unknown>>
  task_tiers?: Record<string, string>
  residency?: string
  no_training_optout?: Record<string, string>
  work_window_cap?: number
  keys_writable: boolean
}

export interface SettingsTestResult {
  model: string
  ok: boolean
  latencyMs?: number
  error?: string
  /** The version that answered, when the service says (a judgment model does). */
  answeredBy?: string
}

interface SettingsModalProps {
  data: SettingsData | null
  error?: string | null
  testResult: SettingsTestResult | null
  notifyResult?: SettingsNotifyResult | null
  onClose: () => void
  onSetKey: (apiKeyEnv: string, value: string) => void
  onTestModel: (model: string) => void
  onSetRouting: (defaultModel: string, routing: Record<string, string>) => void
  /** 模型分配: what the three tiers mean, and the rules around them. Its own action, its own audit profile. */
  onSetModelTiers?: (tiers: Record<string, string>, tierParams: Record<string, Record<string, unknown>>,
                     taskTiers: Record<string, string>, residency: string,
                     noTrainingOptout: Record<string, string>, workWindowCap: number) => void
  onSetFlag: (env: string, enabled: boolean) => void
  onNotifyTest?: () => void
  accountsSlot?: ReactNode
  gateStatus?: GateStatus | null
  onGateStatus?: () => void
  onRetry?: () => void
  connected?: boolean
  /** Open scrolled to this service's row, so 配置 lands on the one the person was looking at. */
  focusProvider?: string
  /** Drawn on 设置, not as a dialog. No overlay and no close control; the page names the section. */
  embedded?: boolean
}

export function SettingsModal({ data, error, testResult, notifyResult, onClose, onSetKey, onTestModel, onSetRouting, onSetModelTiers, onSetFlag, onNotifyTest, accountsSlot, gateStatus, onGateStatus, onRetry, connected = true, focusProvider = '', embedded = false }: SettingsModalProps) {
  const { t, locale } = useI18n()
  const [keyDrafts, setKeyDrafts] = useState<Record<string, string>>({})
  const [testingModel, setTestingModel] = useState<string | null>(null)
  // Start from what is set, so the first paint already shows it (and a value the lists no longer offer is kept).
  const [draftDefault, setDraftDefault] = useState(() => data?.default_model ?? '')
  const [draftQuick, setDraftQuick] = useState(() => data?.routing.quick_tasks ?? '')

  // Opened from 配置 on one service: bring that row to the top of the body, so the person lands where they were
  // looking instead of at the top of a dialog holding every service. Only scrolls; it changes nothing.
  // 模型分配's own draft and its own dirty flag: saving a tier and pinning a model to a slot are different
  // decisions, recorded as different things, so one cannot be half-saved by pressing the other's button.
  const [draftTiers, setDraftTiers] = useState<Record<string, string>>(() => ({ ...(data?.tiers ?? {}) }))
  const [draftTierParams, setDraftTierParams] = useState<Record<string, Record<string, unknown>>>(
    () => ({ ...(data?.tier_params ?? {}) }))
  const [draftResidency, setDraftResidency] = useState<string>(() => String(data?.residency ?? 'cn_only'))
  const [draftOptout, setDraftOptout] = useState<Record<string, string>>(() => ({ ...(data?.no_training_optout ?? {}) }))
  const [draftWindowCap, setDraftWindowCap] = useState<number>(() => Number(data?.work_window_cap ?? 128000))
  const same = (a: unknown, b: unknown) => JSON.stringify(a ?? {}) === JSON.stringify(b ?? {})
  const tiersDirty = !same(draftTiers, data?.tiers) || !same(draftTierParams, data?.tier_params)
    || draftResidency !== String(data?.residency ?? 'cn_only') || !same(draftOptout, data?.no_training_optout)
    || draftWindowCap !== Number(data?.work_window_cap ?? 128000)
  const bodyRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!focusProvider || !data) return
    const timer = window.setTimeout(() => {
      bodyRef.current?.querySelector<HTMLElement>(`[data-provider="${CSS.escape(focusProvider)}"]`)?.scrollIntoView({ block: 'center' })
    }, 40)
    return () => window.clearTimeout(timer)
  }, [focusProvider, data])

  useEffect(() => {
    if (!data) return
    setDraftDefault(data.default_model)
    setDraftQuick(data.routing.quick_tasks ?? '')
  }, [data])

  useEffect(() => {
    if (testResult && testResult.model === testingModel) setTestingModel(null)
  }, [testResult, testingModel])

  const frame = (body: ReactNode) => embedded
    ? <div className="settings-embedded" data-testid="settings-embedded">{body}</div>
    : (
      <div className="settings-overlay" onClick={onClose}>
        <div className="settings-modal" onClick={e => e.stopPropagation()}>{body}</div>
      </div>
    )

  if (!data) {
    return frame(error ? (
      <div className={`settings-error-state settings-error-state--${classifyPageError(error)}`} role="alert" data-error-kind={classifyPageError(error)}>
        <div className="settings-error-message"><strong>{t(pageErrorMessageKey(error) as Parameters<typeof t>[0])}</strong><br />{error}</div>
        <div className="settings-routing-actions">
          <button className="settings-btn settings-btn--primary" onClick={onRetry} disabled={!connected || !onRetry}>
            {t('settings.retry')}
          </button>
          {!embedded && <button className="settings-link-btn" onClick={onClose}>{t('settings.close')}</button>}
        </div>
      </div>
    ) : (
      <div className="settings-loading">{t('settings.loading')}</div>
    ))
  }

  // Only models the catalogue lets a setting newly choose; a value already set is kept, shown and marked with why.
  const configuredModels = data.providers.filter(p => p.configured).flatMap(offeredModels)
  const keptLabel = (model: string) => {
    const refused = modelRefusedText(modelStanding(data.providers, model), t)
    return refused ? (locale === 'zh-CN' ? `${model}（${refused}）` : `${model} (${refused})`) : model
  }
  const routeNote = (slot: string) => {
    const health = data.route_health?.[slot]
    if (!health || health.usable) return null
    const known = `settings.routeReason.${health.reason}`
    const reason = known === 'settings.routeReason.key_not_set' || known === 'settings.routeReason.model_unknown' ? t(known) : health.reason
    return <span className="settings-note settings-note--warn" data-testid={`route-health-${slot}`}>{t('settings.routeUnusable', { reason })}</span>
  }
  const zh = locale === 'zh-CN'
  const routingDirty = draftDefault !== data.default_model || draftQuick !== (data.routing.quick_tasks ?? '')

  // Which vendors the drafted tiers name: only those need a 「退出训练」 record, and only under that setting.
  const tierVendors = Array.from(new Set(Object.values(draftTiers)
    .map(model => String(model || '').split('/')[0]).filter(Boolean)))

  const renderProviderRow = (provider: SettingsProvider) => {
    const draft = keyDrafts[provider.api_key_env] ?? ''
    // The test pings the first model that may be chosen: never a retired, overseas-off or unroutable id.
    const testModel = offeredModels(provider)[0] ?? ''
    const canTest = Boolean(testModel)
    const isTesting = canTest && testingModel === testModel
    const result = canTest && testResult?.model && provider.models.includes(testResult.model) ? testResult : null
    return (
      <div key={provider.id} className="settings-provider-row" data-provider={provider.id}>
        <div className="settings-provider-main">
          <span className="settings-provider-name">{provider.name}</span>
          <span className="settings-provider-note">{zh ? provider.note_zh : provider.note_en}</span>
          <span className={`settings-key-status${provider.configured ? ' ok' : ''}`}>
            {provider.configured
              ? `${t('settings.keyConfigured')} ${provider.key_masked}`
              : t('settings.keyMissing')}
          </span>
        </div>
        <div className="settings-provider-actions">
          {data.keys_writable && (
            <>
              <input
                type="password"
                className="settings-key-input"
                placeholder={t('settings.keyPlaceholder')}
                value={draft}
                autoComplete="off"
                disabled={!connected}
                onChange={e => setKeyDrafts(prev => ({ ...prev, [provider.api_key_env]: e.target.value }))}
              />
              <button
                className="settings-btn"
                disabled={!connected || !draft.trim()}
                onClick={() => {
                  onSetKey(provider.api_key_env, draft.trim())
                  setKeyDrafts(prev => ({ ...prev, [provider.api_key_env]: '' }))
                }}
              >
                {t('settings.saveKey')}
              </button>
            </>
          )}
          {canTest && (
            <button
              className="settings-btn"
              disabled={!connected || !provider.configured || isTesting}
              onClick={() => {
                setTestingModel(testModel)
                onTestModel(testModel)
              }}
            >
              {isTesting ? t('settings.testing') : t('settings.test')}
            </button>
          )}
          {result && (
            <span className={`settings-test-result ${result.ok ? 'ok' : 'bad'}`}>
              {result.ok
                ? `${t('settings.testOk', { ms: result.latencyMs ?? 0 })}${result.answeredBy ? ` · ${result.answeredBy}` : ''}`
                : `✗ ${result.error ?? ''}`.slice(0, 80)}
            </span>
          )}
        </div>
      </div>
    )
  }

  const renderNotifyChannel = (channel: SettingsNotifyChannel) => (
    <div key={channel.id} className="settings-provider-row settings-notify-row">
      <div className="settings-provider-main">
        <span className="settings-provider-name">{zh ? channel.name_zh : channel.name_en}</span>
        <span className="settings-provider-note">{zh ? channel.note_zh : channel.note_en}</span>
        <span className={`settings-key-status${channel.configured ? ' ok' : ''}`}>
          {channel.configured ? t('settings.notifyConfigured') : t('settings.notifyMissing')}
        </span>
      </div>
      <div className="settings-notify-fields">
        {channel.fields.map(field => {
          const draft = keyDrafts[field.env] ?? ''
          return (
            <div key={field.env} className="settings-notify-field">
              <label>{zh ? field.label_zh : field.label_en}{field.set ? ' ✓' : ''}</label>
              {data.keys_writable && (
                <>
                  <input
                    type={field.secret ? 'password' : 'text'}
                    className="settings-key-input"
                    placeholder={field.secret ? t('settings.keyPlaceholder') : (field.value || '')}
                    value={draft}
                    autoComplete="off"
                    disabled={!connected}
                    onChange={e => setKeyDrafts(prev => ({ ...prev, [field.env]: e.target.value }))}
                  />
                  <button
                    className="settings-btn"
                    disabled={!connected || !draft.trim()}
                    onClick={() => {
                      onSetKey(field.env, draft.trim())
                      setKeyDrafts(prev => ({ ...prev, [field.env]: '' }))
                    }}
                  >
                    {t('settings.saveKey')}
                  </button>
                </>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )

  return frame(
    <>
        {!embedded && (
          <div className="settings-header">
            <h2>{t('settings.title')}</h2>
            <button className="settings-close" onClick={onClose} aria-label={t('settings.close')}>✕</button>
          </div>
        )}
        {!connected && <div className="settings-note settings-note--offline" role="status">{t('settings.offline')}</div>}

        <div className="settings-body" ref={bodyRef}>
          <section>
            <h3 className="settings-section-title">{t('settings.llmSection')}</h3>
            {!data.keys_writable && (
              <div className="settings-note">{t('settings.keysReadonly')}</div>
            )}
            <div className="settings-provider-list">
              {data.providers.map(renderProviderRow)}
            </div>
          </section>

          <section>
            <h3 className="settings-section-title">{t('settings.routingSection')}</h3>
            <div className="settings-routing-row">
              <label>{t('settings.defaultModel')}</label>
              <select value={draftDefault} disabled={!connected} onChange={e => setDraftDefault(e.target.value)}>
                {!configuredModels.includes(draftDefault) && draftDefault && (
                  <option value={draftDefault}>{keptLabel(draftDefault)}</option>
                )}
                {configuredModels.map(model => <option key={model} value={model}>{model}</option>)}
              </select>
              {routeNote('default')}
            </div>
            <div className="settings-routing-row">
              <label>{t('settings.quickModel')}</label>
              <select value={draftQuick} disabled={!connected} onChange={e => setDraftQuick(e.target.value)}>
                <option value="">{t('settings.followDefault')}</option>
                {!configuredModels.includes(draftQuick) && draftQuick && (
                  <option value={draftQuick}>{keptLabel(draftQuick)}</option>
                )}
                {configuredModels.map(model => <option key={model} value={model}>{model}</option>)}
              </select>
              {routeNote('quick_tasks')}
            </div>
            <div className="settings-tier-table" data-testid="settings-model-tiers">
              <h4>{t('settings.tiersSection')}</h4>
              {TIERS.map(tier => {
                const params = draftTierParams[tier] ?? {}
                const setParam = (patch: Record<string, unknown>) =>
                  setDraftTierParams({ ...draftTierParams, [tier]: { ...params, ...patch } })
                return (
                  <div className="settings-routing-row" key={tier} data-testid={`settings-tier-${tier}`}>
                    <label>{tierLabel(tier, zh)}</label>
                    <select value={draftTiers[tier] ?? ''} disabled={!connected}
                      onChange={e => setDraftTiers({ ...draftTiers, [tier]: e.target.value })}>
                      <option value="">{t('settings.followDefault')}</option>
                      {!configuredModels.includes(draftTiers[tier] ?? '') && (draftTiers[tier] ?? '') && (
                        <option value={draftTiers[tier]}>{keptLabel(draftTiers[tier] ?? '')}</option>
                      )}
                      {configuredModels.map(model => <option key={model} value={model}>{model}</option>)}
                    </select>
                    <label className="settings-tier-thinking">
                      <input type="checkbox" disabled={!connected} checked={Boolean(params.thinking)}
                        onChange={e => setParam({ thinking: e.target.checked })} />
                      <span>{zh ? '开思考模式' : 'Thinking on'}</span>
                    </label>
                  </div>
                )
              })}
              <div className="settings-routing-row">
                <label>{t('settings.residency')}</label>
                <select value={draftResidency} disabled={!connected} data-testid="settings-residency"
                  onChange={e => setDraftResidency(e.target.value)}>
                  <option value="cn_only">{residencyLabel('cn_only', zh)}</option>
                  <option value="cn_no_training">{residencyLabel('cn_no_training', zh)}</option>
                  <option value="allow_overseas">{residencyLabel('allow_overseas', zh)}</option>
                </select>
              </div>
              {draftResidency === 'cn_no_training' && tierVendors.map(vendor => (
                <div className="settings-routing-row" key={vendor}>
                  <label>{t('settings.noTrainingOptout')} · {vendor}</label>
                  <input type="text" disabled={!connected} data-testid={`settings-optout-${vendor}`}
                    value={draftOptout[vendor] ?? ''}
                    placeholder={zh ? '谁在什么时候办的' : 'who did it, and when'}
                    onChange={e => setDraftOptout({ ...draftOptout, [vendor]: e.target.value })} />
                </div>
              ))}
              <div className="settings-routing-row">
                <label>{t('settings.windowCap')}</label>
                <input type="number" min={0} disabled={!connected} data-testid="settings-window-cap"
                  value={draftWindowCap} onChange={e => setDraftWindowCap(Number(e.target.value || 0))} />
              </div>
              <div className="settings-routing-actions">
                <button className="settings-btn" disabled={!connected} data-testid="settings-fill-recommended"
                  onClick={() => {
                    // Fills the draft and saves nothing: the owner looks at it and presses save themselves.
                    const picked = recommendedTiers()
                    setDraftTiers({ ...draftTiers, ...picked.tiers })
                    setDraftTierParams({ ...draftTierParams, ...picked.tierParams })
                  }}>
                  {t('settings.fillRecommended')}
                </button>
                <button className="settings-btn settings-btn--primary" data-testid="settings-save-tiers"
                  disabled={!connected || !tiersDirty || !onSetModelTiers}
                  onClick={() => onSetModelTiers?.(draftTiers, draftTierParams, data.task_tiers ?? {},
                                                   draftResidency, draftOptout, draftWindowCap)}>
                  {zh ? '保存档位' : 'Save tiers'}
                </button>
              </div>
            </div>
            <div className="settings-routing-actions">
              <button
                className="settings-btn settings-btn--primary"
                disabled={!connected || !routingDirty}
                onClick={() => onSetRouting(draftDefault, { quick_tasks: draftQuick })}
              >
                {t('settings.applyRouting')}
              </button>
              <span className="settings-note">{t('settings.routingHint')}</span>
            </div>
          </section>

          {(data.judge_providers ?? []).length > 0 && (
            <section data-testid="settings-judge">
              <h3 className="settings-section-title">{t('settings.judgeSection')}</h3>
              <div className="settings-provider-list">
                {(data.judge_providers ?? []).map(renderProviderRow)}
              </div>
              <div className="settings-note">{t('settings.judgeHint')}</div>
            </section>
          )}

          {accountsSlot}

          {data.notify && (
            <section>
              <h3 className="settings-section-title">{t('settings.notifySection')}</h3>
              <div className="settings-note">{t('settings.notifyHint')}</div>
              {(data.notify.muted || gateStatus?.muted) && (
                <div className="settings-note settings-channels-muted" role="status" data-channels-muted="true">
                  {t('settings.channelsMuted')}
                </div>
              )}
              <div className="settings-provider-list">
                {data.notify.channels.map(renderNotifyChannel)}
              </div>
              <div className="settings-routing-actions">
                <button
                  className="settings-btn"
                  disabled={!connected || !onNotifyTest || !!data.notify.muted || !data.notify.channels.some(c => c.configured)}
                  onClick={() => onNotifyTest?.()}
                >
                  {t('settings.notifyTest')}
                </button>
                {notifyResult && (
                  <span className={`settings-test-result ${notifyResult.ok ? 'ok' : 'bad'}`}>
                    {notifyResult.error
                      ? `✗ ${notifyResult.error}`
                      : notifyResult.results.map(r => `${r.channel}: ${r.ok ? '✓' : `✗ ${r.detail}`}`).join(' · ').slice(0, 160)}
                  </span>
                )}
                <button className="settings-test-btn" onClick={onGateStatus} disabled={!connected || !onGateStatus}>{t('settings.gateStatus')}</button>
                {gateStatus && (
                  <div className="settings-gate-status">
                    <div>
                      {t('settings.gateListener')}: <b>{gateStatus.listener.status}</b>
                      {gateStatus.listener.last_error ? ` · ${gateStatus.listener.last_error}` : ''}
                      {typeof gateStatus.listener.received === 'number' ? ` · ${t('settings.gateReceived', { n: gateStatus.listener.received })}` : ''}
                      {' · '}{gateStatus.chat ? t('settings.gateChatSet') : t('settings.gateChatMissing')}
                    </div>
                    {gateStatus.recent_senders.length > 0 ? (
                      <ul className="settings-gate-recent">
                        {gateStatus.recent_senders.map((row, i) => (
                          <li key={`${row.at}-${i}`}>
                            <code>{row.at}</code> open_id <code>{row.open_id || '—'}</code> chat_id <code>{row.chat_id || '—'}</code> {row.text ? `“${row.text}”` : ''}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <div className="settings-note">{t('settings.gateRecentHint')}</div>
                    )}
                  </div>
                )}
              </div>
            </section>
          )}
          <section>
            <h3 className="settings-section-title">{t('settings.genSection')}</h3>
            <div className="settings-provider-list">
              {(data.gen_providers ?? []).map(renderProviderRow)}
            </div>
            <div className="settings-gen-list">
              {(data.gen_tools_not_built ?? []).map(tool => (
                <span key={tool.id} className="settings-gen-chip" title={(zh ? tool.missing_zh : tool.missing_en) || undefined}>
                  {zh ? tool.name_zh : tool.name_en} · {t('settings.notBuilt')}
                </span>
              ))}
            </div>
            {(data.gen_tools_not_built ?? []).length > 0 && (
              <div className="settings-note">{t('settings.notBuiltNote')}</div>
            )}
            <div className="settings-note">{t('settings.genHint')}</div>
          </section>

          <section>
            <h3 className="settings-section-title">{t('settings.flagsSection')}</h3>
            {(data.runtime_flags ?? []).length === 0 ? (
              <div className="settings-note">{t('settings.flagsEmpty')}</div>
            ) : (
              (data.runtime_flags ?? []).map(flag => (
                <div key={flag.id} className="settings-flag-row">
                  <label className="settings-flag-main">
                    <input
                      type="checkbox"
                      checked={flag.enabled}
                      disabled={!connected || !data.flags_writable}
                      onChange={e => onSetFlag(flag.env, e.target.checked)}
                    />
                    <span className="settings-flag-name">{zh ? flag.name_zh : flag.name_en}</span>
                    <span className={`settings-flag-state${flag.enabled ? ' on' : ''}`}>
                      {flag.enabled ? t('settings.flagOn') : t('settings.flagOff')}
                    </span>
                  </label>
                  <div className="settings-note">{zh ? flag.note_zh : flag.note_en}</div>
                  {(zh ? flag.caution_zh : flag.caution_en) && (
                    <div className="settings-flag-caution">
                      ⚠ {zh ? flag.caution_zh : flag.caution_en}
                    </div>
                  )}
                </div>
              ))
            )}
          </section>
        </div>

        <div className="settings-footer">{t('settings.securityNote')}</div>
    </>
  )
}
