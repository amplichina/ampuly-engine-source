import { useState } from 'react'

export type ProcessDraft = Record<string, unknown> & {
  contract: number; spec_id: string; name: string; version: string
  steps: Array<Record<string, unknown>>
}

export function readProcessDraft(text: string): ProcessDraft {
  const raw = JSON.parse(text)
  const value = raw?.candidate?.spec ?? raw?.spec ?? raw
  if (!value || Array.isArray(value) || typeof value !== 'object' || value.contract !== 1
    || typeof value.spec_id !== 'string' || !Array.isArray(value.steps)
    || value.steps.some((step: unknown) => !step || Array.isArray(step) || typeof step !== 'object')) {
    throw new Error('需要 contract=1、spec_id 和 steps 的流程草稿；不是发布包。')
  }
  return value as ProcessDraft
}

const blank: ProcessDraft = {
  contract: 1, spec_id: '', name: '', version: '0.1.0', trigger: '', finishes_when: '',
  data_scope: '', acceptance_criteria: [], assumptions: [], steps: [],
}
const text = (value: unknown) => typeof value === 'string' ? value : ''
const lines = (value: unknown) => Array.isArray(value) ? value.join('\n') : ''

export function prepareProcessDraft(draft: ProcessDraft): ProcessDraft {
  const clean = (value: unknown) => Array.isArray(value) ? value.map(item => String(item).trim()).filter(Boolean) : []
  return { ...draft, acceptance_criteria: clean(draft.acceptance_criteria), steps: draft.steps.map(step => ({
    ...step, ...Object.fromEntries(['inputs', 'outputs', 'standard', 'depends_on', 'tools', 'skills', 'writes'].map(key => [key, clean(step[key])])),
  })) }
}

/** Owns a local copy only. Saving creates an immutable version, never installs it. */
export function ProcessDraftEditor({ initial, connected, saving, onSave }: {
  initial?: ProcessDraft | null; connected: boolean; saving?: boolean
  onSave: (spec: ProcessDraft) => void
}) {
  const [draft, setDraft] = useState<ProcessDraft>(() => structuredClone(initial ?? blank))
  const [importText, setImportText] = useState('')
  const [error, setError] = useState('')
  const field = (key: string, value: unknown) => setDraft(previous => ({ ...previous, [key]: value }))
  const stepField = (index: number, key: string, value: unknown) => setDraft(previous => ({
    ...previous, steps: previous.steps.map((step, i) => i === index ? { ...step, [key]: value } : step),
  }))
  const stepInvokeWorkflow = (index: number, value: string) => setDraft(previous => ({
    ...previous, steps: previous.steps.map((step, i) => i === index
      ? { ...step, invoke: { ...(typeof step.invoke === 'object' && step.invoke ? step.invoke as Record<string, unknown> : {}), workflow: value } }
      : step),
  }))
  return <section className="capability-channel-check" data-testid="factory-editor">
    <h3>{initial ? '修订草稿' : '新建流程草稿'}</h3>
    <p className="capability-muted">只保存候选描述，不安装、不授权、不发布。修改已保存内容时请填写新版本。</p>
    <p className="capability-muted">分支由人在它依赖的人工步骤上选择：答复里写入分支条件原话，没选中的分支会明确记为跳过。子工作流节点走正式工单发起钩子；试跑没有受控 launcher 时会失败关闭。结构检查不代表整条流程可运行。</p>
    <details>
      <summary>导入助手给出的候选 JSON</summary>
      <label>候选内容<textarea aria-label="候选 JSON" value={importText} onChange={event => setImportText(event.target.value)} rows={6} /></label>
      <button type="button" disabled={!connected || saving || !importText.trim()} onClick={() => {
        try { setDraft(readProcessDraft(importText)); setError('') } catch (cause) { setError(String(cause)) }
      }}>载入本地编辑器</button>
    </details>
    {error && <p role="alert">{error}</p>}
    <form onSubmit={event => { event.preventDefault(); setError(''); onSave(prepareProcessDraft(draft)) }}>
      <fieldset disabled={!connected || saving} style={{ border: 0, padding: 0, display: 'grid', gap: 12 }}>
        {([
          ['spec_id', '流程标识'], ['name', '流程名称'], ['version', '候选版本'],
          ['trigger', '什么时候开始'], ['finishes_when', '怎样算结束'], ['data_scope', '可以使用哪些客户资料'],
        ] as const).map(([key, label]) => <label key={key}>{label}
          <input aria-label={label} value={text(draft[key])} onChange={event => field(key, event.target.value)} />
        </label>)}
        <label>验收标准（每行一项）<textarea aria-label="验收标准" value={lines(draft.acceptance_criteria)}
          onChange={event => field('acceptance_criteria', event.target.value.split('\n'))} /></label>
        {draft.steps.map((step, index) => <fieldset key={index} style={{ display: 'grid', gap: 8 }}>
          <legend>步骤 {index + 1}</legend>
          {([['id', '步骤标识'], ['name', '步骤名称'], ['role', '执行角色'], ['gate_to', '审批人'],
            ['branch_when', '分支条件'], ['side_effect', '对外动作']] as const).map(([key, label]) => <label key={key}>{label}
              <input aria-label={`步骤 ${index + 1} ${label}`} value={text(step[key])} onChange={event => stepField(index, key, event.target.value)} />
            </label>)}
          <label>步骤类型<select value={text(step.kind) || 'ai_do'} onChange={event => stepField(index, 'kind', event.target.value)}>
            <option value="ai_do">AI 执行</option><option value="human_do">人工执行</option><option value="human_review">人工复核</option><option value="external_call">对外调用</option><option value="invoke_workflow">调用业务流</option>
          </select></label>
          {text(step.kind) === 'invoke_workflow' && <label>调用的业务流
            <input aria-label={`步骤 ${index + 1} 调用的业务流`} value={text((step.invoke as Record<string, unknown> | undefined)?.workflow)}
              onChange={event => stepInvokeWorkflow(index, event.target.value)} />
          </label>}
          {([['inputs', '输入'], ['outputs', '交付物'], ['standard', '完成标准'], ['depends_on', '前置步骤'],
            ['tools', '工具'], ['skills', '技能'], ['writes', '允许写入的目录']] as const).map(([key, label]) => <label key={key}>{label}（每行一项）
              <textarea aria-label={`步骤 ${index + 1} ${label}`} value={lines(step[key])} onChange={event => stepField(index, key, event.target.value.split('\n'))} />
            </label>)}
          <label><input type="checkbox" checked={step.human_gate === true} onChange={event => stepField(index, 'human_gate', event.target.checked)} />需要人工确认</label>
          <button type="button" onClick={() => field('steps', draft.steps.filter((_, i) => i !== index))}>移除此草稿步骤</button>
        </fieldset>)}
        <button type="button" onClick={() => field('steps', [...draft.steps, { id: `step_${draft.steps.length + 1}`, name: '', kind: 'ai_do', outputs: [], standard: [], human_gate: false }])}>添加步骤</button>
        <details><summary>检查完整候选内容（含原有假设与高级字段）</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(draft, null, 2)}</pre></details>
        <button type="submit">{saving ? '正在保存…' : '保存为候选草稿'}</button>
      </fieldset>
    </form>
  </section>
}
