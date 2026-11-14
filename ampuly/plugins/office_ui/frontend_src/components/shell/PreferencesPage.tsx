// 设置: this browser's preferences (theme, language, developer overlay, about) and, on the owner console, the two
// things people look for here — 账号 and 模型与密钥. The layout switch went with the earlier top-bar shell (S3-7):
// there is one shell now. 通知 only reads the paging channels' state; channels themselves are still configured in 配置.
// Accounts and keys are handed in (callback or undefined): a console that may not write them does not list the sections.

import { useEffect, useRef, useState, type ReactNode } from 'react'

import { useI18n } from '../../i18n'
import { currentSection } from '../../lib/currentSection'
import type { SettingsData } from '../SettingsModal'
import { PAGE_LABEL_KEYS } from '../../lib/shellNav'
import { START_PAGES, loadStartPage, saveStartPage, type StartPage } from '../../lib/startPage'
import { THEMES, themeMessageKey, type ThemeName } from '../../lib/theme'

export function PreferencesPage({ theme, onTheme, devToolsShown, onToggleDevTools, quietCompletion = false, quietSaveFailed = false, onQuietCompletionChange, notify, onOpenChannels, accountsSlot, modelsSlot }: {
  theme: ThemeName
  onTheme: (theme: ThemeName) => void
  devToolsShown: boolean
  onToggleDevTools: () => void
  quietCompletion?: boolean
  quietSaveFailed?: boolean
  onQuietCompletionChange?: (value: boolean) => void
  /** 通知: the paging channels as the settings report them; undefined until they arrive (or on a console that may
   *  not read them). Read only here -- they are configured in 配置 → 渠道连接. */
  notify?: SettingsData['notify'] | null
  onOpenChannels?: () => void
  /** 账号: create the owner while none exists, then list and manage accounts. Absent when this console may not. */
  accountsSlot?: ReactNode
  /** 模型与密钥: the same editor the dialog uses, drawn on this page. Absent when this console may not write keys. */
  modelsSlot?: ReactNode
}) {
  const { t, locale, setLocale } = useI18n()
  // The menu says which section is being read, as its twin 配置 does. It stays a scrolling page -- these sections are
  // short and people scan them -- so the current one is worked out from where the content has been scrolled to.
  const contentRef = useRef<HTMLDivElement>(null)
  // Which page this browser opens on. Read once; a store that cannot be read or written leaves it at the default
  // and says so by not changing (the same as the theme).
  const [startPage, setStartPage] = useState<StartPage>(() => { try { return loadStartPage(window.localStorage) } catch { return 'last' } })
  const chooseStartPage = (value: StartPage) => { let saved = false; try { saved = saveStartPage(window.localStorage, value) } catch { saved = false } if (saved) setStartPage(value) }
  const [current, setCurrent] = useState('')
  useEffect(() => {
    const content = contentRef.current
    if (!content) return
    const read = () => {
      const tops = [...content.querySelectorAll<HTMLElement>('section[aria-labelledby]')]
        .map(section => ({ id: section.getAttribute('aria-labelledby') ?? '', top: section.offsetTop }))
        .filter(item => item.id)
      setCurrent(currentSection(tops, content.scrollTop, content.clientHeight, content.scrollHeight))
    }
    read()
    content.addEventListener('scroll', read, { passive: true })
    window.addEventListener('resize', read)
    return () => { content.removeEventListener('scroll', read); window.removeEventListener('resize', read) }
  }, [Boolean(accountsSlot), Boolean(modelsSlot)])
  return (
    <main className="shell-page shell-config">
      <nav className="shell-config-menu" aria-label={t('app.page.preferences')}>
        <h1>{t('app.page.preferences')}</h1>
        <p>{accountsSlot || modelsSlot ? t('prefs.subtitleOwner') : t('prefs.subtitle')}</p>
        {([
          ...(accountsSlot ? [['prefs-accounts', t('settings.accountsSection')] as const] : []),
          ...(modelsSlot ? [['prefs-models', t('settings.title')] as const] : []),
          ['prefs-general', t('prefs.general')] as const,
          ['prefs-appearance', t('prefs.appearance')] as const,
          ['prefs-language', t('prefs.language')] as const,
          ['prefs-notify', t('prefs.notify')] as const,
          ['prefs-safety', t('prefs.safety')] as const,
          ['prefs-keys', t('prefs.keys')] as const,
          ['prefs-developer', t('prefs.developer')] as const,
          ['prefs-about', t('prefs.about')] as const,
        ]).map(([id, label]) => (
          <button key={id} type="button" aria-current={current === id ? 'true' : undefined} className={current === id ? 'is-active' : undefined}
            onClick={() => document.getElementById(id)?.scrollIntoView({ block: 'start', behavior: 'smooth' })}>{label}</button>
        ))}
      </nav>
      <div className="shell-config-content" ref={contentRef}>

        {accountsSlot && (
          <section className="shell-card" aria-labelledby="prefs-accounts" data-testid="prefs-accounts">
            <h2 id="prefs-accounts">{t('settings.accountsSection')}</h2>
            {accountsSlot}
          </section>
        )}

        {modelsSlot && (
          <section className="shell-card" aria-labelledby="prefs-models" data-testid="prefs-models">
            <h2 id="prefs-models">{t('settings.title')}</h2>
            <p className="shell-card-text">{t('prefs.modelsNote')}</p>
            {modelsSlot}
          </section>
        )}

        <section className="shell-card" aria-labelledby="prefs-general">
          <h2 id="prefs-general">{t('prefs.general')}</h2>
          <div className="shell-pref-row">
            <div><strong>{t('prefs.startPage')}</strong><p>{t('prefs.startPageNote')}</p></div>
            <div className="shell-segmented" role="group" aria-label={t('prefs.startPage')} data-testid="prefs-start-page">
              {START_PAGES.map(value => (
                <button key={value} type="button" aria-pressed={startPage === value} className={startPage === value ? 'is-active' : ''}
                  onClick={() => chooseStartPage(value)}>{value === 'last' ? t('prefs.startPage.last') : t(PAGE_LABEL_KEYS[value])}</button>
              ))}
            </div>
          </div>
        </section>

        <section className="shell-card" aria-labelledby="prefs-appearance">
          <h2 id="prefs-appearance">{t('prefs.appearance')}</h2>
          <div className="shell-pref-row">
            <div><strong>{t('prefs.theme')}</strong></div>
            <div className="shell-swatches" role="group" aria-label={t('prefs.theme')}>
              {THEMES.map(({ name }) => (
                <button key={name} type="button" aria-pressed={theme === name}
                  className={`shell-swatch shell-swatch--${name}${theme === name ? ' is-active' : ''}`} onClick={() => onTheme(name)}>
                  <span className="shell-swatch-art" aria-hidden="true"><i /><b /></span>
                  <span>{t(themeMessageKey(name))}</span>
                </button>
              ))}
            </div>
          </div>
        </section>

        <section className="shell-card" aria-labelledby="prefs-language">
          <h2 id="prefs-language">{t('prefs.language')}</h2>
          <div className="shell-pref-row">
            <div><strong>{t('language.label')}</strong></div>
            <div className="shell-segmented" role="group" aria-label={t('language.label')}>
              <button type="button" aria-pressed={locale === 'zh-CN'} className={locale === 'zh-CN' ? 'is-active' : ''} onClick={() => setLocale('zh-CN')}>{t('language.chinese')}</button>
              <button type="button" aria-pressed={locale === 'en'} className={locale === 'en' ? 'is-active' : ''} onClick={() => setLocale('en')}>{t('language.english')}</button>
            </div>
          </div>
        </section>

        <section className="shell-card" aria-labelledby="prefs-notify" data-testid="prefs-notify">
          <h2 id="prefs-notify">{t('prefs.notify')}</h2>
          {onQuietCompletionChange && <div className="shell-pref-row" data-testid="prefs-quiet-completion">
            <div><strong>{t('prefs.quiet.label')}</strong><p>{t('prefs.quiet.note')}</p></div>
            <label><input type="checkbox" checked={quietCompletion} onChange={event => onQuietCompletionChange(event.target.checked)} />{t('prefs.quiet.label')}</label>
            {quietSaveFailed && <p role="alert">{t('prefs.quiet.saveFailed')}</p>}
          </div>}
          <div className="shell-pref-row">
            <div><strong>{t('prefs.notify.what')}</strong><p>{t('prefs.notify.whatNote')}</p></div>
            <ul className="shell-pref-list">
              <li>{t('prefs.notify.waiting')}</li>
              <li>{t('prefs.notify.finished')}</li>
              <li>{t('prefs.notify.stalled')}</li>
            </ul>
          </div>
          <div className="shell-pref-row">
            <div><strong>{t('prefs.notify.where')}</strong><p>{t('prefs.notify.whereNote')}</p></div>
            <div className="shell-pref-tags">
              {notify?.muted && <span className="shell-tag shell-tag--warn">{t('prefs.notify.muted')}</span>}
              {notify
                ? notify.channels.map(channel => (
                  <span key={channel.id} className={`shell-tag${channel.configured ? ' shell-tag--ok' : ''}`}>
                    {(locale.startsWith('zh') ? channel.name_zh : channel.name_en) || channel.id} · {channel.configured ? t('prefs.notify.on') : t('prefs.notify.off')}
                  </span>
                ))
                : <span className="shell-tag">{t('prefs.notify.unknown')}</span>}
              {onOpenChannels && <button type="button" className="shell-btn" onClick={onOpenChannels}>{t('prefs.notify.configure')}</button>}
            </div>
          </div>
        </section>

        <section className="shell-card" aria-labelledby="prefs-safety">
          <h2 id="prefs-safety">{t('prefs.safety')}</h2>
          {(['send', 'spend', 'trial', 'surface'] as const).map(item => (
            <div key={item} className="shell-pref-row">
              <div><strong>{t(`prefs.safety.${item}`)}</strong><p>{t(`prefs.safety.${item}Note`)}</p></div>
              <span className="shell-tag">{t('prefs.safety.locked')}</span>
            </div>
          ))}
        </section>

        <section className="shell-card" aria-labelledby="prefs-keys">
          <h2 id="prefs-keys">{t('prefs.keys')}</h2>
          {([['Ctrl / ⌘ + Z', 'undo'], ['Ctrl / ⌘ + Shift + Z', 'redo'], ['Ctrl / ⌘ + D', 'duplicate'], ['Esc', 'close'], ['Enter', 'send']] as const).map(([keys, what]) => (
            <div key={what} className="shell-pref-row"><div><strong>{t(`prefs.keys.${what}`)}</strong></div><kbd className="shell-kbd">{keys}</kbd></div>
          ))}
        </section>

        <section className="shell-card" aria-labelledby="prefs-developer">
          <h2 id="prefs-developer">{t('prefs.developer')}</h2>
          <div className="shell-pref-row">
            <div><strong>{t('dev.tools')}</strong><p>{t('prefs.developerNote')}</p></div>
            <button type="button" className="shell-btn" aria-pressed={devToolsShown} onClick={onToggleDevTools}>
              {devToolsShown ? t('prefs.developer.hide') : t('prefs.developer.show')}
            </button>
          </div>
        </section>

        <section className="shell-card" aria-labelledby="prefs-about">
          <h2 id="prefs-about">{t('prefs.about')}</h2>
          <p className="shell-card-text">{t('prefs.aboutBody')}</p>
        </section>
      </div>
    </main>
  )
}
