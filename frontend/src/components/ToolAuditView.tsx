import { useState } from 'react'
import { fetchToolAuditPage, type ToolAuditEvent } from '../api'

type Integrity = { ok: boolean; events: number; reason?: string }

export function ToolAuditView({ sessionId, integrity }: {
  sessionId: string
  integrity: Integrity
}) {
  const [events, setEvents] = useState<ToolAuditEvent[]>([])
  const [cursor, setCursor] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [verified, setVerified] = useState(integrity)

  const load = async (after: number) => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const page = await fetchToolAuditPage(sessionId, after)
      setEvents((previous) => after === 0 ? page.events : [...previous, ...page.events])
      setCursor(page.cursor)
      setHasMore(page.has_more)
      setVerified(page.integrity)
      setLoaded(true)
    } catch (reason) {
      setError((reason as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return <details className="tool-audit" onToggle={(event) => {
    if (event.currentTarget.open && !loaded && !busy) void load(0)
  }}>
    <summary>工具事件审计 · {integrity.ok ? `${integrity.events} 条` : '完整性异常'}</summary>
    <button type="button" disabled={busy} onClick={() => { void load(0) }}>刷新审计</button>
    {!verified.ok && <div className="notice err">{verified.reason ?? '工具事件链校验失败'}</div>}
    {error && <div className="notice err">审计读取失败：{error}</div>}
    {busy && <div className="muted">正在读取事件…</div>}
    {loaded && events.length === 0 && <div className="muted">当前会话没有工具事件。</div>}
    {events.map((item) => <div className="tool-audit-row" key={item.event_id}>
      <span className="tool-audit-kind">{item.kind}</span>
      <span title={item.call_id}>{item.call_id}</span>
      <span>{item.actor} · {new Date(item.created).toLocaleString()}</span>
      <code title={item.hash}>seq {item.seq} · {item.hash.slice(0, 12)}</code>
    </div>)}
    {hasMore && <button type="button" disabled={busy} onClick={() => { void load(cursor) }}>加载下一页</button>}
    <p className="muted">这里只展示事件元数据与摘要；工具参数和结果请在上方工具卡片查看。</p>
  </details>
}
