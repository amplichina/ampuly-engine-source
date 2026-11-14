/**
 * The six states a main action can be in, told apart on purpose.
 *
 * Collapsing them is the usual failure, and each collapse costs something
 * specific:
 *
 * - `error` swallowing `permission` sends somebody to debug a thing that is
 *   working as designed. The fix is a person with a different account, not a
 *   retry.
 * - `error` swallowing `unsupported` sends them to debug something this build
 *   cannot do at all. Retrying will never help.
 * - `empty` looking like `loading` makes "there is nothing here" read as "wait
 *   a moment", and people wait.
 * - `offline` without saying what happened to their click leaves them
 *   wondering whether it went out. It did not.
 *
 * The words live here rather than in each panel so that two pages cannot end
 * up disagreeing about what "denied" means.
 */

export type ActionState = 'ready' | 'loading' | 'empty' | 'error' | 'offline' | 'permission' | 'unsupported'

export type ActionCondition = {
  connected?: boolean
  loading?: boolean
  /** The server's refusal, verbatim. */
  error?: string | null
  /** How many things came back; 0 with no error is empty, not broken. */
  count?: number
  /** This build cannot do it -- a missing dependency, an absent provider. */
  unsupportedReason?: string
}

const PERMISSION_MARKERS = ['owner_required', 'permission', 'not_the_assignee', 'forbidden', 'out_of_scope']
const UNSUPPORTED_MARKERS = ['dependency_missing', 'unsupported', 'not_installed', 'runtime_unavailable']

export function classifyAction(condition: ActionCondition): ActionState {
  const error = String(condition.error ?? '').trim().toLowerCase()
  if (condition.unsupportedReason) return 'unsupported'
  if (error && UNSUPPORTED_MARKERS.some(marker => error.includes(marker))) return 'unsupported'
  if (error && PERMISSION_MARKERS.some(marker => error.includes(marker))) return 'permission'
  if (error) return 'error'
  // Offline is checked after the refusals: a refusal that already arrived is
  // news, and hiding it behind "you are offline" loses it.
  if (condition.connected === false) return 'offline'
  if (condition.loading) return 'loading'
  if (typeof condition.count === 'number' && condition.count === 0) return 'empty'
  return 'ready'
}

/** Whether an action should be clickable in this state. */
export function actionable(state: ActionState): boolean {
  return state === 'ready' || state === 'empty'
}

/**
 * The i18n key for what to tell somebody in this state. Kept as keys rather
 * than sentences so both languages stay in one file.
 */
export function messageKey(state: ActionState): string {
  return `actionState.${state}`
}
