// 4.3 X05 / V03: while an order's agent waits for a person on the page, the order is not "running a
// tool" to the person reading the board -- it is waiting for them. One place says so.

export const HANDOFF_TOOL = 'browser_request_human'

export function isWaitingForPerson(tool?: string | null): boolean {
  return String(tool ?? '') === HANDOFF_TOOL
}

export function waitingForPersonLabel(locale: string): string {
  return locale.startsWith('zh') ? '⏸ 等你操作（浏览器窗）' : '⏸ Waiting for you (browser window)'
}
