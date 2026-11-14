/**
 * Pushed changes ask for one full sync, not four (2026-09-26).
 *
 * One change on the server reaches the page as several pushes at once -- org_info, project_run_updated,
 * seat_digest_updated, work_item_batch_updated -- and each used to ask for a full collab_sync of its own: up to four
 * syncs per change per open tab, each costing the server about a second of CPU. Those pushes now go through one
 * `CollabSyncCoalescer`: a burst that arrives together becomes one sync shortly after it, and a steady stream of
 * pushes asks for at most one sync per `COLLAB_SYNC_COALESCE_WINDOW_MS`. The sync itself reads the project at the
 * moment it goes out, so nothing a push announced is lost by waiting.
 */

export const COLLAB_SYNC_COALESCE_WINDOW_MS = 1_000
/** How long the first push of a quiet period waits for the rest of its burst. */
export const COLLAB_SYNC_GATHER_MS = 120

type Schedule = (callback: () => void, ms: number) => unknown
type Cancel = (handle: unknown) => void

export class CollabSyncCoalescer {
  private timer: unknown = null
  private lastSentAt: number | null = null
  private absorbed = 0

  constructor(
    private readonly send: () => void,
    private readonly windowMs = COLLAB_SYNC_COALESCE_WINDOW_MS,
    private readonly gatherMs = COLLAB_SYNC_GATHER_MS,
    private readonly now: () => number = () => Date.now(),
    private readonly schedule: Schedule = (callback, ms) => setTimeout(callback, ms),
    private readonly cancelTimer: Cancel = handle => clearTimeout(handle as ReturnType<typeof setTimeout>),
  ) {}

  /** Asks for a sync; one already waiting absorbs this one. */
  request(): void {
    if (this.timer !== null) {
      this.absorbed += 1
      return
    }
    const now = this.now()
    const delay = this.lastSentAt === null
      ? this.gatherMs
      : Math.max(this.gatherMs, this.lastSentAt + this.windowMs - now)
    this.timer = this.schedule(() => this.fire(), delay)
  }

  /** True while a sync is waiting to go out. */
  get pending(): boolean {
    return this.timer !== null
  }

  /** How many requests the waiting sync has absorbed (for tests and diagnostics). */
  get absorbedCount(): number {
    return this.absorbed
  }

  /** Drops the waiting sync (the page is closing or the connection is gone). */
  cancel(): void {
    if (this.timer !== null) this.cancelTimer(this.timer)
    this.timer = null
    this.absorbed = 0
  }

  private fire(): void {
    this.timer = null
    this.absorbed = 0
    this.lastSentAt = this.now()
    this.send()
  }
}
