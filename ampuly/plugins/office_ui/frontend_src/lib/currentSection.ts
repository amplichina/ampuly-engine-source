// Which section of a long page the person is reading (frontend refactor U4). Pure.
//
// 设置 is one scrolling page with a menu beside it, while its twin 配置 -- the same layout, the same class -- switches
// one section at a time and lights the menu up. 设置's menu lit nothing at all, so a second click on the same entry
// looked like nothing happened, and a keyboard or screen-reader user got no signal at all. It stays a scrolling page
// (its sections are short and people scan them), and the menu now says where they are.

export type SectionTop = { id: string; top: number }

/**
 * The id of the section being read, or '' when there is nothing to say.
 *
 * The reading line sits a third of the way down, not at the very top: a heading scrolled just past the top edge is
 * still what you are reading. At the very bottom the last section wins however short it is, or it could never light.
 */
export function currentSection(
  sections: readonly SectionTop[],
  scrollTop: number,
  viewHeight: number,
  contentHeight: number,
): string {
  if (sections.length === 0) return ''
  if (!Number.isFinite(scrollTop) || !(viewHeight > 0)) return sections[0].id
  const ordered = [...sections].sort((a, b) => a.top - b.top)
  // A page that does not scroll shows every section at once, so "where you are" has no answer: say the first.
  if (contentHeight <= viewHeight) return ordered[0].id
  if (scrollTop + viewHeight >= contentHeight - 4) return ordered[ordered.length - 1].id
  const line = scrollTop + viewHeight / 3
  let current = ordered[0].id
  for (const section of ordered) {
    if (section.top <= line) current = section.id
    else break
  }
  return current
}
