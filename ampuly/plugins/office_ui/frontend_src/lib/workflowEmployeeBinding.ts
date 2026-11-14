// Portable design data only. This parser neither resolves project instances nor
// certifies schemas, reviewer eligibility, adoption, grants or runtime support.
export type BindingJson = null | boolean | number | string | BindingJson[] | { [key: string]: BindingJson }
export interface PortableEmployeeBinding {
  package_id: string
  version_constraint: { mode: 'adopted' | 'exact' | 'at_least'; version?: string }
  primary_slot: 'executor'
  input_mapping?: Record<string, string>
  param_overrides?: Record<string, BindingJson>
  output_mapping?: Record<string, string>
  reviewer_slot?: 'submitter' | 'rule_confirmer' | 'reviewer' | 'approver' | 'escalation' | 'backup' | 'delivery_authorizer'
  collaborators?: never[]
  binding_note?: string
}

const MAX_BYTES = 2 * 1024 * 1024
const ALLOWED = new Set(['package_id', 'version_constraint', 'primary_slot', 'input_mapping', 'param_overrides', 'output_mapping', 'reviewer_slot', 'collaborators', 'binding_note'])
const SLOTS = new Set(['submitter', 'rule_confirmer', 'reviewer', 'approver', 'escalation', 'backup', 'delivery_authorizer'])
const REF = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/
const SOURCE = /^[A-Za-z_][A-Za-z0-9_-]*(?:(?:\.[A-Za-z_][A-Za-z0-9_-]*)|(?:\[(?:0|[1-9][0-9]*)\]))*$/
const SEMVER = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/
const own = (object: object, key: string) => Object.prototype.hasOwnProperty.call(object, key)
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
// Mirror ai_employee_instance_contract._Check.text: Python len counts Unicode
// code points and str.strip includes these whitespace characters, not FEFF.
// Never trim/normalize the returned string; this is validation only.
const PYTHON_BLANK = /^[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]*$/u
const text = (value: unknown, limit = 256, empty = false): value is string => {
  return typeof value === 'string' && value.length <= limit * 2
    && Array.from(value).length <= limit && (empty || !PYTHON_BLANK.test(value))
}
const matches = (pattern: RegExp, value: string) => pattern.exec(value)?.[0] === value

/** A bounded standard-JSON copy, with null-prototype maps and stable key order. */
function jsonCopy(value: unknown): BindingJson {
  let remaining = 50000
  let characters = MAX_BYTES
  const visit = (item: unknown, depth: number): BindingJson => {
    if (--remaining < 0 || depth > 64) throw new Error('Invalid portable employee binding')
    if (item === null || typeof item === 'boolean') return item
    if (typeof item === 'string') {
      characters -= item.length
      if (characters < 0) throw new Error('Invalid portable employee binding')
      return item
    }
    if (typeof item === 'number' && Number.isFinite(item)) return item
    if (Array.isArray(item)) {
      if (item.length > remaining || Object.keys(item).length !== item.length
          || Object.getOwnPropertySymbols(item).length) throw new Error('Invalid portable employee binding')
      const copied: BindingJson[] = []
      for (let index = 0; index < item.length; index++) {
        const descriptor = Object.getOwnPropertyDescriptor(item, String(index))
        if (!descriptor || !own(descriptor, 'value')) throw new Error('Invalid portable employee binding')
        copied.push(visit(descriptor.value, depth + 1))
      }
      return copied
    }
    if (!object(item) || (Object.getPrototypeOf(item) !== Object.prototype && Object.getPrototypeOf(item) !== null)
        || Object.getOwnPropertySymbols(item).length) throw new Error('Invalid portable employee binding')
    const copied: { [key: string]: BindingJson } = Object.create(null)
    for (const key of Object.keys(item).sort()) {
      const descriptor = Object.getOwnPropertyDescriptor(item, key)
      if (!descriptor || !own(descriptor, 'value')) throw new Error('Invalid portable employee binding')
      characters -= key.length
      if (characters < 0) throw new Error('Invalid portable employee binding')
      copied[key] = visit(descriptor.value, depth + 1)
    }
    return copied
  }
  const copied = visit(value, 0)
  if (new TextEncoder().encode(JSON.stringify(copied)).length > MAX_BYTES) throw new Error('Invalid portable employee binding')
  return copied
}

/** Return null on malformed data; never repair it or drop forbidden fields. */
export function employeeBindingFrom(raw: unknown): PortableEmployeeBinding | null {
  try {
    const value = jsonCopy(raw)
    if (!object(value) || Object.keys(value).some(key => !ALLOWED.has(key))
        || !text(value.package_id) || !matches(/^[a-z0-9][a-z0-9_-]{2,63}$/, value.package_id)
        || value.primary_slot !== 'executor') return null
    const constraint = value.version_constraint
    if (!object(constraint) || Object.keys(constraint).some(key => key !== 'mode' && key !== 'version')
        || typeof constraint.mode !== 'string' || !['adopted', 'exact', 'at_least'].includes(constraint.mode)) return null
    if ((constraint.mode !== 'adopted' || own(constraint, 'version')) && !text(constraint.version)) return null
    if (constraint.mode === 'at_least') {
      if (!text(constraint.version) || !matches(SEMVER, constraint.version)) return null
      const prerelease = SEMVER.exec(constraint.version)?.[4]
      if (prerelease?.split('.').some(part => /^\d+$/.test(part) && part.length > 1 && part.startsWith('0'))) return null
    }
    for (const field of ['input_mapping', 'output_mapping']) {
      if (!own(value, field)) continue
      const mapping = value[field]
      if (!object(mapping) || Object.entries(mapping).some(([key, reference]) => !text(key) || !matches(REF, key) || !text(reference) || !matches(SOURCE, reference))) return null
    }
    if (own(value, 'param_overrides') && !object(value.param_overrides)) return null
    if (own(value, 'reviewer_slot') && (typeof value.reviewer_slot !== 'string' || !SLOTS.has(value.reviewer_slot))) return null
    if (own(value, 'collaborators') && (!Array.isArray(value.collaborators) || value.collaborators.length)) return null
    if (own(value, 'binding_note') && !text(value.binding_note, 4096, true)) return null
    return value as unknown as PortableEmployeeBinding
  } catch {
    return null
  }
}

export function copyEmployeeBinding(binding: PortableEmployeeBinding): PortableEmployeeBinding {
  const copied = employeeBindingFrom(binding)
  if (!copied) throw new Error('Invalid portable employee binding')
  return copied
}

/** Proposals may preserve existing bindings, not add/change/unbind/delete them. */
export function employeeBindingsUnchanged(
  base: readonly { id: string; employee_binding?: PortableEmployeeBinding | null }[],
  next: readonly { id: string; employee_binding?: PortableEmployeeBinding | null }[],
): boolean {
  const before = new Map(base.map(node => [node.id, node]))
  const after = new Map(next.map(node => [node.id, node]))
  if (before.size !== base.length || after.size !== next.length) return false
  for (const id of new Set([...before.keys(), ...after.keys()])) {
    const oldNode = before.get(id)
    const changedNode = after.get(id)
    const oldHas = !!oldNode && own(oldNode, 'employee_binding')
    const changedHas = !!changedNode && own(changedNode, 'employee_binding')
    if (!oldHas && !changedHas) continue
    const old = oldNode?.employee_binding
    const changed = changedNode?.employee_binding
    if (!oldHas || !changedHas || !old || !changed) return false
    try {
      if (JSON.stringify(copyEmployeeBinding(old)) !== JSON.stringify(copyEmployeeBinding(changed))) return false
    } catch { return false }
  }
  return true
}

/** Ordinary canvas saves omit unchanged display bindings, not local changes.
 *  The display comparison cannot certify Python integer/float fidelity: the
 *  omitted field lets the server keep its original. Any binding change or new
 *  copied bound node needs an explicit binding-save path, not silent stripping. */
export function canvasNodesForSave<T extends { id: string; employee_binding?: PortableEmployeeBinding | null }>(
  nodes: readonly T[], savedNodes: readonly { id: string; employee_binding?: PortableEmployeeBinding | null }[],
): { ok: true; nodes: T[] } | { ok: false; code: 'employee_binding_save_requires_explicit_action' } {
  const saved = new Map(savedNodes.map(node => [node.id, node]))
  const payload: T[] = []
  for (const node of nodes) {
    const oldNode = saved.get(node.id)
    if ((oldNode && !employeeBindingsUnchanged([oldNode], [node]))
        || (!oldNode && own(node, 'employee_binding'))) {
      return { ok: false, code: 'employee_binding_save_requires_explicit_action' }
    }
    const copied = { ...node }
    delete copied.employee_binding
    payload.push(copied)
  }
  return { ok: true, nodes: payload }
}

/** Explicit owner design save, separate from model proposals. The server still
 * validates owner, project, workflow version/revision and the binding contract.
 * Unchanged bindings are omitted so Python retains its exact numeric values. */
export function ownerCanvasNodesForSave<T extends { id: string; employee_binding?: PortableEmployeeBinding | null }>(
  nodes: readonly T[], savedNodes: readonly { id: string; employee_binding?: PortableEmployeeBinding | null }[],
): { ok: true; nodes: T[] } | { ok: false; code: 'employee_binding_invalid' } {
  if (new Set(nodes.map(node => node.id)).size !== nodes.length
      || new Set(savedNodes.map(node => node.id)).size !== savedNodes.length) return { ok: false, code: 'employee_binding_invalid' }
  const saved = new Map(savedNodes.map(node => [node.id, node]))
  const payload: T[] = []
  for (const node of nodes) {
    const old = saved.get(node.id)
    const copied = { ...node }
    if (old?.employee_binding && !own(node, 'employee_binding')) return { ok: false, code: 'employee_binding_invalid' }
    if (old && employeeBindingsUnchanged([old], [node])) delete copied.employee_binding
    else if (own(node, 'employee_binding')) {
      if (node.employee_binding === null) copied.employee_binding = null
      else {
        const value = employeeBindingFrom(node.employee_binding)
        if (!value) return { ok: false, code: 'employee_binding_invalid' }
        copied.employee_binding = value
      }
    }
    payload.push(copied)
  }
  return { ok: true, nodes: payload }
}
