import type { AuthState } from '../components/LoginScreen'

/**
 * Who may press the owner-only buttons, decided the way the server decides it.
 *
 * The server's rule (ws_handler._ws_owner_actor): an authenticated account
 * counts by its role; a socket without an account is the local console owner
 * only while no accounts exist. The page used to look at the account alone,
 * so a loopback console with no accounts -- the server's own owner -- never
 * saw the factory editor, the trial button or the settle controls, while the
 * connector switches (ungated here, owner-checked there) worked. One rule,
 * shared, so the two ends cannot disagree about who the owner is.
 */
export function isOwnerConsole(auth: AuthState | null | undefined): boolean {
  if (!auth) return false
  if (auth.account) return auth.account.role === 'owner'
  return !auth.required
}
