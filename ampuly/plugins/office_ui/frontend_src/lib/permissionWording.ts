/**
 * What an AI employee asks permission for, in the owner's words.
 *
 * The console twin of feishu_gate.describe_permission: the approval center lists a pause with the tool's name as
 * its subject and the first argument (a url, a path, a command) as its detail, and until 2026-09-25 showed both
 * verbatim -- "list_dir" over "..\..\..\Desktop" -- which the owner could not read. The console does not know the
 * order folder, so a path that climbs out of it is recognised by its leading ".." segments and shown from where
 * it lands, with the climb said in words; the raw argument stays available, folded away.
 *
 * This is the fallback only: when the server worded the request itself (a blocker's `permission.summary`, from
 * feishu_gate.describe_path, which knows the order folder and so tells "C:\Users\..." apart from a path inside
 * it), that sentence is shown as is -- see `permissionText`.
 */
import type { useI18n } from '../i18n'

export type Translate = (key: Parameters<ReturnType<typeof useI18n>['t']>[0], params?: Record<string, string | number>) => string

export type PathVerb = 'view' | 'find' | 'search' | 'read' | 'write' | 'edit'

export type PermissionWording =
  | { kind: 'url'; verb: 'open' | 'fetch'; target: string }
  | { kind: 'path'; verb: PathVerb; target: string; outside: boolean }
  | { kind: 'command'; target: string }
  | { kind: 'tool'; target: string; detail: string }

const PATH_VERBS: Record<string, PathVerb> = {
  list_dir: 'view',
  glob: 'find',
  grep: 'search',
  file_search: 'search',
  file_read: 'read',
  read_file: 'read',
  file_write: 'write',
  write_file: 'write',
  file_edit: 'edit',
  edit_file: 'edit',
  apply_patch: 'edit',
}

const URL_TOOLS: Record<string, 'open' | 'fetch'> = {
  browser_navigate: 'open',
  web_fetch: 'fetch',
}

const COMMAND_TOOLS = new Set(['bash', 'powershell', 'shell_exec', 'shell', 'execute', 'run_command'])

/** A path with its leading ".." segments removed, and whether there were any. */
export function readablePath(raw: string): { target: string; outside: boolean } {
  const text = String(raw ?? '').trim()
  if (!text) return { target: '', outside: false }
  const parts = text.split(/[\\/]+/).filter(Boolean)
  let ups = 0
  while (parts.length > 0 && parts[0] === '..') {
    parts.shift()
    ups += 1
  }
  if (ups === 0) return { target: text, outside: false }
  return { target: parts.join('/') || text, outside: true }
}

export function describePermission(subject: string, detail: string): PermissionWording {
  const name = String(subject ?? '').trim()
  const argument = String(detail ?? '').trim()
  if (name in URL_TOOLS) return { kind: 'url', verb: URL_TOOLS[name], target: argument }
  if (name in PATH_VERBS) {
    const { target, outside } = readablePath(argument)
    return { kind: 'path', verb: PATH_VERBS[name], target, outside }
  }
  if (COMMAND_TOOLS.has(name)) return { kind: 'command', target: argument }
  return { kind: 'tool', target: name, detail: argument }
}

/** "查看 订单文件夹外面的 Desktop/secret.txt" -- the permission request as a sentence, from its wording. */
export function permissionSentence(t: Translate, wording: PermissionWording): string {
  switch (wording.kind) {
    case 'url':
      return t(wording.verb === 'open' ? 'approvals.permission.open' : 'approvals.permission.fetch', { target: wording.target })
    case 'path': {
      const target = wording.outside
        ? t('approvals.permission.outside', { target: wording.target })
        : (wording.target || t('approvals.permission.files'))
      return t(`approvals.permission.${wording.verb}` as Parameters<Translate>[0], { target })
    }
    case 'command':
      return t('approvals.permission.run', { target: wording.target })
    default:
      return t('approvals.permission.use', { target: wording.detail ? `${wording.target}（${wording.detail}）` : wording.target })
  }
}

/**
 * What to show for a permission request: the server's own sentence when it gave one (it resolved the path against
 * the order folder), otherwise this page's reading of the tool name and its first argument.
 */
export function permissionText(t: Translate, request: { subject: string; detail: string; summary?: string }): string {
  const summary = String(request.summary ?? '').trim()
  return summary || permissionSentence(t, describePermission(request.subject, request.detail))
}
