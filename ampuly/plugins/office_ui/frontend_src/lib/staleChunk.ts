// A page opened before a new build was published still names the old build's code-split files. When it first needs
// one (the canvas), the file is gone and the import fails -- the page crashed until someone reloaded it by hand. The
// cure is the reload; this decides when to do it: once per tab for a failure that looks like a missing chunk, never
// in a loop (a build that is really broken must show its error, not spin).

const KEY = 'superones.staleChunkReloadAt.v1'
const QUIET_MS = 60_000

type Store = Pick<Storage, 'getItem' | 'setItem'>

/** Whether an import failure is the missing-file kind (as browsers word it), not an error inside the module. */
export function looksLikeMissingChunk(error: unknown): boolean {
  const message = String((error as { message?: unknown } | null)?.message ?? error ?? '')
  return /dynamically imported module|Importing a module script failed|error loading dynamically imported module|ChunkLoadError/i.test(message)
}

/** Whether to reload now. True at most once a minute per tab; a broken store means no reload (never a loop). */
export function shouldReloadForChunk(error: unknown, store: Store | null | undefined, now: number): boolean {
  if (!looksLikeMissingChunk(error) || !store) return false
  try {
    const last = Number(store.getItem(KEY) ?? 0)
    if (Number.isFinite(last) && last > 0 && now - last < QUIET_MS) return false
    store.setItem(KEY, String(now))
    return true
  } catch {
    return false
  }
}

/** Wrap a dynamic import: on a missing chunk reload once, otherwise (or the second time) let the error through. */
export function importOrReload<T>(load: () => Promise<T>): Promise<T> {
  return load().catch((error: unknown) => {
    let store: Store | null = null
    try { store = window.sessionStorage } catch { store = null }
    if (shouldReloadForChunk(error, store, Date.now())) {
      window.location.reload()
      return new Promise<T>(() => undefined) // the page is going away; never resolve into a crash first
    }
    throw error
  })
}
