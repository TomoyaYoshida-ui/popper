import { useEffect, useRef, useState } from 'react'
import { cancelStudyJob, executeStudyAction, fetchStudyJobs, previewStudyAction,
  type SearchProgress, type StudyActionPreview, type StudyLifecycleJob } from '../api'
import type { WorkbenchResource } from '../workbench'

const jobLabels: Record<StudyLifecycleJob['status'], string> = {
  queued: '排队中', running: '执行中', succeeded: '已完成', failed: '失败',
  cancelled: '已停止', interrupted: '执行中断',
}
const phaseLabels: Record<string, string> = {
  researching: '开发实验', ready_for_confirmation: '待独立确认', confirming: '独立确认中',
  confirmation_failed: '独立确认失败', concluded: '已形成结论', budget_exhausted: '预算已耗尽',
  blocked_external: '等待外部条件', integrity_failed: '完整性检查失败',
  external_confirmation_pending: '等待外部签名确认', external_preparation_incomplete: '外部确认准备中断',
  unavailable: '阶段待核对',
}
const descriptions: Record<string, string> = {
  researching: '预览下一段开发实验的执行范围和停止条件，审批后才会消耗预算。',
  ready_for_confirmation: '先审查服务端选定的候选、范围和评分预算，再单独批准独立确认。',
  concluded: '研究已形成范围限定结论。下一步核验证据、阅读并审校稿件。',
  confirmation_failed: '独立确认失败；已消费的一次性资源不得重试。请先核对失败记录。',
  confirming: '独立确认已开始。请查看运行状态；失败或中断后不会自动重试。',
  budget_exhausted: '研究预算已耗尽。请核对现有观察与停止记录，不会自动追加预算。',
  blocked_external: '研究在等待外部条件。请检查记录中的原因，再处理相关条件。',
  integrity_failed: '研究完整性检查失败。保留当前现场，先核对研究文件与记录。',
  external_confirmation_pending: '已冻结外部签名确认提交，等待并核验外部结果；不以本地确认替代。',
  external_preparation_incomplete: '外部确认准备中断，请核对冻结提交；不自动重新提交或改用本地确认。',
  unavailable: '服务端尚未提供可核对的研究阶段，请重新载入。不会根据候选或界面状态猜测确认资格。',
}
const isActive = (job: StudyLifecycleJob) => job.status === 'queued' || job.status === 'running'
const scopeLabel = (scope: string) => scope === 'dev' ? '固定开发划分' :
  scope === 'dev_search' ? '开发划分上的实验树搜索' :
  scope === 'registered_independent_confirmation' ? '预注册独立确认范围' : scope

export function StudyLifecyclePanel({ studyId, trustedLocal, onChanged, onOpenResource, concluded = false,
  phase, phaseReason, resourceConsumed, confirmationMode, stateVersion, projectKey = '', budgetAvailable }: {
  studyId: string; trustedLocal: boolean; onChanged: () => void
  concluded?: boolean; phase?: string; phaseReason?: string | null
  resourceConsumed?: boolean; confirmationMode?: 'local' | 'signed_external'
  stateVersion?: number | null; projectKey?: string; budgetAvailable?: number
  onOpenResource?: (resource: WorkbenchResource) => void
}) {
  // A missing optional field on an older server is not evidence of eligibility.
  const lifecycle = phase ?? (concluded ? 'concluded' : 'unavailable')
  const routeKey = JSON.stringify([projectKey, studyId])
  const contextKey = JSON.stringify([projectKey, studyId, stateVersion, lifecycle, resourceConsumed, confirmationMode])
  const context = useRef({ routeKey, contextKey })
  context.current = { routeKey, contextKey }
  const [steps, setSteps] = useState(1)
  const [devRuns, setDevRuns] = useState(1)
  const [minutes, setMinutes] = useState(60)
  const [codeFiles, setCodeFiles] = useState('')
  const [previewState, setPreview] = useState<{ key: string; value: StudyActionPreview } | null>(null)
  const [jobState, setJobs] = useState<{ key: string; rows: StudyLifecycleJob[]; loaded: boolean; progress: SearchProgress | null }>({ key: routeKey, rows: [], loaded: false, progress: null })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [jobsError, setJobsError] = useState<string | null>(null)
  const [reload, setReload] = useState(0)
  const previous = useRef<StudyLifecycleJob[]>([])
  const requestInFlight = useRef<symbol | null>(null)
  const mounted = useRef(true)
  const notify = useRef(onChanged)
  notify.current = onChanged
  const jobs = jobState.key === routeKey ? jobState.rows : []
  const searchProgress = jobState.key === routeKey ? jobState.progress : null
  const jobsLoaded = jobState.key === routeKey && jobState.loaded
  const activeJobs = jobs.filter(isActive)
  const active = activeJobs.length > 0
  const preview = previewState?.key === contextKey ? previewState.value : null
  const requestedAction = lifecycle === 'researching' ? 'run' : lifecycle === 'ready_for_confirmation' ? 'confirm' : null
  const validSteps = Number.isInteger(steps) && steps >= 1 && steps <= 100
  const disabledReason = !trustedLocal ? '当前服务为只读模式，不能执行研究。' : !jobsLoaded ? '正在核对作业状态；未核对前不能创建新作业。' :
    active ? '已有待执行或执行中的作业，请先查看状态。' : busy ? '正在处理当前请求，请勿重复提交。' :
    resourceConsumed ? '一次性确认资源已消费；不能创建新的研究或确认作业。' :
    confirmationMode === 'signed_external' && requestedAction === 'confirm' ? '此研究使用外部签名确认，请核对外部确认提交。' :
    !requestedAction ? (phaseReason || descriptions[lifecycle] || '当前研究阶段不允许创建新作业。') :
    requestedAction === 'run' && !validSteps ? '候选步数须为 1 到 100 的整数。' :
    budgetAvailable !== undefined && budgetAvailable < (requestedAction === 'confirm' ? 2 : 1) ?
      `本次${requestedAction === 'confirm' ? '确认至少需要两次评分' : '运行至少需要一次评分'}预算，当前预算不足。` : null

  useEffect(() => {
    setPreview(null); setError(null)
  }, [contextKey])
  useEffect(() => { setBusy(false); requestInFlight.current = null }, [routeKey])
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; requestInFlight.current = null }
  }, [])

  useEffect(() => {
    let live = true
    let timer: ReturnType<typeof setTimeout>
    previous.current = []
    setJobsError(null)
    setJobs({ key: routeKey, rows: [], loaded: false, progress: null })
    const load = async () => {
      try {
        const result = await fetchStudyJobs(studyId)
        if (!live || context.current.routeKey !== routeKey) return
        const completed = result.jobs.some((job) => !isActive(job) &&
          previous.current.some((old) => old.job_id === job.job_id && isActive(old)))
        previous.current = result.jobs
        setJobs({ key: routeKey, rows: result.jobs, loaded: true, progress: result.search_progress ?? null })
        setJobsError(null)
        if (completed) notify.current()
      } catch (cause) {
        if (live && context.current.routeKey === routeKey) {
          setJobsError((cause as Error).message)
          setJobs((current) => ({ ...current, loaded: false }))
        }
      }
      if (live) timer = setTimeout(() => void load(), 1500)
    }
    void load()
    return () => { live = false; clearTimeout(timer) }
  }, [routeKey, studyId, reload])

  const request = async (operation: () => Promise<void>) => {
    if (requestInFlight.current || !mounted.current) return
    const token = Symbol('study-request')
    requestInFlight.current = token
    setBusy(true); setError(null)
    try { await operation() }
    catch (cause) { if (mounted.current && context.current.contextKey === contextKey) setError((cause as Error).message) }
    finally { if (requestInFlight.current === token) { requestInFlight.current = null; setBusy(false) } }
  }
  const acceptPreview = (next: StudyActionPreview) => {
    if (mounted.current && context.current.contextKey === contextKey && next.study_id === studyId &&
      (stateVersion == null || next.state_version === stateVersion)) setPreview({ key: contextKey, value: next })
    else if (mounted.current && context.current.contextKey === contextKey) setError('研究状态已变化，请重新载入后预览。')
  }
  const review = async () => {
    if (!requestedAction || disabledReason) return
    setPreview(null)
    await request(async () => {
      const next = await previewStudyAction(studyId, requestedAction, requestedAction === 'confirm' ? 1 : steps)
      acceptPreview(next)
    })
  }
  const reviewAutopilot = async () => {
    if (requestedAction !== 'run' || disabledReason) return
    const files = codeFiles.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean)
    if (!files.length || !files.every((item) => item.endsWith('.py') && !item.includes('..'))) {
      setError('自治实验需要列出可修改的相对 .py 路径。')
      return
    }
    if (!Number.isInteger(devRuns) || devRuns < 1 || devRuns > steps) {
      setError('开发运行次数须为 1 到候选步数之间的整数。')
      return
    }
    setPreview(null)
    await request(async () => {
      const next = await previewStudyAction(studyId, 'autopilot', steps, {
        max_dev_runs: devRuns, max_model_tokens: 200_000, max_wall_clock_minutes: minutes, code_files: files,
      })
      acceptPreview(next)
    })
  }
  const execute = async () => {
    if (!preview || disabledReason) return
    if (preview.action === 'autopilot' ? requestedAction !== 'run' : preview.action !== requestedAction) return
    await request(async () => {
      const job = await executeStudyAction(preview)
      if (!mounted.current || context.current.contextKey !== contextKey) return
      const rows = [...previous.current.filter((row) => row.job_id !== job.job_id), job]
      previous.current = rows
      setJobs({ key: routeKey, rows, loaded: true, progress: searchProgress }); setPreview(null)
    })
  }
  const stop = async (job: StudyLifecycleJob) => {
    if (!trustedLocal || !jobsLoaded || job.cancel_requested || !isActive(job)) return
    await request(async () => {
      const next = await cancelStudyJob(studyId, job.job_id)
      if (!mounted.current || context.current.routeKey !== routeKey) return
      previous.current = previous.current.map((row) => row.job_id === next.job_id ? next : row)
      setJobs({ key: routeKey, rows: previous.current, loaded: true, progress: searchProgress })
      if (!isActive(next)) notify.current()
    })
  }
  const actionLabel = (action: string) => action === 'confirm' ? '独立确认' : action === 'autopilot' ? '自治实验' : '研究运行'
  const openJob = (job: StudyLifecycleJob) => onOpenResource?.({ kind: 'job', ref: job.job_id, studyId, title: `${actionLabel(job.request.action)}作业` })
  const jobRow = (job: StudyLifecycleJob) => <div key={job.job_id} className="row job-row">
    <div>
      <div><strong>{actionLabel(job.request.action)}</strong> · {jobLabels[job.status]}</div>
      {job.cancel_requested && isActive(job) && <div className="notice" role="status">已请求停止，等待安全检查点及真实终态；尚未结束。</div>}
      {job.error && <div className="notice err">{job.error}</div>}
      {job.result && <div>阶段：{phaseLabels[job.result.phase] || job.result.phase} · 观察 {job.result.observation_ids.length} · 结论 {job.result.claim_ids.length}</div>}
      <div className="actions">
        {onOpenResource && <button className="btn" type="button" onClick={() => openJob(job)}>查看作业详情</button>}
        {isActive(job) && <button className="btn" type="button" disabled={!trustedLocal || !jobsLoaded || busy || job.cancel_requested}
          title={!trustedLocal ? '只读模式不能请求停止' : !jobsLoaded ? '作业状态尚未核对' : job.cancel_requested ? '停止请求已经提交，正在等待终态' : undefined}
          onClick={() => void stop(job)}>请求停止</button>}
      </div>
      <details><summary>技术详情</summary><div className="mono selectable">作业 ID：{job.job_id}</div></details>
    </div>
  </div>

  return <section className="box study-action-panel" aria-label="研究下一步" style={{ marginTop: 12 }}>
    <div className="study-action-heading"><span className="step-kicker">下一步</span><strong>{active ? '查看正在执行的作业' : phaseLabels[lifecycle] || '核对研究状态'}</strong></div>
    {!active && <p className="muted">{phaseReason || descriptions[lifecycle] || '当前阶段请先核对研究记录。'}</p>}
    {!jobsLoaded && <p role="status" className="muted">正在核对作业状态…</p>}
    {activeJobs.some((job) => job.request.action === 'autopilot') && searchProgress && <div className="notice" role="status">
      第 {searchProgress.step}/{searchProgress.max_steps} 步 · 节点 {searchProgress.nodes} · 失败 {searchProgress.buggy}
      {searchProgress.best_value != null && <> · 当前最优 {searchProgress.best_value}</>}
      {searchProgress.max_dev_runs != null && <> · 开发运行 {searchProgress.dev_runs_used}/{searchProgress.max_dev_runs}</>}
      {searchProgress.max_model_tokens != null && <> · token {searchProgress.tokens_used}/{searchProgress.max_model_tokens}</>}
    </div>}
    {activeJobs.map(jobRow)}
    {!active && requestedAction && <div className="actions">
      {requestedAction === 'run' && <label>本次候选步数 <input aria-label="本次候选步数" type="number" min={1} max={100} value={steps}
        disabled={busy || !trustedLocal || resourceConsumed} onChange={(event) => { setSteps(Number(event.target.value)); setPreview(null) }} /></label>}
      <button className="btn primary" disabled={!!disabledReason} title={disabledReason || undefined} onClick={() => void review()}>
        {requestedAction === 'confirm' ? '预览独立确认' : '预览研究运行'}
      </button>
      {requestedAction === 'run' && <>
        <label>开发运行上限 <input aria-label="开发运行上限" type="number" min={1} max={steps} value={devRuns}
          disabled={busy || !trustedLocal || resourceConsumed} onChange={(event) => { setDevRuns(Number(event.target.value)); setPreview(null) }} /></label>
        <label>墙钟分钟 <input aria-label="墙钟分钟" type="number" min={1} max={1440} value={minutes}
          disabled={busy || !trustedLocal || resourceConsumed} onChange={(event) => { setMinutes(Number(event.target.value)); setPreview(null) }} /></label>
        <label>可改代码 <input aria-label="可改代码" value={codeFiles} placeholder="src/model.py"
          disabled={busy || !trustedLocal || resourceConsumed} onChange={(event) => { setCodeFiles(event.target.value); setPreview(null) }} /></label>
        <button className="btn" type="button" disabled={!!disabledReason} title={disabledReason || undefined} onClick={() => void reviewAutopilot()}>预览自治实验</button>
      </>}
    </div>}
    {!active && disabledReason && requestedAction && <p className="muted" role="note">{disabledReason}</p>}
    {!active && lifecycle === 'concluded' && onOpenResource && <div className="actions">
      <button className="btn primary" onClick={() => onOpenResource({ kind: 'evidence', studyId, title: '研究证据' })}>核验证据</button>
      <button className="btn" onClick={() => onOpenResource({ kind: 'manuscripts', studyId, title: '研究稿件' })}>阅读与审校稿件</button>
    </div>}
    {(lifecycle === 'unavailable' || error || jobsError) && <button className="btn" onClick={() => { notify.current(); setReload((value) => value + 1) }}>重新核对状态</button>}
    {error && <div className="notice err" role="alert">{error}</div>}
    {jobsError && <div className="notice err" role="alert">无法核对作业状态：{jobsError}</div>}
    {preview && <div className={`notice action-preview ${preview.action === 'confirm' ? 'confirmation-preview' : ''}`}>
      <strong>{preview.action === 'confirm' ? '独立确认审批预览' : preview.action === 'autopilot' ? '自治实验审批预览' : '研究运行审批预览'}</strong>
      <div>{preview.objective}</div>
      <div>范围：{scopeLabel(preview.scope)} · 研究状态版本：{preview.state_version}</div>
      <div>指标：{preview.metric.name} · 剩余评分预算：{preview.budget.available}</div>
      {preview.action === 'confirm' ? <>
        <div>选定候选：{preview.selected_hypothesis_id} · 本次确认评分预算：{preview.confirmation_cost}</div>
        <div className="confirmation-warning" role="note">{preview.confirmation_warning || '一次性确认资源消费后，失败或中断不得自动重试。'}</div>
      </> : preview.action === 'autopilot' && preview.envelope ? <div>
        最多 {preview.max_steps} 个搜索节点，开发运行不超过 {preview.envelope.max_dev_runs} 次，
        墙钟 {preview.envelope.max_wall_clock_minutes} 分钟。可改文件：{preview.envelope.code_files.join('、')}。
        一次批准只覆盖开发实验；独立确认仍需单独审批。
      </div> : <div>最多推进 {preview.max_steps} 个候选步骤；缺少基线时会先运行基线。</div>}
      <div>停止条件：{preview.stop_conditions.join('；')}</div>
      <details><summary>审批技术详情</summary><div className="mono selectable">Study ID：{preview.study_id}<br />审批摘要：{preview.approval_sha256}</div></details>
      <div className="actions">
        <button className="btn primary" disabled={!!disabledReason} title={disabledReason || undefined} onClick={() => void execute()}>
          {preview.action === 'confirm' ? '批准并执行独立确认' : preview.action === 'autopilot' ? '批准并开始自治实验' : '批准并运行研究'}
        </button>
        <button className="btn" disabled={busy} onClick={() => setPreview(null)}>取消此次审批</button>
      </div>
    </div>}
    {jobs.some((job) => !isActive(job)) && <details className="study-records"><summary>运行记录 · {jobs.filter((job) => !isActive(job)).length}</summary>{jobs.filter((job) => !isActive(job)).reverse().map(jobRow)}</details>}
  </section>
}
