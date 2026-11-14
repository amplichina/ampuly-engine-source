// The reject form's questions, by what the node hands over.
//
// The standard form asks 哪里不对 / 要改成什么 / 补充说明 and preserves the revision sheet's column format.
// Nodes that deliver pictures also ask 保留什么 and 新增/删除元素, using the same rule as the Feishu card
// (feishu_gate._reject_form_is_visual).

/** Files that count as pictures for the form: what the card can upload, plus vectors and PDFs it cannot show inline. */
export const VISUAL_SUFFIXES = ['.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.pdf'] as const

export function isVisualPath(path: string): boolean {
  const clean = String(path ?? '').trim().toLowerCase()
  const dot = clean.lastIndexOf('.')
  if (dot < 0) return false
  return (VISUAL_SUFFIXES as readonly string[]).includes(clean.slice(dot))
}

/** True when any declared, delivered or missing deliverable is a picture (a glob such as round1/*.png keeps its suffix). */
export function hasVisualDeliverable(paths: readonly string[]): boolean {
  return paths.some(isVisualPath)
}
