export type PageErrorKind = 'permission' | 'scope' | 'unsupported' | 'generic'

// The codes the server actually sends.  Matching on prose alone missed
// `owner_required` -- the reply of all sixteen owner-only handlers -- so the
// most common permission refusal rendered as a generic failure (2026-09-06).
const PERMISSION_CODES = [
  'owner_required',
  'owner_actor_unavailable',
  'not_the_assignee',
  'unauthorized',
  'forbidden',
  'permission',
  'owner-only',
  'not authorized',
  'requires owner',
]

// "This build cannot do that" is not "it went wrong". A person told the
// second will retry, and retrying will never help. Added 2026-09-08, when the
// Codex dependency and the runtime-unavailable replies started reaching the
// page and rendered as ordinary failures.
const UNSUPPORTED_CODES = [
  'dependency_missing',
  'unsupported',
  'not_installed',
  'runtime_unavailable',
  'adapter_registry_unavailable',
  'pending_protocol',
  'no adapter yet',
]

const SCOPE_CODES = [
  'project_scope_denied',
  'scope_check_unavailable',
  'scope_denied',
  'project_scope',
  'scope conflict',
  'cross-project',
  'wrong project',
]

/** Classify server failures without hiding the original diagnostic. */
export function classifyPageError(error: string | null | undefined): PageErrorKind {
  const text = String(error ?? '').toLowerCase()
  if (!text) return 'generic'
  if (UNSUPPORTED_CODES.some(code => text.includes(code))) return 'unsupported'
  if (PERMISSION_CODES.some(code => text.includes(code))) return 'permission'
  if (SCOPE_CODES.some(code => text.includes(code))) return 'scope'
  return 'generic'
}

/**
 * The i18n key for a failure, so every page words a refusal the same way.
 *
 * Each page used to spell this out as a chain of ternaries, which is how one
 * page ends up saying something different about the same refusal.
 */
export function pageErrorMessageKey(error: string | null | undefined): string {
  const kind = classifyPageError(error)
  return kind === 'generic' ? 'pageError.generic' : `pageError.${kind}`
}
