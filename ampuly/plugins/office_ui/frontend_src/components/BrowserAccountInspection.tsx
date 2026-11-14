import type { BrowserAccountInspectionView } from '../workspace/browserAccountInspection'

export function BrowserAccountInspection({ view, zh }: { view: BrowserAccountInspectionView; zh: boolean }) {
  if (view.status === 'pending') return <div className="capability-detail" role="status">{zh ? '正在读取账号资料…' : 'Reading account data…'}</div>
  if (view.status === 'error') {
    const words = view.error === 'not_sent' ? (zh ? '请求未发出，请连接后重新查看。' : 'Request not sent. Reconnect and try again.')
      : view.error === 'timeout' ? (zh ? '未收到资料回执，请重新查看。' : 'No account-data receipt arrived. Try again.')
        : view.error === 'invalid_response' ? (zh ? '资料回执不完整，请重新查看。' : 'The account-data receipt was incomplete. Try again.')
          : (zh ? '无法读取该会话的账号资料。' : 'Account data for this session could not be read.')
    return <div className="capability-detail" role="status">{words}</div>
  }
  const account = view.inspection?.account
  return <div className="capability-detail" data-testid="browser-account-inspection" role="status">
    {view.inspection?.state === 'unbound' ? <div>{zh ? '当前会话未绑定保留账号资料。' : 'This session has no retained account binding.'}</div>
      : account ? <>
        <div>{zh ? '当前会话已绑定账号资料。' : 'This session has an account-data binding.'}</div>
        {([
          ['present', '资料记录', 'Account record'], ['profile_present', '浏览器资料', 'Browser profile'],
          ['owner_present', '占用记录', 'Lease record'], ['pending_present', '待核验记录', 'Pending verification record'],
        ] as const).map(([key, cn, en]) => <div key={key}>{zh ? cn : en}{zh ? '：' : ': '}{account[key] ? (zh ? '存在' : 'present') : (zh ? '不存在' : 'absent')}</div>)}
        <div>{zh ? '撤销标记：' : 'Revoked marker: '}{account.revoked ? (zh ? '是' : 'yes') : (zh ? '否' : 'no')}</div>
      </> : <div>{zh ? '资料状态未知。' : 'Account-data status is unknown.'}</div>}
    <div>{zh ? '资料存在不代表已登录，也不授予任务执行权限。' : 'Stored data does not establish login or grant task execution permission.'}</div>
  </div>
}
