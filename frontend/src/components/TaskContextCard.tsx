import { useEffect, useRef, useState } from 'react'
import { commitTaskContext, fetchTaskContext, previewTaskContext } from '../api'
import { HANDOFF_LABELS, type ContextPreview, type HandoffFields, type TaskContext } from '../taskContext'
import type { WorkbenchResource } from '../workbench'

export function TaskContextCard({ workspaceId, sessionId, projectName, busy, versionSignal, expanded = false, onClose, onOpenSession, onOpenResource }: {
  workspaceId: string; sessionId: string; projectName: string; busy: boolean; versionSignal: string; expanded?: boolean
  onClose: () => void; onOpenSession?: (sessionId: string) => void; onOpenResource: (resource: WorkbenchResource) => void
}) {
  const [data, setData] = useState<TaskContext | null>(null)
  const [candidate, setCandidate] = useState<ContextPreview | null>(null)
  const [fields, setFields] = useState<HandoffFields | null>(null)
  const [title, setTitle] = useState('')
  const [status, setStatus] = useState<'active' | 'completed'>('active')
  const [choosing, setChoosing] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [outdated, setOutdated] = useState(false)
  const epoch = useRef(0), locked = useRef(false)
  const receipt = useRef<{ fingerprint: string; key: string } | null>(null)
  const refresh = async () => {
    const generation = ++epoch.current
    setLoading(true)
    try {
      const value = await fetchTaskContext(workspaceId, sessionId)
      if (generation !== epoch.current || value.workspace_id !== workspaceId) return
      setData(value); setError(null)
    } catch (reason) { if (generation === epoch.current) setError((reason as Error).message) }
    finally { if (generation === epoch.current) setLoading(false) }
  }
  useEffect(() => { void refresh(); return () => { epoch.current++ } }, [workspaceId, sessionId, versionSignal])
  const previousSignal = useRef(versionSignal)
  useEffect(() => { if (previousSignal.current !== versionSignal && candidate) setOutdated(true); previousSignal.current = versionSignal }, [versionSignal, candidate])
  const loadPreview = async (source: { task_id?: string; source_session_id?: string; study_ids?: string[] }, preserve = false) => {
    if (locked.current || busy) return
    locked.current = true; setLoading(true)
    const generation = ++epoch.current
    try {
      const value = await previewTaskContext(workspaceId, sessionId, source)
      if (generation !== epoch.current) return
      setCandidate(value); setOutdated(false); setError(null); setChoosing(false)
      if (!preserve) {
        // This is a visible candidate excerpt, never silently adopted memory.
        const next = { ...value.fields }
        if (!next.goal && value.sources.length) next.goal = value.sources[value.sources.length - 1].user_excerpt
        setFields(next); setTitle(value.title); setStatus('active')
      }
    } catch (reason) { if (generation === epoch.current) setError((reason as Error).message) }
    finally { locked.current = false; if (generation === epoch.current) setLoading(false) }
  }
  const mutate = async (detach = false) => {
    if (!data || locked.current || busy || (!detach && (!candidate || !fields || outdated))) return
    const payload = detach ? { op: 'detach' as const, expected_binding_version: data.binding.version }
      : { op: 'adopt' as const, expected_binding_version: candidate!.expected_binding_version, preview: candidate!, fields: fields!, title, status }
    const fingerprint = JSON.stringify(payload)
    if (receipt.current?.fingerprint !== fingerprint) receipt.current = { fingerprint, key: `handoff-${crypto.randomUUID()}` }
    locked.current = true; setSaving(true)
    const generation = epoch.current
    try {
      await commitTaskContext(workspaceId, sessionId, { ...payload, idempotency_key: receipt.current.key })
      if (generation !== epoch.current) return
      setCandidate(null); setFields(null); setError(null); receipt.current = null
      await refresh()
      if (detach) onClose()
    } catch (reason) {
      if (generation === epoch.current) {
        setError((reason as Error).message)
        if (/变化|版本|核对/.test((reason as Error).message)) setOutdated(true)
      }
    } finally { locked.current = false; setSaving(false) }
  }
  if (!expanded && data && !data.selected && data.binding.version > 0 && !candidate) return null
  const recommendation = data?.selected ?? data?.tasks.find((task) => task.status === 'active')
  const source = data?.sessions.find((item) => item.status === 'active') ?? data?.sessions[0]
  return <section className="task-context-card" aria-label="任务交接">
    <header><div><strong>{data?.selected ? '当前任务上下文' : '继续已有任务'}</strong><small>{projectName} · 确认接续不调用模型</small></div>{expanded && <button type="button" onClick={onClose}>收起</button>}</header>
    {error && <p role="alert" className="chat-error">{error} · 填写内容保留。<button type="button" disabled={saving} onClick={() => { void refresh() }}>重新读取</button></p>}
    {loading && <p role="status">正在读取交接来源…</p>}
    {data?.stale && <p role="status">项目进度或任务修订已更新，历史交接需重新核对；研究事实以当前记录为准。</p>}
    {!candidate && !loading && data && <>
      {recommendation ? <><h3>{recommendation.title}</h3><p className="handoff-goal">{recommendation.body.fields.goal}</p>
        {recommendation.body.fields.todo && <p>待办：{recommendation.body.fields.todo}</p>}
        {recommendation.body.fields.blockers && <p>阻塞：{recommendation.body.fields.blockers}</p>}
        {recommendation.body.fields.next_step && <p>下一步：{recommendation.body.fields.next_step}</p>}
        {data.facts.filter((fact) => recommendation.body.study_ids.includes(fact.study_id)).map((fact) => <p key={fact.study_id}>最新记录：{fact.name} · {fact.observations} 条观察 · {fact.claims.length} 条结论 <button type="button" onClick={() => onOpenResource({ kind: 'study', studyId: fact.study_id })}>查看研究 ↗</button></p>)}
        <button type="button" disabled={busy || saving} onClick={() => { void loadPreview({ task_id: recommendation.task_id }) }}>{data.selected ? '核对与修订交接' : '继续此任务'}</button></>
        : source ? <><p>可从「{source.title}」核对目标和约束。旧聊天不自动复制，旧审批不继承。</p><button type="button" disabled={busy || saving} onClick={() => { void loadPreview({ source_session_id: source.session_id }) }}>继续此任务</button></>
          : <p>尚无已确认任务。可以填写新的任务目标，或直接在下方开始讨论。</p>}
      <div className="handoff-actions"><button type="button" disabled={busy || saving} onClick={() => setChoosing(!choosing)}>选择其他任务</button>
        <button type="button" disabled={busy || saving} onClick={() => { void loadPreview({}) }}>填写新任务目标</button>
        <button type="button" disabled={busy || saving} onClick={() => { void mutate(true) }}>{data.selected ? '解除关联 / 开始新任务' : '开始新任务'}</button></div>
      {choosing && <div className="handoff-choices">{data.tasks.map((task) => <button type="button" key={task.task_id} disabled={busy || saving} onClick={() => { void loadPreview({ task_id: task.task_id }) }}>{task.title}{task.status === 'completed' ? ' · 已结束' : ''}</button>)}
        {data.sessions.map((item) => <button type="button" key={item.session_id} disabled={busy || saving} onClick={() => { void loadPreview({ source_session_id: item.session_id }) }}>历史会话 · {item.title}</button>)}
        {!data.tasks.length && !data.sessions.length && <p>当前项目没有其他任务或历史会话。</p>}</div>}
      {data.selected && <details><summary>已确认交接与来源</summary>{Object.entries(HANDOFF_LABELS).map(([key, label]) => <p key={key}>{label}：{data.selected!.body.fields[key as keyof HandoffFields] || '未填写'}</p>)}
        {data.selected.body.sources.map((item) => <p key={item.turn_id}><button type="button" disabled={busy || data.sessions.find((session) => session.session_id === item.session_id)?.status === 'archived'} onClick={() => onOpenSession?.(item.session_id)}>来源会话 ↗</button> {item.user_excerpt}<small>来源摘录 · {item.version}</small></p>)}</details>}
    </>}
    {candidate && fields && <form onSubmit={(event) => { event.preventDefault(); void mutate() }}>
      <p>核对后，以下内容及选定研究摘要将用于本会话后续模型请求。不包含原始文件或日志，不继承旧审批。</p>
      {outdated && <p role="status">来源可能已更新，请重新核对。<button type="button" disabled={loading || busy || saving} onClick={() => { void loadPreview({ task_id: candidate.task_id ?? undefined, source_session_id: candidate.source_session_id, study_ids: candidate.study_ids }, true) }}>重新核对来源（保留填写）</button></p>}
      <label>任务名称<input aria-label="交接任务名称" maxLength={200} value={title} disabled={saving} onChange={(event) => setTitle(event.target.value)} /></label>
      {Object.entries(HANDOFF_LABELS).map(([key, label]) => <label key={key}>{label}{key === 'goal' ? '（必填）' : ''}<textarea aria-label={label} maxLength={4000} value={fields[key as keyof HandoffFields]} disabled={saving} onChange={(event) => setFields({ ...fields, [key]: event.target.value })} /></label>)}
      <label>任务状态<select aria-label="交接任务状态" value={status} disabled={saving} onChange={(event) => setStatus(event.target.value as 'active' | 'completed')}><option value="active">进行中</option><option value="completed">已结束</option></select></label>
      {!!data?.facts.length && <fieldset><legend>关联研究（可选，不执行实验）</legend>{data.facts.map((fact) => <label key={fact.study_id}><input type="checkbox" checked={candidate.study_ids.includes(fact.study_id)} disabled={loading || saving || busy}
        onChange={(event) => { void loadPreview({ task_id: candidate.task_id ?? undefined, source_session_id: candidate.source_session_id, study_ids: event.target.checked ? [...candidate.study_ids, fact.study_id] : candidate.study_ids.filter((id) => id !== fact.study_id) }, true) }} />{fact.name}<button type="button" onClick={() => onOpenResource({ kind: 'study', studyId: fact.study_id })}>查看研究 ↗</button></label>)}</fieldset>}
      {candidate.facts.map((fact) => <p key={fact.study_id}>当前记录：{fact.name} · {fact.observations} 条观察 · {fact.claims.length} 条结论 · 版本 {fact.state_version}</p>)}
      <details><summary>来源摘录（不是语义总结） · {candidate.sources.length} 条</summary>{candidate.sources.map((item) => <article key={item.turn_id}><p>用户原话摘录：{item.user_excerpt}</p><p>Agent 建议摘录（不是用户决定）：{item.agent_suggestion || '无'}</p><small>{item.truncated ? '已截取部分内容 · ' : ''}{item.status === 'completed' ? '对话已结束，不代表工具已执行' : '未完成对话，不能记为已执行'} · {item.version}</small><details><summary>标识与来源版本</summary><code>{item.session_id} / {item.turn_id}</code></details></article>)}</details>
      <div className="handoff-actions"><button type="button" disabled={saving} onClick={() => { setCandidate(null); setFields(null) }}>取消交接</button><button type="submit" disabled={saving || loading || busy || outdated || !fields.goal.trim() || !title.trim()}>{saving ? '保存交接…' : '确认接续（不发送）'}</button></div>
    </form>}
  </section>
}
