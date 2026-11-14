// The desktop connector on a step (4.7-A06): the step works in a program on this computer -- only the programs it
// names, towards the goal it states -- and is done only when code finds the desktop showing its conditions. It sits
// beside the browser universal connector (workspace/browserConnector.ts) in the same one `connector` field, marked
// `kind: 'desktop'`; the save keeps it as the step's own component instance (ampuly/plugins/office_ui/desktop_connector.py
// checks every piece and words its findings for the owner).
//
// The desktop channel is closed until the owner opens it in the settings: a desktop step can be saved while it is
// closed, and stops to ask a person when a run reaches it.

export type DesktopPostconditionKind = 'window_title_contains'

export interface DesktopConnectorParams {
  kind: 'desktop'
  /** The programs this step may work in, by their own names (notepad.exe). */
  allowed_apps: string[]
  goal: string
  postconditions: Array<{ kind: DesktopPostconditionKind; value: string }>
  output: { path: string }
  // What the form does not show is carried back as it came.
  [key: string]: unknown
}

export const DESKTOP_POSTCONDITION_KINDS: readonly DesktopPostconditionKind[] = ['window_title_contains']

export const DESKTOP_POSTCONDITION_LABELS: Record<DesktopPostconditionKind, readonly [string, string]> = {
  window_title_contains: ['窗口标题包含', 'window title contains'],
}

/** What the builder says when the owner has not opened the desktop channel. */
export const DESKTOP_CHANNEL_CLOSED_NOTE: readonly [string, string] = [
  '桌面通道没打开，这一步保存后不会运行',
  'The desktop channel is closed: this step can be saved but will not run',
]

export function isDesktopConnector(value: unknown): value is DesktopConnectorParams {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value) && (value as { kind?: unknown }).kind === 'desktop'
}

/** A new desktop connector: nothing guessed -- no program, no goal -- the save names what is missing. */
export function newDesktopConnector(deliverables: readonly string[] = []): DesktopConnectorParams {
  return {
    kind: 'desktop',
    allowed_apps: [],
    goal: '',
    postconditions: [{ kind: 'window_title_contains', value: '' }],
    output: { path: deliverables[0] ?? '' },
  }
}

/** Programs as a person writes them: one per line or separated by commas; the save checks each one. */
export function appsFromText(text: string): string[] {
  return text.split(/[\n,，、;；]+/).map(item => item.trim()).filter(Boolean)
}

export function appsText(apps: readonly string[] | undefined): string {
  return (apps ?? []).join('\n')
}

/** A done-when row's React key: its content, not only its place. The value box is uncommitted until blur, so a row
 * that moves up after a delete must mount afresh -- keyed by place alone it kept the deleted row's text and wrote it
 * back on blur. */
export function desktopPostKey(nodeId: string, index: number, post: { kind: string; value: string }): string {
  return `${nodeId}-desktop-post-${index}-${post.kind}-${post.value}`
}

/** What the step's card says of its desktop connector: the programs (the first, and how many more); '' without one. */
export function desktopRow(connector: unknown, zh: boolean): string {
  if (!isDesktopConnector(connector)) return ''
  const apps = connector.allowed_apps ?? []
  if (apps.length === 0) return zh ? '未指定程序' : 'no program yet'
  return apps.length === 1 ? apps[0] : `${apps[0]} ${zh ? `等 ${apps.length} 个程序` : `+${apps.length - 1}`}`
}
