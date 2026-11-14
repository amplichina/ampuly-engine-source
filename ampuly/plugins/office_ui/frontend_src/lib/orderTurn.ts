// 轮到谁 -- whose turn an order is on, for the orders table's own column (the prototype has one; ours said it only
// inside the status cell, and only for a gate pause). Pure, over what the row already holds: the order's step
// (lib/orderDrawer), the gate pause if there is one (lib/orderWaiting), and the reviewer the order names.
//
// It never invents a person. The engine records no owner for an order, so: a gate pause shows the signer the gate
// names (or says nobody is named), the intake review shows the reviewer the order carries (or the role), an acceptance
// decision is a role, and everything else is the run itself.

import type { OrderStep } from './orderDrawer'
import type { Waiting } from './orderWaiting'

export type Turn = {
  /** `person` = a human must act; `run` = the AI run has it; `none` = nobody (the order has ended). */
  kind: 'person' | 'run' | 'none'
  /** Who, in words. */
  who: string
  /** True when the name is a real person the data names, false when it is a role or a description. */
  named: boolean
}

const WORDS = {
  ended: ['已结束', 'Ended'],
  unnamedSigner: ['待签核：闸门没有指定给谁，owner 可签', 'Awaiting a signature: the gate names nobody, so the owner can sign'],
  reviewer: ['审单人', 'The intake reviewer'],
  acceptance: ['验收决定人', 'Whoever decides the acceptance'],
  collecting: ['资料收集中', 'Collecting what is needed'],
  run: ['AI 执行中', 'The AI run'],
} as const

export function whoseTurn(step: OrderStep, waiting: Waiting | null, reviewerName: string, zh: boolean): Turn {
  const say = (key: keyof typeof WORDS) => WORDS[key][zh ? 0 : 1]
  if (step.key === 'closed' || step.key === 'rejected') return { kind: 'none', who: say('ended'), named: false }
  // A pause inside the run outranks the step: the run cannot move until that person answers.
  if (waiting) return waiting.who ? { kind: 'person', who: waiting.who, named: true } : { kind: 'person', who: say('unnamedSigner'), named: false }
  if (step.key === 'intake_review') {
    const name = reviewerName.trim()
    return name ? { kind: 'person', who: name, named: true } : { kind: 'person', who: say('reviewer'), named: false }
  }
  if (step.key === 'acceptance_decision') return { kind: 'person', who: say('acceptance'), named: false }
  if (step.key === 'collecting') return { kind: 'run', who: say('collecting'), named: false }
  return { kind: 'run', who: say('run'), named: false }
}
