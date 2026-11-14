// A step's own model (4.7 A16/A17): an AI step runs on the configured default unless it names a model. The choices are
// the models of the providers whose key is set on this machine (settings_get says which, never the key) that the
// console's model catalogue lets a step newly choose; the save checks the model against the console's list and the run
// refuses one whose key is missing, or that the catalogue refuses, before any call. A step already on a retired or
// refused model keeps it, shown and marked -- never silently swapped.
// There is no project level yet -- that needs storage of its own -- so the order is step, then the global default.

/** One model as the console's catalogue describes it (model_catalogue.py); only the fields read here. */
export interface ModelEntry {
  id: string
  /** active | vendor_routed_alias | retired */
  status?: string
  successor?: string
  /** available | off_by_default | not_routable | needs_params */
  availability?: string
}

export interface ModelProvider {
  id: string
  name: string
  configured: boolean
  models: string[]
  /** What each model is and whether it may be newly chosen; absent from an older console, which offers what it lists. */
  entries?: ModelEntry[]
}

/** Why the console refuses a listed model that is not retired -- the same codes a run or a save answers with. */
const BLOCKED_CODE: Record<string, string> = {
  off_by_default: 'model_overseas_off',
  not_routable: 'model_not_routable',
  needs_params: 'model_needs_params',
}

/** Whether a model may be newly chosen (model-routing phase 0): not retired and available. A model without an entry
 *  (an older console) is offered as before. */
export function modelOffered(provider: ModelProvider, model: string): boolean {
  const entry = provider.entries?.find(item => item.id === model)
  return !entry || (entry.status !== 'retired' && (entry.availability ?? 'available') === 'available')
}

/** A provider's models that may be newly chosen, in the console's order. */
export function offeredModels(provider: ModelProvider): string[] {
  return provider.models.filter(model => modelOffered(provider, model))
}

/** What the catalogue says about a model a step or a setting already names: retired (and what replaces it), refused
 *  for another reason (the code), or a name the vendor serves under a newer model (a label only: it still runs). */
export interface ModelStanding {
  retired: { model: string; successor: string } | null
  blocked: { model: string; code: string } | null
  alias: { model: string; successor: string } | null
}

export function modelStanding(providers: readonly ModelProvider[] | null | undefined, model: string): ModelStanding {
  const chosen = model.trim()
  const entry = chosen ? (providers ?? []).flatMap(provider => provider.entries ?? []).find(item => item.id === chosen) : undefined
  if (!entry) return { retired: null, blocked: null, alias: null }
  if (entry.status === 'retired') return { retired: { model: chosen, successor: entry.successor ?? '' }, blocked: null, alias: null }
  const availability = entry.availability ?? 'available'
  if (availability !== 'available') {
    return { retired: null, blocked: { model: chosen, code: BLOCKED_CODE[availability] ?? availability }, alias: null }
  }
  if (entry.status === 'vendor_routed_alias') return { retired: null, blocked: null, alias: { model: chosen, successor: entry.successor ?? '' } }
  return { retired: null, blocked: null, alias: null }
}

type ReasonKey = 'models.retired' | 'models.retiredUse' | 'models.refused.model_overseas_off'
  | 'models.refused.model_not_routable' | 'models.refused.model_needs_params'

/** Why a kept model will be refused, in words: "retired, use X", or the refusal's own sentence. "" when it is not. */
export function modelRefusedText(standing: ModelStanding, t: (key: ReasonKey, params?: Record<string, string>) => string): string {
  if (standing.retired) {
    const successor = standing.retired.successor
    return successor ? t('models.retiredUse', { model: successor }) : t('models.retired')
  }
  if (standing.blocked) {
    const key = `models.refused.${standing.blocked.code}`
    return key === 'models.refused.model_overseas_off' || key === 'models.refused.model_not_routable' || key === 'models.refused.model_needs_params'
      ? t(key) : standing.blocked.code
  }
  return ''
}

export interface StepModelChoices extends ModelStanding {
  groups: Array<{ provider: string; models: string[] }>
  /** The step names a model whose provider's key is not set here (or that the console does not list). */
  unavailable: string
  /** The provider list is not known here (settings not read yet, or not this console's to read): nothing is judged. */
  unknown: boolean
}

export function stepModelChoices(providers: readonly ModelProvider[] | null | undefined, current: string): StepModelChoices {
  if (!providers) return { groups: [], unavailable: '', unknown: true, retired: null, blocked: null, alias: null }
  const groups = providers.filter(provider => provider.configured)
    .map(provider => ({ provider: provider.name || provider.id, models: offeredModels(provider) }))
    .filter(group => group.models.length)
  const chosen = current.trim()
  const offered = groups.some(group => group.models.includes(chosen))
  const standing = modelStanding(providers, chosen)
  // A retired or refused model is kept and marked with its own reason; "no key here" is for the rest.
  const refused = Boolean(standing.retired || standing.blocked)
  return { groups, unavailable: chosen && !offered && !refused ? chosen : '', unknown: false, ...standing }
}

/** What an AI step's card says it runs on: its own model, or nothing (the default is not repeated on every card). */
export function stepModelRow(model: string | undefined): string {
  return (model ?? '').trim()
}
