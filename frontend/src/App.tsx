import { useCallback, useEffect, useState } from 'react'
import { fetchState, fetchWorkspace, fetchCampaign, postAction, adjudicate, subscribeEvents } from './api'
import type { Campaign } from './api'
import type { ExperimentState, PopperState, Workspace } from './types'
import { GateView } from './components/GateView'
import { EnvView } from './components/EnvView'
import { ActivityView } from './components/ActivityView'
import { ReviewApprovalView } from './components/ReviewApprovalView'
import { LitView } from './components/LitView'
import { LoopView } from './components/LoopView'
import { CampaignView } from './components/CampaignView'
import { EditorView } from './components/EditorView'
import { Stars } from './components/Stars'
import { Winbar } from './components/Winbar'
import { Dock } from './components/Dock'
import { SettingsMenu } from './components/SettingsMenu'
import { QuestsSidebar } from './components/QuestsSidebar'
import { MaterializeDialog } from './components/MaterializeDialog'

/* 侧栏图标 */
const ICONS: Record<string, React.ReactNode> = {
  home: <svg className="i" viewBox="0 0 24 24"><path d="M3 10.5 12 3l9 7.5" /><path d="M5 9.5V21h14V9.5" /></svg>,
  campaign: <svg className="i" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" /><path d="M12 6v6l4 2" /></svg>,
  lit: <svg className="i" viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /></svg>,
  gate: <svg className="i" viewBox="0 0 24 24"><path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" /></svg>,
  approve: <svg className="i" viewBox="0 0 24 24"><path d="M12 9v4" /><path d="M12 17h.01" /><circle cx="12" cy="12" r="10" /></svg>,
  env: <svg className="i" viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="8" rx="2" /><rect x="2" y="13" width="20" height="8" rx="2" /><path d="M6 7h.01M6 17h.01" /></svg>,
  loop: <svg className="i" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.64-6.36" /><path d="M21 3v6h-6" /></svg>,
  conv: <svg className="i" viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>,
  activity: <svg className="i" viewBox="0 0 24 24"><path d="M3 3v18h18" /><path d="m7 14 4-4 3 3 5-6" /></svg>,
  runs: <svg className="i" viewBox="0 0 24 24"><path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z" /></svg>,
  editor: <svg className="i" viewBox="0 0 24 24"><path d="M16 3l5 5L8 21H3v-5L16 3z" /></svg>,
}

function isExperiment(state: PopperState['state']): state is ExperimentState {
  return (state as ExperimentState).spec != null
}

type ViewKey = 'home' | 'gate' | 'runs' | 'env' | 'activity' | 'approve' | 'lit' | 'loop' | 'conv' | 'campaign' | 'editor'

export default function App() {
  const [data, setData] = useState<PopperState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [view, setView] = useState<ViewKey>('home')
  const [pending, setPending] = useState(false)
  const [workspace, setWorkspace] = useState<Workspace | null>(null)
  const [campaign, setCampaign] = useState<Campaign | null>(null)
  const [activeQuest, setActiveQuest] = useState<string | null>(null)
  const [showMaterialize, setShowMaterialize] = useState(false)

  const loadState = useCallback(async () => {
    try { const s = await fetchState(); setData(s); setError(null) }
    catch (e) { setError((e as Error).message) }
  }, [])
  const loadWorkspace = useCallback(async () => {
    try { setWorkspace(await fetchWorkspace()) }
    catch (e) { setError((e as Error).message) }
  }, [])
  const loadCampaign = useCallback(async () => {
    try { setCampaign(await fetchCampaign()) }
    catch (e) { setError((e as Error).message) }
  }, [])

  // 首次拉全量基线；之后由 SSE 推送资源级补丁，不再定时轮询。
  useEffect(() => {
    let alive = true
    void Promise.all([loadState(), loadWorkspace(), loadCampaign()])
    const unsubscribe = subscribeEvents(
      (patch) => {
        if (!alive) return
        if (patch.state) setData(patch.state)
        if (patch.workspace) setWorkspace(patch.workspace)
        if (patch.campaign) setCampaign(patch.campaign)
      },
      () => { /* 断线由 EventSource 自动重连，重连后重发全量基线 */ },
    )
    return () => { alive = false; unsubscribe() }
  }, [loadState, loadWorkspace, loadCampaign])

  const run = async (action: string) => {
    setPending(true)
    try { await postAction(action) } catch (e) { setError((e as Error).message) } finally { setPending(false) }
  }
  /** 对话/启动器 composer 发送：推进到下一个真实动作 */
  const send = async () => {
    const a = nextAction(phase, pending)
    if (!a) return
    await run(a)
    setView('conv')
  }

  const openQuest = (title: string) => { setActiveQuest(title); setView('conv') }

  /** 用户对拒稿风险的裁定（放行/驳回），留痕入事件源 */
  const adjudicateRisk = async (riskId: string, verdict: 'allow' | 'reject', reason: string) => {
    try { await adjudicate(riskId, verdict, reason); await loadState() }
    catch (e) { setError((e as Error).message) }
  }

  const exp = data && isExperiment(data.state) ? data.state : null
  const phase = exp?.phase ?? ''
  // 阶段标签：后端 phases 单一下发；缺省显示空串而非回退到原始 key（不再有运行时回退）。
  const phaseLabel = data?.phases?.[phase] ?? ''
  const eventNames = data?.event_names ?? {}
  const stageStatusLabels = data?.stage_status_labels ?? {}
  const seedActive = ['searching', 'frozen', 'confirming'].includes(phase)
  const hasRisk = !!data?.review?.rejection_side?.risks
    && Object.values(data.review.rejection_side.risks).some((r) => r.status === 'fail')

  const navs: { key: ViewKey; label: string }[] = [
    { key: 'home', label: '启动台' },
    { key: 'campaign', label: '进度' },
    { key: 'lit', label: '文献库' },
    { key: 'gate', label: '评审门禁' },
    { key: 'approve', label: '审批中心' },
    { key: 'env', label: '实验环境' },
    { key: 'loop', label: '假设环' },
    { key: 'editor', label: '稿件编辑' },
  ]
  const tasks: { key: ViewKey; label: string }[] = [
    { key: 'conv', label: '事件对话' },
    { key: 'activity', label: '事件流' },
    { key: 'runs', label: '运行明细' },
  ]

  const dockActivate = (id: string) => {
    if (id === 'lit') setView('lit')
    else if (id === 'products') setShowMaterialize(true)
    else if (id === 'terminal') setView('activity')
  }

  const featureBack = (view !== 'home' && view !== 'conv') ? (
    <button className="back" onClick={() => setView('conv')}>← 返回对话</button>
  ) : null

  return (
    <>
      <Stars />
      <Winbar />
      <div className="app">
        <aside className="side">
          <QuestsSidebar workspace={workspace} seedActive={seedActive} reload={loadWorkspace}
            onOpenQuest={openQuest} onOpenMaterialize={() => setShowMaterialize(true)} />

          <div className="sec-h"><span className="t">工作台</span></div>
          {navs.map((n) => (
            <button key={n.key} className={`witem ${view === n.key ? 'active' : ''}`} onClick={() => setView(n.key)}>
              {ICONS[n.key]}<span>{n.label}</span>
              {n.key === 'approve' && hasRisk ? <span className="navbadge">1</span> : null}
            </button>
          ))}

          <div className="side-grow" />
          <div className="sec-h"><span className="t">研究任务</span></div>
          {tasks.map((n) => (
            <button key={n.key} className={`witem ${view === n.key ? 'active' : ''}`} onClick={() => setView(n.key)}>
              {ICONS[n.key]}<span>{n.label}</span>
            </button>
          ))}

          <div className="side-foot">
            <div className="me">
              <span className="avatar">W</span>
              <span className="who">
                <span className="n">wangj</span>
                <span className="gpu">0.0 GPU·h</span>
              </span>
              <SettingsMenu />
            </div>
          </div>
        </aside>

        <main className="main">
          {error && <div className="notice err" style={{ maxWidth: 720, margin: '12px auto' }}>{error}</div>}
          {view === 'home' && exp ? <HomeLauncher state={exp} phaseLabel={phaseLabel} onSend={send} onOpen={(v) => setView(v)}
            onMaterialize={() => setShowMaterialize(true)} /> : null}
          {view === 'home' && !exp ? <div className="page"><div className="notice">正在连接后端真实状态…</div></div> : null}

          {view === 'conv' && exp ? <ConvView exp={exp} events={data?.events ?? []} review={data?.review ?? null}
            deliverables={workspace?.deliverables ?? []} questTitle={activeQuest} phaseLabel={phaseLabel}
            campaign={campaign} pending={pending} eventNames={eventNames}
            onSend={send} onAdjudicate={adjudicateRisk} /> : null}
          {view === 'conv' && !exp ? <div className="page"><div className="notice">正在连接后端真实状态…</div></div> : null}

          {view !== 'home' && view !== 'conv' && (
            <div className="page">
              {featureBack}
              {view === 'campaign' && <CampaignView campaign={campaign} reload={loadCampaign} stageStatusLabels={stageStatusLabels} />}
              {view === 'runs' && <RunsView data={data} exp={exp} phase={phase} pending={pending}
                questTitle={activeQuest} onAction={run} onBack={() => setView('conv')} />}
              {view === 'gate' && <GateView review={data?.review ?? null}
                riskNames={data?.risk_names ?? {}}
                gateStatusLabels={data?.gate_status_labels ?? {}}
                mode7Names={data?.mode7_names ?? {}} />}
              {view === 'approve' && data && <ReviewApprovalView data={data} />}
              {view === 'env' && data && <EnvView data={data} onAction={run} />}
              {view === 'editor' && <EditorView deliverables={workspace?.deliverables ?? []}
                templateLabels={workspace?.template_labels ?? {}}
                disclosureLabels={workspace?.disclosure_labels ?? {}}
                onReload={loadWorkspace} />}
              {view === 'activity' && <ActivityView events={data?.events ?? []} eventNames={eventNames} />}
              {view === 'lit' && <LitView />}
              {view === 'loop' && <LoopView />}
            </div>
          )}
        </main>

        <Dock onActivate={dockActivate} />
      </div>

      {showMaterialize && (
        <MaterializeDialog deliverables={workspace?.deliverables ?? []}
          validTemplates={workspace?.valid_templates ?? []}
          validDisclosures={workspace?.valid_disclosures ?? []}
          disclosureLabels={workspace?.disclosure_labels ?? {}}
          onClose={() => setShowMaterialize(false)} onReload={loadWorkspace} />
      )}
    </>
  )
}

function nextAction(phase: string, pending: boolean): string | null {
  if (pending) return null
  if (phase === 'searching') return 'search'
  if (phase === 'frozen') return 'confirm'
  return null
}

/* ── 首页：启动器（composer + 场景 pill + 案例卡 + 建议，send 进入对话） ── */
const SCEN = ['科研学术', '实验闭环', '论文评审', '数据洞察', '工程开发', '创意创作']

function HomeLauncher({ state, phaseLabel, onSend, onOpen, onMaterialize }: {
  state: ExperimentState; phaseLabel: string; onSend: () => void
  onOpen: (v: ViewKey) => void; onMaterialize: () => void
}) {
  const [q, setQ] = useState('')
  const phase = state.phase
  const ready = !!nextAction(phase, false)

  return (
    <div className="hero">
      <div className="hero-top">
        <h1>Quest on, hands off</h1>
        <div className="runon">
          <span>运行于</span>
          <span className="seg2"><svg className="i" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /></svg>research-agent-v2</span>
          <span className="seg2"><svg className="i" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 3" /></svg>{phaseLabel}</span>
          <span className="seg2"><svg className="i" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5" /></svg>{state.spec.metric.name}</span>
        </div>
      </div>

      <div className="pills">
        {SCEN.map((s, i) => <button key={s} className={`pill ${i === 1 ? 'on' : ''}`}>{s}</button>)}
      </div>

      <div className="cards">
        <div className="card" onClick={() => onOpen('runs')}>
          <span className="tag live">闭环</span>
          <h3>跑通 {state.spec.name} 实验闭环</h3>
          <p>{state.spec.objective}</p>
          <div className="ghost">{state.claim ? `δ ${state.claim.delta.toPrecision(3)} · 阈值 ${state.spec.min_improvement}` : `${state.spec.seeds?.length ?? 1} 个种子 · 预算 ${state.spec.budget}`}</div>
        </div>
        <div className="card" onClick={() => onOpen('lit')}>
          <span className="tag">文献</span>
          <h3>写一篇文献综述</h3>
          <p>检索并精读重要论文、梳理脉络，产出带完整引用锚点的结构化综述。</p>
          <div className="ghost">元启发式 · LLM 超参优化</div>
        </div>
        <div className="card" onClick={() => onOpen('gate')}>
          <span className="tag">门禁</span>
          <h3>R1-R7 拒稿树自评</h3>
          <p>用拒稿理由分类树逐条消解风险，输出可辩护性报告。</p>
          <div className="ghost">4 pass / 1 warn · R2 incremental 已消解</div>
        </div>
        <div className="card" onClick={onMaterialize}>
          <span className="tag">写作</span>
          <h3>把结果写成论文</h3>
          <p>基于真实结果物化可交付稿件（md/tex/docx/pdf，含 AI 披露）。</p>
          <div className="ghost">每个数据皆可溯源</div>
        </div>
      </div>

      <div className="composer-big">
        <textarea rows={1} value={q} placeholder="提出你的研究需求，专家团帮你完成，@ 添加上下文，/ 使用命令"
          onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && ready) onSend() }} />
        <div className="crow">
          <span className="cpill"><svg className="i" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 9-9" /><path d="M3 3v6h6" /></svg>{state.spec.seeds?.length ?? 1} 个种子</span>
          <span className="cpill">基线 {JSON.stringify(state.spec.baseline)}</span>
          <span className="cpill">阈值 {state.spec.min_improvement}</span>
          <div className="crt">
            <button className="send" disabled={!ready} title="下达指令 → 推进实验" onClick={onSend}>↑</button>
          </div>
        </div>
      </div>

      <div className="sugs">
        <div className="sug" onClick={() => onOpen('conv')}><svg className="i" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 9-9" /><path d="M3 3v6h6" /></svg>进入事件对话，查看当前推进结果与下一步</div>
        <div className="sug" onClick={() => onOpen('loop')}><svg className="i" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.64-6.36" /><path d="M21 3v6h-6" /></svg>在假设搜索环里查看符号化假说树</div>
        <div className="sug" onClick={() => onOpen('approve')}><svg className="i" viewBox="0 0 24 24"><path d="M12 9v4" /><path d="M12 17h.01" /><circle cx="12" cy="12" r="10" /></svg>到审批中心处理结论声明与门禁裁定</div>
      </div>
    </div>
  )
}

/* ── 对话视图（Qoder 结构 × ClawsGO 真实数据） ── */
function ConvView({ exp, events, review, deliverables, questTitle, phaseLabel, campaign, pending, eventNames, onSend, onAdjudicate }: {
  exp: ExperimentState; events: PopperState['events']; review: ReviewProgress | null
  deliverables: Workspace['deliverables']; questTitle: string | null; phaseLabel: string
  campaign: Campaign | null
  pending: boolean; eventNames: Record<string, string>
  onSend: () => void; onAdjudicate: (riskId: string, verdict: 'allow' | 'reject', reason: string) => void
}) {
  const feed = [...events].slice(-6).reverse()
  const needAction = review ? Object.values(review.rejection_side.risks).some((r) => r.status === 'fail') : false
  const ready = !!nextAction(exp.phase, pending)

  const runEvents = events.filter((e) => e.kind.startsWith('run_'))

  return (
    <div className="conv-shell">
      <div className="conv-top">
        <span className="tt" id="convTitle">{questTitle ?? exp.spec.name}</span>
        <span className={`st-chip ${needAction ? 'wait' : 'run'}`}><i />{pending ? 'Running' : needAction ? 'Action Required' : phaseLabel}</span>
        <span className="ws2"><svg className="i" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /></svg>{questTitle ? 'Quest' : 'research'}</span>
      </div>

      <div className="conv">
        <div className="msg-user">{exp.spec.objective}</div>
        <div className="msg-time">会话 {questTitle ?? exp.spec.name}</div>

        <div className="toolrow"><svg className="i" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 9-9" /><path d="M3 3v6h6" /></svg>已执行 {runEvents.length} 次运行<span className="chev">▸</span>
          <div className="body">
            {runEvents.length === 0 && <div className="tstep"><span className="mk">·</span>尚未运行，点击下方输入框下达指令推进会启动</div>}
            {runEvents.map((e) => (
              <div className="tstep" key={e.seq}><span className="mk">{e.kind === 'run_completed' ? '✓' : '⟳'}</span>{eventNames[e.kind] ?? ''}<span className="d">seq {e.seq}</span></div>
            ))}
          </div>
        </div>

        <div className="prog-card">
          <div className="ph2">统一进度 · {campaign?.stages.length ?? 9} 阶段<span className="spin" /></div>
          <div className="stages-mini">
            {campaign ? campaign.stages.map((s, i) => {
              const done = s.status === 'done', curStage = s.status === 'active'
              return <div key={s.key} className={`stp2 ${done ? 'done' : ''} ${curStage ? 'cur' : ''}`}>
                <span className="k">{i + 1}</span>
                <span className="mk">{done ? '✓' : curStage ? '⟳' : s.status === 'warn' ? '!' : '·'}</span>
                <span className="nm-stage">{s.label}</span>
              </div>
            }) : <div className="tstep"><span className="mk">·</span>阶段模型由后端统一下发，等待 /api/campaign…</div>}
          </div>
          <div className="metrics">
            <div className="metric"><div className="v">{exp.claim ? exp.claim.delta.toPrecision(4) : '—'}</div><div className="l">结论 δ（阈值 {exp.spec.min_improvement}）</div></div>
            <div className="metric"><div className="v up">{runEvents.some((e) => e.kind === 'run_completed') ? '✓' : '—'}</div><div className="l">开发集证据</div></div>
            <div className="metric"><div className="v">{exp.claim ? (exp.claim.status === 'supports_threshold' ? '支持' : '不足') : '—'}</div><div className="l">结论状态</div></div>
          </div>
          <div className="feed">
            {feed.length === 0 && <div className="fev"><span className="ty">~</span><span className="ms">协议已登记 · 运行后出现实时事件</span></div>}
            {feed.map((e) => (
              <div className="fev" key={e.seq}>
                <span className="ty">{e.kind === 'run_completed' ? 'ok' : e.kind === 'run_failed' ? 'gate' : 'tool'}</span>
                <span className="ms">{eventNames[e.kind] ?? ''}</span>
              </div>
            ))}
          </div>
        </div>

        {needAction && review && <GateCard review={review} onAdjudicate={onAdjudicate} />}

        {deliverables.length > 0 && (
          <div className="deliv-card">
            <div className="dh">📄 {deliverables.length} 份可交付稿件
              <span className="stat"><b>可下载</b></span>
            </div>
            {deliverables.map((d) => (
              <a className="df" key={d.id} href={`/api/file?item=${encodeURIComponent(d.filename)}`} download>
                <span className="code-ic">&lt;&gt;</span><span className="fn">{d.filename}</span>
                <span className="stat">{d.template} · {(d.bytes / 1024).toFixed(1)} KB</span>
              </a>
            ))}
          </div>
        )}

        <div className="fbrow">
          <button className="cbtn">👍</button><button className="cbtn">👎</button>
          <span className="gen">内容由 AI 生成</span>
        </div>
      </div>

      <div className="composer-foot">
        <div className="inner composer-big">
          <input type="text" placeholder={ready ? `按 Enter 推进实验（${nextAction(exp.phase, false)}）` : '当前阶段无需手动推进，或结论已生成'}
            spellCheck={false} onKeyDown={(e) => { if (e.key === 'Enter' && ready) onSend() }} />
          <div className="crow">
            <span className="cpill"><svg className="i" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /></svg>专家团</span>
            <span className="cpill">Auto</span>
            <div className="crt">
              <button className="send" disabled={!ready} onClick={onSend}>↑</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

type ReviewProgress = NonNullable<PopperState['review']>

function GateCard({ review, onAdjudicate }: {
  review: ReviewProgress
  onAdjudicate: (riskId: string, verdict: 'allow' | 'reject', reason: string) => void
}) {
  const [reason, setReason] = useState('')
  const [open, setOpen] = useState(false)
  const fail = Object.entries(review.rejection_side.risks).filter(([, r]) => r.status === 'fail')
  const adjudications = review.adjudications ?? {}
  const [id, r] = fail[0] ?? [null, null]
  if (!r) return null
  const done = adjudications[id] ?? null

  const decide = (verdict: 'allow' | 'reject') => {
    if (!reason.trim()) return
    onAdjudicate(id, verdict, reason.trim())
    setReason('')
    setOpen(false)
  }

  return (
    <div className="gate-card">
      <div className="t">⚠ L1 门禁 · {r.status}（拒稿树）</div>
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
              <input className="ginput" autoFocus value={reason} placeholder="留痕理由（必填，入事件流）"
                onChange={(e) => setReason(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') decide('allow') }} />
              <div className="ops2">
                <button className="gbtn ok" disabled={!reason.trim()} onClick={() => decide('allow')}>放行（留痕）</button>
                <button className="gbtn warn" disabled={!reason.trim()} onClick={() => decide('reject')}>驳回</button>
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

/* ── 运行明细（真实 runs 表格，从对话进度卡下钻） ── */
function RunsView({ data, exp, phase, pending, questTitle, onAction, onBack }: {
  data: PopperState | null; exp: ExperimentState | null
  phase: string; pending: boolean; questTitle: string | null; onAction: (a: string) => void; onBack: () => void
}) {
  const results = data?.results ?? []
  const can = (a: string) => {
    if (pending || !data?.trusted_local) return true
    if (a === 'search') return phase === 'searching'
    if (a === 'freeze') return phase === 'searching'
    if (a === 'confirm') return phase === 'frozen'
    return false
  }
  return (
    <div className="fv-wrap">
      <button className="back" onClick={onBack}>← 返回对话</button>
      <div className="fv-head">
        <div>
          <p className="eyebrow">EXPERIMENT / 运行明细{questTitle ? ` · ${questTitle}` : ''}</p>
          <div className="tt">{exp?.spec.name ?? '实验推进'}</div>
        </div>
      </div>
      <div className="actions">
        <button className="btn" disabled={!can('search')} onClick={() => onAction('search')}>运行开发集实验</button>
        <button className="btn" disabled={!can('freeze')} onClick={() => onAction('freeze')}>冻结候选</button>
        <button className="btn primary" disabled={!can('confirm')} onClick={() => onAction('confirm')}>最终测试</button>
      </div>
      <div className="section">
        {results.length === 0 && <div className="notice">协议已登记。运行开发集实验后，这里显示真实运行记录。</div>}
        {results.map((r) => (
          <div className="row" key={r.run_id}>
            <div>
              <div className="metric">{r.split === 'dev' ? '开发集' : '最终测试'} · {JSON.stringify(r.config)}</div>
              <small className="muted">种子 {r.n_seeds} · 标准差 {r.std.toPrecision(4)}</small>
            </div>
            <span className="metric" style={{ fontSize: 18 }}>{r.mean.toPrecision(6)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}