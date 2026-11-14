/** No placeholder/default fallback: employee actions start only in a hydrated,
 * explicitly listed project after the shared project switch has settled.
 * This is a view guard, not a substitute for the server's owner/project checks.
 */
export function employeeAssetProjectId(
  hydrated: boolean,
  pendingProjectId: string | null,
  activeProjectId: unknown,
  projects: readonly { id: string }[],
): string | null {
  if (!hydrated || pendingProjectId !== null || typeof activeProjectId !== 'string'
    || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(activeProjectId)) return null
  return projects.some(project => project.id === activeProjectId) ? activeProjectId : null
}
