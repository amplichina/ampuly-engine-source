import { useI18n } from '../i18n'
import type { FlowTarget } from '../lib/componentCatalog'

type Translate = ReturnType<typeof useI18n>['t']

/**
 * 调用 / 调用测试: the 业务流 a component can be taken to, each saying whether the builder can take it there now.
 * Shared by the internal library's cards and the market's.
 */
export function FlowPicker({ title, note, targets, onPick, onClose, t }: {
  title: string
  note?: string
  targets: readonly FlowTarget[]
  onPick: (workflowId: string) => void
  onClose: () => void
  t: Translate
}) {
  return (
    <div className="cl-use-picker" role="dialog" aria-label={title} data-testid="use-picker">
      <p>{title}</p>
      {note && <p className="cl-note">{note}</p>}
      {targets.length === 0 ? <p className="cl-empty">{t('components.useNoFlows')}</p> : (
        <ul>
          {targets.map(target => (
            <li key={target.id}>
              <button type="button" disabled={target.state !== 'ok'} data-testid="use-target" data-id={target.id} data-state={target.state} onClick={() => onPick(target.id)}>
                <strong>{target.name}</strong>
                <small>{target.id} · v{target.version} · {t(`components.flowStatus.${target.status}` as Parameters<Translate>[0])}{target.uses ? ` · ${t('components.useUses')}` : ''}</small>
                {target.state !== 'ok' && <em>{t(`components.useState.${target.state}` as Parameters<Translate>[0])}</em>}
              </button>
            </li>
          ))}
        </ul>
      )}
      <button type="button" className="workflows-cancel" onClick={onClose}>{t('components.useClose')}</button>
    </div>
  )
}
