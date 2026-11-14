/** Browser generations are observations, never credentials or a second permission model. */
export interface BrowserControlContext {
  resource_id: string
  control_fence: string
}

export interface BrowserScopedState {
  project_id?: string
  binding_key?: string
  resource_id?: string
  control_fence?: string
}

export interface BrowserAccountStatus {
  account_id?: string
  selection?: string
  availability?: string
  resource_id?: string
  control_fence?: string
  access_mode?: string
  login?: {
    state?: string
    observation_id?: string
    policy_id?: string
    policy_digest?: string
    clock?: string
    age_seconds?: number | null
    valid_for_seconds?: number | null
  }
  pending_known?: boolean
  pending?: Record<string, unknown> | null
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function identity(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 256
    && value.trim() === value && !/[\u0000-\u001f\u007f]/.test(value)
}

/** Copy the exact generation the person saw; callers must not fill it from a newer row later. */
export function browserControlContext(value: unknown): BrowserControlContext | null {
  const source = record(value)
  if (!source || !identity(source.resource_id) || !identity(source.control_fence)) return null
  return { resource_id: source.resource_id, control_fence: source.control_fence }
}

export function browserPushScope(value: unknown): (BrowserControlContext & { project_id: string; binding_key: string }) | null {
  const source = record(value)
  const context = browserControlContext(source)
  if (!source || !context || !identity(source.project_id) || !identity(source.binding_key)) return null
  return { project_id: source.project_id, binding_key: source.binding_key, ...context }
}

/** All four coordinates must agree before a picture is displayed or can receive input. */
export function browserFrameMatchesSession(frame: unknown, session: unknown): boolean {
  const picture = browserPushScope(frame)
  const current = browserPushScope(session)
  return Boolean(picture && current && picture.project_id === current.project_id
    && picture.binding_key === current.binding_key && picture.resource_id === current.resource_id
    && picture.control_fence === current.control_fence)
}

export function browserTaskControlText(state: string, zh: boolean): string {
  const labels: Record<string, [string, string]> = {
    created: ['已创建', 'created'], ready: ['就绪', 'ready'], agent_operating: ['代理操作中', 'agent operating'],
    awaiting_human: ['人工接管中', 'held by a person'], human_returned: ['人工已交还，待身份核验', 'returned, identity check pending'],
    identity_verified: ['身份已核验', 'identity verified'], lease_expired: ['租约已到期', 'lease expired'],
    revoked: ['已撤销', 'revoked'], closed: ['已关闭', 'closed'],
  }
  return labels[state]?.[zh ? 0 : 1] ?? (zh ? '控制状态未知' : 'control state unknown')
}

export function browserOutcomeText(outcome: string, zh: boolean, reason = ''): string {
  if (outcome === 'outcome_unknown' && reason === 'input_accepted_unverified') return zh ? '已送达，未核验' : 'input accepted, not verified'
  const labels: Record<string, [string, string]> = {
    succeeded: ['成功', 'succeeded'], failed_before_effect: ['发出前失败', 'failed before effect'],
    failed_after_effect: ['发出后失败', 'failed after effect'], outcome_unknown: ['结果未知', 'outcome unknown'],
    awaiting_human: ['待人确认', 'awaiting a person'], cancelled_unknown: ['已中止，结果待核验', 'stopped, outcome needs verification'],
    cancelled_before_effect: ['发出前已取消', 'cancelled before effect'], refused: ['已拒绝', 'refused'],
  }
  return labels[outcome]?.[zh ? 0 : 1] ?? outcome
}

export function browserAccountLines(account: BrowserAccountStatus | null | undefined, zh: boolean): Array<{ key: string; text: string }> {
  if (!account) return [{ key: 'profile', text: zh ? '账号资料：本次临时会话' : 'Account data: temporary session' }]
  const labels: Record<string, [string, string]> = {
    unverified: ['未检测', 'not checked'], authenticated: ['检测到已登录', 'signed-in marker observed'],
    login_required: ['需要登录', 'login required'], challenge: ['等待挑战验证', 'challenge required'],
    unknown: ['未知', 'unknown'], expired: ['检测已过期', 'check expired'],
  }
  const login = account.login
  let detection = labels[login?.state ?? 'unverified']?.[zh ? 0 : 1] ?? (zh ? '未知' : 'unknown')
  // These are monotonic durations, never Unix timestamps or local calendar dates.
  if (login?.clock === 'monotonic') {
    if (typeof login.age_seconds === 'number' && Number.isFinite(login.age_seconds) && login.age_seconds >= 0) {
      detection += zh ? ` · ${Math.floor(login.age_seconds)} 秒前检测` : ` · checked ${Math.floor(login.age_seconds)}s ago`
    }
    if (typeof login.valid_for_seconds === 'number' && Number.isFinite(login.valid_for_seconds) && login.valid_for_seconds >= 0) {
      detection += zh ? ` · 还有效 ${Math.floor(login.valid_for_seconds)} 秒` : ` · valid for ${Math.floor(login.valid_for_seconds)}s`
    }
  }
  const availability: Record<string, [string, string]> = {
    available: ['可用', 'available'], account_recovery_required: ['需要核验', 'verification required'],
    close_failed: ['关闭未完成', 'close incomplete'], account_revoked: ['已撤销', 'revoked'],
    account_busy: ['正在使用', 'in use'], account_unavailable: ['不可用', 'unavailable'],
  }
  return [
    { key: 'profile', text: zh ? `账号资料：${account.selection === 'retained_account' ? '已保留' : '状态未知'}` : `Account data: ${account.selection === 'retained_account' ? 'retained' : 'unknown'}` },
    { key: 'login', text: `${zh ? '登录检测：' : 'Login check: '}${detection}` },
    { key: 'availability', text: `${zh ? '账号资源：' : 'Account resource: '}${availability[account.availability ?? '']?.[zh ? 0 : 1] ?? (zh ? '不可用或未知' : 'unavailable or unknown')}` },
    { key: 'access', text: account.access_mode === 'verification_only'
      ? (zh ? '受限核验：不能继续业务操作' : 'Restricted verification: business actions unavailable')
      : (zh ? '任务许可：仍由当前任务与控制权决定' : 'Task permission: governed by the current task and control lease') },
    { key: 'pending', text: account.pending_known !== true || account.pending === undefined
      ? (zh ? '待核验：状态不可用' : 'Pending verification: status unavailable')
      : account.pending
        ? (zh ? '待核验：原操作结果尚未确认' : 'Pending verification: prior outcome unconfirmed')
        : (zh ? '待核验：无记录' : 'Pending verification: none recorded') },
  ]
}
