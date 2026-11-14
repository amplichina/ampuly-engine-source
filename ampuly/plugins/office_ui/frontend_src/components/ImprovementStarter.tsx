import { useState } from 'react'

import {
  REQUEST_LIMIT, TARGET_KIND_WORDS, editorWords, improvementRefusal, requestProblem,
  type ImprovementEditor, type ImprovementStart, type ImprovementTarget,
} from '../lib/improvementProposal'

// 4.7-A11 提一个改进, on a published flow's card and an internal-library skill's detail: pick what to improve, say it
// in one sentence, and the console makes an inert candidate -- a draft flow, a candidate skill version, a draft
// connector -- then offers the way to its editor. Nothing is tested, approved or published by this.

export interface ImprovementStarterProps {
  zh?: boolean
  /** What this detail can improve; the first is preselected. */
  targets: ImprovementTarget[]
  connected: boolean
  /** A start from this detail is on its way. */
  busy: boolean
  /** The last start from this detail: the candidate made, or the refusal in words. */
  result?: ImprovementStart | null
  refusal?: string | null
  onStart: (target: ImprovementTarget, request: string) => void
  onOpenEditor: (editor: ImprovementEditor) => void
  /** The form was opened: a chance to refresh what can be picked (the project's registered connectors). */
  onOpen?: () => void
  /** Start with the form open (the form is otherwise behind the 提一个改进 button). */
  defaultOpen?: boolean
}

export function ImprovementStarter({ zh = true, targets, connected, busy, result = null, refusal = null, onStart, onOpenEditor, onOpen, defaultOpen = false }: ImprovementStarterProps) {
  const [open, setOpen] = useState(defaultOpen)
  const [chosen, setChosen] = useState(0)
  const [request, setRequest] = useState('')
  if (targets.length === 0) return null
  const target = targets[Math.min(chosen, targets.length - 1)]
  const problem = requestProblem(request)
  return (
    <div className="cl-improvement-start" data-testid="improvement-starter">
      {!open && (
        <button type="button" className="workflows-cancel" data-testid="improvement-start-open" disabled={!connected} onClick={() => { setOpen(true); onOpen?.() }}>
          {zh ? '提一个改进' : 'Suggest an improvement'}
        </button>
      )}
      {open && (
        <form className="cl-candidate-form" data-testid="improvement-start-form" onSubmit={event => {
          event.preventDefault()
          if (problem || busy || !connected) return
          onStart(target, request.trim())
        }}>
          <p className="cl-line">{zh
            ? '选要改进的对象，用一句话说要改成什么样。会生成一个候选放在旁边：正在用的不变，候选要你编辑、测试、按真实订单评估收益并批准后，才走它自己的发布。'
            : 'Pick what to improve and say how in one sentence. A candidate is made beside it: what runs now does not change until you edit, test, judge it on real orders and approve it.'}</p>
          {targets.length > 1 && (
            <label>{zh ? '改进对象' : 'Improve'}
              <select value={String(chosen)} data-testid="improvement-start-target" onChange={event => setChosen(Number(event.target.value))}>
                {targets.map((item, index) => (
                  <option key={`${item.kind}:${item.id}`} value={String(index)}>{TARGET_KIND_WORDS[item.kind][zh ? 0 : 1]}{item.kind === 'process' ? '' : `：${item.label}`}</option>
                ))}
              </select>
            </label>
          )}
          <label>{zh ? '一句话说要改进什么' : 'In one sentence'}
            <input value={request} maxLength={REQUEST_LIMIT} data-testid="improvement-start-request"
              placeholder={zh ? '例如：报价单里写清楚报价的有效期' : 'e.g. state how long the quote is valid'}
              onChange={event => setRequest(event.target.value)} />
          </label>
          <button type="submit" className="workflows-invoke" data-testid="improvement-start-submit" disabled={Boolean(problem) || busy || !connected}>
            {busy ? (zh ? '生成中…' : 'Making…') : (zh ? '生成改进候选' : 'Make the candidate')}
          </button>
          <button type="button" className="workflows-cancel" onClick={() => setOpen(false)}>{zh ? '收起' : 'Close'}</button>
        </form>
      )}
      {result && (
        <div className="cl-line" role="status" data-testid="improvement-start-result">
          <span>{editorWords(result.editor)[zh ? 0 : 1]}</span>
          <button type="button" className="workflows-invoke" data-testid="improvement-open-editor" onClick={() => onOpenEditor(result.editor)}>{zh ? '去编辑' : 'Edit it'}</button>
          {result.not_carried.length > 0 && (
            <p className="cl-line" data-testid="improvement-not-carried">{zh ? '这些步骤设置没有带进草稿，在搭建台补上：' : 'Not carried into the draft; set them again on the builder: '}{result.not_carried.join('；')}</p>
          )}
        </div>
      )}
      {refusal && <p className="cl-line cl-detail-error" role="alert" data-testid="improvement-start-refusal">{zh ? '没有生成：' : 'Not made: '}{improvementRefusal(refusal)}</p>}
    </div>
  )
}
