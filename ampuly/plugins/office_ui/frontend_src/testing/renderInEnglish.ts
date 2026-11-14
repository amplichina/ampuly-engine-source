import { createElement, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { I18nProvider } from '../i18n'

/**
 * Render a component to static markup inside the i18n provider, in English.
 *
 * The contract tests that predate the provider assert English copy and rendered their component bare; once a component
 * started calling `useI18n` they threw before asserting anything -- and because nothing ran them, nobody saw. The
 * provider picks its first locale from `localStorage`, which Node does not have, so a one-key stand-in is installed
 * for the length of the render and removed again: nothing else in the test sees it.
 */
export function renderInEnglish(element: ReactElement): string {
  const target = globalThis as { localStorage?: unknown }
  const had = Object.prototype.hasOwnProperty.call(target, 'localStorage')
  const before = target.localStorage
  target.localStorage = { getItem: (key: string) => (key === 'ampuly_locale' ? 'en' : null), setItem: () => undefined }
  try {
    return renderToStaticMarkup(createElement(I18nProvider, null, element))
  } finally {
    if (had) target.localStorage = before
    else delete target.localStorage
  }
}
