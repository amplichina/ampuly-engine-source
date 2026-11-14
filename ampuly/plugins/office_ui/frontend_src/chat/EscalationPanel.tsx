import React, { useCallback, useMemo } from 'react'
import type { ChatMessageMeta, CheckpointReplyMetadata, InteractionOption } from '../types/chat'
import { MarkdownBody } from './MarkdownBody'
import { useI18n } from '../i18n'
import { permissionText } from '../lib/permissionWording'

interface EscalationPanelProps {
  meta: ChatMessageMeta
  onReply: (text: string, metadata?: Partial<CheckpointReplyMetadata>) => void
  responded: boolean
  submitting?: boolean
}

function firstLine(text: string): string {
  return text.split('\n').map((line) => line.trim()).find(Boolean) ?? text
}

function checkpointStatusLabel(status: string): string {
  switch (status) {
    case 'timeout':
    case 'timed_out':
    case 'expired':
      return 'Expired'
    case 'stale':
    case 'invalid':
      return 'Inactive'
    case 'cancelled':
    case 'canceled':
      return 'Cancelled'
    case 'failed':
      return 'Failed'
    case 'outcome_unknown':
      return 'Outcome unknown'
    case 'resolved':
      return 'Resolved'
    default:
      return 'Responded'
  }
}

export const EscalationPanel = React.memo(function EscalationPanel({
  meta, onReply, responded, submitting = false,
}: EscalationPanelProps) {
  const { t, translateMaybe } = useI18n()
  const isResponded = responded
  const checkpointStatus = String(meta.checkpoint_status ?? '').trim().toLowerCase()
  const resolvedLabel = checkpointStatusLabel(checkpointStatus)
  const prompt = String(meta.prompt ?? meta.summary ?? '')
  const lines = useMemo(
    () => prompt.split('\n').map((line) => line.trim()).filter(Boolean),
    [prompt],
  )
  const title = firstLine(prompt).replace(/^\[[^\]]+\]\s*/, '') || t('esc.actionRequired')
  const isPermission = ['tool_permission', 'action_permission'].includes(String(meta.checkpoint_type ?? ''))
  const isEffectApproval = String(meta.effect_approval_protocol ?? '') === 'superones.effect-approval/v1'
  const effectPreview = useMemo(
    () => (meta.effect_preview && typeof meta.effect_preview === 'object' ? meta.effect_preview : {}),
    [meta.effect_preview],
  )
  const effectSimulation = useMemo(
    () => (meta.effect_simulation && typeof meta.effect_simulation === 'object' ? meta.effect_simulation : {}),
    [meta.effect_simulation],
  )
  const effectResult = useMemo(
    () => (meta.effect_result && typeof meta.effect_result === 'object' ? meta.effect_result : {}),
    [meta.effect_result],
  )
  const effectReceipt = useMemo(
    () => (effectResult.provider_receipt && typeof effectResult.provider_receipt === 'object'
      ? effectResult.provider_receipt
      : {}),
    [effectResult.provider_receipt],
  )
  const displayTitle = meta.checkpoint_type === 'tool_permission'
    ? t('esc.toolAuth')
    : meta.checkpoint_type === 'action_permission'
      ? t('esc.actionAuth')
      : title
  const permissionBadge = isPermission
    ? String(meta.checkpoint_type).replace(/_/g, ' ')
    : String(meta.escalation_type ?? 'decision_needed').replace(/_/g, ' ')
  // Non-permission cards use the first prompt line as their card title. A
  // typed permission card has a protocol title instead, so its whole prompt
  // remains request content; otherwise a one-line command/request vanishes.
  const request = isPermission
    ? prompt
    : lines.slice(1).join('\n').trim()
  // A tool request in the owner's words ("查看 订单文件夹外面的 …"), the same wording as the approval center, never
  // the prompt verbatim with its raw path (2026-09-25); the prompt stays, folded away. Only when the card names
  // the tool: an older card without it keeps the prompt as its request.
  const toolName = meta.checkpoint_type === 'tool_permission' ? String(meta.tool_name ?? '').trim() : ''
  const permissionWording = toolName ? permissionText(t, { subject: toolName, detail: String(meta.tool_argument ?? '') }) : ''
  const summary = String(meta.summary ?? '').trim()
  const options = (meta.options ?? []).filter((opt): opt is InteractionOption => !!opt?.id)
  const activeSubagents = useMemo(
    () => (meta.active_subagents ?? []).filter((item) => !!item && typeof item === 'object'),
    [meta.active_subagents],
  )
  const permissionRequests = useMemo(
    () => (meta.permission_requests ?? []).filter((item) => !!item && typeof item === 'object'),
    [meta.permission_requests],
  )
  const worktreePath = String(meta.worktree_path ?? '').trim()
  const hasRuntimeState = activeSubagents.length > 0 || permissionRequests.length > 0 || !!worktreePath

  const handleReply = useCallback((option: InteractionOption) => {
    if (isResponded || submitting) return
    onReply(option.label || option.id, {
      interaction_option_id: option.id,
      interaction_option_label: option.label || option.id,
      checkpoint_reply_kind: option.id.includes('deny') ? 'deny' : 'approve',
    })
  }, [isResponded, onReply, submitting])

  return (
    <div className="ckpt-panel ckpt-escalation">
      <div className="ckpt-header">
        <div className="ckpt-icon ckpt-icon-escalation">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M8 1.5L14.5 13H1.5L8 1.5Z" />
            <path d="M8 5.5V9" />
            <circle cx="8" cy="11.5" r="0.75" fill="currentColor" stroke="none" />
          </svg>
        </div>
        <div className="ckpt-title">{displayTitle}</div>
        <span className="ckpt-badge ckpt-badge-scope">
          {translateMaybe('ckpt.badge', permissionBadge)}
        </span>
        {isResponded && <span className="ckpt-badge ckpt-badge-responded">{translateMaybe('ckpt.status', resolvedLabel)}</span>}
      </div>

      {summary && summary !== title && (
        <div className="ckpt-section">
          <div className="ckpt-section-title">{t('ckpt.summary')}</div>
          <MarkdownBody
            content={summary}
            className="ckpt-markdown"
            collapseMode={isPermission ? 'never' : 'auto'}
          />
        </div>
      )}

      {permissionWording && (
        <div className="ckpt-section" data-testid="ckpt-permission-wording">
          <div className="ckpt-section-title">{t('ckpt.request')}</div>
          <p className="ckpt-permission-sentence">{permissionWording}</p>
          <p className="ckpt-runtime-body">{t('approvals.permission.lead')}</p>
          {request && (
            <details className="ckpt-permission-raw">
              <summary>{t('approvals.permission.raw')}</summary>
              <pre className="ckpt-permission-verbatim"><code>{request}</code></pre>
            </details>
          )}
        </div>
      )}

      {request && !permissionWording && (
        <div className="ckpt-section">
          <div className="ckpt-section-title">{t('ckpt.request')}</div>
          {isPermission ? (
            <pre className="ckpt-permission-verbatim"><code>{request}</code></pre>
          ) : (
            <MarkdownBody content={request} className="ckpt-markdown" />
          )}
        </div>
      )}

      {isEffectApproval && (
        <div className="ckpt-section ckpt-effect-preview">
          <div className="ckpt-section-title">执行预览 / Effect preview</div>
          <div className="ckpt-effect-approval-boundary" role="note">
            副作用审批仅可在 SuperOnes 控制台完成；飞书卡片只承载业务闸门，不会代签真实写入。
            <span> Effect approvals are console-only; Feishu cards cannot authorize external writes.</span>
          </div>
          <div className="ckpt-runtime-body">
            {meta.effect_kind && <div>Operation <code>{meta.effect_kind}</code></div>}
            {meta.effect_provider && <div>Provider <code>{meta.effect_provider}</code></div>}
            {meta.effect_environment && <div>Environment <code>{meta.effect_environment}</code></div>}
            {meta.approval_type && <div>Approval <code>{meta.approval_type}</code></div>}
            {effectSimulation.status && (
              <div data-testid="effect-simulation-kind">
                Simulation <code>{String(effectSimulation.status)}</code>
                {' '}
                <span className={String(effectSimulation.status) === 'preview_only' ? 'ckpt-warn' : undefined}>
                  {t(`esc.simulation.${String(effectSimulation.status)}` as Parameters<typeof t>[0])}
                </span>
              </div>
            )}
          </div>
          {Object.keys(effectPreview).length > 0 && (
            <pre className="ckpt-permission-verbatim"><code>{JSON.stringify(effectPreview, null, 2)}</code></pre>
          )}
          {effectSimulation.details && Object.keys(effectSimulation.details).length > 0 && (
            <pre className="ckpt-permission-verbatim"><code>{JSON.stringify(effectSimulation.details, null, 2)}</code></pre>
          )}
          {(Object.keys(effectResult).length > 0 || Object.keys(effectReceipt).length > 0) && (
            <div className="ckpt-runtime-body">
              <div className="ckpt-section-title">通知回执 / Delivery receipt</div>
              {effectResult.status && <div>Execution <code>{String(effectResult.status)}</code></div>}
              {effectResult.state && <div>Effect state <code>{String(effectResult.state)}</code></div>}
              {effectResult.verification_status && <div>Verification <code>{String(effectResult.verification_status)}</code></div>}
              {typeof effectReceipt.delivery_status !== 'undefined' && <div>已发送 / Sent <code>{String(effectReceipt.delivery_status)}</code></div>}
              {typeof effectReceipt.read_status !== 'undefined' && <div>已读 / Read <code>{String(effectReceipt.read_status)}</code></div>}
              {typeof effectReceipt.feedback_status !== 'undefined' && <div>已反馈 / Feedback <code>{String(effectReceipt.feedback_status)}</code></div>}
              {typeof effectReceipt.business_status !== 'undefined' && <div>业务状态 / Business <code>{String(effectReceipt.business_status)}</code></div>}
              {effectResult.last_error && <div className="ckpt-error">Error <code>{String(effectResult.last_error)}</code></div>}
            </div>
          )}
        </div>
      )}

      {hasRuntimeState && (
        <details className="ckpt-runtime-details">
          <summary>{t('ckpt.runtimeState')}</summary>
          <div className="ckpt-runtime-body">
            {worktreePath && <div>{t('ckpt.worktree')} <code>{worktreePath}</code></div>}
            {activeSubagents.length > 0 && <div>{t('ckpt.activeSubagents', { count: activeSubagents.length })}</div>}
            {permissionRequests.length > 0 && <div>{t('ckpt.pendingPermissions', { count: permissionRequests.length })}</div>}
          </div>
        </details>
      )}

      {!isResponded && options.length > 0 && (
        <div className="ckpt-actions ckpt-escalation-actions">
          {options.map((option) => (
            <button
              key={option.id}
              className={`ckpt-btn ${option.id.includes('deny') ? 'ckpt-btn-deny' : 'ckpt-btn-approve'}${submitting ? ' ckpt-btn-submitting' : ''}`}
              onClick={() => handleReply(option)}
              disabled={submitting}
            >
              {translateMaybe('ckpt.option', option.label || option.id)}
            </button>
          ))}
        </div>
      )}

      {!isResponded && meta.default_action && (
        <div className="ckpt-escalation-hint">
          {t('ckpt.defaultOnTimeout')} <code>{meta.default_action}</code>
        </div>
      )}
    </div>
  )
})
