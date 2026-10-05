import { useEffect, useState } from 'react'
import { cancelDesignJob, cancelRevisionJob, createBenchmark, createBoundDraft, createFollowup, createRevisionWorktree, executeInitialRevision, executeWorktreeRevision, executeDesign, fetchArbor, fetchBenchmarkJob, fetchBenchmarkJobs, fetchBoundDrafts, fetchDesignDetail, fetchDesignJob, fetchDesignJobs, fetchFollowupPreview, fetchRevision, fetchRevisionJob, fetchRevisionWorktrees, migrateRevisionDiff, proposeWorktreeRevision, removeRevisionWorktree, retryDesignJob, submitWorktreeRevision, type ArborTree, type BenchmarkDetail, type BenchmarkJob, type BoundDraftRow, type CreatedBoundDraft, type CreatedFollowup, type DesignDetail, type DesignExecutionJob, type FollowupPreview, type ResearchRunState, type ResearchState, type RevisionDetail, type RevisionExecutionJob, type RevisionWorktree } from '../api'
import { MetricBars, BudgetBar, LegendDot, type BarPoint } from './charts'
import { StudyLifecyclePanel } from './StudyLifecyclePanel'
import type { WorkbenchResource } from '../workbench'
import { ProjectPreparation } from './ProjectPreparation'
import { useConfirmation } from './ConfirmProvider'

const lifecycleLabels: Record<string, string> = {
  researching: '开发实验', ready_for_confirmation: '待独立确认', confirming: '独立确认中',
  confirmation_failed: '独立确认失败', concluded: '已形成结论', budget_exhausted: '预算已耗尽',
  blocked_external: '等待外部条件', integrity_failed: '完整性检查失败',
  external_confirmation_pending: '等待外部签名确认', external_preparation_incomplete: '外部确认准备中断', unavailable: '阶段待核对',
}
/** 单个研究 run 的只读投影卡（/api/research，真实 research.sqlite）。 */
type ResearchRunCardProps = { run: ResearchRunState; labels: ResearchState['status_labels']; trustedLocal: boolean; onResearchChanged: () => void; onOpenResource?: (resource: WorkbenchResource) => void; projectName?: string; projectDescription?: string }
function ResearchRunCard(props: ResearchRunCardProps) {
  if (!props.run.ok) return <div className="section box"><h2>Study 暂时不可读取</h2><p className="muted">{props.run.run_dir}</p><div className="notice err">{props.run.reason}</div></div>
  return <ResearchRunBody {...props} />
}
function ResearchRunBody({ run, labels, trustedLocal, onResearchChanged, onOpenResource, projectName, projectDescription }: ResearchRunCardProps) {
  const [revision, setRevision] = useState<RevisionDetail | null>(null)
  const [revisionError, setRevisionError] = useState<string | null>(null)
  const [revisionLoading, setRevisionLoading] = useState<string | null>(null)
  const [worktrees, setWorktrees] = useState<RevisionWorktree[]>([])
  const [revisionJobs, setRevisionJobs] = useState<RevisionExecutionJob[]>([])
  const [worktreeBusy, setWorktreeBusy] = useState<string | null>(null)
  const [designDetail, setDesignDetail] = useState<DesignDetail | null>(null)
  const [designJobs, setDesignJobs] = useState<DesignExecutionJob[]>([])
  const [designBusy, setDesignBusy] = useState<string | null>(null)
  const [designError, setDesignError] = useState<string | null>(null)
  const [followupPreview, setFollowupPreview] = useState<FollowupPreview | null>(null)
  const [followupProjectName, setFollowupProjectName] = useState('')
  const [followupRunName, setFollowupRunName] = useState('')
  const [followupTestPath, setFollowupTestPath] = useState('')
  const [followupCandidates, setFollowupCandidates] = useState('[]')
  const [followupBudget, setFollowupBudget] = useState('1')
  const [followupObjective, setFollowupObjective] = useState('')
  const [followupBusy, setFollowupBusy] = useState(false)
  const [followupError, setFollowupError] = useState<string | null>(null)
  const [followupCreated, setFollowupCreated] = useState<CreatedFollowup | null>(null)
  const [draftTitle, setDraftTitle] = useState('研究结果草稿')
  const [draftNote, setDraftNote] = useState('')
  const [draftBusy, setDraftBusy] = useState(false)
  const [draftError, setDraftError] = useState<string | null>(null)
  const [createdDraft, setCreatedDraft] = useState<CreatedBoundDraft | null>(null)
  const [draftRows, setDraftRows] = useState<BoundDraftRow[]>([])
  const [copyNotice, setCopyNotice] = useState<string | null>(null)
  const [claimCopyNotice, setClaimCopyNotice] = useState<Record<string, string>>({})
  const confirmation = useConfirmation()
  const study = run.study
  const projectLabel = projectName ?? run.manifest?.project?.split(/[\\/]/).pop() ?? '当前项目'
  const runLabel = run.run_dir.split(/[\\/]/).pop() ?? 'Study'
  const lifecycle = run.lifecycle_phase ?? (study?.status === 'concluded' ? 'concluded' : 'unavailable')
  const phaseLabel = lifecycleLabels[lifecycle] ?? '阶段待核对'
  const budget = run.budget
  const hypotheses = run.hypotheses ?? []
  const revisions = run.revisions ?? []
  const observations = run.observations ?? []
  const decisions = run.decisions ?? []
  const claims = run.claims ?? []
  const submitBoundDraft = async () => {
    const studyId = run.manifest?.study_id
    if (!studyId || !claims.length) return
    setDraftBusy(true); setDraftError(null); setCreatedDraft(null)
    try {
      const blocks: Array<{ kind: 'claim'; claim_id: string } | { kind: 'author_note'; text: string }> =
        claims.map((claim) => ({ kind: 'claim', claim_id: claim.entity_id }))
      if (draftNote.trim()) blocks.push({ kind: 'author_note', text: draftNote.trim() })
      const created = await createBoundDraft(studyId, {
        schema_version: '1.0', title: draftTitle.trim(),
        sections: [{ heading: '研究结果', blocks }],
      })
      setCreatedDraft(created)
      onOpenResource?.({ kind: 'manuscript', ref: created.draft_id, studyId, title: draftTitle.trim() || '证据绑定稿件' })
      setDraftRows(await fetchBoundDrafts(studyId))
    } catch (error) { setDraftError((error as Error).message) }
    finally { setDraftBusy(false) }
  }
  useEffect(() => {
    const studyId = run.manifest?.study_id
    if (!studyId) return
    let alive = true
    fetchBoundDrafts(studyId).then((rows) => { if (alive) setDraftRows(rows) })
      .catch((error) => { if (alive) setDraftError((error as Error).message) })
    return () => { alive = false }
  }, [run.manifest?.study_id])
  const reflections = run.reflections ?? []
  const graph = run.experiment_graph
  const codeRevisions = graph?.nodes.filter((node) => node.kind === 'code_revision') ?? []
  const gitAnchored = codeRevisions.filter((node) => node.source_control?.kind === 'git')
  const dirtyAnchors = gitAnchored.filter((node) => node.source_control?.source_files_clean === false)
  const externalSources = graph?.nodes.filter((node) => node.kind === 'external_source') ?? []
  const openRevision = async (revisionId: string) => {
    setRevisionLoading(revisionId)
    setRevisionError(null)
    try { setRevision(await fetchRevision(revisionId)) }
    catch (error) { setRevisionError((error as Error).message) }
    finally { setRevisionLoading(null) }
  }
  useEffect(() => {
    let alive = true
    fetchDesignJobs().then((jobs) => { if (alive) setDesignJobs(jobs) })
      .catch((error) => { if (alive) setDesignError((error as Error).message) })
    return () => { alive = false }
  }, [])
  useEffect(() => {
    const active = designJobs.filter((job) => job.status === 'queued' || job.status === 'running')
    if (!active.length) return
    const timer = window.setInterval(() => {
      void Promise.all(active.map((job) => fetchDesignJob(job.job_id)))
        .then((latest) => setDesignJobs((rows) => rows.map((row) =>
          latest.find((job) => job.job_id === row.job_id) ?? row)))
        .catch((error) => setDesignError((error as Error).message))
    }, 1000)
    return () => window.clearInterval(timer)
  }, [designJobs])
  const reviewDesign = async (designId: string) => {
    setDesignBusy(designId); setDesignError(null)
    try { setDesignDetail(await fetchDesignDetail(run.manifest?.study_id ?? '', designId)) }
    catch (error) { setDesignError((error as Error).message) }
    finally { setDesignBusy(null) }
  }
  const submitDesign = async (detail: DesignDetail) => {
    if (!await confirmation({ title: '执行冻结设计', description: '将在沙箱中运行冻结设计，并消耗开发预算；失败也可能消耗资源。', details: [{ label: '设计', value: detail.design_id }, { label: 'Study', value: run.manifest?.study_id ?? run.run_dir }], confirmLabel: '批准并执行' })) return
    setDesignBusy(detail.design_id); setDesignError(null)
    try {
      const job = await executeDesign(detail)
      setDesignJobs((rows) => [...rows.filter((row) => row.job_id !== job.job_id),
        { ...job, research_run: run.run_dir }])
      setDesignDetail(null)
    } catch (error) { setDesignError((error as Error).message) }
    finally { setDesignBusy(null) }
  }
  const stopDesign = async (job: DesignExecutionJob) => {
    if (!await confirmation({ title: '停止设计执行', description: '已启动的实验会停止；已消耗的预算不会自动退回。', details: [{ label: '作业', value: job.job_id }], confirmLabel: '停止执行', danger: true })) return
    setDesignBusy(job.design_id); setDesignError(null)
    try {
      const next = await cancelDesignJob(job.job_id)
      setDesignJobs((rows) => rows.map((row) => row.job_id === next.job_id ?
        { ...next, research_run: run.run_dir } : row))
    } catch (error) { setDesignError((error as Error).message) }
    finally { setDesignBusy(null) }
  }
  const retryDesign = async (job: DesignExecutionJob) => {
    if (!await confirmation({ title: '重新执行设计', description: '这将再次使用开发预算，请核对当前研究状态仍适用。', details: [{ label: '设计', value: job.design_id }], confirmLabel: '重新执行' })) return
    setDesignBusy(job.design_id); setDesignError(null)
    try {
      const next = await retryDesignJob(job)
      setDesignJobs((rows) => rows.map((row) => row.job_id === next.job_id ?
        { ...next, research_run: run.run_dir } : row))
    } catch (error) { setDesignError((error as Error).message) }
    finally { setDesignBusy(null) }
  }
  const openFollowup = async () => {
    const studyId = run.manifest?.study_id
    if (!studyId) return
    setFollowupBusy(true); setFollowupError(null)
    try { setFollowupPreview(await fetchFollowupPreview(studyId)) }
    catch (error) { setFollowupError((error as Error).message) }
    finally { setFollowupBusy(false) }
  }
  const submitFollowup = async () => {
    if (!followupPreview) return
    setFollowupBusy(true); setFollowupError(null)
    try {
      const parsed: unknown = JSON.parse(followupCandidates)
      if (!Array.isArray(parsed) || parsed.length === 0 || parsed.some((item) =>
        !item || typeof item !== 'object' || Array.isArray(item))) {
        throw new Error('新候选必须是非空 JSON 对象数组')
      }
      const budget = Number(followupBudget)
      if (!Number.isInteger(budget) || budget < 1) throw new Error('预算必须为正整数')
      if (!await confirmation({ title: '创建后继研究', description: '新建预注册项目和 Study，必须使用真正独立的新确认数据；原 Study 不会被修改。', details: [{ label: '父 Study', value: followupPreview.parent_study_id }, { label: '新确认数据', value: followupTestPath.trim() }, { label: '预算', value: String(budget) }], confirmLabel: '核对并创建' })) return
      const created = await createFollowup({
        parent_study_id: followupPreview.parent_study_id,
        expected_parent_manifest_sha256: followupPreview.parent_manifest_sha256,
        new_project_name: followupProjectName.trim(), new_run_name: followupRunName.trim(),
        new_test_path: followupTestPath.trim(),
        candidates: parsed as Record<string, unknown>[], budget,
        objective: followupObjective.trim(),
      })
      setFollowupCreated(created); setFollowupPreview(null); onResearchChanged()
    } catch (error) { setFollowupError((error as Error).message) }
    finally { setFollowupBusy(false) }
  }
  useEffect(() => {
    let alive = true
    fetchRevisionWorktrees()
      .then((value) => {
        if (!alive) return
        setWorktrees(value.worktrees)
        setRevisionJobs(value.jobs)
        const broken = value.registries.find((item) => !item.integrity.ok)
        if (broken) setRevisionError(`worktree 审计链损坏：${broken.integrity.reason ?? broken.research_run}`)
        const brokenJob = value.job_registries.find((item) => !item.integrity.ok)
        if (brokenJob) setRevisionError(`revision job 审计链损坏：${brokenJob.integrity.reason ?? brokenJob.research_run}`)
      })
      .catch(() => undefined)
    return () => { alive = false }
  }, [])
  useEffect(() => {
    const active = revisionJobs.filter((job) => job.status === 'queued' || job.status === 'running')
    if (!active.length) return
    const timer = window.setInterval(() => {
      void Promise.all(active.map((job) => fetchRevisionJob(job.job_id)))
        .then(async (latest) => {
          setRevisionJobs((rows) => rows.map((row) => latest.find((job) => job.job_id === row.job_id) ?? row))
          if (latest.some((job) => !['queued', 'running'].includes(job.status))) {
            const value = await fetchRevisionWorktrees()
            setWorktrees(value.worktrees); setRevisionJobs(value.jobs)
            onResearchChanged()
          }
        })
        .catch(() => undefined)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [revisionJobs])
  const createWorktree = async (revisionId: string) => {
    setWorktreeBusy(revisionId); setRevisionError(null)
    try {
      const created = await createRevisionWorktree(revisionId)
      setWorktrees((rows) => [...rows.filter((row) => row.worktree_id !== created.worktree_id), created])
    } catch (error) { setRevisionError((error as Error).message) }
    finally { setWorktreeBusy(null) }
  }
  const removeWorktree = async (row: RevisionWorktree) => {
    if (row.status === 'failed' && !await confirmation({ title: '清理失败的隔离副本', description: '确认后 Git 副本及其中的修改会被删除。请先核对并保留需要的文件。', details: [{ label: '隔离副本', value: row.worktree_id }], confirmLabel: '清理副本', danger: true })) return
    const worktreeId = row.worktree_id
    setWorktreeBusy(worktreeId); setRevisionError(null)
    try {
      const removed = await removeRevisionWorktree(worktreeId)
      setWorktrees((rows) => rows.map((row) => row.worktree_id === removed.worktree_id ? removed : row))
    } catch (error) { setRevisionError((error as Error).message) }
    finally { setWorktreeBusy(null) }
  }
  const advanceWorktree = async (row: RevisionWorktree) => {
    setWorktreeBusy(row.worktree_id); setRevisionError(null)
    try {
      const next = row.status === 'active'
        ? await proposeWorktreeRevision(row.worktree_id)
        : await submitWorktreeRevision(row.worktree_id, row.proposal_sha256 ?? '')
      setWorktrees((rows) => rows.map((item) => item.worktree_id === next.worktree_id ? next : item))
    } catch (error) { setRevisionError((error as Error).message) }
    finally { setWorktreeBusy(null) }
  }
  const executeWorktree = async (row: RevisionWorktree) => {
    setWorktreeBusy(row.worktree_id); setRevisionError(null)
    try {
      const next = await executeWorktreeRevision(row.worktree_id)
      setWorktrees((rows) => rows.map((item) => item.worktree_id === next.worktree.worktree_id ? next.worktree : item))
      setRevisionJobs((rows) => [...rows.filter((job) => job.job_id !== next.job.job_id), next.job])
    } catch (error) { setRevisionError((error as Error).message) }
    finally { setWorktreeBusy(null) }
  }
  const executeInitial = async (detail: RevisionDetail) => {
    if (!await confirmation({ title: '执行代码修订', description: '运行冻结代码并消耗研究预算，执行可能产生新的 Observation。', details: [{ label: '修订', value: detail.revision_id }, { label: '冻结版本', value: detail.manifest_sha256 }], confirmLabel: '批准并执行' })) return
    setRevisionLoading(detail.revision_id); setRevisionError(null)
    try {
      const job = await executeInitialRevision(detail.revision_id, detail.manifest_sha256)
      setRevisionJobs((rows) => [...rows.filter((row) => row.job_id !== job.job_id), job])
    } catch (error) { setRevisionError((error as Error).message) }
    finally { setRevisionLoading(null) }
  }
  const cancelExecution = async (job: RevisionExecutionJob) => {
    setWorktreeBusy(job.worktree_id); setRevisionError(null)
    try {
      const next = await cancelRevisionJob(job.job_id)
      setRevisionJobs((rows) => rows.map((item) => item.job_id === next.job_id ? next : item))
    } catch (error) { setRevisionError((error as Error).message) }
    finally { setWorktreeBusy(null) }
  }
  const copyIdentity = async () => {
    const text = `项目：${projectLabel}\nStudy：${run.manifest?.study_id ?? '未提供'}\n研究目录：${run.run_dir}\n状态版本：${run.state_version ?? '未提供'}\n事件：${run.events_total ?? 0}`
    try { await navigator.clipboard.writeText(text); setCopyNotice('已复制 Study 标识与版本。') }
    catch { setCopyNotice('无法自动复制，请选中下方研究标识手动复制。') }
  }
  const copyClaimIdentity = async (id: string, refs: string[], decisionId: string) => {
    const text = `Study：${run.manifest?.study_id ?? '未提供'}\nClaim：${id}\nObservation：${refs.join('、')}\nDecision：${decisionId}`
    try { await navigator.clipboard.writeText(text); setClaimCopyNotice((current) => ({ ...current, [id]: '已复制 Claim 及证据标识。' })) }
    catch { setClaimCopyNotice((current) => ({ ...current, [id]: '无法自动复制，请选中研究标识手动复制。' })) }
  }
  return (
    <div className="section box">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <div className="study-title"><span className="step-kicker" title={run.manifest?.project}>当前项目 · {projectLabel}</span>
          <h2 title={run.run_dir}>{runLabel}</h2>
        </div>
      </div>
      {projectDescription && projectDescription.trim() !== study?.question?.trim() && <p className="muted">{projectDescription}</p>}
      {study?.question && <p className="study-question"><span className="muted">研究问题：</span>{study.question}</p>}
      <div className="study-summary" aria-label="Study 摘要">
        <div><span>阶段</span><strong>{phaseLabel}</strong></div>
        <div><span>剩余预算</span><strong>{budget ? budget.available.toPrecision(3) : '—'}</strong></div>
        <div><span>观察</span><strong>{observations.length}</strong></div>
        <div><span>范围限定 Claim</span><strong>{claims.length}</strong></div>
      </div>
      <details className="section research-records"><summary>后继研究 · 使用新的独立确认资源</summary>
        <button className="mini" type="button" disabled={!trustedLocal || followupBusy}
                onClick={() => void openFollowup()}>创建后继 Study</button>
        {followupCreated && <div className="notice" style={{ marginTop: 6 }}>
          {followupCreated.replayed ? '已恢复/重放' : '已创建'} {followupCreated.study_id} · 项目 {followupCreated.project} · 运行目录 {followupCreated.research_run}
        </div>}
        {followupError && <div className="notice err" style={{ marginTop: 6 }}>{followupError}</div>}
        {followupPreview && <div className="notice" style={{ marginTop: 6 }}>
          <div>父 Study：{followupPreview.parent_study_id}</div>
          <div className="muted" style={{ fontSize: 11 }}>新确认集须位于 {followupPreview.allowed_test_root}，且与父研究 train/dev/test 无样本重叠。</div>
          <div className="muted" style={{ fontSize: 11 }}>父研究已登记候选：{JSON.stringify(followupPreview.existing_candidates)}</div>
          <div className="fld" style={{ display: 'grid', gap: 6, marginTop: 8 }}>
          <input aria-label="新项目目录名" placeholder="新项目目录名" value={followupProjectName}
                 onChange={(event) => setFollowupProjectName(event.target.value)} />
          <input aria-label="新研究运行目录名" placeholder="新研究运行目录名" value={followupRunName}
                 onChange={(event) => setFollowupRunName(event.target.value)} />
          <input aria-label="新确认集绝对路径" placeholder="新确认集绝对路径" value={followupTestPath}
                 onChange={(event) => setFollowupTestPath(event.target.value)} />
          <textarea aria-label="新候选 JSON 数组" placeholder='新候选 JSON 数组，例如 [{"scheme":"heun"}]'
                    value={followupCandidates} onChange={(event) => setFollowupCandidates(event.target.value)} />
          <input aria-label="实验预算" placeholder="实验预算" value={followupBudget}
                 onChange={(event) => setFollowupBudget(event.target.value)} />
          <input aria-label="新研究问题" placeholder="新研究问题" value={followupObjective}
                 onChange={(event) => setFollowupObjective(event.target.value)} />
          </div>
          <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
            <button className="mini" type="button" disabled={followupBusy}
                    onClick={() => void submitFollowup()}>核对并创建</button>
            <button className="mini" type="button" onClick={() => setFollowupPreview(null)}>关闭</button>
          </div>
        </div>}
      </details>
      {run.manifest && <StudyLifecyclePanel studyId={run.manifest.study_id}
        concluded={study?.status === 'concluded'} trustedLocal={trustedLocal} onChanged={onResearchChanged} onOpenResource={onOpenResource}
        phase={run.lifecycle_phase} phaseReason={run.lifecycle_reason} resourceConsumed={run.confirmation_resource_consumed}
        confirmationMode={run.confirmation_mode} stateVersion={run.state_version} budgetAvailable={run.budget?.available} projectKey={run.manifest.project ?? run.run_dir} />}
      <details className="section research-records"><summary>技术信息、源码与溯源 · {graph?.integrity.ok === false ? '完整性需核对' : '查看冻结记录'}</summary>
      <dl className="resource-facts">
        <div><dt>Study 标识</dt><dd><code>{run.manifest?.study_id ?? '未提供'}</code></dd></div>
        <div><dt>状态版本</dt><dd>v{run.state_version ?? '—'}</dd></div>
        <div><dt>事件记录</dt><dd>{run.events_total ?? 0} 条</dd></div>
        <div><dt>项目目录</dt><dd>{run.manifest?.project ?? '未提供'}</dd></div>
        <div><dt>研究目录</dt><dd>{run.run_dir}</dd></div>
      </dl>
      <button className="mini" type="button" onClick={() => void copyIdentity()}>复制 Study 标识与版本</button>
      {copyNotice && <p className="muted" role="status">{copyNotice}</p>}
      <div className="row">
        <span className="muted">能力模式 / 策略</span>
        <span className="metric">{run.manifest?.capability_mode ?? '—'} · {run.manifest?.policy ?? '—'}</span>
      </div>
      {run.manifest?.followup_lineage && <div className="notice" style={{ marginTop: 6 }}>
        <div>自适应后继研究 · 父 Study <span className="code">{run.manifest.followup_lineage.parent_study_id}</span></div>
        <div className="muted" style={{ fontSize: 11 }}>
          新确认集 SHA-256：{run.manifest.followup_lineage.new_confirmation_sha256}
        </div>
        <div className="muted" style={{ fontSize: 11 }}>
          父研究证据仅作探索背景；当前结论范围仅限本 Study。
        </div>
      </div>}
      <div className="row">
        <span className="muted">重复单位 / 数量</span>
        <span className="metric">{run.manifest?.repeat_unit ?? 'train_seed'} · {run.manifest?.repeat_values?.length ?? 0}</span>
      </div>
      <div className="row">
        <span className="muted">预算（已花费 / 上限）</span>
        <span className="metric">
          {budget ? `${budget.spent.toPrecision(3)} / ${budget.cap}` : '—'}
          {budget ? ` · 可用 ${budget.available.toPrecision(3)}` : ''}
        </span>
      </div>
      {graph && (
        <div className={`notice ${graph.integrity.ok ? '' : 'err'}`} style={{ marginTop: 8 }}>
          <strong>实验溯源图：{graph.integrity.ok ? '完整' : '存在断链'}</strong>
          <span className="muted" style={{ marginLeft: 8 }}>
            {graph.nodes.length} 节点 · {graph.edges.length} 条关系
          </span>
          <div className="muted" style={{ marginTop: 4, fontSize: 12 }}>
            Git 锚点 {gitAnchored.length}/{codeRevisions.length}
            {dirtyAnchors.length ? `（其中 ${dirtyAnchors.length} 个源文件集偏离 HEAD）` : ''}
            {externalSources.length ? ` · 已验签外部源码 ${externalSources.length} 份` : ''}
          </div>
          {!graph.integrity.ok && (
            <div style={{ marginTop: 6, fontSize: 12 }}>
              {graph.integrity.issues.slice(0, 5).map((issue) => (
                <div key={`${issue.code}:${issue.entity_id}:${issue.reference ?? ''}`}>
                  <span className="code">{issue.code}</span> · {issue.entity_id}
                  {issue.reference ? ` → ${issue.reference}` : ''}
                  {issue.detail ? ` · ${issue.detail}` : ''}
                </div>
              ))}
              {graph.integrity.issues.length > 5 && (
                <div>另有 {graph.integrity.issues.length - 5} 项完整性问题</div>
              )}
            </div>
          )}
        </div>
      )}
      {codeRevisions.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <div className="muted" style={{ fontSize: 12, marginBottom: 5 }}>不可变代码修订</div>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {codeRevisions.map((node) => (
              <span key={node.id} style={{ display: 'inline-flex', gap: 3 }}>
                <button className="mini" type="button" disabled={revisionLoading === node.id}
                        onClick={() => void openRevision(node.id)}>
                  {revisionLoading === node.id ? '读取中…' : node.id}
                </button>
                {node.source_control?.kind === 'git' && (
                  <button className="mini" type="button"
                          title={trustedLocal ? '创建隔离 detached worktree' : '需要 --trusted-local'}
                          disabled={!trustedLocal || worktreeBusy === node.id}
                          onClick={() => void createWorktree(node.id)}>隔离编辑</button>
                )}
              </span>
            ))}
          </div>
          {worktrees.filter((row) => row.research_run === run.run_dir && row.status !== 'removed').map((row) => (
            <div className="row" key={row.worktree_id} style={{ marginTop: 6 }}>
              <div>
                <span className="code">{row.worktree_id}</span> · {row.status}
                <div className="muted" style={{ fontSize: 11 }}>{row.path}</div>
                {row.error && <div className="err" style={{ fontSize: 11 }}>{row.error}</div>}
                {row.proposal_sha256 && <div className="muted" style={{ fontSize: 11 }}>提案 {row.proposal_sha256.slice(0, 12)}…</div>}
                {row.successor_revision_id && <div className="muted" style={{ fontSize: 11 }}>后继 {row.successor_revision_id}</div>}
                {row.execution_run_id && <div className="muted" style={{ fontSize: 11 }}>Run {row.execution_run_id}</div>}
                {row.execution_error && <div className="err" style={{ fontSize: 11 }}>{row.execution_error}</div>}
                {revisionJobs.filter((job) => job.worktree_id === row.worktree_id).map((job) => (
                  <div key={job.job_id} className="muted" style={{ fontSize: 11 }}>
                    Job {job.job_id} · {job.phase} · {job.progress}%
                    {job.cancel_requested ? ' · 正在请求取消' : ''}
                    {job.error ? ` · ${job.error}` : ''}
                  </div>
                ))}
              </div>
              <div style={{ display: 'flex', gap: 4 }}>
                {(row.status === 'active' || row.status === 'review_required') && (
                  <button className="mini" type="button" disabled={worktreeBusy === row.worktree_id}
                          onClick={() => void advanceWorktree(row)}>
                    {row.status === 'active' ? '冻结提案' : '批准为后继 revision'}
                  </button>
                )}
                {(row.status === 'submitted' || row.status === 'executing') && (
                  <button className="mini" type="button" disabled={worktreeBusy === row.worktree_id}
                          onClick={() => void executeWorktree(row)}>
                    {row.status === 'executing' ? '恢复执行' : '执行并独立评估'}
                  </button>
                )}
                {revisionJobs.filter((job) => job.worktree_id === row.worktree_id
                  && (job.status === 'queued' || job.status === 'running') && !job.cancel_requested).map((job) => (
                  <button key={job.job_id} className="mini" type="button"
                          disabled={worktreeBusy === row.worktree_id}
                          onClick={() => void cancelExecution(job)}>取消执行</button>
                ))}
                {(row.status === 'failed' || row.status === 'active' || row.status === 'review_required' || row.status === 'submitted'
                  || row.status === 'executed' || row.status === 'execution_failed'
                  || row.status === 'execution_cancelled') && (
                  <button className="mini" type="button" disabled={worktreeBusy === row.worktree_id}
                          onClick={() => void removeWorktree(row)}>{row.status === 'failed' ? '清理失败副本' : '移除'}</button>
                )}
              </div>
            </div>
          ))}
          {revisionError && <div className="notice err" style={{ marginTop: 6 }}>{revisionError}</div>}
          {revision && (
            <div className="box" style={{ marginTop: 8 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                <strong className="code">{revision.revision_id}</strong>
                <div style={{ display: 'flex', gap: 4 }}>
                  {trustedLocal && revision.diff_source === 'unavailable' && revision.parent_revision_id && (
                    <button className="mini" type="button" onClick={() => {
                      setRevisionLoading(revision.revision_id); setRevisionError(null)
                      void migrateRevisionDiff(revision.revision_id)
                        .then(setRevision).catch((error) => setRevisionError((error as Error).message))
                        .finally(() => setRevisionLoading(null))
                    }}>迁移旧 Diff</button>
                  )}
                  {trustedLocal && !revision.parent_revision_id && revision.diff !== null &&
                    revision.research_runs.includes(run.run_dir) &&
                    !revisionJobs.some((job) => job.revision_id === revision.revision_id) &&
                    !observations.some((observation) => observation.hypothesis_id === revision.hypothesis_id) && (
                    <button className="mini" type="button" disabled={revisionLoading === revision.revision_id}
                      onClick={() => void executeInitial(revision)}>确认并执行</button>
                  )}
                  <button className="mini" type="button" onClick={() => setRevision(null)}>关闭</button>
                </div>
              </div>
              <div className="muted" style={{ fontSize: 12, marginTop: 5 }}>
                manifest {revision.manifest_sha256.slice(0, 12)}… · 文件 {revision.files.length}
                {revision.parent_revision_id ? ` · parent ${revision.parent_revision_id}` : ''}
              </div>
              <div className="muted" style={{ fontSize: 12, marginTop: 3 }}>
                Diff 来源：{revision.diff_source === 'frozen_identity' ? '原始身份冻结'
                  : revision.diff_source === 'migrated_attestation'
                    ? `旧版本旁路证明 ${revision.diff_attestation_id ?? ''}` : '不可证明'}
              </div>
              <pre style={{ overflow: 'auto', maxHeight: 420, whiteSpace: 'pre',
                            lineHeight: 1.45, fontSize: 12, marginTop: 8 }}>
                {revision.diff === null
                  ? '历史 revision 没有可验证 Diff；禁止从当前工作目录推测。'
                  : revision.diff || '该 revision 是已注册源码的零差异快照。'}
              </pre>
              {revisionJobs.filter((job) => job.revision_id === revision.revision_id).map((job) =>
                <div className="notice" key={job.job_id}>
                  执行作业 {job.job_id} · {job.status} · {job.phase} · {job.progress}%
                  {job.error ? ` · ${job.error}` : ''}{job.run_id ? ` · Run ${job.run_id}` : ''}
                  {['queued', 'running'].includes(job.status) && trustedLocal &&
                    <button className="mini" type="button" onClick={() => void cancelExecution(job)}>取消执行</button>}
                </div>)}
            </div>
          )}
        </div>
      )}
      {budget && (
        <>
          <BudgetBar cap={budget.cap} spent={budget.spent} reserved={budget.reserved} available={budget.available} />
          <div style={{ display: 'flex', gap: 12 }}>
            <LegendDot color="var(--accent)">已花费 {budget.spent.toPrecision(3)}</LegendDot>
            <LegendDot color="var(--warn)">预留 {budget.reserved.toPrecision(3)}</LegendDot>
            <LegendDot color="var(--card-2)">可用 {budget.available.toPrecision(3)}</LegendDot>
          </div>
        </>
      )}

      </details>
      <details className="section research-records"><summary>假设、实验设计与修订 · {hypotheses.length} 个假设</summary>
      <h2 style={{ fontSize: 14, marginTop: 10 }}>假设（{hypotheses.length}）</h2>
      {hypotheses.length === 0 && <div className="notice">研究库中还没有假设。</div>}
      {hypotheses.map((h) => (
        <div className="row" key={h.entity_id}>
          <div>
            <div><span className="code">{h.entity_id}</span> · v{h.version} · {labels.hypothesis?.[h.status] ?? ''}</div>
            <div style={{ fontSize: 13 }}>{h.mechanism}</div>
            <div className="muted" style={{ fontSize: 12 }}>
              适用：{h.applicability}｜预测 {h.predictions.length} 条｜证伪判据 {h.falsification.length} 条
            </div>
          </div>
        </div>
      ))}

      <h2 style={{ fontSize: 14, marginTop: 10 }}>实验设计（{run.designs?.length ?? 0}）</h2>
      {(run.designs ?? []).map((design) => {
        const job = designJobs.find((item) => item.design_id === design.entity_id
          && item.research_run === run.run_dir)
        return <div className="row" key={design.entity_id}>
          <div>
            <div><span className="code">{design.entity_id}</span> · {design.status} · {design.hypothesis_id}</div>
            {job && <div className="muted" style={{ fontSize: 11 }}>
              Job {job.job_id} · 第 {job.attempt} 次 · {job.status}{job.cancel_requested && job.status === 'running' ? '（停止中）' : ''}{job.observation_id ? ` · ${job.observation_id}` : ''}
              {job.error ? ` · ${job.error}` : ''}
            </div>}
          </div>
          {job && (job.status === 'queued' || job.status === 'running') && !job.cancel_requested &&
            <button className="mini" type="button" disabled={!trustedLocal || designBusy !== null}
                    onClick={() => void stopDesign(job)}>取消执行</button>}
          {job && job.status === 'cancelled' &&
            <button className="mini" type="button" disabled={!trustedLocal || designBusy !== null}
                    onClick={() => void retryDesign(job)}>重试执行</button>}
          <button className="mini" type="button" disabled={!trustedLocal || designBusy === design.entity_id}
                  title={trustedLocal ? '核对冻结设计和执行条件' : '需要 --trusted-local'}
                  onClick={() => void reviewDesign(design.entity_id)}>
            {designBusy === design.entity_id ? '核对中…' : '审查执行'}
          </button>
        </div>
      })}
      {designDetail && <div className="notice" style={{ marginTop: 8 }}>
        <div><strong>{designDetail.design_id}</strong> · Study v{designDetail.state_version}</div>
        <div className="muted" style={{ fontSize: 11 }}>设计摘要 {designDetail.design_sha256}</div>
        <div className="muted" style={{ fontSize: 12 }}>
          {designDetail.execution_readiness.registered_candidate_match
            ? `匹配预注册候选 ${designDetail.execution_readiness.registered_candidate_match}`
            : '未匹配预注册候选'}
        </div>
        {designDetail.execution_readiness.registered_design_reasons.length > 0 &&
          <div className="muted" style={{ fontSize: 11 }}>
            已注册候选执行入口缺口：{designDetail.execution_readiness.registered_design_reasons.join('、')}
          </div>}
        <div className="muted" style={{ fontSize: 11 }}>最终输入、评分器与沙箱门禁将在后台再次核对；此处不是执行成功保证。</div>
        <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
          <button className="mini" type="button"
                  disabled={designBusy !== null || designDetail.design.status !== 'frozen'
                    || !designDetail.hypothesis_version_matches
                    || !designDetail.execution_readiness.registered_design_route_ready}
                  onClick={() => void submitDesign(designDetail)}>确认并排队执行</button>
          <button className="mini" type="button" onClick={() => setDesignDetail(null)}>关闭</button>
        </div>
      </div>}
      {designError && <div className="notice err" style={{ marginTop: 6 }}>{designError}</div>}

      <h2 style={{ fontSize: 14, marginTop: 10 }}>修订（{revisions.length}）</h2>
      {revisions.length === 0 && <div className="muted" style={{ fontSize: 12 }}>尚无假设版本事件。</div>}
      {revisions.map((r) => (
        <div className="row" key={`${r.hypothesis_id}-v${r.version}`}>
          <div>
            <span className="code">{r.hypothesis_id}</span> v{r.version}
            {r.parent_version != null ? ` ← v${r.parent_version}` : ''}
          </div>
          <span className="metric" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            {labels.origin?.[r.origin] ?? ''} · seq {r.seq}
          </span>
        </div>
      ))}

      </details>
      <details className="section research-records"><summary>实验观察 · {observations.length} 条真实 Observation</summary>
      <h2 style={{ fontSize: 14, marginTop: 10 }}>观察（{observations.length}）</h2>
      {observations.length === 0 && <div className="muted" style={{ fontSize: 12 }}>尚无真实实验观察。推进研究后，执行结果会登记为 Observation。</div>}
      {observations.length > 0 && (
        <div style={{ marginBottom: 6 }}>
          <MetricBars
            points={observations.map((o): BarPoint => ({
              label: `${o.hypothesis_id} · ${o.scope}`,
              mean: o.value,
              std: o.uncertainty,
            }))}
          />
          <div className="muted" style={{ fontSize: 11 }}>条 = 观察值，须线 = ±不确定度（{observations[0]?.unit ?? ''}）。</div>
        </div>
      )}
      {observations.map((o) => (
        <div className="row" key={o.entity_id}>
          <div>
            <div><span className="code">{o.hypothesis_id}</span> · {o.scope}{onOpenResource && <button type="button" className="resource-inline" onClick={() => onOpenResource({ kind: 'observation', ref: o.entity_id, studyId: run.manifest?.study_id, title: o.entity_id })}>查看 Observation ↗</button>}</div>
            <div className="muted" style={{ fontSize: 12 }}>
              {o.value} {o.unit}｜不确定度 {o.uncertainty ?? '—'}｜run {o.run_id}｜{o.trust}
            </div>
          </div>
        </div>
      ))}

      </details>
      <details className="section research-records"><summary>决策记录 · {decisions.length} 条</summary>
      <h2 style={{ fontSize: 14, marginTop: 10 }}>决策（{decisions.length}）</h2>
      {decisions.length === 0 && <div className="muted" style={{ fontSize: 12 }}>尚无决策记录。</div>}
      {decisions.map((d) => (
        <div className="row" key={d.entity_id}>
          <div>
            <div>{labels.action?.[d.action] ?? ''} <span className="muted" style={{ fontSize: 12 }}>（基于状态 v{d.state_version}）</span></div>
            <div style={{ fontSize: 13 }}>{d.rationale}</div>
            <div className="muted" style={{ fontSize: 12 }}>
              证据 {d.observation_refs.length} 条｜{d.actor}{d.model ? ` · ${d.model}` : ''}
            </div>
          </div>
        </div>
      ))}

      </details>
      <h2 style={{ fontSize: 14, marginTop: 10 }}>范围限定结论（{claims.length}）</h2>
      {claims.length > 0 && <div style={{ marginBottom: 8 }}>
        <button className="mini" type="button" disabled={!onOpenResource}
          onClick={() => onOpenResource?.({ kind: 'evidence', studyId: run.manifest?.study_id, title: '研究证据' })}>在证据标签核验 ↗</button>
        {trustedLocal && <div className="fld" style={{ display: 'grid', gap: 6, marginTop: 8 }}>
          <div className="muted" style={{ fontSize: 12 }}>生成证据绑定草稿（自动包含全部 Claim；作者说明不经系统验证）</div>
          <input aria-label="草稿标题" value={draftTitle} maxLength={200}
                 onChange={(event) => setDraftTitle(event.target.value)} />
          <textarea aria-label="作者说明（可选）" placeholder="作者说明（可选；不能包含未绑定的数字结果）"
                    value={draftNote} maxLength={4000}
                    onChange={(event) => setDraftNote(event.target.value)} />
          <button className="mini" type="button" disabled={draftBusy}
                  onClick={() => void submitBoundDraft()}>
            {draftBusy ? '正在生成并核验…' : '保存可审校草稿'}
          </button>
          {draftError && <div className="notice err">{draftError}</div>}
          {draftRows.length > 0 && <div className="muted" style={{ fontSize: 12 }}>
            已保存草稿：{draftRows.map((row, index) => <span key={row.draft_id} style={{ marginRight: 8 }}>
              {row.ok ? <button className="mini" type="button" disabled={draftBusy || !onOpenResource} title={row.draft_id}
                                 onClick={() => onOpenResource?.({ kind: 'manuscript', ref: row.draft_id, studyId: run.manifest?.study_id, title: '证据绑定稿件' })}>
                证据绑定稿件 {index + 1}</button>
                : <span title={row.reason}>稿件 {index + 1} 校验失败</span>}
            </span>)}
          </div>}
          {createdDraft && <div className="notice">
            {createdDraft.replayed ? '已核验现有草稿' : '已生成可审校草稿'} · {draftTitle}
            <div className="muted" style={{ fontSize: 11 }}>{createdDraft.flow_state === 'reviewed' ? '已登记人工审校' : '待人工审校'} · 证据绑定快照只读，不代表已投稿或发表。</div>
            {onOpenResource && <button type="button" className="mini" onClick={() => onOpenResource({ kind: 'manuscript', ref: createdDraft.draft_id, studyId: run.manifest?.study_id, title: '证据绑定稿件' })}>在工作标签中阅读 ↗</button>}
          </div>}
        </div>}
      </div>}
      {claims.length === 0 && <div className="muted" style={{ fontSize: 12 }}>
        尚无由独立确认观察和决策共同绑定的 Claim；开发集观察不能单独生成结论。
      </div>}
      {claims.map((claim) => (
        <div className="row" key={claim.entity_id}>
          <div>
            <div>{onOpenResource ? <button type="button" className="resource-inline" onClick={() => onOpenResource({ kind: 'claim', ref: claim.entity_id, studyId: run.manifest?.study_id, title: `${claim.metric} 结论` })} title={claim.entity_id}>{claim.metric} 结论 ↗</button> : <span>{claim.metric} 结论</span>} ·
              {claim.status === 'boundary_supported_in_scope' ? '预注册切片支持存在边界'
                : claim.status === 'boundary_not_supported_in_scope' ? '未确认预注册边界'
                : claim.status === 'supported_in_scope' ? '当前预注册范围内支持'
                : '未达到预注册阈值'}</div>
            <div style={{ fontSize: 12 }}>
              {claim.metric} 整体方向统一效应 {claim.oriented_delta.toPrecision(5)} ·
              最小有意义阈值 {claim.min_meaningful_effect}
            </div>
            {claim.slice_effects && claim.slice_effects.length > 0 && (
              <div style={{ fontSize: 12 }}>
                预注册切片：{claim.slice_effects.map((item) =>
                  `${item.slice_id} ${item.oriented_delta.toPrecision(5)}`).join(' · ')}
              </div>
            )}
            <p className="muted" style={{ fontSize: 12 }}>适用范围：{({ registered_independent_confirmation: '预注册独立确认范围', signed_external_confirmation: '外部签名确认范围', signed_external_scope_boundary: '外部签名边界确认范围' } as Record<string, string>)[claim.scope] ?? claim.scope ?? '未提供'}。不证明机制或域外泛化。</p>
            <details className="resource-technical"><summary>证据标识与技术记录</summary>
              <dl className="resource-facts"><div><dt>Claim</dt><dd><code>{claim.entity_id}</code></dd></div><div><dt>Observation</dt><dd><code>{claim.observation_refs.join('、')}</code></dd></div><div><dt>Decision</dt><dd><code>{claim.decision_id}</code></dd></div>{claim.receipt_sha256 && <div><dt>签名回执</dt><dd><code>{claim.receipt_sha256}</code> · 信任级别 {claim.trust}</dd></div>}</dl>
              <p className="muted">仅限本 Study 的预注册确认范围；同账户工程签名不等于机构独立复核。</p>
              <button type="button" className="mini" onClick={() => void copyClaimIdentity(claim.entity_id, claim.observation_refs, claim.decision_id)}>复制 Claim 与证据标识</button>
              {claimCopyNotice[claim.entity_id] && <p className="muted" role="status">{claimCopyNotice[claim.entity_id]}</p>}
            </details>
          </div>
        </div>
      ))}

      <details className="section research-records"><summary>策略反思 · {reflections.length} 条</summary>
      <h2 style={{ fontSize: 14, marginTop: 10 }}>反思（{reflections.length}）</h2>
      {reflections.length === 0 && (
        <div className="muted" style={{ fontSize: 12 }}>
          尚无策略反思记录。候选开发实验完成后，已接入的研究策略可写入反思；没有记录不表示实验失败。
        </div>
      )}
      {reflections.map((r) => (
        <div className="row" key={r.entity_id}>
          <div>
            <div>{labels.action?.[r.action] ?? ''}{r.next_hypothesis_id ? ` → ${r.next_hypothesis_id}` : ''}</div>
            <div style={{ fontSize: 13 }}>{r.rationale}</div>
            <div className="muted" style={{ fontSize: 12 }}>
              替代解释：{r.alternative_explanation}｜证据 {r.evidence_refs.length} 条｜{r.actor}{r.model ? ` · ${r.model}` : ''}
            </div>
          </div>
        </div>
      ))}
      </details>
    </div>
  )
}

function BenchmarkPanel({ workspaceKey, trustedLocal }: { workspaceKey: string | null; trustedLocal: boolean }) {
  const confirmation = useConfirmation()
  const [runName, setRunName] = useState(() => `benchmark-${Date.now()}`)
  const [requestKey, setRequestKey] = useState(() => crypto.randomUUID())
  const [jobs, setJobs] = useState<BenchmarkJob[]>([])
  const [integrityError, setIntegrityError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [detail, setDetail] = useState<BenchmarkDetail | null>(null)
  const reload = async () => {
    const payload = await fetchBenchmarkJobs()
    setJobs(payload.jobs)
    setIntegrityError(payload.integrity.ok ? null : payload.integrity.reason ?? '作业审计链失败')
  }
  useEffect(() => {
    let alive = true
    setJobs([]); setDetail(null); setError(null)
    fetchBenchmarkJobs().then((payload) => {
      if (alive) {
        setJobs(payload.jobs)
        setIntegrityError(payload.integrity.ok ? null : payload.integrity.reason ?? '作业审计链失败')
      }
    }).catch((cause) => { if (alive) setError((cause as Error).message) })
    return () => { alive = false }
  }, [workspaceKey])
  useEffect(() => {
    if (!jobs.some((job) => job.status === 'queued' || job.status === 'running')) return
    const timer = window.setInterval(() => {
      void reload().catch((cause) => setError((cause as Error).message))
    }, 1500)
    return () => window.clearInterval(timer)
  }, [jobs])
  const start = async () => {
    if (!await confirmation({ title: '运行跨领域基准', description: '将在当前工作区执行受信任的内置 ODE 与排序吞吐代码，并创建新的基准目录；不调用模型。', details: [{ label: '运行名称', value: runName.trim() }], confirmLabel: '运行基准' })) return
    setBusy(true); setError(null)
    try {
      const job = await createBenchmark(runName.trim(), requestKey)
      setJobs((rows) => [...rows.filter((row) => row.job_id !== job.job_id), job])
      setRunName(`benchmark-${Date.now()}`)
      setRequestKey(crypto.randomUUID())
    } catch (cause) { setError((cause as Error).message) }
    finally { setBusy(false) }
  }
  const open = async (jobId: string) => {
    setBusy(true); setError(null)
    try { setDetail(await fetchBenchmarkJob(jobId)) }
    catch (cause) { setError((cause as Error).message) }
    finally { setBusy(false) }
  }
  return <div className="section box">
    <h2 style={{ fontSize: 16 }}>跨领域内核基准</h2>
    <p className="muted" style={{ fontSize: 12 }}>
      离线运行 ODE 与排序吞吐；只衡量这台机器上的 Research Kernel 流程，不是模型能力排名。
      否定或零 Claim 也会如实保留。
    </p>
    {trustedLocal && <div className="fld" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
      <input aria-label="基准运行名称" value={runName} maxLength={64}
             onChange={(event) => { setRunName(event.target.value); setRequestKey(crypto.randomUUID()) }} />
      <button className="mini" type="button" disabled={busy || !runName.trim()}
              onClick={() => void start()}>{busy ? '请稍候…' : '运行基准'}</button>
    </div>}
    {!trustedLocal && <div className="muted" style={{ fontSize: 12 }}>启动工作台时使用 --trusted-local 才能运行；已保存报告仍可查看。</div>}
    {integrityError && <div className="notice err">基准 Job 审计失败：{integrityError}</div>}
    {error && <div className="notice err">{error}</div>}
    {jobs.map((job) => <div className="row" key={job.job_id}>
      <div><span className="code">{job.job_id}</span> · {job.status} · {job.output_dir}
        {job.error && <div className="muted" style={{ fontSize: 12 }}>{job.error}</div>}
      </div>
      <button className="mini" type="button" disabled={busy || !!integrityError}
              onClick={() => void open(job.job_id)}>查看报告</button>
    </div>)}
    {detail && <div className="notice" style={{ marginTop: 8 }}>
      <div>Job {detail.job.job_id} · {detail.job.status} · {detail.verification?.ok ? '原始证据重验通过' : '尚无通过重验的完整报告'}</div>
      {(detail.report?.cases ?? []).map((item) => <div key={item.case} style={{ marginTop: 6 }}>
        {item.case} · {item.status} · {item.wall_seconds}s
        {item.summary && <> · {item.summary.study_status} · {item.summary.metric} ·
          Claim {item.summary.claims.length} 条 · 预算 {item.summary.budget_spent}</>}
        {item.error && <div className="notice err">{item.error}</div>}
      </div>)}
      <div className="muted" style={{ fontSize: 11 }}>耗时仅描述当前宿主；不代表独立科学复核。原始目录：{detail.job.output_dir}</div>
    </div>}
  </div>
}

export type StudyProjectSummary = { name: string; description?: string; phaseLabel?: string;
  legacyResult?: { delta: number; threshold?: number; scope?: string } }

export function LoopView({ research, workspaceKey, selectedRunDir, onSelectRun, onResearchChanged, initialSection = 'studies', onOpenResource, onProjectChanged, onRequestProjectChange, projectSummary, requestedStudyId }: {
  initialSection?: 'studies' | 'prepare' | 'advanced'; onOpenResource?: (resource: WorkbenchResource) => void
  projectSummary?: StudyProjectSummary
  requestedStudyId?: string
  research: ResearchState | null; workspaceKey: string | null; selectedRunDir: string | null
  onSelectRun: (runDir: string) => void; onResearchChanged: () => void
  onProjectChanged?: () => Promise<void>; onRequestProjectChange?: (action: () => void) => void
}) {
  const [section, setSection] = useState<'studies' | 'prepare' | 'advanced'>(initialSection)
  const [data, setData] = useState<{ trees: ArborTree[]; runs: string[]; node_status_labels: Record<string, string> } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [reloadSeq, setReloadSeq] = useState(0)
  useEffect(() => {
    if (section !== 'advanced') return
    let alive = true
    setData(null)
    setError(null)
    fetchArbor()
      .then((d) => { if (alive) { setData(d); setError(null) } })
      .catch((e) => { if (alive) setError((e as Error).message) })
    return () => { alive = false }
  }, [workspaceKey, reloadSeq, section])

  const trees = data?.trees ?? []
  const statusLabels = data?.node_status_labels ?? {}
  const researchRuns = research?.runs ?? []
  const activeRun = requestedStudyId ? researchRuns.find((run) => run.manifest?.study_id === requestedStudyId)
    : researchRuns.find((run) => run.run_dir === selectedRunDir) ?? researchRuns[researchRuns.length - 1]

  return (
    <>
      <p className="eyebrow">RESEARCH WORKSPACE</p>
      <h1>Study 与研究</h1>
      <p className="muted">从预注册项目开始，推进实验、独立确认，再核验证据和撰写稿件。</p>
      <div className="research-tabs" role="tablist" aria-label="研究工作台区域">
        <button type="button" role="tab" aria-selected={section === 'studies'} className={section === 'studies' ? 'active' : ''} onClick={() => setSection('studies')}>Study 与执行 <span>{researchRuns.length}</span></button>
        <button type="button" role="tab" aria-selected={section === 'prepare'} className={section === 'prepare' ? 'active' : ''} onClick={() => setSection('prepare')}>项目与协议</button>
        <button type="button" role="tab" aria-selected={section === 'advanced'} className={section === 'advanced' ? 'active' : ''} onClick={() => setSection('advanced')}>高级分析</button>
      </div>
      {section === 'advanced' && error && <div className="notice err">{error}</div>}

      {section === 'prepare' && <div role="tabpanel"><ProjectPreparation workspaceKey={workspaceKey}
        trustedLocal={research?.trusted_local ?? false} currentProjectName={projectSummary?.name}
        onProjectChanged={onProjectChanged} onResearchChanged={onResearchChanged} onRequestProjectChange={onRequestProjectChange}
        onStudyCreated={(runDir, studyId) => { onSelectRun(runDir); if (studyId && onOpenResource) onOpenResource({ kind: 'study', studyId }); else setSection('studies') }} /></div>}

      {section === 'studies' && <div role="tabpanel">
      <p className="muted">以下 Study 来自当前工作区的研究库；运行和确认均有独立审批与作业记录。</p>
      {!research && <div className="notice">正在读取研究控制器状态…</div>}
      {research && requestedStudyId && !activeRun && <div className="notice study-empty"><strong>尚未读取到指定 Study</strong><p>当前项目未找到 {requestedStudyId}。新登记的研究可能仍在刷新；不会用其他 Study 的结果替代。</p><button type="button" className="btn" onClick={onResearchChanged}>刷新研究记录</button></div>}
      {research && researchRuns.length === 0 && !requestedStudyId && (
        <div className="notice study-empty">
          <span className="step-kicker">{projectSummary?.name ?? '当前项目'}</span>
          <h2>尚未登记 Research Study</h2>
          {projectSummary?.description && <p>{projectSummary.description}</p>}
          {projectSummary?.phaseLabel && <p className="muted">项目实验状态：{projectSummary.phaseLabel}</p>}
          {projectSummary?.legacyResult && <div className="notice"><strong>已有项目实验结果</strong><p>改善量 {projectSummary.legacyResult.delta.toPrecision(4)}{projectSummary.legacyResult.threshold != null ? ` · 预注册阈值 ${projectSummary.legacyResult.threshold}` : ''}</p>{projectSummary.legacyResult.scope && <p className="muted">{projectSummary.legacyResult.scope}</p>}<p>这是项目实验的已有结果，不是 Research Study 的独立确认 Claim。两者保留独立的研究与审批记录。</p></div>}
          <p>先校验并预注册目标项目，再登记 Study。已完成的项目实验不能被当成尚未开始的 Study；如需后续研究，请准备新的协议和独立确认资源。</p>
          <details><summary>加载已有 Study 的命令</summary><p><span className="metric">popper serve &lt;项目&gt; --research-run &lt;研究目录&gt;</span></p></details>
          {research.trusted_local ? <button type="button" className="btn primary" onClick={() => setSection('prepare')}>准备项目与协议</button>
            : <p>当前工作台为只读模式；需要在受信任的本地项目中启动写入模式才能新建 Study。</p>}
        </div>
      )}
      {researchRuns.length > 1 && <div className="study-picker" aria-label="选择 Study">
        {researchRuns.map((run) => <button type="button" key={run.run_dir}
          className={activeRun?.run_dir === run.run_dir ? 'active' : ''}
          aria-pressed={activeRun?.run_dir === run.run_dir} title={run.run_dir}
          onClick={() => onSelectRun(run.run_dir)}>
          <strong>{run.run_dir.split(/[\\/]/).pop()}</strong>
          <span>{run.ok ? (research?.status_labels.study?.[run.study?.status ?? ''] ?? run.study?.status ?? '进行中') : '读取失败'}</span>
        </button>)}
      </div>}
      {activeRun && (
        <ResearchRunCard key={activeRun.run_dir} run={activeRun} labels={research?.status_labels ?? {}}
                         trustedLocal={research?.trusted_local ?? false}
                         projectName={projectSummary?.name} projectDescription={projectSummary?.description}
                         onResearchChanged={onResearchChanged} onOpenResource={onOpenResource} />
      )}
      </div>}

      {section === 'advanced' && <div role="tabpanel">
      <BenchmarkPanel workspaceKey={workspaceKey} trustedLocal={research?.trusted_local ?? false} />
      <button type="button" className="btn" onClick={() => setReloadSeq((value) => value + 1)}>刷新 Arbor 产物</button>
      {!data && !error && <div className="notice">正在读取 Arbor 假设树产物…</div>}

      {data && trees.length === 0 && (
        <div className="notice">
          当前工作区未发现假设树。将 <span className="metric">popper vendor arbor-init</span> 的运行目录放在当前工作区的 <span className="metric">integrations/runs</span> 下后，
          刷新本页可读取真实假设节点（persistent hypothesis tree、frontier、evidence、merge gate）。
        </div>
      )}

      {trees.length > 0 && trees.map((tree) => {
        const nodes = tree.nodes ?? []
        const depthLabel = Math.max(...nodes.map((n) => n.depth ?? 0), 0)
        return (
          <div className="section box" key={tree.run_dir}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
              <h2 style={{ fontSize: 16 }}>{tree.run_dir}</h2>
              <span className="muted" style={{ fontSize: 12 }}>深度 {depthLabel} · 节点 {nodes.length}</span>
            </div>
            <p className="muted" style={{ fontSize: 13, margin: '6px 0 4px' }}>目标：{tree.run?.objective}</p>
            <div className="row">
              <span className="muted">预算轮次</span>
              <span className="metric">{tree.run?.cycles_used ?? 0} / {tree.run?.budget_cycles ?? '—'}</span>
            </div>
            {nodes.map((n) => (
              <div className="row" key={n.id}>
                <div>
                  <div><span className="code">{n.id}</span>{n.parent ? ` ← ${n.parent}` : ''}</div>
                  <div style={{ fontSize: 13 }}>{n.hypothesis}</div>
                </div>
                <span className="metric" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  <span className={`st ${n.status}`} />
                  {statusLabels[n.status] ?? ''}
                </span>
              </div>
            ))}
          </div>
        )
      })}
      </div>}
    </>
  )
}
