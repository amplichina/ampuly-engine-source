import { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { ChatMessage } from '../types/chat'
import { useI18n } from '../i18n'
import type { ArtifactQuoteDraft } from '../lib/artifactReference'
import { nodeProposalFrom, type NodeProposal } from '../lib/nodeProposal'
import type { MasterAttachment, MasterReference } from '../lib/wsClient'
import { componentAskStates, flowDraftFrom, type ComponentAskState } from '../lib/flowDraft'
import { FlowDraftCard, componentAskText } from './FlowDraftCard'
import { FlowComponentCandidateCard } from './FlowComponentCandidateCard'
import { FlowComponentAdoptionControl } from './FlowComponentAdoptionControl'
import type { DraftSendResult } from '../lib/flowComponentAdoption'
import { componentCandidatesFrom, continueDraftText, type FlowDraftReference } from '../lib/flowComponentCandidate'
import { proposalWidens, wideningText } from '../workspace/workflowGraph'
import {
  attachmentCancelKey, attachmentCancellable, attachmentReadLine, attachmentsReadFrom, attachmentSourcesFrom, attachmentStatus,
  attachmentStatusWord, attachmentWords, cancelWaiting, previewNote, sourceLabel, sourceUrl, type AttachmentSource,
  type SourcePreviewState,
} from '../lib/attachmentsRead'
import { attachmentDownloadUrl } from '../lib/attachmentUrl'
import { statusSentence, type TodayCounts, type TodayGroup } from '../lib/masterToday'
import { masterToolWords } from '../lib/masterToolWords'
import { buildCardFrom, continueDraft, lastTalk, liveBuildMessages } from '../lib/masterBuild'
import { MasterBuildCard, type MasterBuildHooks } from './MasterBuildCard'

export interface MasterToolActivity {
  messageId: string
  sequence: number
  toolName: string
  state: 'started' | 'finished' | 'refused' | 'failed' | 'cancelled' | 'outcome_unknown'
}

/** What a message carries besides its words (4.7 Q04): what it points at, and files. */
export interface MasterSendExtras {
  references: MasterReference[]
  attachments: MasterAttachment[]
}

/** A file picked for the next message, read as the order chat reads one (a data: URL). */
interface PickedFile extends MasterAttachment {
  size: number
}

// The server's limits (master_attachments.py); checked here too so a person is told before sending.
const MAX_FILES = 5
const MAX_TOTAL_BYTES = 20 * 1024 * 1024

interface MasterControlPageProps {
  /** The conversation shown, whole (总控对话栏, policy: one conversation per thread; nothing is folded). */
  messages: ChatMessage[]
  /** The conversation is named but its messages are not on hand yet (its project's history or list still coming): a
   *  quiet line says it is being opened, instead of the landing turning into the conversation a moment later. */
  opening?: boolean
  /** Why 总控 is on a new conversation instead of the one the address named (gone, archived, no longer takes
   *  messages): one short line on the landing, inside the conversation area -- nothing above it. */
  threadNote?: string | null
  /** The small button in the title line that shows the conversation column: at narrow widths it opens the column as a
   *  drawer; at full width it is drawn only while the column is collapsed (the stylesheet's). Absent = no column. */
  threadsToggle?: { label: string; expanded: boolean; onToggle: () => void }
  connected: boolean
  /** Returns false when the message did not go out: the words stay in the box. */
  onSend: (content: string, extras?: MasterSendExtras) => boolean | void
  /** What 安安 is shown as the thing a message is about (the builder's flow or open step). Absent = nothing offered. */
  reference?: { label: string; value: MasterReference } | null
  artifactQuote?: ArtifactQuoteDraft | null
  onClearArtifactQuote?: (key: string) => void
  /** Absent = no ＋文件 control (a console that cannot store files for this project). */
  canAttach?: boolean
  onOpenOrder: (taskId: string) => void
  /** 安安起草的工单 (policy, M1): the order a reply drafted, in 业务工单 where it waits 「待接单」. Absent = no
   * link is drawn. A reply from before that decision still links the session it opened (onOpenOrder). */
  onOpenDraftedOrder?: (orderId: string) => void
  /** The title line's counts (policy: the top of 总控 is the title line alone), from owner_attention_list
   * only. Absent = a console that is not the owner's, or 安安's side panel on another page: the conversation alone. */
  today?: MasterToday
  /** The tool events of the turn being answered (App keeps the last twenty): the step still running is said in the
   *  thinking bubble, an outcome that is unknown once at the end of the conversation. No strip of its own. */
  toolActivity?: MasterToolActivity[]
  /** 安安's turn is running now (the server says so): the 停止这一轮 / 插话 row shows by the input. */
  canInterrupt?: boolean
  interruptPending?: boolean
  interruptRequested?: boolean
  interruptError?: string | null
  onInterrupt?: () => void
  /** Owner-only: one more instruction for the turn that is running now. Absent = no steer control at all.
   *  Returns whether it actually went out; text that was not sent stays in the box. */
  onSteer?: (text: string) => boolean
  steerPending?: boolean
  /** What the server said about the last steer; delivery is never reported as "applied". */
  steerNote?: string | null
  /** Open a node-table proposal on the builder canvas (it is previewed there, never saved from here). */
  onOpenNodeProposal?: (proposal: NodeProposal) => void
  /** 在搭建台打开 a new-flow draft from 安安's reply (4.7 Q04-B), run by the chosen saved team. Absent = not the owner's. */
  onOpenFlowDraft?: (messageId: string, team: string, source?: FlowDraftReference, expectedMaterialized?: unknown) => void
  onComponentDraftAction?: (payload: Record<string, unknown>) => DraftSendResult
  componentDraftAck?: Record<string, unknown> | null
  /** The reply whose draft is being opened right now. */
  flowDraftOpening?: string
  /** Where a connector candidate 安安 drafted for a draft's missing component is tested and reviewed (4.7 A05).
   *  Absent = the card says a candidate was drafted, without the way there. */
  onOpenConnectorCandidate?: (candidateId: string) => void
  /** Saved teams a new flow can run on. */
  teams?: Array<{ name: string; label: string }>
  /** 继续上次的工作: what is really unfinished or latest (lib/recentWork), one line of links on the landing only. */
  recent?: Array<{ key: string; title: string; sub: string; onOpen: () => void }>
  /** The configured default model, as the settings report it ('' until they arrive). */
  defaultModel?: string
  /** Open 配置, where the model is chosen. Absent = no way in is drawn. */
  onOpenModelConfig?: () => void
  /** 取消 a file the vision model is waiting on or reading (4.7 A13). Absent = no cancel button is drawn. Returns
   *  false when the request did not go out, so the button stays usable. */
  onCancelAttachment?: (messageId: string, attachmentId: string) => boolean | void
  /** How many times the server refused a 取消, by attachmentCancelKey: a refusal makes that file's 取消 usable again. */
  cancelRefusals?: Readonly<Record<string, number>>
  /** Ask the server for one page of a cited file (a PDF page, another file's text) for the side panel. Absent = the
   *  panel offers only the original file. Pictures need no asking: they are shown as they are. */
  onLoadSource?: (source: AttachmentSource) => void
  /** What came back for the page last asked for. */
  sourcePreview?: SourcePreviewState | null
  /** 总控施工模式 (policy, B2): what a build card can reach -- the step each running job is on, 停下 / 插话,
   * the answers to a build's question, the builder on its draft. The owner's console only; absent = each job's card
   * shows where the build stands and offers nothing to press. */
  build?: MasterBuildHooks
  /** Words a new conversation's first send could not deliver (the conversation never came back: no receipt, a refusal,
   * the connection dropped): put back in the box, so nothing the owner typed is lost. A new `seq` each
   * time; an empty box is filled, one the owner has typed into again is left alone. */
  restoreDraft?: { text: string; seq: number } | null
}

/** 总控's counts, as App hands them in (policy): what the server said (lib/masterToday reads the reply), where
 * the read stands, and where each number goes. Every number is the server's; none is counted here. The rows of what
 * waits and their card left 总控 with that decision: those orders are handled in 业务工单 and on the Feishu cards. */
export interface MasterToday {
  /** null = no counts came (an older server, or nothing back yet): nothing is counted. */
  counts: TodayCounts | null
  /** How many projects the server could not read: their waiting is unknown, not zero, and the title line says so. */
  partial?: number
  /** Asked, nothing back within the list's time (lib/requestTracker): a small note in the title line, with 刷新. */
  stale: boolean
  /** The read was refused, in words: a small note in the title line. */
  error: string | null
  /** A number in the title line: 业务工单 on the group with the same filter. */
  onOpenGroup: (group: TodayGroup) => void
  onRefresh?: () => void
}

function dispatchedTaskId(message: ChatMessage): string {
  if (message.sender === 'user') return ''
  const meta = message.metadata
  return String(meta?.task_id ?? meta?.taskId ?? '').trim()
}

// The work order a reply drafted (policy, M1): its id rides in the reply's metadata only -- the words name it
// by title -- and the link opens it in 业务工单, never a session.
function draftedOrderId(message: ChatMessage): string {
  if (message.sender === 'user') return ''
  const meta = message.metadata as Record<string, unknown> | undefined
  return meta?.type === 'master_order_drafted' ? String(meta?.order_id ?? '').trim() : ''
}

// What a reply looked at and did, from the message's own metadata. Names and
// read/write only; the running step in the thinking bubble is ephemeral, this survives a reload.
// Shown in the owner's words (lib/masterToolWords), each tool once with how many times; the raw name stays in a data
// attribute (owner report ).
function recordedToolCalls(message: ChatMessage): {name: string; writes: boolean}[] {
  if (message.sender === 'user') return []
  const raw: unknown = message.metadata?.master_tool_calls
  if (!Array.isArray(raw)) return []
  return raw.flatMap(item => {
    const name = String((item as {name?: unknown})?.name ?? '').trim()
    if (!name.startsWith('master_') || name.length > 96) return []
    return [{name, writes: (item as {writes?: unknown})?.writes === true}]
  }).slice(0, 40)
}

function recordedToolGroups(calls: {name: string; writes: boolean}[]): {name: string; writes: boolean; times: number}[] {
  const groups = new Map<string, {name: string; writes: boolean; times: number}>()
  for (const call of calls) {
    const group = groups.get(call.name)
    if (group) group.times += 1
    else groups.set(call.name, { ...call, times: 1 })
  }
  return [...groups.values()]
}

export function MasterControlPage({ messages, opening = false, threadNote = null, threadsToggle, connected, onSend, reference = null, canAttach = false, onOpenOrder, onOpenDraftedOrder, today,
  toolActivity = [], canInterrupt = false, interruptPending = false, interruptRequested = false, interruptError = null, onInterrupt, onSteer, steerPending = false, steerNote = null, onOpenNodeProposal, onOpenFlowDraft, flowDraftOpening = '', onOpenConnectorCandidate, teams = [], recent, defaultModel = '', onOpenModelConfig, onCancelAttachment, cancelRefusals = {}, onLoadSource, sourcePreview = null, build, restoreDraft = null, onComponentDraftAction, componentDraftAck, artifactQuote = null, onClearArtifactQuote }: MasterControlPageProps) {
  const { t, locale } = useI18n()
  const zh = locale.startsWith('zh')
  const words = attachmentWords(zh)
  // 来源定位 (4.7 A13): the file a reply cites, opened in the side panel at its page. One at a time.
  const [openSource, setOpenSource] = useState<AttachmentSource | null>(null)
  const showSource = (source: AttachmentSource) => {
    setOpenSource(source)
    if (source.kind !== 'image') onLoadSource?.(source)
  }
  // The files already asked to stop, so a second click does not send a second cancel before the status line changes;
  // each remembers how many refusals it had when pressed, so a refusal since makes 取消 usable again.
  const [cancelAsked, setCancelAsked] = useState<ReadonlyMap<string, number>>(() => new Map())
  const [draft, setDraft] = useState('')
  const [artifactSubmission, setArtifactSubmission] = useState<{ key: string; text: string } | null>(null)
  const [steerDraft, setSteerDraft] = useState('')
  const listRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  useEffect(() => {
    setArtifactSubmission(null)
    if (!artifactQuote) return
    setDraft(current => current.trim() ? current : (zh ? '请帮我分析这段内容：' : 'Please help me analyze this excerpt:'))
    inputRef.current?.focus()
    // A new explicit quote is offered once; it never sends itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [artifactQuote?.key])
  // 施工卡片 (B2): each build job's latest message carries its live card; its earlier messages are plain lines.
  const liveBuilds = useMemo(() => liveBuildMessages(messages), [messages])
  // 接着改 on a build card: 「接着改：」 in the input, focused; the owner finishes the sentence and sends it to 安安.
  const continueBuild = () => {
    setDraft(current => continueDraft(current, t('master.build.continuePrefix')))
    inputRef.current?.focus()
  }
  // A first send that never went out comes back into the box (restoreDraft above).
  useEffect(() => {
    if (!restoreDraft || !restoreDraft.text) return
    setDraft(current => (current.trim() ? current : restoreDraft.text))
    inputRef.current?.focus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [restoreDraft?.seq])
  // The reference is offered, not forced: one click leaves it out of the next message, and a new one is offered again.
  const [referenceOff, setReferenceOff] = useState('')
  const referenceKey = reference ? JSON.stringify(reference.value) : ''
  const referenceOn = Boolean(reference) && referenceOff !== referenceKey
  const [files, setFiles] = useState<PickedFile[]>([])
  const [fileNote, setFileNote] = useState('')
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  // Picked with ＋, dropped on the input row, or pasted (a screenshot on the clipboard): one way in, the same limits.
  const [dropping, setDropping] = useState(false)
  const pickFiles = (list: FileList | readonly File[] | null) => {
    const picked = Array.from(list ?? [])
    if (!picked.length) return
    setFileNote('')
    const room = MAX_FILES - files.length
    if (picked.length > room) setFileNote(t('master.attach.tooMany', { n: MAX_FILES }))
    let total = files.reduce((sum, file) => sum + file.size, 0)
    for (const file of picked.slice(0, Math.max(0, room))) {
      if (total + file.size > MAX_TOTAL_BYTES) { setFileNote(t('master.attach.tooLarge', { mb: MAX_TOTAL_BYTES / (1024 * 1024) })); break }
      total += file.size
      const reader = new FileReader()
      reader.onload = () => setFiles(current => (current.length >= MAX_FILES ? current : [...current, {
        filename: file.name, mime_type: file.type || 'application/octet-stream', data: String(reader.result ?? ''), size: file.size,
      }]))
      reader.readAsDataURL(file)
    }
  }

  // 总控对话栏 (policy): the conversation is one thread, shown whole. The 9-28 fold (lib/masterVisit:
  // 「接着上次的对话」 as one line under the input) is gone with it: 总控 opens on a new conversation -- the landing -- and
  // the earlier ones are a click away in the column. The page still never turns by itself once it is on screen: a new
  // conversation is the landing until the owner sends in it, and a conversation that is named but not on hand yet says
  // it is being opened (opening) rather than showing the landing first.
  const shown = messages

  // A build's lines (B2) are left out: one landing while 安安 is still answering neither answers the owner nor ends the
  // wait (a build started in 安安's turn posts its first line before the reply).
  const lastShown = lastTalk(shown)
  const awaitingReply = !!lastShown && lastShown.sender === 'user'
  // The latest tool event while a reply is being made: only a step still running is worth a word in the bubble.
  const lastStep = toolActivity.length ? toolActivity[toolActivity.length - 1] : null
  const stepNow = awaitingReply && lastStep && lastStep.state === 'started' ? lastStep : null
  // A step whose outcome is unknown may have done something: said once, at the end of the conversation -- each tool
  // once, in the owner's words (lib/masterToolWords); the raw names only in data attributes.
  // Only the latest turn's (the events of one turn share its message id): once 安安 is on to the next turn, an older
  // turn's alert is no longer hung at the end of a conversation that has moved on (review of MT).
  const latestTurn = lastStep ? lastStep.messageId : ''
  const unknownSteps = toolActivity.filter(event => event.messageId === latestTurn && event.state === 'outcome_unknown')
  const unknownWhat = [...new Set(unknownSteps.map(event => masterToolWords(event.toolName, zh)))].join(zh ? '、' : ', ')

  // 让安安起草这个组件 (4.7 A05): every request a draft card can send, and what the conversation made of each -- read
  // from the messages, so a candidate drafted before a reload is still shown on its placeholder.
  const componentAsks = useMemo(() => {
    const asks = new Map<string, string>()
    for (const message of messages) {
      if (message.sender === 'user') continue
      const found = flowDraftFrom(message.metadata)
      if (!found) continue
      for (const gap of found.missing) {
        const text = componentAskText(found, gap.step, t)
        if (text) asks.set(text, `${message.id}\n${gap.step}`)
      }
    }
    return componentAskStates(messages, asks)
  }, [messages, t])
  const componentAsksFor = (messageId: string): Record<string, ComponentAskState> => Object.fromEntries(
    Object.entries(componentAsks)
      .filter(([key]) => key.startsWith(`${messageId}\n`))
      .map(([key, value]) => [key.slice(messageId.length + 1), value]),
  )

  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [shown.length, awaitingReply])

  const send = () => {
    const content = draft.trim()
    if ((!content && !files.length) || !connected) return
    const references: MasterReference[] = reference && referenceOn ? [reference.value] : []
    if (artifactQuote) references.push(artifactQuote.reference)
    const attachments = files.map(({ filename, mime_type, data }) => ({ filename, mime_type, data }))
    // A message that did not go out (not connected, the console still switching project) keeps its words in the box.
    const went = references.length || attachments.length ? onSend(content, { references, attachments }) : onSend(content)
    if (went === false) return
    setDraft('')
    setFiles([])
    setFileNote('')
    // Like a selected canvas node, the quote remains until explicitly removed.
    // Transport acceptance is not server acceptance: keep the original words
    // available if a stale revision is refused. This never retries a send.
    if (artifactQuote) setArtifactSubmission({ key: artifactQuote.key, text: content })
  }

  const quickAsk = (text: string) => {
    if (!connected || awaitingReply) return
    onSend(text)
  }

  // The landing (policy: no 今天 block on 总控 any more) is always the hero -- the brand, one sentence, the
  // input -- under the title line. Every number in the title line is the server's (lib/masterToday): none is counted here.
  // A conversation still being opened is not a landing: its messages are on their way.
  const landing = messages.length === 0 && !opening
  const sentence = today?.counts ? statusSentence(today.counts, zh) : null
  // What the title line says beside the numbers when the read did not simply come back: a refusal in words, or the
  // wait ran out (then 刷新). A read still out says nothing, rather than a spinner.
  const countsNote = !today ? null
    : today.error ? { kind: 'error' as const, text: t('master.today.failed', { reason: today.error }) }
    : today.stale ? { kind: 'stale' as const, text: t('master.today.slow') }
    : null
  const partial = today?.partial ?? 0
  // The words of the 停止这一轮 / 插话 row (unchanged): where the stop stands, and the steer's receipt or hint.
  const interruptWords = t(interruptRequested ? 'master.interruptRequested' : interruptPending ? 'master.interruptPending' : 'master.interruptHint')
  const steerWords = steerNote || t(steerPending ? 'master.steerPending' : 'master.steerHint')

  return (
    <div className="master-page" data-master-landing={landing ? 'hero' : undefined}>
      {/* policy: the top of 总控 is the title line alone. The counts sit in it, small, to the right of 总控
          and its subtitle -- never a line of their own; nothing else stands between it and the conversation but the
          offline notice. The live tool strip, the 今天 rows with their card, and the 停止这一轮 / 插话 panel left the top:
          the running step is said in the thinking bubble, an unknown outcome once at the end of the conversation, the
          orders that wait are handled in 业务工单 and on the Feishu cards, and the stop / steer row sits by the input. */}
      <div className="master-header">
        {threadsToggle && (
          // 总控对话栏 (policy): the column's way back -- in the title line itself, never a line of its own.
          <button type="button" className="master-threads-toggle" data-testid="master-threads-toggle" aria-label={threadsToggle.label}
            title={threadsToggle.label} aria-expanded={threadsToggle.expanded} onClick={threadsToggle.onToggle}>☰</button>
        )}
        <h2 className="master-title">{t('master.title')}</h2>
        <span className="master-subtitle">{t('master.subtitle')}</span>
        {today && (sentence || countsNote || partial > 0) && (
          // The one number set on the page: each number opens 业务工单 on the group with the same filter. 0 is not
          // coloured; only 出错 is, and only when it is not 0 (lib/masterToday).
          <p className="master-today-sentence" data-testid="master-today-sentence" aria-label={t('master.today.title')}>
            {sentence?.map((part, index) => (
              <span key={part.group}>
                {index > 0 && <span className="master-today-sep" aria-hidden="true"> · </span>}
                <button type="button" className="master-today-count" data-today-group={part.group} data-tone={part.tone || undefined}
                  onClick={() => today.onOpenGroup(part.group)}>{part.text}</button>
              </span>
            ))}
            {/* Projects the server could not read: their waiting is unknown, never counted as zero. */}
            {partial > 0 && <small className="master-today-partial" data-testid="master-today-partial"> {t('master.today.partial', { n: partial })}</small>}
            {countsNote && (
              <small className="master-today-note" role={countsNote.kind === 'error' ? 'alert' : 'status'} data-today-note={countsNote.kind} title={countsNote.text}>
                {sentence ? ' ' : ''}{countsNote.text}
                {countsNote.kind === 'stale' && today.onRefresh && <button type="button" disabled={!connected} onClick={today.onRefresh}>{t('approvals.refresh')}</button>}
              </small>
            )}
          </p>
        )}
      </div>
      {!connected && (
        <div className="master-state master-state--offline" role="status">
          {t('master.offline')}
        </div>
      )}

      <div className="master-messages" ref={listRef}>
        {landing && (
          <div className="master-welcome">
            <div className="master-welcome-icon" aria-hidden="true">SO</div>
            {/* The new shell's landing: the brand mark and one line saying what this page is for. The earlier shell
                keeps its "SO" badge; which of the two shows is the stylesheet's. */}
            <img className="master-welcome-mark master-welcome-mark--on-light" src="./brand/ampuly-lockup.png" alt="安普里 Ampuly" width={188} height={70} />
            <img className="master-welcome-mark master-welcome-mark--on-dark" src="./brand/ampuly-lockup-light.png" alt="安普里 Ampuly" width={188} height={70} />
            <h1 className="master-welcome-title">{t('master.hero')}</h1>
            <p>{t('master.welcome')}</p>
            {threadNote && <p className="master-thread-note" role="status" data-testid="master-thread-note">{threadNote}</p>}
          </div>
        )}
        {opening && messages.length === 0 && (
          <p className="master-opening" role="status" data-testid="master-opening">{t('master.threads.opening')}</p>
        )}
        {shown.map(message => {
          // A build's message (B2): the job's latest shows its live card in place of the words; an earlier one of the
          // same job is a plain line, its words as they were written.
          const buildCard = message.sender !== 'user' ? buildCardFrom(message.metadata) : null
          const liveCard = buildCard && liveBuilds.get(buildCard.jobId) === message.id ? buildCard : null
          return (
          <div
            key={message.id}
            className={`master-msg master-msg--${message.sender === 'user' ? 'user' : message.sender === 'system' ? 'system' : 'assistant'}`}
            data-build-line={buildCard && !liveCard ? buildCard.jobId : undefined}
          >
            <div className={`master-msg-bubble${liveCard ? ' master-msg-bubble--build' : ''}`}>
              {message.sender === 'user'
                ? <span className="master-msg-plain">{message.content}</span>
                : liveCard
                  ? <MasterBuildCard card={liveCard} content={message.content} hooks={build} connected={connected}
                      onContinue={build ? continueBuild : undefined} t={t} zh={zh} />
                  : <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>}
              {/* What was read out of each file sent with this message -- and, when nothing was, why. */}
              {message.sender === 'user' && attachmentsReadFrom(message.metadata).length > 0 && (
                <ul className="master-msg-files" data-testid="master-attachments-read">
                  {attachmentsReadFrom(message.metadata).map((item, index) => {
                    const line = attachmentReadLine(item, zh)
                    // 排队 / 解析中 / 完成 / 部分解析 / 未解析 / 已取消 (4.7 A13), then what was read or why not.
                    const status = attachmentStatus(item)
                    const cancelKey = attachmentCancelKey(message.id, item.attachment_id ?? '')
                    const asked = cancelWaiting(cancelAsked.get(cancelKey), cancelRefusals[cancelKey])
                    return <li key={`${item.filename}:${index}`} data-read={line.read ? 'yes' : 'no'} data-status={status}>
                      <b>{item.filename}</b> · <span className="master-file-status" data-testid="master-file-status">{attachmentStatusWord(status, zh)}</span> · {line.text}
                      {onCancelAttachment && attachmentCancellable(item) && (
                        <button type="button" className="master-file-cancel" data-testid="master-file-cancel" title={words.cancelHint}
                          disabled={!connected || asked}
                          onClick={() => {
                            if (onCancelAttachment(message.id, item.attachment_id ?? '') !== false) {
                              setCancelAsked(current => new Map(current).set(cancelKey, cancelRefusals[cancelKey] ?? 0))
                            }
                          }}>{words.cancel}</button>
                      )}
                    </li>
                  })}
                </ul>
              )}
              {/* 来源定位 (4.7 A13): what the reply cites from a file it read, opened in the side panel at that page. */}
              {message.sender !== 'user' && attachmentSourcesFrom(message.metadata).length > 0 && (
                <p className="master-msg-sources" data-testid="master-sources" aria-label={words.sources}>
                  {/* Each link already reads 「来自：…」, so no 「来源：」 in front of it. */}
                  {attachmentSourcesFrom(message.metadata).map((source, index) => (
                    <button key={`${source.attachment_id}:${source.page ?? 0}:${index}`} type="button" className="master-source-link"
                      data-source-kind={source.kind} title={words.open} onClick={() => showSource(source)}>
                      {sourceLabel(source, zh)}
                    </button>
                  ))}
                </p>
              )}
              {recordedToolCalls(message).length > 0 && (
                // 「这次安安：查了工单、查了等你处理的事」 -- each tool once, in words; never its name (owner report ).
                <p className="master-msg-tools" data-recorded-tools={recordedToolCalls(message).length}>
                  {t('master.activity.recorded')}{zh ? '：' : ': '}{recordedToolGroups(recordedToolCalls(message)).map((call, index) => (
                    <span key={call.name} className="master-tool-words" data-tool-name={call.name} data-tool-writes={call.writes ? 'true' : 'false'}>
                      {index > 0 ? (zh ? '、' : ', ') : ''}{masterToolWords(call.name, zh)}{call.times > 1 ? (zh ? `（${call.times} 次）` : ` (${call.times}×)`) : ''}
                    </span>
                  ))}
                </p>
              )}
              {message.sender !== 'user' && (() => {
                // Only a console that can act on it (the owner's) is given onOpenNodeProposal and shows the card.
                const proposal = onOpenNodeProposal ? nodeProposalFrom(message.metadata) : null
                if (!proposal) return null
                return (
                  <div className="master-node-proposal" data-node-proposal={proposal.package_id}>
                    <strong>{t('master.proposal.title')}</strong>
                    <span>{t('master.proposal.summary', { name: proposal.package_name, base: proposal.base_version, next: proposal.new_version })}</span>
                    <span>{t('master.proposal.counts', proposal.counts)}</span>
                    {proposal.change_note && <span className="master-node-proposal-note">「{proposal.change_note}」</span>}
                    {/* 权限扩大 (4.7 A04): said here too; the builder asks for a yes before it is applied. */}
                    {proposalWidens(proposal.base_nodes, proposal.nodes).map(item => (
                      <span key={item.id} className="master-node-proposal-candidates" data-testid="proposal-widen">
                        {t('workflows.proposal.widen', { step: item.name, what: wideningText(item, locale.startsWith('zh')) })}
                      </span>
                    ))}
                    {proposal.candidates.length > 0 && (
                      // Suggested from outside the library: named here so the owner knows what is still to try before saving.
                      <span className="master-node-proposal-candidates" data-testid="proposal-candidates">
                        {t('master.proposal.candidates', { count: proposal.candidates.length, names: proposal.candidates.slice(0, 5).map(item => item.name).join('、') })}
                      </span>
                    )}
                    <span className="master-node-proposal-hint">{t('master.proposal.notSaved')}</span>
                    <button type="button" className="master-open-order-btn" disabled={!onOpenNodeProposal} onClick={() => onOpenNodeProposal?.(proposal)}>
                      {t('master.proposal.open')} →
                    </button>
                  </div>
                )
              })()}
              {message.sender !== 'user' && (() => {
                // 需求 → 流程草案 (4.7 Q04-B): shown to everyone who can see the reply; opened only by the owner's console.
                const draft = flowDraftFrom(message.metadata)
                if (!draft) return null
                return (
                  <FlowDraftCard draft={draft} teams={teams} connected={connected} t={t}
                    busy={flowDraftOpening === message.id}
                    onOpen={onOpenFlowDraft ? team => onOpenFlowDraft(draft.reference?.message_id ?? message.id, team,
                      draft.reference, (message.metadata as { master_flow_draft_resume?: { materialized?: unknown } })?.master_flow_draft_resume?.materialized) : undefined}
                    // Sent as an ordinary message (4.7 A05): 安安 may only draft an inert candidate from it; testing,
                    // approval and registration stay the owner's, on the candidate's own page.
                    // A card is on a message the page shows: it goes into the conversation that is open.
                    onAskComponent={onOpenFlowDraft && connected ? request => { if (!awaitingReply) onSend(request) } : undefined}
                    askDisabled={awaitingReply}
                    asks={componentAsksFor(message.id)}
                    onOpenCandidate={onOpenConnectorCandidate} />
                )
              })()}
              {message.sender !== 'user' && componentCandidatesFrom(message.metadata).map(candidate =>
                <FlowComponentCandidateCard key={candidate.candidate_id} candidate={candidate} disabled={!connected || awaitingReply}
                  adoptionControl={onComponentDraftAction ? <FlowComponentAdoptionControl candidate={candidate} messageId={message.id}
                    connected={connected} onSend={onComponentDraftAction} ack={componentDraftAck} /> : undefined}
                  onContinue={onOpenFlowDraft ? source => { if (!awaitingReply) onSend(continueDraftText(source)) } : undefined} />)}
              {draftedOrderId(message) && onOpenDraftedOrder && (
                <button
                  type="button"
                  className="master-open-order-btn"
                  data-testid="master-open-drafted-order"
                  onClick={() => onOpenDraftedOrder(draftedOrderId(message))}
                >
                  {t('master.openDraftedOrder')} →
                </button>
              )}
              {dispatchedTaskId(message) && (
                <button
                  className="master-open-order-btn"
                  onClick={() => onOpenOrder(dispatchedTaskId(message))}
                >
                  {t('master.openOrder')} →
                </button>
              )}
            </div>
          </div>
          )
        })}
        {awaitingReply && (
          <div className="master-msg master-msg--assistant">
            <div className="master-msg-bubble master-msg-thinking">
              {t('master.thinking')}
              {/* The step in progress, in the owner's words (lib/masterToolWords); the raw name only in the attribute. */}
              {stepNow && <span className="master-thinking-step" data-tool-state={stepNow.state}>
                {zh ? '（' : ' ('}<span className="master-tool-words" data-tool-name={stepNow.toolName}>{masterToolWords(stepNow.toolName, zh)}</span>{zh ? '）' : ')'}
              </span>}
            </div>
          </div>
        )}
        {unknownSteps.length > 0 && (
          <p className="master-tool-alert" role="alert" data-tool-state="outcome_unknown">
            {t('master.activity.unknownAlert', { what: unknownWhat })}
            {unknownSteps.map(event => <span key={`${event.messageId}:${event.sequence}`} hidden data-tool-name={event.toolName} />)}
          </p>
        )}
      </div>

      <div className="master-quick">
        {[t('master.quick.running'), t('master.quick.cost'), t('master.quick.blocked')].map(q => (
          <button key={q} className="master-quick-chip" disabled={!connected || awaitingReply} onClick={() => quickAsk(q)}>
            {q}
          </button>
        ))}
      </div>

      {landing && recent && recent.length > 0 && (
        // 继续上次的工作, one line of links (lib/recentWork): each is the flow or the order, with where it stands as its title.
        <p className="master-recent" data-testid="master-recent" aria-label={t('master.recent')}>
          <span className="master-recent-label">{t('master.recentLine')}</span>
          {recent.map((item, index) => (
            <span key={item.key}>
              {index > 0 && <span className="master-today-sep" aria-hidden="true"> · </span>}
              <button type="button" className="master-recent-link" title={item.sub} onClick={item.onOpen}>{item.title}</button>
            </span>
          ))}
        </p>
      )}
      {canInterrupt && (
        // 停止这一轮 / 插话 (policy): one compact row by the input -- between the conversation and the input
        // box -- only while 安安's turn runs; it no longer sits at the top. Behaviour, words and tracker states unchanged.
        <div className="master-interrupt-row" data-master-interrupt data-testid="master-interrupt">
          <button type="button" className="master-open-order-btn" onClick={onInterrupt}
            disabled={!connected || !onInterrupt || interruptPending || interruptRequested || !!interruptError}>
            {t('master.interrupt')}
          </button>
          <p role="status" title={interruptWords}>{interruptWords}</p>
          {interruptError && <p role="alert" title={interruptError}>{interruptError}</p>}
          {onSteer && <div className="master-steer" data-testid="master-steer">
            <input
              className="master-input"
              value={steerDraft}
              maxLength={4000}
              placeholder={t('master.steerPlaceholder')}
              aria-label={t('master.steer')}
              disabled={!connected || steerPending}
              onChange={event => setSteerDraft(event.target.value)}
            />
            <button
              type="button"
              className="master-open-order-btn"
              data-testid="master-steer-send"
              disabled={!connected || steerPending || !steerDraft.trim()}
              onClick={() => { if (onSteer(steerDraft.trim())) setSteerDraft('') }}
            >
              {t('master.steer')}
            </button>
            <p role="status" title={steerWords}>{steerWords}</p>
          </div>}
        </div>
      )}
      {(reference || artifactQuote || files.length > 0 || fileNote) && (
        // What goes with the next message besides its words: the thing it is about, and files. Each can be taken off.
        <div className="master-context" data-testid="master-context">
          {artifactQuote && <details className="master-context-chip is-on" data-testid="master-artifact-quote">
            <summary>{zh ? '已引用 · ' : 'Quoted · '}{artifactQuote.reference.path}
              <button type="button" aria-label={zh ? '移除产物引用' : 'Remove artifact quote'}
                onClick={() => onClearArtifactQuote?.(artifactQuote.key)}>×</button>
            </summary>
            <pre style={{whiteSpace: 'pre-wrap', maxHeight: 160, overflow: 'auto'}}>{artifactQuote.text}</pre>
            {artifactSubmission?.key === artifactQuote.key && !draft.trim() && <button type="button"
              onClick={() => { setDraft(artifactSubmission.text); inputRef.current?.focus() }}>
              {zh ? '恢复上次输入的文字' : 'Restore last typed text'}
            </button>}
          </details>}
          {reference && (
            <button type="button" className={`master-context-chip${referenceOn ? ' is-on' : ''}`} aria-pressed={referenceOn}
              data-testid="master-reference" title={t('master.reference.hint')}
              onClick={() => setReferenceOff(referenceOn ? referenceKey : '')}>
              {referenceOn ? t('master.reference.on', { label: reference.label }) : t('master.reference.off', { label: reference.label })}
            </button>
          )}
          {files.map((file, index) => (
            <span key={`${file.filename}-${index}`} className="master-context-chip is-on" data-testid="master-file">
              {file.filename}
              <button type="button" aria-label={t('master.attach.remove', { name: file.filename })}
                onClick={() => setFiles(current => current.filter((_, at) => at !== index))}>×</button>
            </span>
          ))}
          {fileNote && <span className="master-context-note" role="status">{fileNote}</span>}
          {/* The prototype's side-panel shortcuts, as words put in the box (never sent): what one usually asks about
              the thing referenced. The person finishes the sentence and sends it, or not. */}
          {reference && referenceOn && connected && (
            <span className="master-context-asks" data-testid="master-context-asks">
              {(['change', 'why', 'build'] as const).map(kind => (
                <button key={kind} type="button" className="master-context-ask" data-ask={kind}
                  onClick={() => {
                    const words = t(`master.ask.${kind}` as Parameters<typeof t>[0], { label: reference.label })
                    setDraft(current => (current.trim() ? `${current.trimEnd()}\n${words}` : words))
                  }}>{t(`master.ask.${kind}.label` as Parameters<typeof t>[0])}</button>
              ))}
            </span>
          )}
        </div>
      )}
      <div className={`master-input-row${dropping ? ' is-dropping' : ''}`} data-testid="master-input-row"
        onDragOver={canAttach ? event => { if (Array.from(event.dataTransfer.types).includes('Files')) { event.preventDefault(); setDropping(true) } } : undefined}
        onDragLeave={canAttach ? () => setDropping(false) : undefined}
        onDrop={canAttach ? event => { if (!event.dataTransfer.files.length) return; event.preventDefault(); setDropping(false); if (connected) pickFiles(event.dataTransfer.files) } : undefined}>
        {canAttach && (
          <>
            <button type="button" className="master-attach-btn" data-testid="master-attach" disabled={!connected || files.length >= MAX_FILES}
              title={t('master.attach.hint')} aria-label={t('master.attach.label')} onClick={() => fileInputRef.current?.click()}>＋</button>
            <input ref={fileInputRef} type="file" multiple hidden
              accept=".txt,.md,.csv,.json,.yaml,.yml,.docx,.xlsx,.pptx,.pdf,image/*"
              onChange={event => { pickFiles(event.target.files); event.target.value = '' }} />
          </>
        )}
        <textarea
          ref={inputRef}
          className="master-input"
          value={draft}
          placeholder={connected ? t('master.placeholder') : t('master.disconnected')}
          disabled={!connected}
          rows={2}
          onChange={e => setDraft(e.target.value)}
          // A file on the clipboard (a screenshot) is attached; pasting words is left to the text box.
          onPaste={canAttach ? e => { const pasted = Array.from(e.clipboardData?.files ?? []); if (pasted.length) { e.preventDefault(); pickFiles(pasted) } } : undefined}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              send()
            }
          }}
        />
        <button className="master-send-btn" disabled={!connected || (!draft.trim() && !files.length)} onClick={send}>
          {t('master.sendToAnan')}
        </button>
      </div>
      {(defaultModel || onOpenModelConfig) && (
        // Which model answers, from the settings the server reports -- and the way to change it. Not a picker: a
        // message cannot choose its own model (master_send has no model field), so none is offered.
        <p className="master-model-line" data-testid="master-model-line">
          {defaultModel ? t('master.modelLine', { model: defaultModel }) : t('master.modelLineUnknown')}
          {onOpenModelConfig && <button type="button" onClick={onOpenModelConfig}>{t('master.modelConfigure')}</button>}
        </p>
      )}
      {openSource && (() => {
        // 来源定位 (4.7 A13): the cited file in a side panel -- a picture whole; a PDF at the cited page (its picture when
        // it is a scan, and the text read from it), with the pages around it a click away; another file's text. Read
        // by this console from the stored file; the original is one link away.
        const page = Math.trunc(Number(openSource.page ?? 0))
        const loaded = openSource.kind !== 'image' && sourcePreview && sourcePreview.attachmentId === openSource.attachment_id
          && sourcePreview.page === (openSource.kind === 'page' ? page : 0) ? sourcePreview : null
        const preview = loaded?.preview
        const ref = { attachment_id: openSource.attachment_id, filename: openSource.filename, disk_path: openSource.disk_path }
        const turnTo = (next: number) => showSource({ ...openSource, kind: 'page', page: next })
        return (
          <aside className="master-source-pane" data-testid="master-source-pane" aria-label={words.pane}>
            <header className="master-source-pane__head">
              <strong title={openSource.filename}>{sourceLabel(openSource, zh)}</strong>
              <a href={openSource.kind === 'image' ? sourceUrl(openSource) : attachmentDownloadUrl(ref)} target="_blank" rel="noopener noreferrer">{words.original}</a>
              <button type="button" onClick={() => setOpenSource(null)} aria-label={words.close}>×</button>
            </header>
            <div className="master-source-pane__body">
              {openSource.kind === 'image'
                ? <img className="master-source-pane__image" src={sourceUrl(openSource)} alt={openSource.filename} />
                : !onLoadSource ? <p role="status">{words.onlyOriginal}</p>
                  : !loaded || loaded.loading ? <p role="status">{words.loading}</p>
                    : !preview ? <p role="alert">{previewNote(loaded.error ?? '', zh)}</p>
                      : <>
                        {preview.kind === 'page' && preview.pages > 1 && (
                          <nav className="master-source-pane__pages" data-testid="master-source-pages">
                            <button type="button" disabled={preview.page <= 1} onClick={() => turnTo(preview.page - 1)}>{words.prev}</button>
                            <span>{zh ? `第 ${preview.page} / ${preview.pages} 页` : `page ${preview.page} of ${preview.pages}`}</span>
                            <button type="button" disabled={preview.page >= preview.pages} onClick={() => turnTo(preview.page + 1)}>{words.next}</button>
                          </nav>
                        )}
                        {preview.image && <img className="master-source-pane__image" src={preview.image} alt={sourceLabel(openSource, zh)} />}
                        {preview.text && <>
                          <p className="master-source-pane__note">{words.readHere}</p>
                          <pre className="master-source-pane__text" data-testid="master-source-text">{preview.text}</pre>
                          {preview.truncated && <p className="master-source-pane__note">{words.cut}</p>}
                        </>}
                        {!preview.image && !preview.text
                          ? <p role="status">{previewNote(preview.note || 'nothing_to_show', zh)}</p>
                          : preview.note ? <p role="status">{previewNote(preview.note, zh)}</p> : null}
                      </>}
            </div>
          </aside>
        )
      })()}
    </div>
  )
}
