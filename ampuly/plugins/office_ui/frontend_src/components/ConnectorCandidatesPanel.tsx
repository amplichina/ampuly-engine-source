import { useState } from 'react'

export interface ConnectorCandidateRow {
  candidate_id: string
  connector: string
  version: string
  status: string
  candidate_fingerprint: string
  created_at: string
}

export interface ConnectorRegistrationRow {
  connector: string
  version: string
  candidate_id: string
  candidate_fingerprint: string
  status: string
  enabled: boolean
  registered_at: string
}

export interface ConnectorCandidatesPanelProps {
  candidates: ConnectorCandidateRow[]
  registrations: ConnectorRegistrationRow[]
  connected: boolean
  error?: string | null
  onRefresh: () => void
  onTest?: (candidateId: string) => void
  onReview?: (candidateId: string, fingerprint: string, approved: boolean, note: string) => void
  onRegister?: (candidateId: string, fingerprint: string) => void
  onRevoke?: (connector: string, fingerprint: string, reason: string) => void
}

const statusText: Record<string, string> = {
  draft: '草稿',
  test_failed: '测试失败',
  tested: '测试通过',
  approved: '已批准',
  rejected: '已打回',
  registered: '已注册（未启用）',
  revoked: '已撤销',
}

export function ConnectorCandidatesPanel({
  candidates, registrations, connected, error, onRefresh, onTest, onReview, onRegister, onRevoke,
}: ConnectorCandidatesPanelProps) {
  const [review, setReview] = useState({ candidateId: '', note: '' })
  const [revoke, setRevoke] = useState({ connector: '', reason: '' })

  return (
    <article className="capability-panel" data-testid="connector-candidates">
      <div className="capability-panel-title">
        <h2>连接器候选</h2>
        <span>{candidates.length}</span>
        <button type="button" className="capability-refresh-btn" disabled={!connected} onClick={onRefresh}>刷新</button>
      </div>
      <div className="capability-muted">
        总控对话只能生成声明式草稿。通过本地合同测试、人工审查和指纹锁定注册后仍默认禁用；启用是另一项决定。
      </div>
      {error && <p className="capability-detail capability-detail--warn" role="alert" data-testid="connector-candidate-error">{error}</p>}
      {candidates.length === 0 ? (
        <div className="capability-muted">尚无候选。可在总控中描述接口合同并要求“生成连接器候选”。</div>
      ) : (
        <div className="capability-list">
          {candidates.map(candidate => {
            const isReviewing = review.candidateId === candidate.candidate_id
            return (
              <div className="capability-list-item capability-list-item--stacked" key={candidate.candidate_id} data-testid="connector-candidate-row">
                <div>
                  <strong>{candidate.connector} · {candidate.version}</strong>
                  <small>{statusText[candidate.status] ?? candidate.status}</small>
                  <small className="capability-detail">指纹 {candidate.candidate_fingerprint}</small>
                  {isReviewing && (
                    <span className="capability-reason-form" data-testid="connector-candidate-review-form">
                      <input
                        className="capability-refresh-btn"
                        placeholder="审查意见（必须填写）"
                        value={review.note}
                        onChange={event => setReview({ candidateId: candidate.candidate_id, note: event.target.value })}
                      />
                      <button type="button" className="capability-refresh-btn" disabled={!connected || review.note.trim().length < 4} onClick={() => {
                        onReview?.(candidate.candidate_id, candidate.candidate_fingerprint, true, review.note.trim())
                        setReview({ candidateId: '', note: '' })
                      }}>批准</button>
                      <button type="button" className="workflows-cancel" disabled={!connected || review.note.trim().length < 4} onClick={() => {
                        onReview?.(candidate.candidate_id, candidate.candidate_fingerprint, false, review.note.trim())
                        setReview({ candidateId: '', note: '' })
                      }}>打回</button>
                    </span>
                  )}
                </div>
                <span>
                  <span className={`capability-pill capability-pill--${['tested', 'approved', 'registered'].includes(candidate.status) ? 'ok' : 'warn'}`}>
                    {statusText[candidate.status] ?? candidate.status}
                  </span>
                  {onTest && ['draft', 'test_failed'].includes(candidate.status) && (
                    <button type="button" className="capability-refresh-btn" disabled={!connected} data-testid="connector-candidate-test" onClick={() => onTest(candidate.candidate_id)} title="只调用进程内样例；模拟写入需授权，但不会访问外部平台">本地合同测试</button>
                  )}
                  {onReview && candidate.status === 'tested' && !isReviewing && (
                    <button type="button" className="capability-refresh-btn" disabled={!connected} data-testid="connector-candidate-review" onClick={() => setReview({ candidateId: candidate.candidate_id, note: '' })}>人工审查</button>
                  )}
                  {onRegister && candidate.status === 'approved' && (
                    <button type="button" className="capability-refresh-btn" disabled={!connected} data-testid="connector-candidate-register" onClick={() => onRegister(candidate.candidate_id, candidate.candidate_fingerprint)}>注册为禁用</button>
                  )}
                </span>
              </div>
            )
          })}
        </div>
      )}
      {registrations.length > 0 && (
        <div className="capability-list" data-testid="connector-registrations">
          {registrations.map(item => {
            const isRevoking = revoke.connector === item.connector
            return (
              <div className="capability-list-item" key={`${item.connector}:${item.candidate_fingerprint}`}>
                <div>
                  <strong>{item.connector} · {item.version}</strong>
                  <small>{statusText[item.status] ?? item.status}{item.enabled ? ' · 已启用' : ' · 未启用'}</small>
                  {isRevoking && (
                    <span className="capability-reason-form">
                      <input className="capability-refresh-btn" placeholder="撤销原因（必须填写）" value={revoke.reason} onChange={event => setRevoke({ connector: item.connector, reason: event.target.value })} />
                      <button type="button" className="workflows-cancel" disabled={!connected || revoke.reason.trim().length < 4} onClick={() => {
                        onRevoke?.(item.connector, item.candidate_fingerprint, revoke.reason.trim())
                        setRevoke({ connector: '', reason: '' })
                      }}>确认撤销</button>
                    </span>
                  )}
                </div>
                {onRevoke && item.status !== 'revoked' && !isRevoking && (
                  <button type="button" className="workflows-cancel" disabled={!connected} data-testid="connector-registration-revoke" onClick={() => setRevoke({ connector: item.connector, reason: '' })}>撤销注册</button>
                )}
              </div>
            )
          })}
        </div>
      )}
    </article>
  )
}
