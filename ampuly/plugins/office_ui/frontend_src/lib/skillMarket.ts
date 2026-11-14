// 在线技能市场 + 试用区: what the console's skill_market_* / skill_trial_* actions return. The market is SkillHub
// read through the console (Skill-market policy: no mirror server for now); a trial is a downloaded,
// checked, scanned copy in the runtime trial area, tested and then admitted by hand.

import type { useI18n } from '../i18n'

type Translate = (key: Parameters<ReturnType<typeof useI18n>['t']>[0], params?: Record<string, string | number>) => string

export interface MarketSkill {
  source: string
  slug: string
  name: string
  summary: string
  category: string
  version: string
  stars: number
  downloads: number
  installs: number
  verified: boolean
  requires_api_key: boolean
  /** SkillHub's pay-skill category: per-use paid, never downloaded here. */
  paid: boolean
  upstream: string
  updated_at: number
}

export interface MarketCategory {
  key: string
  name: string
  name_en: string
}

export type TrialStatus = 'downloaded' | 'test_passed' | 'test_failed' | 'admitted' | 'discarded'

export interface TrialTest {
  outcome: 'passed' | 'failed'
  note: string
  by: string
  at: string
}

/** Where a trial came from: the SkillHub market, or the local resource library the catalog was scanned from. */
export type TrialSource = 'skillhub' | 'catalog'

export interface TrialSkill {
  source: TrialSource | string
  slug: string
  name: string
  version: string
  category: string
  downloaded_at: string
  status: TrialStatus
  governance: { risk_level: 'low' | 'medium' | 'high'; reasons: string[]; admission_blockers: string[] }
  secret_findings: string[]
  security_reports: Array<{ vendor: string; status: string; status_text: string; url: string }>
  tests: TrialTest[]
  files: string[]
  /** A local library import: the SKILL.md sha256 it was checked against, so a library changed since shows. */
  catalog_skill_md_sha256?: string
  /** A SkillHub download: how its package differed from SkillHub's own file list; {} (or absent) when they agreed. */
  listing_mismatch?: ListingMismatch
  /**
   * The fingerprint of those differences ('' when none): sent back with the owner's yes, so the yes is to the differences
   * the card showed. The server refuses (listing_mismatch_changed) a key that is not the trial's current one.
   */
  listing_mismatch_key?: string
}

/**
 * SkillHub fills its per-file list and its download package from separate syncs, and they drift apart (seen
 * on about half of the popular skills). The package is what lands (policy: 以下载包为准，差异标出来);
 * the differences are kept here, shown on the trial card and acknowledged at admission. Each list holds up to 50 names
 * and its count is whole.
 */
export interface ListingMismatch {
  listing_version?: string
  package_version?: string
  meta_version?: string
  version_differs?: boolean
  different?: string[]
  different_count?: number
  missing?: string[]
  missing_count?: number
  extra?: string[]
  extra_count?: number
}

const MISMATCH_GROUPS = ['different', 'missing', 'extra'] as const

/**
 * How many files one kind of difference covers: the larger of its count and its list (the list holds up to 50 names, the
 * count is whole). The one rule for whether a kind is shown -- by the warning, the confirmation and hasListingMismatch.
 */
function mismatchCount(mismatch: ListingMismatch, group: typeof MISMATCH_GROUPS[number]): number {
  const names = mismatch[group]
  return Math.max(Number(mismatch[`${group}_count` as const]) || 0, Array.isArray(names) ? names.length : 0)
}

/** Whether a trial's package differed from SkillHub's own list in any way the owner is told about. */
export function hasListingMismatch(mismatch: ListingMismatch | null | undefined): boolean {
  if (!mismatch) return false
  return MISMATCH_GROUPS.some(group => mismatchCount(mismatch, group) > 0) || mismatch.version_differs === true
}

/** How many names of one kind the warning spells out before it says 「等 N 个」. */
export const MISMATCH_NAMES_SHOWN = 5

/**
 * The trial card's warning and the admission confirmation, in words: which files differ, which the list names and the
 * package lacks, which only the package has (a long list shortened with its count), and the versions when they do not
 * match. Both '' when the list and the package agreed; something whenever hasListingMismatch says they did not.
 */
export function listingMismatchWords(t: Translate, mismatch: ListingMismatch | null | undefined): { files: string; version: string } {
  if (!hasListingMismatch(mismatch) || !mismatch) return { files: '', version: '' }
  const names = (group: typeof MISMATCH_GROUPS[number]) => {
    const list = mismatch[group]
    const shown = (Array.isArray(list) ? list : []).slice(0, MISMATCH_NAMES_SHOWN)
    const count = mismatchCount(mismatch, group)
    if (!shown.length) return t('market.mismatch.countOnly', { count })
    const joined = shown.join(t('market.mismatch.nameSep'))
    return count > shown.length ? t('market.mismatch.more', { list: joined, count }) : joined
  }
  const parts = MISMATCH_GROUPS.filter(group => mismatchCount(mismatch, group) > 0)
    .map(group => t(`market.mismatch.${group}`, { list: names(group) }))
  const files = parts.length ? t('market.mismatch.files', { parts: parts.join(t('market.mismatch.partSep')) }) : ''
  const listed = mismatch.listing_version ?? ''
  const packed = [...new Set([mismatch.package_version, mismatch.meta_version].filter((found): found is string => Boolean(found) && found !== listed))]
  const version = mismatch.version_differs === true
    ? t('market.mismatch.version', {
      listed: listed ? `v${listed}` : t('market.mismatch.noVersion'),
      packed: packed.length ? packed.map(found => `v${found}`).join(t('market.mismatch.nameSep')) : t('market.mismatch.noVersion'),
    })
    : ''
  return { files, version }
}

/**
 * Every refusal reason the market and trial actions answer with, said in the owner's words. The server's own message
 * is English for SkillHub's refusals and never shown. Where the server already said it in Chinese (an admission, a
 * local-library import, 「这个文件不在下载记录里」) that sentence is the more exact one and is said as it is while the
 * page speaks Chinese; in English the reason's words stand in for it, except for a local-library import (preferSaid),
 * whose shared codes' words are about SkillHub, and a reason nobody words. 'failed' is never the server's text: that
 * is an exception's message (on Chinese Windows a localized one, with paths), not a sentence for the owner.
 */
export const MARKET_REFUSAL_REASONS = [
  'unreachable', 'bad_status', 'package_missing', 'unsafe_archive', 'too_large', 'not_a_skill', 'paid', 'unexpected_host',
  'bad_response', 'not_found', 'bad_request', 'owner_required', 'failed', 'listing_mismatch_unacknowledged', 'listing_mismatch_changed',
] as const

export type MarketRefusalReason = typeof MARKET_REFUSAL_REASONS[number]

/**
 * A refusal as the page keeps it: the reason and what the server said, never words already chosen -- they are chosen
 * when shown (marketRefusalWords), so a refusal on screen follows the owner switching language.
 */
export interface MarketRefusal {
  reason: string
  said: string
  preferSaid?: boolean
}

const HAN = /[㐀-鿿]/

export function marketRefusalWords(t: Translate, reason: unknown, said?: unknown, { preferSaid = false }: { preferSaid?: boolean } = {}): string {
  const known = (code: string): code is MarketRefusalReason => (MARKET_REFUSAL_REASONS as readonly string[]).includes(code)
  const code = String(reason ?? '').trim()
  const text = String(said ?? '').trim()
  // A reply that names no reason may carry a bare code as its error (owner_required).
  const named = code || (/^[a-z][a-z0-9_]*$/.test(text) ? text : '')
  const words = known(named) ? t(`market.refusal.${named}`) : ''
  if (HAN.test(text) && named !== 'failed' && (preferSaid || !words || HAN.test(words))) return text
  return words || t('market.refusal.unknown')
}


/** How many ranked skills the market shows before anyone searches (owner: a few hundred, as the page can hold). */
export const MARKET_TOP_LIMIT = 300

export function trialKey(item: { slug: string; version?: string; source?: string }): string {
  // SkillHub's keys stay as they were; another source is prefixed, so one id in two sources is two trials.
  const base = item.version ? `${item.slug}@${item.version}` : item.slug
  return item.source && item.source !== 'skillhub' ? `${item.source}:${base}` : base
}

export type AdmissionBlocker = 'not_tested' | 'test_failed' | 'admitted' | 'discarded' | 'secrets' | 'paid' | 'license_restricted' | 'no_metadata'

/** What stops a trial from being admitted right now, in the order a person should deal with it. */
export function admissionBlockers(trial: TrialSkill): AdmissionBlocker[] {
  const blockers: AdmissionBlocker[] = []
  if (trial.status === 'admitted') return ['admitted']
  if (trial.status === 'discarded') return ['discarded']
  if (trial.status === 'downloaded') blockers.push('not_tested')
  if (trial.status === 'test_failed') blockers.push('test_failed')
  if (trial.secret_findings.length > 0) blockers.push('secrets')
  const catalog = trial.governance.admission_blockers
  if (catalog.includes('owner_budget_approval')) blockers.push('paid')
  if (catalog.includes('license_restricts_redistribution')) blockers.push('license_restricted')
  if (catalog.includes('frontmatter_missing')) blockers.push('no_metadata')
  return blockers
}

/** Admitting a skill with no declared license needs the owner to confirm the right to use and ship it. */
export function needsLicenseConfirmation(trial: TrialSkill): boolean {
  return trial.governance.admission_blockers.includes('license_unconfirmed')
}

export type TrialAction = 'use' | 'discard' | 'admit' | 'restore' | 'redownload'

/**
 * The trial a market card stands for when several versions of one skill were downloaded: the version the market
 * lists now, else the most recently downloaded (a string sort would put 1.9.0 after 1.10.0).
 */
export function trialForListing(trials: readonly TrialSkill[], slug: string, listedVersion: string, source: TrialSource = 'skillhub'): TrialSkill | undefined {
  const mine = trials.filter(trial => trial.slug === slug && (trial.source || 'skillhub') === source)
  return mine.find(trial => trial.version === listedVersion)
    ?? [...mine].sort((a, b) => b.downloaded_at.localeCompare(a.downloaded_at))[0]
}

/**
 * The buttons a market or trial card offers (policy): 调用测试 until a test has passed; after it,
 * 弃用 and 录入内部库. A discarded trial can be tried again -- or, where the market is at hand, downloaded again (its
 * files may have been changed, which 重新试用 refuses); an admitted one is the internal library's now.
 */
export function trialActions(trial: Pick<TrialSkill, 'status'> | undefined): TrialAction[] {
  switch (trial?.status) {
    case undefined: return ['use']
    case 'downloaded':
    case 'test_failed': return ['use', 'discard']
    case 'test_passed': return ['discard', 'admit']
    case 'discarded': return ['restore', 'redownload']
    case 'admitted': return []
  }
}

export type CatalogTrialState = 'none' | 'current' | 'discarded' | 'outdated' | 'outdated_admitted'

/**
 * Where a local catalog skill's trial stands against the catalog as it is now. A library skill can be edited without
 * its version moving, so a trial filed under the same version may hold the old bytes: importing again is refused
 * until it is discarded (or, once admitted, the version is raised). A discarded trial is imported again afresh.
 */
export function catalogTrialState(trial: Pick<TrialSkill, 'status' | 'catalog_skill_md_sha256'> | undefined, skillMdSha256: string): CatalogTrialState {
  if (!trial) return 'none'
  if (trial.status === 'discarded') return 'discarded'
  const changed = Boolean(trial.catalog_skill_md_sha256 && skillMdSha256 && trial.catalog_skill_md_sha256 !== skillMdSha256)
  if (!changed) return 'current'
  return trial.status === 'admitted' ? 'outdated_admitted' : 'outdated'
}

/**
 * The trials the builder must not save onto a package: every one not admitted. A discarded trial counts too --
 * a step still naming it would save a skill nothing installs.
 */
export function trialsNotAdmitted(trials: readonly TrialSkill[]): TrialSkill[] {
  return trials.filter(trial => trial.status !== 'admitted')
}

export const MAX_SUCCESS_CRITERIA = 20
export const MAX_CRITERION_CHARS = 300

/**
 * 成功标准, one per line, as admission records them: what a step using the skill must achieve. Admission turns
 * them into the skill's release contract, so a testing workflow can lock it. Blank lines go; none is refused.
 */
export function parseSuccessCriteria(text: string): { criteria: string[]; problem: '' | 'none' | 'too_many' | 'too_long' } {
  // Only a list marker goes ("- ", "1. ", "2、"): "3 套方案可选" keeps its number.
  const criteria = text.split(/\r?\n/).map(line => line.replace(/^\s*(?:[-*•·]|\d+[.、)])\s*/, '').trim()).filter(Boolean)
  if (criteria.length === 0) return { criteria, problem: 'none' }
  if (criteria.length > MAX_SUCCESS_CRITERIA) return { criteria, problem: 'too_many' }
  if (criteria.some(line => line.length > MAX_CRITERION_CHARS)) return { criteria, problem: 'too_long' }
  return { criteria, problem: '' }
}

export function formatCount(value: number, locale: string): string {
  if (!Number.isFinite(value)) return '0'
  if (value >= 10000 && locale.startsWith('zh')) return `${(value / 10000).toFixed(value >= 100000 ? 0 : 1)}万`
  if (value >= 1000 && !locale.startsWith('zh')) return `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}k`
  return String(value)
}
