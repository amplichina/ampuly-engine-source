/** V2 C/D/E/F surface flags — mirror of server default-off contract.
 *  Do not wire WS handlers behind these until product opens a flag.
 */
export const V2_SURFACE_FLAGS = {
  c_ws_preview: false,
  c_ws_approve: false,
  c_ws_propose: false,
  c_frontend_entry: false,
  d_ws_composition_gate: false,
  d_frontend_publish_entry: false,
  e_ws_experience_gate: false,
  e_frontend_task_center_entry: false,
  e_ws_task_center_read: false,
  f_ws_usability_read: false,
  f_frontend_preferences_safety: false,
  f_frontend_connector_inventory: false,
  f_frontend_pending_approval_badge: false,
  f_ws_pending_approval_badge: false,
} as const;

export type V2SurfaceFlag = keyof typeof V2_SURFACE_FLAGS;

export function isV2SurfaceEnabled(flag: V2SurfaceFlag): boolean {
  return Boolean(V2_SURFACE_FLAGS[flag]);
}

export function anyV2SurfaceOn(): boolean {
  return (Object.values(V2_SURFACE_FLAGS) as boolean[]).some(Boolean);
}
