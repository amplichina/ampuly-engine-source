import { useState } from 'react'
import { useI18n } from '../i18n'
import type { AuthAccount, AuthState } from './LoginScreen'
import type { OrgEmployee } from '../types/visual'

interface AccountsSectionProps {
  auth: AuthState
  accounts: AuthAccount[] | null
  employees: OrgEmployee[]
  lastError: string
  onRefresh: () => void
  onBootstrapOwner: (login: string, password: string, displayName: string) => void
  onCreate: (login: string, password: string, employeeId: string, displayName: string) => void
  onSetDisabled: (login: string, disabled: boolean) => void
  onSetPassword: (login: string, password: string) => void
  onSetCustomerScope?: (login: string, customerIds: string[]) => void
  onLogout: () => void
  connected?: boolean
}

// The owner's view of who can log in. Sub-accounts are always linked to a
// person in the org directory; an AI persona never gets a login.
export function AccountsSection({ auth, accounts, employees, lastError, onRefresh, onBootstrapOwner, onCreate, onSetDisabled, onSetPassword, onSetCustomerScope, onLogout, connected = true }: AccountsSectionProps) {
  const { t } = useI18n()
  const [login, setLogin] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [employeeId, setEmployeeId] = useState('')
  const [pwTarget, setPwTarget] = useState<string | null>(null)
  const [scopeTarget, setScopeTarget] = useState<string | null>(null)
  const [scopeDraft, setScopeDraft] = useState('')
  const [pwDraft, setPwDraft] = useState('')
  const people = employees.filter(e => e.kind === 'human' || e.kind === 'supervised_ai')

  return (
    <section>
      <h3 className="settings-section-title">{t('settings.accountsSection')}</h3>
      <div className="settings-note">{auth.bootstrap_needed ? t('settings.accountsBootstrapHint') : t('settings.accountsHint')}</div>
      {auth.account && (
        <div className="accounts-me">
          {t('settings.accountsSignedInAs', { name: auth.account.display_name || auth.account.login })}
          <button className="settings-link-btn" onClick={onLogout} disabled={!connected}>{t('employee.logout')}</button>
        </div>
      )}

      {auth.bootstrap_needed ? (
        <div className="accounts-form">
          <input placeholder={t('login.login')} value={login} onChange={e => setLogin(e.target.value)} />
          <input placeholder={t('login.displayName')} value={displayName} onChange={e => setDisplayName(e.target.value)} />
          <input type="password" placeholder={t('login.password')} value={password} onChange={e => setPassword(e.target.value)} />
          <button
            className="settings-btn"
            disabled={!connected || !login.trim() || password.length < 6}
            onClick={() => { onBootstrapOwner(login.trim(), password, displayName.trim()); setPassword('') }}
          >
            {t('settings.accountsCreateOwner')}
          </button>
        </div>
      ) : (
        <>
          <div className="accounts-list">
            {(accounts ?? []).map(account => (
              <div key={account.login} className={`accounts-row${account.disabled ? ' accounts-row--disabled' : ''}`}>
                <span className="accounts-login">{account.login}</span>
                <span className="accounts-role">{account.role === 'owner' ? t('settings.accountsOwner') : account.role === 'customer' ? t('settings.accountsCustomer') : t('settings.accountsEmployee')}</span>
                <span className="accounts-name">
                  {account.display_name}
                  {account.employee_id ? ` · ${employees.find(e => e.employee_id === account.employee_id)?.name ?? account.employee_id}` : ''}
                </span>
                <span className="accounts-scope" data-customer-scope={(account.customer_ids ?? []).join(',')}>
                  {account.role === 'owner'
                    ? t('settings.accountsScopeOwner')
                    : (account.customer_ids ?? []).length > 0
                      ? `${t('settings.accountsScopeBound')}: ${(account.customer_ids ?? []).join(' · ')}`
                      : t('settings.accountsScopeAll')}
                </span>
                <span className="accounts-actions">
                  <button className="settings-link-btn" onClick={() => { setPwTarget(account.login); setPwDraft('') }} disabled={!connected}>{t('settings.accountsSetPassword')}</button>
                  {account.role !== 'owner' && onSetCustomerScope && (
                    <button className="settings-link-btn" data-testid="accounts-edit-scope" onClick={() => { setScopeTarget(account.login); setScopeDraft((account.customer_ids ?? []).join(', ')) }} disabled={!connected}>
                      {t('settings.accountsScopeEdit')}
                    </button>
                  )}
                  {account.role !== 'owner' && (
                    <button className="settings-link-btn" onClick={() => onSetDisabled(account.login, !account.disabled)} disabled={!connected}>
                      {account.disabled ? t('settings.accountsEnable') : t('settings.accountsDisable')}
                    </button>
                  )}
                </span>
                {scopeTarget === account.login && onSetCustomerScope && (
                  <span className="accounts-scope-edit">
                    <input
                      value={scopeDraft}
                      placeholder={t('settings.accountsScopePlaceholder')}
                      onChange={e => setScopeDraft(e.target.value)}
                      aria-label={t('settings.accountsScopeEdit')}
                    />
                    <button className="settings-btn" disabled={!connected} onClick={() => { onSetCustomerScope(account.login, scopeDraft.split(/[,，\s]+/).map(v => v.trim()).filter(Boolean)); setScopeTarget(null) }}>{t('settings.accountsSave')}</button>
                    <button className="settings-link-btn" onClick={() => setScopeTarget(null)}>{t('settings.accountsCancel')}</button>
                    <span className="settings-note">{t('settings.accountsScopeHint')}</span>
                  </span>
                )}
                {pwTarget === account.login && (
                  <span className="accounts-pw">
                    <input type="password" placeholder={t('settings.accountsNewPassword')} value={pwDraft} onChange={e => setPwDraft(e.target.value)} />
                    <button className="settings-btn" disabled={!connected || pwDraft.length < 6} onClick={() => { onSetPassword(account.login, pwDraft); setPwTarget(null) }}>{t('settings.accountsSave')}</button>
                    <button className="settings-link-btn" onClick={() => setPwTarget(null)}>{t('settings.accountsCancel')}</button>
                  </span>
                )}
              </div>
            ))}
            {accounts !== null && accounts.length === 0 && <div className="settings-note">{t('settings.accountsNone')}</div>}
            <button className="settings-link-btn" onClick={onRefresh} disabled={!connected}>{t('settings.accountsRefresh')}</button>
          </div>

          <div className="accounts-form">
            <div className="settings-note">{t('settings.accountsCreateHint')}</div>
            <select value={employeeId} onChange={e => setEmployeeId(e.target.value)}>
              <option value="">{t('settings.accountsPickEmployee')}</option>
              {people.map(p => <option key={p.employee_id} value={p.employee_id}>{p.name}（{p.kind === 'human' ? t('team.kind.human') : t('team.kind.supervised_ai')}）</option>)}
            </select>
            <input placeholder={t('login.login')} value={login} onChange={e => setLogin(e.target.value)} />
            <input placeholder={t('login.displayName')} value={displayName} onChange={e => setDisplayName(e.target.value)} />
            <input type="password" placeholder={t('login.password')} value={password} onChange={e => setPassword(e.target.value)} />
            <button
              className="settings-btn"
              disabled={!connected || !employeeId || !login.trim() || password.length < 6}
              onClick={() => { onCreate(login.trim(), password, employeeId, displayName.trim()); setPassword(''); setLogin('') }}
            >
              {t('settings.accountsCreate')}
            </button>
            {people.length === 0 && <div className="settings-note">{t('settings.accountsNoPeople')}</div>}
          </div>
        </>
      )}
      {lastError && <div className="login-error">{lastError}</div>}
    </section>
  )
}
