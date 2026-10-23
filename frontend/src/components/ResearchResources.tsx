import { PredictionSummary } from './PredictionSummary'
import { scientificClaimLabel, scientificClaimEffect } from '../scientificClaim'
import { useEffect, useRef, useState } from 'react'
import { cancelStudyJob, fetchBoundDraft, fetchBoundDrafts, fetchDesignJob, fetchResearchEvidence, fetchRevisionJob, fetchStudyJobs, reviewBoundDraft, type BoundDraftRow, type CreatedBoundDraft, type ResearchEvidencePreview, type ResearchRunState, type ResearchState, type StudyLifecycleJob } from '../api'
import type { ChatState, PopperState, Workspace } from '../types'
import type { WorkbenchResource, WorkbenchTab } from '../workbench'
import { Markdown } from './Markdown'
import { useConfirmation } from './ConfirmProvider'
import { DevelopmentDiagnostics, developmentDiagnostics } from './DevelopmentDiagnostics'

type Open = (resource: WorkbenchResource) => void
const statuses: Record<string, string> = {
  queued: '等待执行', running: '正在执行', succeeded: '执行成功', failed: '执行失败', cancelled: '已取消', interrupted: '执行中断', completed: '已完成', confirmed: '已确认',
  supported_in_scope: '在声明范围内获得支持', not_supported_in_scope: '在声明范围内未获得支持', contradicted_in_scope: '在声明范围内被证据反驳',
  boundary_supported_in_scope: '范围边界获得支持', boundary_not_supported_in_scope: '范围边界未获得支持', supports_threshold: '达到预注册阈值', insufficient_evidence: '证据不足', inconclusive: '证据不足',
  draft: '待人工审校', reviewed: '已登记人工审校', submitted: '已登记投稿', published: '已登记发表',
  ready_for_confirmation: '待独立确认', research: '研究探索', researching: '研究探索', searching: '研究探索', concluded: '已形成结论', active: '研究进行中',
  confirming: '正在独立确认', confirmation_failed: '独立确认失败', external_confirmation_pending: '等待外部独立确认', external_preparation_incomplete: '外部确认准备未完成',
  budget_exhausted: '预算已耗尽', blocked_external: '等待外部条件', integrity_failed: '完整性核验失败', unavailable: '状态不可读取', frozen: '已冻结', recorded: '已登记',
  infrastructure_failed: '基础设施执行失败', implementation_failed: '实验实现失败', untested: '尚未实验', retired: '已退出研究',
}
const scopes: Record<string, string> = { registered_independent_confirmation: '预注册的独立确认范围', signed_external_scope_boundary: '外部签名确认的范围边界', confirmation: '独立确认范围', dev: '探索性开发评估' }
const limitations: Record<string, string> = { not_generalizable_beyond_registered_scope: '不能推广到预注册范围之外', not_medical_validity: '不构成医学有效性声明', not_publication_attestation: '不构成投稿或发表认证' }
function label(value: unknown): string { return typeof value === 'string' ? statuses[value] ?? value : '未提供' }
function object(value: unknown): Record<string, unknown> { return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {} }
function ids(value: unknown): string[] { return Array.isArray(value) ? value.filter((id): id is string => typeof id === 'string') : [] }
function studyName(run: ResearchRunState | undefined): string { return run?.study?.question || '当前研究' }
function errorText(value: unknown): string | null { if (typeof value === 'string') return value; const error = object(value); return typeof error.message === 'string' ? error.message : typeof error.reason === 'string' ? error.reason : null }
// A → B → A must not revive an asynchronous result from the first A.
function useContextToken(identity: string): string {
  const generation = useRef({ identity, epoch: 0 })
  if (generation.current.identity !== identity) generation.current = { identity, epoch: generation.current.epoch + 1 }
  return `${identity}:${generation.current.epoch}`
}
function TechnicalDetails({ value, title = '标识与技术详情' }: { value: unknown; title?: string }) {
  const [copyError, setCopyError] = useState(false)
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  return <details className="resource-technical"><summary>{title}</summary><button type="button" className="btn" onClick={() => { void navigator.clipboard?.writeText(text).then(() => setCopyError(false)).catch(() => setCopyError(true)); if (!navigator.clipboard) setCopyError(true) }}>复制详情</button>{copyError && <p className="muted">复制不可用，请选择下方文本复制。</p>}<pre>{text}</pre></details>
}
export function EntityPanel({ tab, research, onOpenResource }: { tab: WorkbenchTab; research: ResearchState | null; onOpenResource: Open }) {
  const runs = (research?.runs ?? []).filter((run) => run.ok && (!tab.studyId || run.manifest?.study_id === tab.studyId) &&
    (tab.kind === 'claim' ? run.claims?.some((claim) => claim.entity_id === tab.ref) : run.observations?.some((observation) => observation.entity_id === tab.ref)))
  if (runs.length !== 1) return <div className="panel-empty"><h2>{runs.length ? '请选择所属研究' : '研究对象不可读取'}</h2><p>{runs.length ? '此对象标识存在于多项研究中。' : '当前项目未找到此对象；请检查项目和 Study。'}</p>{runs.map((run) => <button type="button" key={run.run_dir} onClick={() => onOpenResource({ ...tab, studyId: run.manifest!.study_id })}>{studyName(run)}</button>)}</div>
  const run = runs[0]
  const claim = tab.kind === 'claim' ? run.claims?.find((item) => item.entity_id === tab.ref) : null
  const observation = tab.kind === 'observation' ? run.observations?.find((item) => item.entity_id === tab.ref) : null
  const value = claim ?? observation
  const scientificJudgments = claim ? (run.judgments ?? []).filter((j) =>
    j.scope === 'confirmation' && j.hypothesis_id === claim.hypothesis_id &&
    j.hypothesis_version === claim.hypothesis_version && j.observation_refs.length === claim.observation_refs.length &&
    j.observation_refs.every((id, index) => id === claim.observation_refs[index])) : []
  if (!value) return null
  const claimStatus = claim ? scientificClaimLabel(claim) : label(value.status)
  return <div className="resource-detail"><span className="eyebrow">{claim ? '研究结论 / CLAIM' : '实验观察 / OBSERVATION'}</span><h2>{claim ? `${claim.metric} 的研究结论` : '实验观察结果'}</h2><span className="resource-status">{claim ? claimStatus : label(value.status)}</span><p className="muted">所属研究：{studyName(run)}</p>
    <p className="resource-scope">适用范围：{scopes[value.scope] ?? value.scope}</p><dl className="resource-facts">
      {claim ? <><div><dt>指标方向</dt><dd>{claim.metric_direction === 'max' ? '越大越好' : '越小越好'}</dd></div><div><dt>方向统一效应</dt><dd>{claim.oriented_delta}</dd></div><div><dt>预注册阈值</dt><dd>{claim.min_meaningful_effect}</dd></div></>
        : observation && <><div><dt>观察值</dt><dd>{observation.value} {observation.unit}</dd></div><div><dt>不确定度</dt><dd>{observation.uncertainty ?? '未提供'}</dd></div><div><dt>信任级别</dt><dd>{observation.trust}</dd></div></>}
    </dl>
    {claim && <><h3>来源证据</h3><PredictionSummary value={value} judgments={scientificJudgments} /><div className="resource-links">{claim.observation_refs.map((id, index) => <button type="button" key={id} title={id} onClick={() => onOpenResource({ kind: 'observation', ref: id, studyId: claim.study_id, title: '实验观察' })}>实验观察 {index + 1} ↗</button>)}</div><h3>适用限制</h3><ul>{claim.limitations.map((text, index) => <li key={index}>{limitations[text] ?? text}</li>)}</ul></>}
    <button type="button" className="btn" onClick={() => onOpenResource({ kind: 'study', studyId: run.manifest?.study_id })}>打开所属 Study →</button>
    <TechnicalDetails value={value} title="完整研究记录与对象标识" />
  </div>
}
export function EvidencePanel({ run, workspaceId = '', studyName: displayName, onOpenResource }: { run: ResearchRunState | undefined; workspaceId?: string; studyName?: string; onOpenResource: Open }) {
  const context = useContextToken(`${workspaceId}:${run?.manifest?.study_id ?? ''}:${run?.state_version ?? ''}`)
  const currentContext = useRef(context); currentContext.current = context
  const [verified, setVerified] = useState<{ context: string; value: ResearchEvidencePreview } | null>(null)
  const [error, setError] = useState<{ context: string; message: string } | null>(null)
  const inFlight = useRef<string | null>(null)
  const [busyContext, setBusyContext] = useState<string | null>(null)
  const [verificationAttempted, setVerificationAttempted] = useState(false)
  const busy = busyContext === context
  useEffect(() => { setVerified(null); setError(null); setVerificationAttempted(false) }, [workspaceId, run?.manifest?.study_id])
  if (!run?.manifest) return <div className="panel-empty"><h2>尚无 Study 证据</h2><p>登记研究并执行实验后，可在这里追溯 Claim 与 Observation。</p></div>
  const evidence = verified?.context === context ? verified.value : null
  const stale = verificationAttempted && (!evidence || evidence.bundle.study_state_version !== run.state_version)
  const verify = async () => {
    if (inFlight.current === context) return
    const requestContext = context, studyId = run.manifest!.study_id, version = run.state_version
    inFlight.current = requestContext; setBusyContext(requestContext); setError(null); setVerified(null); setVerificationAttempted(true)
    try {
      const value = await fetchResearchEvidence(studyId)
      if (currentContext.current !== requestContext) return
      setVerificationAttempted(true)
      if (value.bundle.study_id !== studyId || value.bundle.study_state_version !== version || version == null) { setVerified(null); setError({ context: requestContext, message: '核验版本与当前研究不一致，需重新核验。' }); return }
      setVerified({ context: requestContext, value })
    } catch (reason) { if (currentContext.current === requestContext) setError({ context: requestContext, message: (reason as Error).message }) }
    finally { if (inFlight.current === requestContext) { inFlight.current = null; setBusyContext(null) } }
  }
  return <div className="evidence-panel"><header className="panel-heading"><div><span className="eyebrow">研究证据 / EVIDENCE</span><h2>{displayName || studyName(run)}</h2></div><button type="button" className="btn" disabled={busy || !run.claims?.length} title={!run.claims?.length ? '形成真实研究结论后才能核验绑定' : undefined} onClick={() => { void verify() }}>{busy ? '正在核验…' : '核验证据绑定'}</button></header>
    <p className="muted">{(run.claims ?? []).length} 项研究结论 · {(run.observations ?? []).length} 项实验观察</p><p className="muted">本机核验只证明研究记录与源制品一致，不代表外部复现或稿件通过评审。</p>
    {error?.context === context && <div className="notice err" role="alert">{error.message}</div>}{stale && !busy && <div className="notice warn">需重新核验：当前研究尚无有效核验结果，旧结果不再作为当前依据。</div>}
    <div className="entity-list">{run.claims?.map((claim) => <button type="button" key={claim.entity_id} title={claim.entity_id} onClick={() => onOpenResource({ kind: 'claim', ref: claim.entity_id, studyId: run.manifest?.study_id, title: `${claim.metric} 结论` })}><span className="entity-kind">结论</span><strong>{claim.metric} · {scientificClaimEffect(claim)}</strong><span>{scientificClaimLabel(claim)}</span><span>↗</span></button>)}</div>
    <div className="entity-list">{run.observations?.map((observation, index) => <button type="button" key={observation.entity_id} title={observation.entity_id} onClick={() => onOpenResource({ kind: 'observation', ref: observation.entity_id, studyId: run.manifest?.study_id, title: `实验观察 ${index + 1}` })}><span className="entity-kind">观察</span><strong>实验观察 {index + 1}</strong><span>{observation.value} {observation.unit}</span><span>↗</span></button>)}</div>
    {evidence && <div className="verified-evidence"><div className="notice">已核验 {evidence.bundle.bindings.length} 条结论与本机源制品的绑定 · 状态版本 {evidence.bundle.study_state_version}</div><Markdown text={evidence.markdown} /><TechnicalDetails value={evidence.json_text} title="完整证据 JSON" /></div>}
    <TechnicalDetails value={{ study_id: run.manifest.study_id, state_version: run.state_version }} />
  </div>
}
export function ManuscriptsBrowser({ workspace, research, onOpenResource, onMaterialize }: { workspace: Workspace | null; research: ResearchState | null; onOpenResource: Open; onMaterialize: () => void }) {
  const [drafts, setDrafts] = useState<Array<BoundDraftRow & { studyId: string }>>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(true)
  const [retry, setRetry] = useState(0)
  const signature = (research?.runs ?? []).map((run) => `${run.manifest?.study_id}:${run.state_version}`).join('|')
  useEffect(() => {
    let current = true; setDrafts([]); setError(null); setBusy(true)
    const ids = [...new Set((research?.runs ?? []).flatMap((run) => run.manifest ? [run.manifest.study_id] : []))]
    void Promise.allSettled(ids.map(async (studyId) => (await fetchBoundDrafts(studyId)).map((draft) => ({ ...draft, studyId })))).then((results) => {
      if (!current) return
      setBusy(false)
      setDrafts(results.flatMap((result) => result.status === 'fulfilled' ? result.value : []))
      setError(results.some((result) => result.status === 'rejected') ? '部分研究稿件不可读取，可在所属 Study 中重试。' : null)
    })
    return () => { current = false }
  }, [signature, workspace?.current_root, retry])
  return <div className="manuscripts-browser"><header className="panel-heading"><div><span className="eyebrow">研究稿件 / MANUSCRIPTS</span><h2>研究稿件</h2></div><button type="button" className="btn" onClick={onMaterialize}>导出独立产物</button></header>
    {error && <div className="notice err">{error}<button type="button" onClick={() => setRetry((value) => value + 1)}>重新加载</button></div>}
    <h3>证据绑定快照</h3><p className="muted">只读、绑定真实 Claim；阅读、核验证据及登记人工审校可在稿件标签中完成。</p>{busy ? <div className="panel-loading">正在读取研究稿件…</div> : !drafts.length && <p className="muted">完成独立确认后，可在 Study 中生成证据绑定稿件。</p>}
    <div className="entity-list">{drafts.map((draft, index) => <button type="button" key={`${draft.studyId}:${draft.draft_id}`} disabled={!draft.ok} title={draft.reason ?? draft.draft_id} onClick={() => onOpenResource({ kind: 'manuscript', ref: draft.draft_id, studyId: draft.studyId, title: '证据绑定稿件' })}><span className="entity-kind">快照</span><strong>证据绑定稿件 {index + 1}</strong><span>{draft.ok ? `${draft.claim_count ?? 0} 条结论 · 可审校` : '核验失败'}</span><span>↗</span></button>)}</div>
    <h3>独立导出产物</h3><p className="muted">可编辑文件与证据绑定快照独立保存；编辑导出内容不会修改或重新绑定原快照。写作门禁未通过时不会出现在此列表，也不表示论文已完成。</p>{!workspace?.deliverables.length && <p className="muted">尚无导出产物。</p>}
    <div className="entity-list">{workspace?.deliverables.map((item) => <div className="deliverable-resource" key={item.id}><button type="button" disabled={!/\.(md|tex|json)$/i.test(item.filename)} onClick={() => onOpenResource({ kind: 'manuscript', ref: item.id, title: item.filename })}><span className="entity-kind">{item.template.toUpperCase()}</span><strong>{item.filename}</strong><span>{(item.bytes / 1024).toFixed(1)} KB</span></button><a href={`/api/file?item=${encodeURIComponent(item.filename)}`} download>下载</a></div>)}</div>
  </div>
}
export function BoundManuscriptPanel({ studyId, draftId, workspaceId = '', studyName: displayName, stateVersion, trustedLocal = false, onOpenResource, onResearchChanged }: { studyId: string; draftId: string; workspaceId?: string; studyName?: string; stateVersion?: number | null; trustedLocal?: boolean; onOpenResource: Open; onResearchChanged?: () => void }) {
  const context = useContextToken(`${workspaceId}:${studyId}:${draftId}:${stateVersion ?? ''}`)
  const currentContext = useRef(context); currentContext.current = context
  const confirm = useConfirmation()
  const [loaded, setLoaded] = useState<{ context: string; draft: CreatedBoundDraft; evidence: ResearchEvidencePreview } | null>(null)
  const [error, setError] = useState<{ context: string; message: string } | null>(null)
  const [raw, setRaw] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [retry, setRetry] = useState(0)
  const requestInFlight = useRef<string | null>(null)
  const draft = loaded?.context === context ? loaded.draft : null
  const evidence = loaded?.context === context ? loaded.evidence : null
  const versionMatches = !!evidence && stateVersion != null && evidence.bundle.study_id === studyId && evidence.bundle.study_state_version === stateVersion
  useEffect(() => { setReason(''); setRaw(false) }, [workspaceId, studyId, draftId])
  useEffect(() => {
    let current = true; setError(null); setBusy(false)
    void Promise.all([fetchBoundDraft(studyId, draftId), fetchResearchEvidence(studyId)]).then(([value, binding]) => {
      if (!current || currentContext.current !== context) return
      if ((value.study_id && value.study_id !== studyId) || binding.bundle.study_id !== studyId) { setError({ context, message: '返回稿件或证据不属于当前研究，请重新加载。' }); return }
      setLoaded({ context, draft: value, evidence: binding })
    }).catch((failure: Error) => { if (current && currentContext.current === context) setError({ context, message: failure.message }) })
    return () => { current = false }
  }, [context, retry, studyId, draftId])
  const markReviewed = async () => {
    if (!draft || !versionMatches || !trustedLocal || !reason.trim() || requestInFlight.current === context) return
    const requestContext = context, reviewReason = reason.trim()
    requestInFlight.current = requestContext; setBusy(true); setError(null)
    try {
      const approved = await confirm({ title: '登记人工审校', description: '确认你已阅读稿件、核对所有结论与证据，并检查未验证的作者说明。此操作只登记本地审校状态，不代表投稿、发表或外部验证。', details: [{ label: '所属研究', value: displayName || '当前研究' }, { label: '核验版本', value: String(stateVersion) }, { label: '审校理由', value: reviewReason }], confirmLabel: '确认登记审校' })
      if (!approved || currentContext.current !== requestContext) return
      await reviewBoundDraft(studyId, draftId, reviewReason)
      if (currentContext.current !== requestContext) return
      const value = await fetchBoundDraft(studyId, draftId)
      if (currentContext.current !== requestContext) return
      if (value.study_id && value.study_id !== studyId) throw new Error('返回稿件归属不匹配，请重新核验。')
      setLoaded({ context: requestContext, draft: value, evidence: evidence! }); onResearchChanged?.()
    } catch (failure) { if (currentContext.current === requestContext) setError({ context: requestContext, message: `${(failure as Error).message} · 审校理由已保留。` }) }
    finally { if (requestInFlight.current === requestContext) { requestInFlight.current = null; if (currentContext.current === requestContext) setBusy(false) } }
  }
  return <div className="bound-manuscript"><header className="panel-heading"><div><span className="eyebrow">证据绑定快照 / MANUSCRIPT</span><h2>证据绑定稿件</h2></div><button type="button" disabled={!draft} onClick={() => setRaw((value) => !value)}>{raw ? '阅读预览' : '查看源文'}</button></header>
    <p className="muted">所属研究：{displayName || '当前研究'} · 只读证据快照，编辑导出产物不会修改此稿件。</p>
    {error?.context === context && <div className="notice err" role="alert">{error.message}<button type="button" disabled={busy} onClick={() => { setLoaded(null); setRetry((value) => value + 1) }}>重新核验稿件</button></div>}{!draft && error?.context !== context && <div className="panel-loading">正在读取稿件与核验证据…</div>}
    {draft && <><div className="document-meta">{label(draft.flow_state ?? 'draft')} · {draft.claim_count} 条结论绑定</div>{!versionMatches && <div className="notice warn">需重新核验：证据返回版本与当前研究不一致，审校登记已禁用。<button type="button" disabled={busy} onClick={() => { setLoaded(null); setRetry((value) => value + 1) }}>重新核验</button></div>}{raw ? <pre>{draft.manuscript}</pre> : <Markdown text={draft.manuscript} />}<div className="resource-links"><button type="button" onClick={() => onOpenResource({ kind: 'study', studyId })}>所属研究 ↗</button><button type="button" onClick={() => onOpenResource({ kind: 'evidence', studyId })}>研究证据 ↗</button></div>
      <details className="section box"><summary>已核验的证据绑定</summary>{versionMatches ? <><p className="muted">当前状态版本 {evidence!.bundle.study_state_version} · {evidence!.bundle.bindings.length} 条绑定。本机一致性核验不等于外部验证。</p><Markdown text={evidence!.markdown} />{evidence!.bundle.bindings.map((binding, index) => <button type="button" className="btn" key={binding.claim_id} onClick={() => onOpenResource({ kind: 'claim', studyId, ref: binding.claim_id, title: `研究结论 ${index + 1}` })}>研究结论 {index + 1} ↗</button>)}</> : <p className="muted">需重新核验。</p>}</details>
      {(draft.flow_state ?? 'draft') === 'draft' ? <section className="section box"><h3>人工审校登记</h3><p className="muted">核对结论、证据和作者说明后，填写可追溯的审校理由。</p><label>审校理由（必填）<textarea className="ginput" value={reason} disabled={busy} onChange={(event) => setReason(event.target.value)} placeholder="说明已核对的内容与仍存在的限制" rows={3} /></label><button type="button" className="btn primary" disabled={busy || !trustedLocal || !versionMatches || !reason.trim()} onClick={() => { void markReviewed() }}>{busy ? '正在登记…' : '登记人工审校'}</button>{!trustedLocal ? <p className="muted">需以可信本机模式启动后，才能登记人工审校。</p> : !versionMatches ? <p className="muted">请先重新核验当前研究版本。</p> : !reason.trim() && <p className="muted">填写审校理由后才能提交。</p>}</section> : <div className="notice">此稿件已登记人工审校；该状态不代表外部复现、投稿或发表认证。</div>}
      <TechnicalDetails value={{ study_id: studyId, state_version: stateVersion, draft_id: draftId, draft_dir: draft.draft_dir, evidence_dir: draft.evidence_dir, manuscript_path: draft.manuscript_path, publication_attested: draft.publication_attested }} />
    </>}
  </div>
}
export function JobPanel({ tab, chat, research, workspaceId = '', trustedLocal = false, onOpenResource }: { tab: WorkbenchTab; chat: ChatState | null; research: ResearchState | null; workspaceId?: string; trustedLocal?: boolean; onOpenResource: Open }) {
  const version = tab.studyId ? research?.runs.find((entry) => entry.manifest?.study_id === tab.studyId)?.state_version : (research?.runs ?? []).map((entry) => `${entry.manifest?.study_id}:${entry.state_version}`).join('|')
  const context = useContextToken(`${workspaceId}:${tab.studyId ?? ''}:${tab.ref ?? ''}:${version ?? ''}`)
  const currentContext = useRef(context); currentContext.current = context
  const confirm = useConfirmation()
  const [loaded, setLoaded] = useState<{ context: string; job: Record<string, unknown> } | null>(null)
  const [error, setError] = useState<{ context: string; message: string } | null>(null)
  const [cancelBusy, setCancelBusy] = useState(false)
  const cancelInFlight = useRef<string | null>(null)
  const [refresh, setRefresh] = useState(0)
  const fields = loaded?.context === context ? loaded.job : null
  useEffect(() => {
    let current = true; let timer: number | undefined; setError(null); setCancelBusy(false)
    const read = async () => {
      const studyIds = tab.studyId ? [tab.studyId] : (research?.runs ?? []).flatMap((run) => run.manifest ? [run.manifest.study_id] : [])
      let value: unknown = chat?.jobs.find((item) => item.job_id === tab.ref)
      if (!value) {
        const lifecycle = await Promise.allSettled(studyIds.map(fetchStudyJobs))
        value = lifecycle.flatMap((result) => result.status === 'fulfilled' ? result.value.jobs : []).find((item) => item.job_id === tab.ref)
      }
      if (!value && tab.ref) {
        const results = await Promise.allSettled([fetchDesignJob(tab.ref), fetchRevisionJob(tab.ref)])
        value = results.flatMap((result) => result.status === 'fulfilled' ? [result.value] : []).find((item) => item.job_id === tab.ref)
      }
      if (!current || currentContext.current !== context) return
      if (value) { const next = object(value); setLoaded({ context, job: next }); setError(null); if (['running', 'queued'].includes(String(next.status)) && !chat?.jobs.some((item) => item.job_id === tab.ref)) timer = window.setTimeout(() => { void read().catch((failure: Error) => { if (current) setError({ context, message: failure.message }) }) }, 1500) }
      else setError({ context, message: '当前项目未找到此作业。请从所属研究或会话查看最新记录。' })
    }
    void read().catch((reason: Error) => { if (current) setError({ context, message: reason.message }) })
    return () => { current = false; window.clearTimeout(timer) }
  }, [context, tab.ref, tab.studyId, chat?.jobs, research, refresh])
  const request = object(fields?.request), result = object(fields?.result)
  const diagnosticStudyId = developmentDiagnostics(fields?.development_diagnostics)?.study_id
  const jobStudyId = typeof request.study_id === 'string' ? request.study_id : typeof fields?.study_id === 'string' ? fields.study_id
    : tab.studyId ?? (typeof diagnosticStudyId === 'string' ? diagnosticStudyId : undefined)
  const run = research?.runs.find((entry) => entry.manifest?.study_id === jobStudyId)
  const observations = ids(result.observation_ids), claims = ids(result.claim_ids)
  if (typeof fields?.observation_id === 'string' && !observations.includes(fields.observation_id)) observations.push(fields.observation_id)
  const diagnostic = developmentDiagnostics(fields?.development_diagnostics)
  const diagnosticObservation = object(diagnostic?.candidate).observation_id
  if (diagnostic && diagnostic.job_id === tab.ref && diagnostic.study_id === jobStudyId
      && typeof diagnosticObservation === 'string' && !observations.includes(diagnosticObservation)) observations.push(diagnosticObservation)
  const action = request.action === 'confirm' ? '独立确认' : request.action === 'run' ? '研究执行' : fields?.revision_id ? '源码修订执行' : fields?.design_id ? '实验设计执行' : fields?.turn_id ? 'Agent 回复生成' : '执行作业'
  const active = ['queued', 'running'].includes(String(fields?.status)), cancelling = !!fields?.cancel_requested
  const failure = errorText(fields?.error)
  const cancel = async () => {
    if (!active || cancelling || !trustedLocal || typeof request.study_id !== 'string' || cancelInFlight.current === context || !tab.ref) return
    const requestContext = context
    cancelInFlight.current = context; setCancelBusy(true)
    try {
      const accepted = await confirm({ title: '请求取消研究作业', description: '取消请求会等待当前执行安全结束。已执行的步骤与已消费的资源不会回滚；独立确认消费后不能自动重试。', details: [{ label: '所属研究', value: studyName(run) }, { label: '动作', value: action }], confirmLabel: '请求取消', danger: true })
      if (!accepted || currentContext.current !== requestContext) return
      const value = await cancelStudyJob(request.study_id, tab.ref)
      if (currentContext.current !== requestContext) return
      setLoaded({ context, job: object(value) }); setRefresh((index) => index + 1)
    } catch (reason) { if (currentContext.current === requestContext) setError({ context, message: (reason as Error).message }) }
    finally { if (cancelInFlight.current === requestContext) { cancelInFlight.current = null; if (currentContext.current === requestContext) setCancelBusy(false) } }
  }
  return <div className="resource-detail"><span className="eyebrow">执行作业 / JOB</span><h2>{action}</h2>{error?.context === context && <div className="notice err" role="alert">{error.message}<button type="button" onClick={() => setRefresh((value) => value + 1)}>重新读取</button></div>}{!fields && error?.context !== context && <div className="panel-loading">正在读取作业…</div>}
    {fields && <><p className="muted">归属：{jobStudyId ? studyName(run) : '当前 Agent 会话'}</p><dl className="resource-facts"><div><dt>状态</dt><dd>{label(fields.status)}</dd></div>{!!(fields.phase || result.phase) && <div><dt>阶段 / 结束原因</dt><dd>{label(fields.phase || result.phase)}</dd></div>}{fields.progress != null && <div><dt>进度</dt><dd>{String(fields.progress)}</dd></div>}{fields.total_tokens != null && <div><dt>实际 tokens</dt><dd>{String(fields.total_tokens)}</dd></div>}</dl>
      {failure && <div className="notice err" role="alert">{failure}</div>}{cancelling && active && <div className="notice warn">已请求取消，正在等待作业安全结束。当前状态仍为{label(fields.status)}；已消费资源不会回滚。</div>}{fields.status === 'succeeded' && <div className="notice">执行已完成，结果以以下真实产物记录为准。</div>}{['failed', 'interrupted'].includes(String(fields.status)) && <p className="muted">执行未成功完成；请检查错误与已有产物，不会自动重试或重新消费确认资源。</p>}
      {fields.development_diagnostics != null && <DevelopmentDiagnostics value={fields.development_diagnostics} studyId={jobStudyId} jobId={tab.ref}
        currentStateVersion={run?.state_version} onOpenResource={onOpenResource} />}
      {fields.development_diagnostics_error != null && <div className="notice err" role="status">开发诊断不可读取：{errorText(fields.development_diagnostics_error) ?? '诊断核验失败'}。作业状态与已有产物保留，未补造诊断或成绩。</div>}
      <div className="resource-links">{observations.map((id, index) => <button type="button" key={id} onClick={() => onOpenResource({ kind: 'observation', ref: id, studyId: jobStudyId, title: `实验观察 ${index + 1}` })}>实验观察 {index + 1} ↗</button>)}{claims.map((id, index) => <button type="button" key={id} onClick={() => onOpenResource({ kind: 'claim', ref: id, studyId: jobStudyId, title: `研究结论 ${index + 1}` })}>研究结论 {index + 1} ↗</button>)}</div>{!observations.length && !claims.length && !active && <p className="muted">此作业没有登记可直接打开的研究结论或观察；完整记录见技术详情。</p>}
      {active && typeof request.study_id === 'string' && <><button type="button" className="btn" disabled={cancelBusy || cancelling || !trustedLocal} onClick={() => { void cancel() }}>{cancelBusy ? '正在提交取消…' : cancelling ? '等待取消结果' : '请求取消'}</button>{!trustedLocal && <p className="muted">可信本机模式才能取消研究作业。</p>}</>}<TechnicalDetails value={fields} title="真实作业记录与对象标识" />
    </>}{jobStudyId && <button type="button" className="btn" onClick={() => onOpenResource({ kind: 'study', studyId: jobStudyId })}>查看所属研究 →</button>}
  </div>
}
export function RunLogPanel({ state, research, onOpenResource }: { state: PopperState | null; research: ResearchState | null; onOpenResource?: Open }) {
  const [jobs, setJobs] = useState<Array<StudyLifecycleJob & { studyId: string }>>([])
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let current = true; let timer: number | undefined
    const read = async () => {
      const ids = (research?.runs ?? []).flatMap((run) => run.manifest ? [run.manifest.study_id] : [])
      const results = await Promise.allSettled(ids.map(async (studyId) => (await fetchStudyJobs(studyId)).jobs.map((job) => ({ ...job, studyId }))))
      if (!current) return
      const next = results.flatMap((result) => result.status === 'fulfilled' ? result.value : [])
      setJobs(next); setError(results.some((result) => result.status === 'rejected') ? '部分作业日志读取失败。' : null)
      if (next.some((job) => ['running', 'queued'].includes(job.status))) timer = window.setTimeout(() => { void read() }, 2000)
    }
    void read(); return () => { current = false; window.clearTimeout(timer) }
  }, [research])
  const events = (state?.events ?? []).filter((event) => event.kind.startsWith('run_'))
  return <div className="run-log-panel">{error && <div className="notice err">{error}</div>}{!jobs.length && !events.length && <div className="log-empty">尚无执行日志。运行研究后，作业状态与执行结果会显示在这里。</div>}
    {jobs.map((job, index) => <div className={`log-line ${job.status}`} key={job.job_id}><span className="log-label">{label(job.status)}</span><button type="button" title={job.job_id} onClick={() => onOpenResource?.({ kind: 'job', ref: job.job_id, studyId: job.studyId, title: '研究作业' })}>{job.request.action === 'confirm' ? '独立确认' : '研究执行'} {index + 1} ↗</button>{job.cancel_requested && ['queued', 'running'].includes(job.status) && <span>已请求取消，等待终态</span>}{job.error && <span className="log-error">{job.error}</span>}{job.result && <span>{label(job.result.phase)} · 观察 {job.result.observation_ids.length} · 结论 {job.result.claim_ids.length}</span>}</div>)}
    {events.map((event) => <div className="log-line" key={event.seq}><span className="log-label">#{event.seq}</span><strong>{state?.event_names[event.kind] ?? event.kind}</strong><details><summary>执行详情</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details></div>)}
  </div>
}
