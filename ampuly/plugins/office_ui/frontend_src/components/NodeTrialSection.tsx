import { useEffect, useMemo, useState } from 'react'
import { ArtifactPreviewOverlay, type ArtifactPreview } from './ArtifactsPanel'
import { DEFAULT_IMAGE_LIMIT, IMAGE_TOOL, MAX_IMAGE_LIMIT, imagesOfUnknownOutcome, parseImageLimit, composeReply, reconcileSelection, trialGate, type NodeTrialPin, type NodeTrialPinning, type PinSelection } from '../lib/nodeTrialPins'

export interface NodeTrialRecord {
  spec_id: string
  node_id: string
  state: string
  model_call_limit: number
  /** How many images this trial may make; 0 for a step that generates none. */
  image_limit?: number
  artifacts: Array<{ path: string; size: number; from_upstream?: boolean; changed_here?: boolean }>
  counts_as_evidence: boolean
  pinned_upstream?: boolean
  pins?: Array<{ node_id: string; spec_id: string; files: Array<{ path: string }> }>
  released_by_assumption?: string[]
  /** The step asked a person and the trial waits: what it asked. */
  question?: { step: string; reason: string; context_note?: string; questions: Array<{ id: string; header: string; question: string; options: string[]; allow_freeform: boolean }> }
  unproduced?: string[]
  removed_here?: string[]
  artifacts_truncated?: boolean
  execution_error?: string
  /** Why the trial ended without a verdict of its own (asked more often than one trial allows). */
  ended?: string
  budget?: { state?: string; consumed?: Record<string, number>; held?: Record<string, number> }
  round?: { failure_reason?: string; provider_error?: string }
  /** The model the saved step asks for ('' = the default). */
  model_requested?: string
  /** What the step's calls were sent with, from the attempt's own record (the route, not a verified remote identity). */
  model_route?: { model?: string; source?: string }
}

export interface TrialArtifactState {
  preview: (ArtifactPreview & { spec_id?: string; from_upstream?: boolean; changed_here?: boolean }) | null
  loading: boolean
  error: string | null
}

function blockedZh(plan: NodeTrialPinning): string {
  const step = plan.blocked_step ? `「${plan.blocked_step}」` : ''
  switch (plan.blocked_code) {
    case 'human_hands_over_files': return `上游人工节点${step}要交出文件，单节点试跑造不出来；请运行完整工作流。`
    case 'upstream_not_pinnable': return `上游节点${step}不是 AI 步骤（例如调用另一条流程），它的结果无法钉住；请运行完整工作流。`
    case 'unknown_upstream': return `它依赖的${step}不在已保存的工作流里。`
    case 'tools_unavailable': return `这一步用到了单节点试跑提供不了的工具（${plan.blocked_step ?? ''}）；请运行完整工作流来试它。`
    case 'unavailable': return `服务器没能给出上游清单（${plan.blocked}）。`
    default: return plan.blocked
  }
}

function stateZh(state: string): string {
  return ({ queued: '排队中', running: '运行中', succeeded: '成功', failed: '失败', waiting_for_answer: '等你回答', abandoned: '已放弃', outcome_unknown: '结果不明' } as Record<string, string>)[state] ?? state
}

function when(iso: string): string {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return iso
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${parsed.getMonth() + 1}/${parsed.getDate()} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`
}

/** "Run this step for real" for one saved AI step: a root step runs as is; a step with upstream steps runs on the
 *  succeeded trials a person pins for them. What it left opens in place. */
/** What a step asked a person inside a trial, beside the step: the question, the offered answers, a box for the
 *  person's own words, and a way out. Nobody answers for the person; an empty answer is not sent. */
export function TrialQuestion({ zh, trial, busy, onAnswer, onAbandon }: {
  zh: boolean
  trial: NodeTrialRecord
  busy: boolean
  onAnswer: (specId: string, reply: string) => void
  onAbandon: (specId: string) => void
}) {
  const [answers, setAnswers] = useState<string[]>([])
  const question = trial.question
  if (!question) return null
  const items = question.questions.length ? question.questions : [{ id: '', header: '', question: question.reason, options: [], allow_freeform: true }]
  const single = items.length === 1
  const reply = composeReply(items, answers)
  const set = (index: number, value: string) => setAnswers(previous => { const next = [...previous]; next[index] = value; return next })
  return (
    <div className="wf-node-trial-question" role="group" aria-label={zh ? '这一步在问你' : 'This step is asking you'}>
      <strong>{zh ? '这一步停下来问你' : 'This step stopped to ask you'}</strong>
      {question.reason && <span>{question.reason}</span>}
      {items.map((item, index) => (
        <div key={item.id || index} className="wf-node-trial-question-item">
          <span>{items.length > 1 ? `${index + 1}. ` : ''}{item.header ? `${item.header}：` : ''}{item.question}</span>
          {item.options.length > 0 && (
            <div className="wf-node-trial-question-options">
              {item.options.map(option => (
                // One question: an offered answer is the whole reply. Several: it fills in this question only.
                <button type="button" key={option} disabled={busy} aria-pressed={!single && answers[index] === option}
                  onClick={() => (single ? onAnswer(trial.spec_id, option) : set(index, option))}>{option}</button>
              ))}
            </div>
          )}
          {item.allow_freeform && (
            <textarea rows={2} value={answers[index] ?? ''} placeholder={zh ? '用你自己的话回答' : 'Answer in your own words'} onChange={event => set(index, event.target.value)} />
          )}
        </div>
      ))}
      {question.context_note && <em>{question.context_note}</em>}
      {(!single || items[0].allow_freeform) && (
        <button type="button" className="wf-node-preflight-run" disabled={busy || !reply} onClick={() => onAnswer(trial.spec_id, reply)}>
          {zh ? '回答并继续试跑' : 'Answer and continue the trial'}
        </button>
      )}
      <span>{zh ? '继续会用同一份预算、同一个工作区把这一步带着你的回答再跑一遍；等待期间预算保持打开。' : 'Continuing runs this step again with your answer, on the same allowance and workspace; the allowance stays open while it waits.'}</span>
      <button type="button" className="wf-node-edit-cancel" disabled={busy} onClick={() => onAbandon(trial.spec_id)}>{zh ? '放弃这次试跑（关闭预算）' : 'Abandon this trial (closes the allowance)'}</button>
    </div>
  )
}

export function NodeTrialSection({ zh, node, draftOpen, busy, trials, pinning, onPlan, onRun, artifact, onOpenArtifact, onCloseArtifact, onAnswer, onAbandon }: {
  zh: boolean
  node: { id: string; depends_on?: string[]; tools?: string[] }
  /** An unsaved draft is open: a real trial only ever runs a saved version. */
  draftOpen: boolean
  busy: boolean
  trials: NodeTrialRecord[]
  pinning: NodeTrialPinning | null
  onPlan?: (nodeId: string) => void
  /** `imageLimit` only for a step that generates images. */
  onRun: (nodeId: string, pins: NodeTrialPin[], imageLimit?: number) => void
  artifact?: TrialArtifactState
  onOpenArtifact?: (specId: string, path: string) => void
  onCloseArtifact?: () => void
  onAnswer?: (specId: string, reply: string) => void
  onAbandon?: (specId: string) => void
}) {
  const dependsOn = node.depends_on ?? []
  const plan = pinning && pinning.node_id === node.id ? pinning : null
  // What the person chose, reconciled against the plan while rendering -- not in an effect, which would show one
  // frame of "still missing" for a plan that has a perfectly good default, or keep another node's choices.
  const [chosen, setSelection] = useState<PinSelection>({})
  const selection = useMemo(() => reconcileSelection(plan, chosen), [plan, chosen])
  const latest = trials.find(trial => trial.node_id === node.id)
  const makesImages = (node.tools ?? []).includes(IMAGE_TOOL)
  // Kept as typed; it is a ceiling only when it parses as one, and the run button waits until it does.
  const [imageLimitText, setImageLimitText] = useState(String(DEFAULT_IMAGE_LIMIT))
  const imageLimit = parseImageLimit(imageLimitText)
  const unknownImages = latest ? imagesOfUnknownOutcome(latest) : 0

  // The plan is the server's: ask for it when a step with upstream steps is opened, and again when one of this
  // workflow's trials settles -- a trial that just succeeded upstream is a new thing to pin.
  const settled = trials.filter(trial => trial.state === 'succeeded').length
  useEffect(() => {
    // Every step asks: a step with upstream steps needs to know what to pin, and any step may name a tool a node
    // trial cannot provide.
    if (!draftOpen) onPlan?.(node.id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node.id, dependsOn.length, draftOpen, settled])

  // A file opened from this step's trial belongs to this panel: closing the editor or moving to another step
  // closes it, so it cannot reappear later over a step it has nothing to do with.
  useEffect(() => () => { onCloseArtifact?.() },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [node.id])

  const gate = trialGate({ dependsOn, nodeId: node.id, draftOpen, busy, waiting: latest?.state === 'waiting_for_answer', pinning, selection })
  const pinned = dependsOn.length > 0

  return (
    <div className="wf-node-preflight wf-node-trial">
      {pinned && plan && !plan.blocked && (
        <div className="wf-node-trial-pins" role="group" aria-label={zh ? '上游产物' : 'Upstream results'}>
          <strong>{zh ? '这一步用哪次上游试跑的产物' : 'Which upstream trial this step runs on'}</strong>
          {plan.upstream.map(step => (
            <label key={step.node_id} className="wf-node-edit-field">
              <span>{step.name || step.node_id}{step.required ? '' : (zh ? '（可选）' : ' (optional)')}</span>
              {step.candidates.length === 0 ? (
                <em role="status">{step.required
                  ? (zh ? '还没有可用的成功试跑：先试跑这一步上游（须是当前已保存版本）。' : 'No succeeded trial of the saved version yet: trial this upstream step first.')
                  : (zh ? '没有可选的试跑' : 'nothing to pin')}</em>
              ) : (
                <select value={selection[step.node_id] ?? ''} onChange={event => setSelection(previous => ({ ...previous, [step.node_id]: event.target.value }))}>
                  {!step.required && <option value="">{zh ? '不带' : 'none'}</option>}
                  {step.candidates.map(item => (
                    <option key={item.spec_id} value={item.spec_id}>
                      {when(item.created_at)} · {zh ? `${item.files} 个文件` : `${item.files} files`}{item.pinned_upstream ? (zh ? ' · 含它的上游产物' : ' · carries its upstream') : ''}
                    </option>
                  ))}
                </select>
              )}
            </label>
          ))}
          {plan.released_by_assumption.length > 0 && (
            <span role="note">{zh
              ? `中间的人工节点（${plan.released_by_assumption.join('、')}）按「已放行」来试；正式运行仍要人放行。`
              : `Human steps in between (${plan.released_by_assumption.join(', ')}) are assumed released for this trial; a real run still waits for them.`}</span>
          )}
        </div>
      )}
      <button type="button" className="wf-node-preflight-run" disabled={!gate.canRun || (makesImages && imageLimit === null)}
        onClick={() => {
          if (makesImages && imageLimit === null) return
          const outside = zh ? '外部工具不会执行' : 'outside tools will not execute'
          const body = (text: string) => makesImages
            ? text.replace(outside, zh ? '除出图工具外，其他外部工具不会执行' : 'no outside tool other than the image generator will execute')
            : text
          const images = zh
            ? `这一步会真实联网出图（与正式订单同一个出图工具和默认图像模型，会花钱）：本次最多 ${imageLimit} 张，到上限就停下来报告，不重试。出图较慢，这次试跑最长可能要 10 分钟。\n`
            : `This step generates real images over the network (the same image tool and default image model as a real order; it costs money): at most ${imageLimit} this time; at the ceiling it stops and reports, no retry. Generation is slow: this trial may take up to 10 minutes.\n`
          const approved = window.confirm((makesImages ? images : '') + body(zh
            ? (pinned
              ? '带着选定的上游试跑产物，真实调用模型试跑这一步？最多 12 次模型调用，只能写隔离工作区；外部工具不会执行。结果不算发布证据。'
              : '真实调用模型试跑这一步？最多 12 次模型调用，只能写隔离工作区；外部工具不会执行。')
            : (pinned
              ? 'Run this step against the real model on the pinned upstream results? At most 12 model calls, isolated workspace only; outside tools will not execute. The result is not release evidence.'
              : 'Run this step against the real model? At most 12 model calls, isolated workspace only; outside tools will not execute.')))
          if (approved) onRun(node.id, gate.pins, makesImages && imageLimit !== null ? imageLimit : undefined)
        }}>
        {busy ? (zh ? '正在受理…' : 'Accepting…') : (zh ? '真实试跑这一步' : 'Run this step for real')}
      </button>
      {makesImages && (
        <label className="wf-node-edit-field wf-node-trial-images">
          <span>{zh ? `本次最多出图（1–${MAX_IMAGE_LIMIT} 张）` : `Images this trial may make (1–${MAX_IMAGE_LIMIT})`}</span>
          <input type="number" min={1} max={MAX_IMAGE_LIMIT} step={1} value={imageLimitText} disabled={!gate.canRun}
            aria-invalid={imageLimit === null} onChange={event => setImageLimitText(event.target.value)} />
          {imageLimit === null && <em role="status">{zh ? `填 1 到 ${MAX_IMAGE_LIMIT} 的整数，才能试跑。` : `Enter a whole number from 1 to ${MAX_IMAGE_LIMIT} to run.`}</em>}
        </label>
      )}
      <span>{makesImages
        ? (zh ? `预算上限：12 次模型调用 · 出图最多 ${imageLimit ?? '—'} 张（联网、计费）· 隔离文件工具 · 不接触渠道和生产数据` : `Budget: 12 model calls · at most ${imageLimit ?? '—'} images (network, billed) · isolated file tools · no channel or production data`)
        : (zh ? '预算上限：12 次模型调用 · 隔离文件工具 · 不接触渠道和生产数据' : 'Budget: 12 model calls · isolated file tools · no channel or production data')}</span>
      {gate.reason === 'draft' && <span role="status">{zh ? '先保存当前草稿；真实试跑只认已保存版本。' : 'Save this draft first; real trials only use saved versions.'}</span>}
      {gate.reason === 'waiting' && <span role="status">{zh ? '这一步有一次试跑在等你回答：先回答或放弃它，再开新的试跑。' : 'A trial of this step is waiting for your answer: answer or abandon it before starting another.'}</span>}
      {gate.reason === 'plan_pending' && <span role="status">{zh ? '正在确认这一步需要哪些上游产物…' : 'Checking which upstream results this step needs…'}</span>}
      {gate.reason === 'blocked' && plan && <span role="status">{zh ? `这一步不能用单节点试跑：${blockedZh(plan)}` : `This step cannot be tried on its own: ${plan.blocked}`}</span>}
      {gate.reason === 'missing_upstream' && (
        <span role="status">{zh
          ? `还缺上游产物：${gate.missing.map(step => step.name || step.node_id).join('、')}。单节点试跑不会伪造输入。`
          : `Upstream results still missing: ${gate.missing.map(step => step.name || step.node_id).join(', ')}. A node trial never fabricates inputs.`}</span>
      )}
      {latest && (
        <div className={`wf-node-preflight-result${latest.state === 'succeeded' ? ' is-ready' : latest.state === 'failed' ? ' is-blocked' : ''}`} role="status">
          <strong>{zh ? `真实试跑：${stateZh(latest.state)}` : `Real trial: ${latest.state}`}</strong>
          {latest.state === 'waiting_for_answer' && onAnswer && onAbandon && (
            <TrialQuestion zh={zh} trial={latest} busy={busy} onAnswer={onAnswer} onAbandon={onAbandon} />
          )}
          <span>{zh ? `上限 ${latest.model_call_limit} 次模型调用` : `${latest.model_call_limit} model-call limit`}</span>
          {latest.model_route?.model && (
            <span data-testid="node-trial-model">{zh
              ? `调用发往 ${latest.model_route.model}（${latest.model_route.source === 'node' ? '这一步选的' : '默认模型'}）`
              : `Calls sent to ${latest.model_route.model} (${latest.model_route.source === 'node' ? 'this step\'s own' : 'the default'})`}</span>
          )}
          {latest.model_requested && latest.model_route?.model && latest.model_route.model !== latest.model_requested && (
            <span className="wf-node-trial-unknown">{zh
              ? `这一步要的是 ${latest.model_requested}，但调用发往了 ${latest.model_route.model}。`
              : `This step asks for ${latest.model_requested}, but its calls went to ${latest.model_route.model}.`}</span>
          )}
          {(latest.image_limit ?? 0) > 0 && <span>{zh ? `出图 ${latest.budget?.consumed?.image ?? 0} / ${latest.image_limit} 张` : `${latest.budget?.consumed?.image ?? 0} / ${latest.image_limit} images`}</span>}
          {unknownImages > 0 && (
            <span className="wf-node-trial-unknown">{zh
              ? `有 ${unknownImages} 张图结果未知：请求已发出但没拿到结果，可能已经计费——请到图像服务的账单核对。系统不会自动重试。`
              : `${unknownImages} image(s) of unknown outcome: the request left and no result came back; they may have been billed -- check the image provider's bill. Nothing is retried automatically.`}</span>
          )}
          <span>{zh ? `已计量 ${latest.budget?.consumed?.model_call ?? 0} 次 · 预算 ${latest.budget?.state ?? 'open'}` : `${latest.budget?.consumed?.model_call ?? 0} metered · budget ${latest.budget?.state ?? 'open'}`}</span>
          {latest.pinned_upstream && (
            <span>{zh
              ? `用的是钉住的上游产物（${(latest.pins ?? []).map(pin => `${pin.node_id}：${pin.files.length} 个文件`).join('；')}）——不算发布证据。`
              : `Ran on pinned upstream results (${(latest.pins ?? []).map(pin => `${pin.node_id}: ${pin.files.length} files`).join('; ')}) -- not release evidence.`}</span>
          )}
          {(latest.unproduced ?? []).length > 0 && (
            <span>{zh
              ? `不算成功：承诺的交付物 ${(latest.unproduced ?? []).join('、')} 是上游带进来的，这一步没有写过它。`
              : `Not a success: the promised ${(latest.unproduced ?? []).join(', ')} was carried in from upstream and this step never wrote it.`}</span>
          )}
          {(latest.removed_here ?? []).length > 0 && (
            <span>{zh
              ? `上游带来、这一步之后已不在：${(latest.removed_here ?? []).join('、')}`
              : `Carried in from upstream and gone after this step: ${(latest.removed_here ?? []).join(', ')}`}</span>
          )}
          {latest.artifacts_truncated && (
            <span>{zh ? '文件太多或太大，记录不完整；这次试跑不能被下游钉住。' : 'Too many or too large files: the record is incomplete, and this trial cannot be pinned downstream.'}</span>
          )}
          {latest.ended && <span>{zh ? '这一步在一次试跑里提问的次数超过了上限，预算已关闭；请开一次新的试跑。' : latest.ended}</span>}
          {latest.execution_error && <span>{zh ? `运行中断：${latest.execution_error}` : `Interrupted: ${latest.execution_error}`}</span>}
          {latest.artifacts.length > 0 && (
            <ul className="wf-node-trial-files">
              {latest.artifacts.map(item => (
                <li key={item.path}>
                  {onOpenArtifact
                    ? <button type="button" className="wf-node-trial-file" onClick={() => onOpenArtifact(latest.spec_id, item.path)}>{item.path}</button>
                    : <span>{item.path}</span>}
                  <span> · {item.size} B</span>
                  {item.from_upstream && <em> · {item.changed_here ? (zh ? '上游带来，这一步改写过' : 'from upstream, rewritten here') : (zh ? '上游带来' : 'from upstream')}</em>}
                </li>
              ))}
            </ul>
          )}
          {latest.round?.failure_reason && <span>{latest.round.failure_reason}</span>}
          {latest.round?.provider_error && <span>{latest.round.provider_error}</span>}
          {latest.counts_as_evidence && <span>{zh ? '已留下真实运行证据；仍需人工决定是否纳入候选。' : 'Real-run evidence exists; a person still decides whether to admit it.'}</span>}
        </div>
      )}
      {artifact && onCloseArtifact && (
        <ArtifactPreviewOverlay
          preview={artifact.preview} loading={artifact.loading} error={artifact.error} onClose={onCloseArtifact}
          notice={zh
            ? `这是一次试跑留下的文件，不是发布证据，也不是订单交付物。${artifact.preview?.from_upstream ? (artifact.preview.changed_here ? '它来自上游试跑，这一步改写过。' : '它来自上游试跑，这一步没有改动它。') : ''}`
            : `A file one trial left: not release evidence, and not an order deliverable.${artifact.preview?.from_upstream ? (artifact.preview.changed_here ? ' It came from the upstream trial and this step rewrote it.' : ' It came from the upstream trial unchanged.') : ''}`}
        />
      )}
    </div>
  )
}
