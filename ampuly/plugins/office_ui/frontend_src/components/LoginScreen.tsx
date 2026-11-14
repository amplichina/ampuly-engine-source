import { useState, type FormEvent } from 'react'
import { useI18n } from '../i18n'

export interface AuthAccount {
  login: string
  /** owner (everything), employee (their own work), customer (their own orders only; S3-6A, labelled 客户 where an
   *  existing account is listed -- no account is created from this screen). */
  role: 'owner' | 'employee' | 'customer'
  employee_id: string
  display_name: string
  disabled: boolean
  customer_ids?: string[]
}

export interface AuthState {
  required: boolean
  bootstrap_needed: boolean
  account: AuthAccount | null
}

interface LoginScreenProps {
  auth: AuthState
  connected: boolean
  error: string
  busy: boolean
  onLogin: (login: string, password: string) => void
  onBootstrapOwner: (login: string, password: string, displayName: string) => void
}

// The console's front door. Before any account exists the owner sets up
// their own login here; afterwards everyone signs in on the same screen and
// the server decides what they see.
export function LoginScreen({ auth, connected, error, busy, onLogin, onBootstrapOwner }: LoginScreenProps) {
  const { t } = useI18n()
  const [login, setLogin] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const bootstrap = auth.bootstrap_needed

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (busy || !login.trim() || !password) return
    if (bootstrap) onBootstrapOwner(login.trim(), password, displayName.trim())
    else onLogin(login.trim(), password)
  }

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">安普里<span className="logo-accent"> Ampuly</span></div>
        <h2 className="login-title">{bootstrap ? t('login.bootstrapTitle') : t('login.title')}</h2>
        <p className="login-subtitle">{bootstrap ? t('login.bootstrapHint') : t('login.hint')}</p>
        <label className="login-field">
          <span>{t('login.login')}</span>
          <input type="text" autoFocus autoComplete="username" value={login} onChange={e => setLogin(e.target.value)} />
        </label>
        {bootstrap && (
          <label className="login-field">
            <span>{t('login.displayName')}</span>
            <input type="text" value={displayName} onChange={e => setDisplayName(e.target.value)} />
          </label>
        )}
        <label className="login-field">
          <span>{t('login.password')}</span>
          <input type="password" autoComplete={bootstrap ? 'new-password' : 'current-password'} value={password} onChange={e => setPassword(e.target.value)} />
        </label>
        {error && <div className="login-error">{error}</div>}
        {!connected && <div className="login-note">{t('login.connecting')}</div>}
        <button type="submit" className="login-submit" disabled={busy || !connected || !login.trim() || !password}>
          {busy ? t('login.busy') : bootstrap ? t('login.bootstrapSubmit') : t('login.submit')}
        </button>
      </form>
    </div>
  )
}
