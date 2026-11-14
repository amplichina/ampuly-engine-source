/** A refused verification never authenticates a socket. Only an explicit,
 * terminal credential result tells the browser to discard its saved token. */
export interface AuthTokenFailurePolicy {
  clearCredential: boolean
  message: string
}

const INVALID_CREDENTIAL_CODES = new Set([
  'invalid_token',
  'token_expired',
  'expired_token',
  'token_revoked',
  'revoked_token',
  'session_expired',
  'session_revoked',
])

export function authTokenFailurePolicy(payload: {
  error_code?: unknown
  error?: unknown
}): AuthTokenFailurePolicy {
  // error_code is the protocol result; a legacy display message must not
  // override it. Unknown failures preserve credentials but still stay blocked.
  const code = typeof payload.error_code === 'string' && payload.error_code
    ? payload.error_code : typeof payload.error === 'string' ? payload.error : ''
  if (INVALID_CREDENTIAL_CODES.has(code)) {
    return {
      clearCredential: true,
      message: '登录信息已失效，请重新登录。',
    }
  }
  return {
    clearCredential: false,
    message: '暂时无法验证登录，登录信息已保留。请稍后刷新页面重新验证，无需重新输入密码；业务操作不会自动重发。',
  }
}
