// 总控 / 安安 · 附件 (4.7 Q04-A): what the console read out of each file sent with a message, as the owner reads it
// under that message. From the stored message's attachments_read only -- never the excerpt, never a guess.

import { attachmentUrl } from './attachmentUrl'

export interface AttachmentRead {
  filename: string
  attachment_id?: string
  mime_type?: string
  parser?: string
  chars?: number
  truncated?: boolean
  unread?: string
  pages?: number
  pages_read?: number
  /** Read by 通义千问's vision model (switch AMPULY_MASTER_VISION): its name. */
  vision_model?: string
  /** While the vision model works on it (4.7 A13): 'queued' or 'reading'. */
  vision_state?: string
  /** The server's reading of the above (master_attachments.read_status); older messages have none. */
  status?: string
}

/** 排队 / 解析中 / 完成 / 部分解析 / 失败 / 已取消 -- one file's reading status (4.7 A13). */
export type AttachmentStatus = 'queued' | 'reading' | 'done' | 'partial' | 'failed' | 'cancelled'

const STATUS_WORDS: Record<AttachmentStatus, readonly [string, string]> = {
  queued: ['排队', 'queued'],
  reading: ['解析中', 'reading'],
  done: ['完成', 'done'],
  partial: ['部分解析', 'partly read'],
  failed: ['未解析', 'not read'],
  cancelled: ['已取消', 'cancelled'],
}

const UNREAD: Record<string, readonly [string, string]> = {
  no_local_ocr: ['图片：本机没有 OCR，未读', 'picture: no local OCR, not read'],
  scanned: ['扫描件：PDF 没有文字层，未读出字', 'scan: the PDF has no text layer, no text came out'],
  encrypted: ['加密的 PDF：未打开', 'encrypted PDF: not opened'],
  unreadable: ['读取失败', 'reading it failed'],
  empty: ['没有读到文字', 'no text found'],
  budget: ['未读：本条消息的读取额度已用完', 'not read: this message’s reading budget was used up'],
  no_reader: ['本机没有这种类型的读取器，未读', 'no local reader for this type, not read'],
  vision_failed: ['视觉识别结果未知（可能已计费），没有重发', 'vision read: outcome unknown (may be billed), not sent again'],
  vision_refused: ['视觉识别被拒绝或没有返回文字', 'vision read refused or returned no text'],
  vision_no_key: ['没有可用的通义千问密钥，未识别', 'no Qwen key: not read'],
  vision_limit: ['未识别：每条消息最多识别 3 个', 'not read: at most 3 are read per message'],
  vision_unsupported: ['未识别：只识别 7 MB 以内的 PNG / JPEG / WebP', 'not read: PNG / JPEG / WebP up to 7 MB only'],
  vision_unmetered: ['未识别：无法记账，付费识别没有开始', 'not read: the spend could not be recorded, nothing paid started'],
  scan_no_image: ['扫描件里取不出页面图片，未识别', 'no page image in the scan: not read'],
  vision_cancelled: ['已取消识别：没有发出，不计费', 'cancelled before it was sent: nothing billed'],
  vision_cancelled_billed: ['已取消识别：请求已经发出，可能已计费；结果不交给总控', 'cancelled after it was sent: it may have been billed; the result is not used'],
}

const LIVE = new Set(['queued', 'reading'])
const STATUSES = new Set<string>(['queued', 'reading', 'done', 'partial', 'failed', 'cancelled'])

/** The file's status: the live vision state first, then the server's own reading, then worked out from what was read. */
export function attachmentStatus(item: AttachmentRead): AttachmentStatus {
  if (item.vision_state && LIVE.has(item.vision_state)) return item.vision_state as AttachmentStatus
  if (item.status && STATUSES.has(item.status)) return item.status as AttachmentStatus
  if (item.unread === 'vision_cancelled' || item.unread === 'vision_cancelled_billed') return 'cancelled'
  if ((item.chars ?? 0) > 0) {
    if (item.truncated || (item.pages && (item.pages_read ?? 0) < item.pages)) return 'partial'
    return 'done'
  }
  return 'failed'
}

export function attachmentStatusWord(status: AttachmentStatus, zh: boolean): string {
  return STATUS_WORDS[status][zh ? 0 : 1]
}

/** Only a file that is waiting or being read can be cancelled -- and only one the server can name. */
export function attachmentCancellable(item: AttachmentRead): boolean {
  return !!item.attachment_id && LIVE.has(attachmentStatus(item))
}

/** One file of one message, as a pressed 取消 and its refusals are kept. */
export function attachmentCancelKey(messageId: string, attachmentId: string): string {
  return `${messageId}\n${attachmentId}`
}

/** A pressed 取消 still waiting for its answer: pressed (remembering how many refusals that file had then) and not
 *  refused since. A refusal makes the button usable again; what did happen shows in the status line. */
export function cancelWaiting(refusalsWhenPressed: number | undefined, refusals: number | undefined): boolean {
  return refusalsWhenPressed !== undefined && refusalsWhenPressed === (refusals ?? 0)
}

/** Why a 取消 was not done, in words -- the server's code is for scripts, never shown. */
export function cancelRefusedNote(error: string, zh: boolean): string {
  const words: Record<string, readonly [string, string]> = {
    not_running: ['这个文件已经读完或已经停下了，不用再取消', 'this file has already been read or has stopped; there is nothing to cancel'],
  }
  return (words[error] ?? ['没有取消成功，请刷新页面后再试', 'the cancel did not go through; reload the page and try again'])[zh ? 0 : 1]
}

/** The words next to the cancel button and the side panel's controls. */
export function attachmentWords(zh: boolean) {
  return zh
    ? { cancel: '取消', cancelHint: '取消后这个文件不交给总控；已经发出的识别请求可能已计费', sources: '来源', open: '在侧栏打开', close: '关闭',
        pane: '附件预览', original: '原文件', loading: '正在打开…', prev: '上一页', next: '下一页', cut: '（只显示了这一页的前一部分）',
        readHere: '这一页读出的文字（本机读取，不是原版式）', onlyOriginal: '这里不能预览，请打开原文件查看' }
    : { cancel: 'Cancel', cancelHint: 'Cancelled, the file is not given to master control; a request already sent may have been billed', sources: 'Sources', open: 'Open in the side panel', close: 'Close',
        pane: 'Attachment preview', original: 'Original file', loading: 'Opening…', prev: 'Previous page', next: 'Next page', cut: '(only the start of this page is shown)',
        readHere: 'The text read from this page (read here, not its layout)', onlyOriginal: 'No preview here: open the original file' }
}

// ── 来源定位 (4.7 A13): what a reply cites, as master_attachments.cited_sources stored it ────────────────────────────

export interface AttachmentSource {
  attachment_id: string
  filename: string
  mime_type?: string
  kind: 'page' | 'image' | 'file'
  page?: number
  label?: string
  disk_path?: string
}

export function attachmentSourcesFrom(metadata: unknown): AttachmentSource[] {
  const raw = (metadata as { attachment_sources?: unknown } | null | undefined)?.attachment_sources
  if (!Array.isArray(raw)) return []
  return raw.filter((item): item is AttachmentSource => !!item
    && typeof (item as AttachmentSource).attachment_id === 'string' && !!(item as AttachmentSource).attachment_id
    && typeof (item as AttachmentSource).filename === 'string'
    && ['page', 'image', 'file'].includes(String((item as AttachmentSource).kind))).slice(0, 12)
}

/** 「来自：<文件名> 第 N 页」/「来自：<文件名> 图片」/「来自：<文件名>」 -- built here, so a stored label is never trusted. */
export function sourceLabel(source: AttachmentSource, zh: boolean): string {
  const page = Math.trunc(Number(source.page ?? 0))
  if (source.kind === 'page' && page > 0) return zh ? `来自：${source.filename} 第 ${page} 页` : `from: ${source.filename}, page ${page}`
  if (source.kind === 'image') return zh ? `来自：${source.filename} 图片` : `from: ${source.filename} (image)`
  return zh ? `来自：${source.filename}` : `from: ${source.filename}`
}

/** The stored file itself: a picture opens inline; anything else the server hands over as a download. */
export function sourceUrl(source: AttachmentSource): string {
  return attachmentUrl({ attachment_id: source.attachment_id, filename: source.filename, disk_path: source.disk_path })
}

/** One page as the server read it for the side panel (master_attachments.page_preview). */
export interface AttachmentPagePreview {
  kind: 'page' | 'image' | 'file'
  page: number
  pages: number
  text: string
  image: string
  truncated: boolean
  note: string
}

/** The side panel's load: which page was asked for, and what came back (or that nothing did). */
export interface SourcePreviewState {
  attachmentId: string
  page: number
  loading: boolean
  preview?: AttachmentPagePreview
  error?: string
}

/** The ack `master_attachment_page` as the side panel keeps it; a data URL that is not a picture is dropped. */
export function sourcePreviewFromAck(payload: Record<string, unknown>): SourcePreviewState {
  const attachmentId = String(payload.attachment_id ?? '')
  const page = Math.max(0, Math.trunc(Number(payload.page ?? 0)) || 0)
  const raw = payload.preview as Partial<AttachmentPagePreview> | undefined
  if (!payload.ok || !raw || typeof raw !== 'object') return { attachmentId, page, loading: false, error: String(payload.error ?? 'unreadable') }
  const image = String(raw.image ?? '')
  return {
    attachmentId, page, loading: false,
    preview: {
      kind: raw.kind === 'image' || raw.kind === 'page' ? raw.kind : 'file',
      page: Math.max(0, Math.trunc(Number(raw.page ?? 0)) || 0),
      pages: Math.max(0, Math.trunc(Number(raw.pages ?? 0)) || 0),
      text: String(raw.text ?? ''),
      image: /^data:image\/(png|jpeg|webp);base64,/.test(image) ? image : '',
      truncated: raw.truncated === true,
      note: String(raw.note ?? ''),
    },
  }
}

/** Why the side panel shows nothing, in words. */
export function previewNote(note: string, zh: boolean): string {
  const words: Record<string, readonly [string, string]> = {
    encrypted: ['加密的 PDF，打不开', 'an encrypted PDF: it cannot be opened'],
    unreadable: ['这个文件读不出来', 'this file cannot be read'],
    too_large: ['图片太大，这里不显示；可以下载原文件查看', 'the picture is too large to show here; download the file to see it'],
    nothing_to_show: ['这一页没有能显示的文字或图片', 'nothing on this page can be shown'],
    not_found: ['找不到这个文件（可能不是这段对话里发的）', 'this file cannot be found (it may not have been sent in this conversation)'],
  }
  return (words[note] ?? words.unreadable)[zh ? 0 : 1]
}

export function attachmentsReadFrom(metadata: unknown): AttachmentRead[] {
  const raw = (metadata as { attachments_read?: unknown } | null | undefined)?.attachments_read
  if (!Array.isArray(raw)) return []
  return raw.filter((item): item is AttachmentRead => !!item && typeof (item as AttachmentRead).filename === 'string')
}

/** One file, one line: read (how much) or not read (why). */
export function attachmentReadLine(item: AttachmentRead, zh: boolean): { read: boolean; text: string } {
  const status = attachmentStatus(item)
  if (status === 'queued') return { read: false, text: zh ? '等通义千问视觉模型识别' : 'waiting for the Qwen vision model' }
  if (status === 'reading') return { read: false, text: zh ? '通义千问视觉模型正在识别' : 'the Qwen vision model is reading it' }
  if (item.parser === 'vision' && (item.chars ?? 0) > 0) {
    const pages = item.pages ? (zh ? `，${item.pages_read ?? 0}/${item.pages} 页` : `, ${item.pages_read ?? 0}/${item.pages} pages`) : ''
    const model = item.vision_model ?? ''
    return { read: true, text: zh ? `视觉识别（${model}，${item.chars} 字${pages}）：模型读出，可能有误` : `read by vision (${model}, ${item.chars} chars${pages}): a model's reading, may be wrong` }
  }
  if (item.parser && (item.chars ?? 0) > 0) {
    const pages = item.pages ? (zh ? `，${item.pages_read ?? 0}/${item.pages} 页` : `, ${item.pages_read ?? 0}/${item.pages} pages`) : ''
    const cut = item.truncated ? (zh ? '，只读了前面一部分' : ', only its start was read') : ''
    return { read: true, text: zh ? `已读（${item.parser}，${item.chars} 字${pages}${cut}）` : `read (${item.parser}, ${item.chars} chars${pages}${cut})` }
  }
  const reason = UNREAD[item.unread ?? ''] ?? (zh ? ['未读', 'not read'] as const : ['未读', 'not read'] as const)
  return { read: false, text: reason[zh ? 0 : 1] }
}
