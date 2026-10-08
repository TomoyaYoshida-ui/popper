import { currencyToMicros, microsToCurrency, validCurrency } from '../money'
import { useEffect, useRef, useState } from 'react'
import type { ChatState, PopperState } from '../types'
import type { WorkbenchResource } from '../workbench'
import { Markdown } from './Markdown'
import { ToolCallCard } from './ToolCallCard'
import { TaskContextCard } from './TaskContextCard'
import { WorkbenchIcon } from './WorkbenchIcon'

export function AgentChatPane({ chat, chatPending, requestError, contextKey, projectName, studyLabel, review, onSend,
  onToolAction, onContinueTools, onCancelTurn, onRetryTurn, onSteerTurn, onSetBudget, onSetCostBudget, onAdjudicate, onOpenResource, workspaceId, researchVersion = '', onOpenSession }: {
  chat: ChatState | null; chatPending: boolean; contextKey: string; projectName: string; studyLabel: string | null
  requestError?: string | null
  workspaceId?: string; researchVersion?: string; onOpenSession?: (sessionId: string) => void
  review: PopperState['review'] | null; onSend: (text: string) => Promise<boolean>
  onToolAction: (callId: string, action: 'approve' | 'execute' | 'reject' | 'cancel') => Promise<void>
  onContinueTools: (turnId: string) => Promise<void>; onCancelTurn: () => Promise<void>
  onRetryTurn: (turnId: string) => Promise<void>; onSteerTurn: (turnId: string, text: string) => Promise<boolean>
  onSetBudget: (budget: number | null) => Promise<void>; onSetCostBudget: (budget: number | null) => Promise<void>
  onAdjudicate: (riskId: string, verdict: 'allow' | 'reject', reason: string) => Promise<void>
  onOpenResource: (resource: WorkbenchResource) => void
}) {
  const [showContext, setShowContext] = useState(false)
  const draftKey = `popper-composer:${contextKey}`
  const [draftState, setDraftState] = useState(() => {
    try { return { key: draftKey, text: localStorage.getItem(draftKey) ?? '' } }
    catch { return { key: draftKey, text: '' } }
  })
  const draft = draftState.key === draftKey ? draftState.text : ''
  const latestDraft = useRef(draftState)
  latestDraft.current = draftState
  const setDraft = (text: string) => setDraftState({ key: draftKey, text })
  const [budgetDraft, setBudgetDraft] = useState('')
  const [costDraft, setCostDraft] = useState('')
  const feed = useRef<HTMLDivElement>(null)
  const composer = useRef<HTMLTextAreaElement>(null)
  const follow = useRef(true)
  const scrollReady = useRef(false)
  const contextScroll = useRef(0)
  const composing = useRef(false)
  const scrollKey = `popper-scroll:${contextKey}`
  const running = chat?.turns.find((turn) => turn.status === 'running')
  const [requestClock, setRequestClock] = useState(Date.now())
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => setRequestClock(Date.now()), 5000)
    return () => window.clearInterval(timer)
  }, [running?.turn_id])
  const latest = chat?.turns[chat.turns.length - 1]
  const latestCalls = chat?.tool_calls.filter((call) => call.turn_id === latest?.turn_id) ?? []
  const continuation = chat?.continuation
  const continuationTurn = continuation?.source_turn_id ? chat?.turns.find((turn) => turn.turn_id === continuation.source_turn_id) : latest
  const legacyCanContinue = latest?.status === 'completed' && latestCalls.length > 0 &&
    latestCalls.every((call) => ['completed', 'failed', 'rejected', 'cancelled', 'interrupted', 'invalid'].includes(call.status))
  const canContinue = continuation ? continuation.state === 'ready' && !!continuationTurn && !running : legacyCanContinue
  const ready = !!chat?.configured && !!draft.trim() && !chatPending
  const title = chat?.sessions.find((session) => session.session_id === chat.session_id)?.title ?? '新的研究会话'

  useEffect(() => {
    try { setDraftState({ key: draftKey, text: localStorage.getItem(draftKey) ?? '' }) }
    catch { setDraftState({ key: draftKey, text: '' }) }
    setBudgetDraft(''); setCostDraft('')
    scrollReady.current = false
    const frame = requestAnimationFrame(() => {
      if (feed.current) {
        let saved: string | null = null
        try { saved = localStorage.getItem(scrollKey) } catch { /* Storage is optional. */ }
        feed.current.scrollTop = saved == null ? (chat?.turns.length ? feed.current.scrollHeight : 0) : Number(saved) || 0
        follow.current = feed.current.scrollHeight - feed.current.scrollTop - feed.current.clientHeight < 90
      }
      scrollReady.current = true
    })
    return () => cancelAnimationFrame(frame)
  }, [draftKey, scrollKey])
  useEffect(() => {
    if (draftState.key !== draftKey) return
    try { localStorage.setItem(draftKey, draftState.text) } catch { /* Keep the in-memory draft. */ }
  }, [draftState, draftKey])
  useEffect(() => {
    const element = composer.current
    if (!element) return
    element.style.height = 'auto'
    element.style.height = `${Math.min(180, Math.max(58, element.scrollHeight))}px`
  }, [draft])
  useEffect(() => {
    if (chat?.turns.length && !showContext && follow.current && feed.current && scrollReady.current) feed.current.scrollTop = feed.current.scrollHeight
  }, [chat, chatPending])
  const closeContext = () => {
    setShowContext(false)
    requestAnimationFrame(() => { if (feed.current) feed.current.scrollTop = contextScroll.current })
  }
  const submit = async () => {
    if (!ready) return
    const value = draft.trim()
    follow.current = true
    if (await onSend(value)) {
      // A reply can arrive after a session switch or after the user has started
      // composing the next message. Never clear that newer draft.
      try { if (localStorage.getItem(draftKey) === draft) localStorage.removeItem(draftKey) } catch { /* Storage is optional. */ }
      if (latestDraft.current.key === draftKey && latestDraft.current.text === draft) setDraft('')
    }
  }
  return <section className="agent-pane" aria-label="Agent 对话">
    <header className="agent-header">
      <div className="agent-heading"><span className="agent-title" title={title}>{title}</span>
        <span className="agent-context">{projectName}{studyLabel ? ` / ${studyLabel}` : ''}</span></div>
      {!!chat?.session_id && workspaceId && <button type="button" className="task-context-toggle" disabled={chatPending} aria-expanded={showContext} onClick={() => {
        if (showContext) closeContext()
        else { contextScroll.current = feed.current?.scrollTop ?? 0; setShowContext(true); requestAnimationFrame(() => { if (feed.current) feed.current.scrollTop = 0 }) }
      }}>任务上下文</button>}
      <details className="agent-settings"><summary title="会话预算与模型设置" aria-label="会话设置"><WorkbenchIcon kind="more" /></summary>
        <div className="agent-settings-body">
          <strong>会话设置</strong><span>{chat?.model ?? '模型未配置'}</span>
          <span>{chat?.usage.total_tokens.toLocaleString() ?? 0} tokens 已用</span>
          <label>Token 额度<input aria-label="会话 Token 额度" value={budgetDraft} placeholder={chat?.usage.token_budget?.toString() ?? '无限额'} onChange={(event) => setBudgetDraft(event.target.value)} /></label>
          <button type="button" disabled={!chat?.session_id || chatPending || (!!budgetDraft.trim() && !/^[1-9]\d*$/.test(budgetDraft.trim()))}
            onClick={() => { void onSetBudget(budgetDraft.trim() ? Number(budgetDraft) : null) }}>设置 Token 额度</button>
          {chat?.usage.currency && <><span>{chat.usage.currency} {(chat.usage.cost_micros / 1_000_000).toFixed(6)} 已用</span>
            <label>费用额度<input aria-label="会话费用额度" value={costDraft} placeholder={microsToCurrency(chat.usage.cost_budget_micros) || '无限额'} onChange={(event) => setCostDraft(event.target.value)} /></label>
            <button type="button" disabled={!chat?.session_id || chatPending || !validCurrency(costDraft)}
              onClick={() => { void onSetCostBudget(currencyToMicros(costDraft)) }}>设置费用额度</button></>}
          {!chat?.configured && <p>设置模型与 API 凭据后即可发送。<button type="button" onClick={() => window.dispatchEvent(new Event('popper:provider-settings'))}>打开模型配置</button></p>}
        </div>
      </details>
    </header>
    <div className="agent-feed" ref={feed} onScroll={() => {
      if (!feed.current || !scrollReady.current) return
      follow.current = feed.current.scrollHeight - feed.current.scrollTop - feed.current.clientHeight < 90
      try { localStorage.setItem(scrollKey, String(feed.current.scrollTop)) } catch { /* Storage is optional. */ }
    }}>
      {workspaceId && chat?.session_id && (showContext || chat.turns.length === 0) && <TaskContextCard workspaceId={workspaceId} sessionId={chat.session_id}
        projectName={projectName} busy={chatPending} expanded={showContext} onClose={closeContext}
        versionSignal={`${chat.sessions.map((session) => `${session.session_id}:${session.updated}`).join('|')}:${researchVersion}`}
        onOpenSession={onOpenSession} onOpenResource={onOpenResource} />}
      {!chat && <div className="chat-empty"><span className="popper-mark"><WorkbenchIcon kind="orbit" /></span><h2>正在连接科研工作区</h2><p>读取会话与项目状态…</p></div>}
      {chat && chat.turns.length === 0 && <div className="chat-empty">
        <span className="popper-mark"><WorkbenchIcon kind="orbit" /></span><h2>从一个研究问题开始</h2>
        <p>{chat.configured ? '与 Agent 讨论方案，在右侧查看实验、证据和稿件。' : '尚未配置对话模型。设置模型与凭据后即可开始。'}</p>
        {!chat.configured && <button type="button" onClick={() => window.dispatchEvent(new Event('popper:provider-settings'))}>配置对话模型</button>}
        <div className="chat-suggestions">{['梳理当前研究的目标与下一步', '核查已有实验与证据', '提出一个可验证的研究方案'].map((text) =>
          <button type="button" key={text} onClick={() => { setDraft(text); composer.current?.focus() }}>{text}<WorkbenchIcon kind="arrowOut" /></button>)}</div>
      </div>}
      {chat?.turns.map((turn) => <article className="agent-turn" key={turn.turn_id} data-turn-id={turn.turn_id}>
        {turn.user_message && <div className="chat-message user"><div className="chat-text">{turn.user_message}</div></div>}
        {(turn.assistant_message || turn.assistant_draft) && <div className="chat-message assistant">
          <div className="chat-meta"><span className="agent-dot" />{chat.model ?? 'Agent'}</div>
          <div className="chat-text"><Markdown text={turn.assistant_message ?? turn.assistant_draft} /></div>
        </div>}
        {turn.status === 'running' && <div className="agent-working" role="status"><span className="pulse-dot" />
          {turn.request_progress?.state === 'retrying' ? '正在重试' : turn.assistant_draft ? '正在接收回复' : '等待模型响应'}
          {' · '}{Math.max(0, Math.floor((requestClock - Date.parse(turn.created)) / 1000))} 秒
        </div>}
        {chat.tool_calls.filter((call) => call.turn_id === turn.turn_id).map((call) =>
          <ToolCallCard key={call.call_id} call={call} projectName={projectName} chatPending={chatPending} onToolAction={onToolAction} onOpenResource={onOpenResource} />)}
        {(turn.error || ['failed', 'interrupted'].includes(turn.status)) && <div className="turn-error" role="status">
          <span>{turn.error?.message ?? (turn.status === 'interrupted' ? '此轮已中断，已有结果仍被保留。' : '此轮执行失败。')}</span>
          <button type="button" disabled={chatPending} onClick={() => { void onRetryTurn(turn.turn_id) }}>重试此轮</button>
        </div>}
      </article>)}
      {continuation && <div className={`continuation-notice ${continuation.state}`} role="status">
        <strong>{continuation.state === 'waiting_approval' ? '等待工具审批' : continuation.state === 'waiting_job' ? '等待研究结果' : continuation.state === 'blocked' ? '续推理暂不可继续' : '可以根据真实工具结果继续'}</strong>
        <p>{continuation.reason ?? (continuation.state === 'waiting_approval' ? '请先核对并处理有效提案；此提示不会替代任何创建或执行审批。' : continuation.state === 'waiting_job' ? '研究作业仍在执行；真实结果到达后才继续推理，已入队不代表实验完成。' : continuation.state === 'blocked' ? '服务端暂未允许续推理，请核对预算、上下文版本与纠错次数；已有结果和输入仍被保留。' : '工具结果已保留，继续时仍受会话预算、总账本与审批规则约束。')}</p>
        {continuation.correction_attempts != null && <span>参数纠错：{continuation.correction_attempts}{continuation.max_corrections != null ? ` / ${continuation.max_corrections}` : ''} 次</span>}
      </div>}
      {canContinue && continuationTurn && <button className="continue-tools" type="button" disabled={chatPending} onClick={() => { void onContinueTools(continuationTurn.turn_id) }}>根据真实工具结果继续 →</button>}
      {review && <GateCard review={review} projectName={projectName} onAdjudicate={onAdjudicate} />}
    </div>
    <footer className="agent-composer">
      {requestError && <p className="chat-error" role="alert">{requestError} · 输入已保留，请核对后重试。</p>}
      <div className="composer-box">
        <textarea ref={composer} aria-label="向 Agent 发送研究消息" rows={2} value={draft}
          placeholder={chat?.configured ? '描述研究问题，或继续当前任务…' : '模型尚未配置，可先准备研究项目'}
          spellCheck={false} onChange={(event) => setDraft(event.target.value)}
          onCompositionStart={() => { composing.current = true }} onCompositionEnd={() => { composing.current = false }}
          onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && !composing.current && event.keyCode !== 229) { event.preventDefault(); void submit() } }} />
        <div className="composer-toolbar">
          <button type="button" className="composer-context" title="在工作区查看当前研究，不会添加或发送附件" onClick={() => onOpenResource({ kind: 'study' })}>{studyLabel ? `研究 · ${studyLabel}` : '查看研究'}</button>
          <button type="button" className="composer-model" title="查看真实模型配置与会话预算" onClick={(event) => {
            const settings = event.currentTarget.closest('.agent-pane')?.querySelector<HTMLDetailsElement>('.agent-settings')
            if (settings) { settings.open = !settings.open; if (settings.open) settings.querySelector<HTMLInputElement>('input')?.focus() }
          }}>{chat?.model ?? '未配置模型'}</button>
          <div className="composer-controls">{running ? <>
            {draft.trim() && <button type="button" className="steer-button" onClick={() => { void onSteerTurn(running.turn_id, draft).then((ok) => { if (ok) setDraft('') }) }}>转向</button>}
            <button type="button" className="send-button stop" title="停止生成" aria-label="停止生成" onClick={() => { void onCancelTurn() }}><WorkbenchIcon kind="stop" /></button>
          </> : <button type="button" className="send-button" title="发送消息" aria-label="发送消息" disabled={!ready} onClick={() => { void submit() }}><WorkbenchIcon kind="arrowUp" /></button>}</div>
        </div>
      </div>
      <span className="composer-note">Enter 发送 · Shift + Enter 换行</span>
    </footer>
  </section>
}

type ReviewProgress = NonNullable<PopperState['review']>

function GateCard({ review, projectName, onAdjudicate }: {
  review: ReviewProgress
  projectName: string
  onAdjudicate: (riskId: string, verdict: 'allow' | 'reject', reason: string) => Promise<void>
}) {
  const [reason, setReason] = useState('')
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inFlight = useRef(false)
  const fail = Object.entries(review.rejection_side.risks).filter(([, r]) => r.status === 'fail' && r.user_adjudication)
  const adjudications = review.adjudications ?? {}
  const [id, r] = fail[0] ?? [null, null]
  if (!r) return null
  const done = adjudications[id] ?? null

  const decide = async (verdict: 'allow' | 'reject') => {
    if (!reason.trim() || inFlight.current) return
    inFlight.current = true; setBusy(true); setError(null)
    try { await onAdjudicate(id, verdict, reason.trim()); setReason(''); setOpen(false) }
    catch (cause) { setError(`${(cause as Error).message} · 裁定理由已保留。`) }
    finally { inFlight.current = false; setBusy(false) }
  }

  return (
    <div className="gate-card">
      <div className="t">项目评审待裁决 · {projectName}</div>
      {error && <p className="chat-error" role="alert">{error}</p>}
      <p>检测到 {id} 风险：{r.reason}{r.user_adjudication ? '（机器产证据 · 需用户裁决）' : ''}</p>
      {done ? (
        <div className="ops2">
          <span className="muted" style={{ fontSize: 12 }}>
            已裁定：{done.verdict === 'allow' ? '放行' : '驳回'} · {done.reason}
          </span>
        </div>
      ) : (
        <div className="ops2" style={{ flexDirection: 'column', alignItems: 'stretch' }}>
          {open ? (
            <>
              <input className="ginput" autoFocus disabled={busy} value={reason} placeholder="留痕理由（必填，入事件流）"
                onChange={(e) => setReason(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) void decide('allow') }} />
              <div className="ops2">
                <button className="gbtn ok" disabled={busy || !reason.trim()} onClick={() => { void decide('allow') }}>放行（留痕）</button>
                <button className="gbtn warn" disabled={busy || !reason.trim()} onClick={() => { void decide('reject') }}>驳回</button>
              </div>
            </>
          ) : (
            <button className="gbtn" onClick={() => setOpen(true)}>处置这项风险…</button>
          )}
        </div>
      )}
      <div className="ops2" style={{ marginTop: 8 }}>
        <span className="muted" style={{ fontSize: 12 }}>裁定仅留痕，不改动机器评测数字。</span>
      </div>
    </div>
  )
}
