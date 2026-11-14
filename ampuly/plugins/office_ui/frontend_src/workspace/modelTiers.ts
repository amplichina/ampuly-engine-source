// 档位: what 轻 / 标准 / 强 are called, what to suggest for a step, and how to read an order's plan back.
//
// Pure, and the display twin of ampuly/core/model_tiers.py. The suggestion rules are the same rules,
// in the same order, and a test asserts the two agree case by case -- a builder that suggests one thing while the
// server decides another is worse than a builder that suggests nothing.

export const TIERS = ['light', 'standard', 'strong'] as const
export type Tier = typeof TIERS[number]

export type TierNode = {
  type?: string
  gate?: string
  gate_to?: string
  standard?: string
  deliverables?: string[]
  connector?: unknown
  invoke?: string
  model?: string
  tier?: string
}

export type ModelPlan = {
  plan_digest?: string
  tiers?: Record<string, string>
  task_tiers?: Record<string, string>
  residency?: string
  work_window_cap?: number
  source?: string
} | null

const TIER_NAMES: Record<Tier, [string, string]> = {
  light: ['轻', 'Light'],
  standard: ['标准', 'Standard'],
  strong: ['强', 'Strong'],
}

const RESIDENCY_NAMES: Record<string, [string, string]> = {
  cn_only: ['只用境内', 'Mainland only'],
  cn_no_training: ['境内且不用于训练', 'Mainland, and not trained on'],
  allow_overseas: ['允许出境', 'Overseas allowed'],
}

export function tierLabel(tier: string, zh: boolean): string {
  const pair = TIER_NAMES[tier as Tier]
  return pair ? (zh ? pair[0] : pair[1]) : ''
}

export function residencyLabel(value: string, zh: boolean): string {
  const pair = RESIDENCY_NAMES[String(value || '')]
  return pair ? (zh ? pair[0] : pair[1]) : String(value || '')
}

/** What to suggest for this step, and the sentence that says why. Mirrors model_tiers.suggest_tier exactly. */
export function suggestTier(node: TierNode | null | undefined): { tier: Tier | ''; zh: string; en: string } {
  const step = node ?? {}
  if (String(step.type ?? '') !== 'ai_do') {
    return { tier: '', zh: '不是 AI 步骤，不用选档', en: 'Not an AI step; no tier needed' }
  }
  const deliverables = step.deliverables ?? []
  const gated = String(step.gate ?? '') === 'human_required'
    || Boolean(String(step.gate_to ?? '').trim())
    || (deliverables.length > 0 && Boolean(String(step.standard ?? '').trim()))
  if (gated) {
    return { tier: 'strong', zh: '有质量门、要交客户的交付物 → 建议强',
             en: 'A quality gate and a deliverable for the customer → strong' }
  }
  if (step.connector && String(step.connector ?? '').trim() !== '') {
    return { tier: 'strong', zh: '要在浏览器里操作，出错代价高 → 建议强',
             en: 'Works a browser, where a mistake is expensive → strong' }
  }
  if (String(step.invoke ?? '').trim()) {
    return { tier: 'standard', zh: '要调起子流程 → 建议标准', en: 'Calls a sub-flow → standard' }
  }
  return { tier: 'standard', zh: '普通 AI 步骤 → 建议标准', en: 'An ordinary AI step → standard' }
}

/** 「这张单是按什么跑的」, as lines to render. Empty when the order has no plan -- the caller says so instead. */
export function planSummary(plan: ModelPlan, zh: boolean): string[] {
  if (!plan) return []
  const lines: string[] = []
  const tiers = plan.tiers ?? {}
  for (const tier of TIERS) {
    const model = String(tiers[tier] ?? '')
    if (model) lines.push(`${tierLabel(tier, zh)} · ${model}`)
  }
  if (lines.length === 0) {
    lines.push(zh ? '没有设过档位，按默认模型跑' : 'No tiers set; the default model is used')
  }
  const residency = String(plan.residency ?? '')
  if (residency) lines.push(residencyLabel(residency, zh))
  const cap = Number(plan.work_window_cap ?? 0)
  if (cap > 0) {
    lines.push(zh ? `工作窗口上限 ${Math.round(cap / 1000)} 千 token`
                  : `Work window capped at ${Math.round(cap / 1000)}k tokens`)
  }
  const digest = String(plan.plan_digest ?? '')
  if (digest) lines.push(zh ? `方案编号 ${digest.slice(0, 8)}` : `Plan ${digest.slice(0, 8)}`)
  return lines
}

/** The owner's ruled defaults, for the 「按推荐填入」 button. Fills a draft; it never saves. */
export function recommendedTiers(): { tiers: Record<string, string>; tierParams: Record<string, Record<string, unknown>> } {
  return {
    tiers: {
      light: 'deepseek/deepseek-flash',
      standard: 'deepseek/deepseek-flash',
      strong: 'deepseek/deepseek-v4-pro',
    },
    // Thinking off until a multi-turn tool trial has been recorded: turning it on would assert, for the owner,
    // that a thing nobody has run works.
    tierParams: {
      light: { thinking: false },
      standard: { thinking: false },
      strong: { thinking: false },
    },
  }
}
