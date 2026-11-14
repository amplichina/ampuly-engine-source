import type { RecoveryOffer } from '../lib/draftRecovery'

function when(iso: string): string {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return iso
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${parsed.getMonth() + 1}/${parsed.getDate()} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`
}

/** An unsaved draft found in this browser. It is offered, never applied: recovering opens it as a draft to review and
 *  save like any other; nothing reaches the server until a person saves. A draft of an earlier saved version is not
 *  offered for recovery -- putting it back would silently undo what was saved since. */
export function DraftRecoveryBanner({ offer, savedVersion, zh, onRecover, onDiscard }: {
  offer: RecoveryOffer
  savedVersion: string
  zh: boolean
  onRecover: () => void
  onDiscard: () => void
}) {
  if (offer.kind === 'none') return null
  const { draft } = offer
  const at = draft.storedAt ? when(draft.storedAt) : ''
  return (
    <div className="wf-draft-recovery" role="status" data-testid="draft-recovery" data-kind={offer.kind}>
      {offer.kind === 'recover' ? (
        <>
          <span>{zh
            ? `这台浏览器上有一份没保存的草稿${at ? `（${at}）` : ''}，${draft.nodes.length} 个步骤，基于 v${draft.baseVersion}。恢复后仍是草稿，要你保存才生效。先决定恢复还是丢弃，画布才能继续编辑。`
            : `This browser holds an unsaved draft${at ? ` (${at})` : ''}: ${draft.nodes.length} steps, started from v${draft.baseVersion}. Recovering it opens a draft; nothing is saved until you save. Decide first: the canvas stays read-only until you do.`}</span>
          <button type="button" className="workflows-approve" onClick={onRecover}>{zh ? '恢复草稿' : 'Recover the draft'}</button>
        </>
      ) : (
        <span>{zh
          ? `这台浏览器上有一份旧草稿${at ? `（${at}）` : ''}，它基于 v${draft.baseVersion}，而现在已保存的是 v${savedVersion}。直接恢复会悄悄抹掉这之间保存过的改动，所以不提供恢复。`
          : `This browser holds an older draft${at ? ` (${at})` : ''} started from v${draft.baseVersion}; the saved version is now v${savedVersion}. Putting it back would silently undo what was saved since, so it is not offered.`}</span>
      )}
      <button type="button" className="workflows-cancel" onClick={onDiscard}>{zh ? '丢弃它' : 'Discard it'}</button>
    </div>
  )
}
