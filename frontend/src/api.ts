import type { PopperState, ChatState, EntityRef } from './types'
import type { ContextPreview, HandoffFields, TaskContext } from './taskContext'

async function contextRequest<T>(path: string, data?: Record<string, unknown>): Promise<T> {
  const t = await sessionToken()
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 20_000)
  try {
    const result = await fetch(path, { method: data ? 'POST' : 'GET', signal: controller.signal, headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' }, body: data ? JSON.stringify({ schema_version: '1.0', ...data }) : undefined })
    const body = await result.json() as { data: T; error?: { message?: string } }
    if (!result.ok) throw new Error(body.error?.message ?? `HTTP ${result.status}`)
    return body.data
  } catch (reason) {
    if ((reason as Error).name === 'AbortError') throw new Error('交接请求超时，保存结果可能已生效；请重新读取核对，重试相同内容会复用原幂等标识。')
    throw reason
  } finally { clearTimeout(timer) }
}
export const fetchTaskContext = (workspaceId: string, sessionId: string) => contextRequest<TaskContext>(`/api/v1/sessions/${encodeURIComponent(sessionId)}/context?workspace_id=${encodeURIComponent(workspaceId)}`)
export const previewTaskContext = (workspaceId: string, sessionId: string, source: { task_id?: string; source_session_id?: string; study_ids?: string[] }) => contextRequest<ContextPreview>('/api/v1/context/preview', { workspace_id: workspaceId, session_id: sessionId, ...source })
export const commitTaskContext = (workspaceId: string, sessionId: string, data: { op: 'adopt' | 'detach'; expected_binding_version: number; idempotency_key: string; preview?: ContextPreview; fields?: HandoffFields; title?: string; status?: 'active' | 'completed' }) => contextRequest<Pick<TaskContext, 'binding' | 'selected'>>(`/api/v1/sessions/${encodeURIComponent(sessionId)}/context`, { workspace_id: workspaceId, ...data })

// 会话 token 只在首次取一次；之后带在 X-Popper-Token 请求态。
let token: string | null = null

async function sessionToken(): Promise<string> {
  if (token) return token
  const res = await fetch('/api/session')
  if (!res.ok) throw new Error(`收不到会话 token：HTTP ${res.status}`)
  const body = (await res.json()) as { token: string }
  token = body.token
  return token
}

export async function fetchState(): Promise<PopperState> {
  const t = await sessionToken()
  const res = await fetch('/api/state', { headers: { 'X-Popper-Token': t } })
  const body = (await res.json()) as PopperState & { error?: string }
  if (!res.ok) throw new Error(body.error || `请求失败：HTTP ${res.status}`)
  return body
}

/**
 * SSE 订阅：连接建立先收到一帧全量基线，之后服务端只推送发生变化的资源。
 * EventSource 不支持自定义请求头，令牌走 query 参数（loopback 同源）。
 * 断线由浏览器自动重连，重连后重新收到全量基线。
 */
export type EventPatch = {
  context?: { workspace_id: string; navigation_version: number }
  state?: PopperState
  workspace?: WorkspaceState
  campaign?: Campaign
  research?: ResearchState
  chat?: ChatState
}

export function subscribeEvents(
  onPatch: (patch: EventPatch) => void,
  onError: () => void,
): () => void {
  let source: EventSource | null = null
  let closed = false
  void sessionToken().then((t) => {
    if (closed) return
    source = new EventSource(`/api/events?token=${encodeURIComponent(t)}`)
    source.onmessage = (e: MessageEvent<string>) => {
      try {
        onPatch(JSON.parse(e.data) as EventPatch)
      } catch {
        /* 坏帧直接丢弃，等下一帧完整资源补丁 */
      }
    }
    source.onerror = () => onError()
  }).catch(() => onError())
  return () => {
    closed = true
    source?.close()
  }
}

export async function postAction(action: string): Promise<void> {
  const t = await sessionToken()
  const res = await fetch('/api/action', {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ action })
  })
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { error?: string }
    throw new Error(body.error || `操作失败：HTTP ${res.status}`)
  }
}

export type Literature = {
  workspace_root: string
  papers: {
    title: string; authors: string[]; year: number; venue?: string
    arxiv_id?: string; doi?: string | null; url?: string; source?: string
    relevance_score?: number; citation_count?: number
  }[]
  runs: string[]
}

export async function fetchLiterature(): Promise<Literature> {
  const t = await sessionToken()
  const res = await fetch('/api/literature', { headers: { 'X-Popper-Token': t } })
  const body = (await res.json()) as Literature & { error?: string }
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`)
  return body
}

export type ArborTree = {
  run_dir: string
  run: { objective: string; budget_cycles?: number; cycles_used?: number }
  nodes: { id: string; hypothesis: string; status: string; depth: number; parent?: string | null }[]
  frontier: { id: string }[]
  evidence: { node_id?: string; dev_score?: number; result?: string }[]
}

export type ArborResponse = { workspace_root: string; trees: ArborTree[]; runs: string[]; node_status_labels: Record<string, string> }

export async function fetchArbor(): Promise<ArborResponse> {
  const t = await sessionToken()
  const res = await fetch('/api/arbor', { headers: { 'X-Popper-Token': t } })
  const body = (await res.json()) as ArborResponse & { error?: string }
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`)
  return body
}

// ---- 假设环：research controller 真实状态（/api/research 只读投影） ----
export type ResearchHypothesis = {
  entity_id: string; entity_ref: EntityRef; status: string; version: number; parent_version: number | null
  mechanism: string; applicability: string
  predictions: string[]; falsification: string[]; alternatives: string[]
}
export type ResearchObservation = {
  entity_id: string; entity_ref: EntityRef; status: string; hypothesis_id: string; run_id: string
  value: number; unit: string; uncertainty: number | null; scope: string; trust: string
}
export type ResearchDecision = {
  entity_id: string; entity_ref: EntityRef; status: string; action: string; rationale: string
  alternatives: string[]; observation_refs: string[]; actor: string; model: string | null
  state_version: number; budget_request: number
}
export type ResearchClaim = {
  entity_id: string; entity_ref: EntityRef; status: string; study_id: string
  decision_id: string; hypothesis_id: string; hypothesis_version: number
  design_id: string; observation_refs: string[]; metric: string
  metric_direction: 'min' | 'max'; oriented_delta: number
  min_meaningful_effect: number; scope: string; limitations: string[]
  confirmation_kind?: 'positive_effect' | 'scope_boundary'
  receipt_sha256?: string; trust?: string
  slice_effects?: { slice_id: string; oriented_delta: number }[]
}
export type ResearchReflection = {
  entity_id: string; status: string; action: string; rationale: string
  alternative_explanation: string; evidence_refs: string[]
  next_hypothesis_id: string | null; actor: string; model: string | null; decision_id: string
}
export type ResearchRevision = {
  hypothesis_id: string; hypothesis_ref: EntityRef; version: number; parent_version: number | null
  origin: string; seq: number
}
export type ResearchDesign = { entity_id: string; entity_ref: EntityRef; status: string; hypothesis_id: string }
export type ResearchRun = {
  entity_id: string; entity_ref: EntityRef; status: string; study_id: string; design_id: string
  revision_id?: string; revision_manifest_sha256?: string
  external_source?: {
    kind: 'signed_confirmation_bundle'; submission_id: string
    submission_sha256: string; role: 'control' | 'candidate'; files_sha256: string
  }
}
export type ExperimentGraph = {
  schema_version: string
  study_id: string
  nodes: Array<{
    kind: string; id: string; status?: string
    source_control?: {
      kind: 'git' | 'unversioned' | 'legacy_unbound'
      head_commit?: string; project_path?: string
      source_files_clean?: boolean; source_status_sha256?: string
    }
  }>
  edges: Array<{
    source: { kind: string; id: string }
    target: { kind: string; id: string }
    relation: string
  }>
  integrity: {
    ok: boolean
    issues: Array<{ code: string; entity_id: string; reference?: string; detail?: string }>
  }
}
export type RevisionDetail = {
  revision_id: string; schema_version: string; status: string; manifest_sha256: string
  research_runs: string[]; hypothesis_id: string | null; design_id: string | null
  parent_revision_id: string | null; actor: string | null
  source_control: ExperimentGraph['nodes'][number]['source_control']
  files: Array<{ path: string; sha256: string }>
  diff: string | null; diff_sha256: string | null
  diff_source: 'frozen_identity' | 'migrated_attestation' | 'unavailable'
  diff_attestation_id: string | null
}
export type ResearchRunState = {
  run_dir: string
  ok: boolean
  lifecycle_phase?: string
  lifecycle_reason?: string
  confirmation_resource_consumed?: boolean
  confirmation_mode?: 'local' | 'signed_external'
  reason?: string
  manifest?: { study_id: string; project?: string; study_ref: EntityRef; capability_mode?: string; policy?: string;
    repeat_unit?: string; repeat_values?: number[];
    metric?: { name?: string }; min_meaningful_effect?: number;
    followup_lineage?: { kind: string; parent_study_id: string; parent_manifest_sha256: string;
      new_confirmation_sha256: string; claim_scope: string } | null }
  study?: { entity_id: string; entity_ref: EntityRef; status: string; family_id: string; question: string }
  state_version?: number | null
  hypotheses?: ResearchHypothesis[]
  revisions?: ResearchRevision[]
  designs?: ResearchDesign[]
  runs?: ResearchRun[]
  observations?: ResearchObservation[]
  decisions?: ResearchDecision[]
  claims?: ResearchClaim[]
  reflections?: ResearchReflection[]
  budget?: { family_id: string; cap: number; reserved: number; spent: number; available: number } | null
  events_total?: number
  experiment_graph?: ExperimentGraph
}
export type ResearchState = {
  runs: ResearchRunState[]
  status_labels: Record<string, Record<string, string>>
  trusted_local: boolean
}

export type StudyEnvelope = {
  max_dev_runs: number; max_model_tokens: number; max_wall_clock_minutes: number; code_files: string[]
}
export type StudyActionPreview = {
  study_id: string; action: 'run' | 'confirm' | 'autopilot'; max_steps: number; state_version: number
  approval_sha256: string; objective: string; metric: { name: string; direction: string }
  selected_hypothesis_id: string | null; scope: string; confirmation_cost: number
  budget: { available: number }; stop_conditions: string[]; confirmation_warning: string | null
  envelope?: StudyEnvelope & { envelope_sha256: string; study_id: string; state_version: number }
}
export type StudyLifecycleJob = {
  job_id: string; status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'
  request: { study_id: string; action: 'run' | 'confirm' | 'autopilot'; max_steps: number }
  cancel_requested: boolean; error: string | null
  result: { phase: string; observation_ids: string[]; claim_ids: string[] } | null
}
export function fetchStudyJobs(studyId: string): Promise<{ jobs: StudyLifecycleJob[] }> {
  return projectFilesRequest(`/api/v1/research/${encodeURIComponent(studyId)}/lifecycle`)
}
export function previewStudyAction(studyId: string, action: 'run' | 'confirm' | 'autopilot', maxSteps: number,
  envelope?: StudyEnvelope): Promise<StudyActionPreview> {
  return projectFilesRequest(`/api/v1/research/${encodeURIComponent(studyId)}/lifecycle/preview`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', action, max_steps: maxSteps,
      ...(envelope ? { envelope } : {}) }),
  })
}
export function executeStudyAction(preview: StudyActionPreview): Promise<StudyLifecycleJob> {
  return projectFilesRequest(`/api/v1/research/${encodeURIComponent(preview.study_id)}/lifecycle/jobs`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', action: preview.action, max_steps: preview.max_steps,
      approval_sha256: preview.approval_sha256, idempotency_key: `web-study-${preview.approval_sha256}`,
      ...(preview.envelope ? { envelope: preview.envelope } : {}) }),
  })
}
export function cancelStudyJob(studyId: string, jobId: string): Promise<StudyLifecycleJob> {
  return projectFilesRequest(`/api/v1/research/${encodeURIComponent(studyId)}/lifecycle/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0' }),
  })
}

export type StudyInitializationPreview = {
  project: string; run_dir: string; run_name: string; budget_cap: number
  objective: string; metric: { name: string; direction: 'min' | 'max' }; candidate_count: number
  already_initialized: boolean; study_id: string | null; proposal_sha256: string
}
export type ProjectPreparationPreview = {
  project_path: string; name: string; objective: string
  metric: { name: string; direction: 'min' | 'max' }
  domain_pack: string; entrypoint: string; code_files: string[]
  data_files: { train: string; dev: string; test: string }
  repeat_unit: string; repeat_values: Array<number | string>
  candidate_count: number; experiment_budget: number
  input_hashes: Record<string, string>; already_initialized: boolean
  approval_sha256: string
}
export function previewProjectPreparation(projectPath: string, protocol: Record<string, unknown> | null,
  files: Record<string, string>): Promise<ProjectPreparationPreview> {
  return projectFilesRequest('/api/v1/research/projects/preview', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', project_path: projectPath, protocol, files }),
  })
}
export function commitProjectPreparation(preview: ProjectPreparationPreview,
  protocol: Record<string, unknown> | null, files: Record<string, string>): Promise<ProjectPreparationPreview & { workspace_id: string }> {
  return projectFilesRequest('/api/v1/research/projects', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', project_path: preview.project_path,
      protocol, files, approval_sha256: preview.approval_sha256 }),
  })
}
export function previewStudyInitialization(runName: string, budgetCap: number): Promise<StudyInitializationPreview> {
  return projectFilesRequest('/api/v1/research/studies/preview', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', run_name: runName, budget_cap: budgetCap }),
  })
}
export function initializeStudy(runName: string, budgetCap: number, proposalSha256: string): Promise<{
  study_id: string; run_dir: string; already_initialized: boolean; state_version: number
}> {
  return projectFilesRequest('/api/v1/research/studies', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', run_name: runName, budget_cap: budgetCap,
      proposal_sha256: proposalSha256 }),
  })
}

export async function fetchResearch(): Promise<ResearchState> {
  const t = await sessionToken()
  const res = await fetch('/api/research', { headers: { 'X-Popper-Token': t } })
  const body = (await res.json()) as ResearchState & { error?: string }
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`)
  return body
}

export type BenchmarkJob = {
  job_id: string; output_dir: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted'
  error: string | null; created: string; updated: string
}
export type BenchmarkCase = {
  case: string; status: 'passed' | 'failed'; wall_seconds: number
  error?: string
  summary?: { study_status: string; metric: string; budget_spent: number
    evidence_claim_count: number; evidence_available: boolean
    claims: Array<{ claim_id: string; status: string; oriented_delta: number }> }
}
export type BenchmarkDetail = {
  job: BenchmarkJob
  report: { status: string; limitations: string[]; cases: BenchmarkCase[] } | null
  verification: { ok: boolean; case_count: number } | null
}

export async function fetchBenchmarkJobs(): Promise<{
  jobs: BenchmarkJob[]; integrity: { ok: boolean; reason?: string }
}> {
  const t = await sessionToken()
  const res = await fetch('/api/v1/benchmarks', { headers: { 'X-Popper-Token': t } })
  const body = (await res.json().catch(() => ({}))) as {
    data?: { jobs: BenchmarkJob[]; integrity: { ok: boolean; reason?: string } }
    error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}

export async function createBenchmark(runName: string, idempotencyKey: string): Promise<BenchmarkJob> {
  const t = await sessionToken()
  const res = await fetch('/api/v1/benchmarks', {
    method: 'POST', headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', run_name: runName,
      idempotency_key: idempotencyKey }),
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: BenchmarkJob; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}

export async function fetchBenchmarkJob(jobId: string): Promise<BenchmarkDetail> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/benchmarks/${encodeURIComponent(jobId)}`, {
    headers: { 'X-Popper-Token': t },
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: BenchmarkDetail; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}

export type ResearchEvidencePreview = {
  bundle: { study_id: string; study_state_version?: number; verification_level: string; bindings: Array<{ claim_id: string }> }
  json_text: string
  markdown: string
}

export async function fetchResearchEvidence(studyId: string): Promise<ResearchEvidencePreview> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/research/${encodeURIComponent(studyId)}/evidence-preview`, {
    headers: { 'X-Popper-Token': t },
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: ResearchEvidencePreview; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}

export type CreatedBoundDraft = {
  status: 'draft_needs_review'; replayed?: boolean; study_id?: string; draft_id: string; claim_count: number
  draft_dir: string; evidence_dir: string; draft_spec_path: string
  manuscript_path: string; manuscript: string; publication_attested: false
  flow_state?: 'draft' | 'reviewed' | 'submitted' | 'published'
}

export type BoundDraftRow = {
  draft_id: string; ok: boolean; reason?: string
  claim_count?: number; manuscript_path?: string
}

export async function fetchBoundDrafts(studyId: string): Promise<BoundDraftRow[]> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/research/${encodeURIComponent(studyId)}/bound-drafts`, {
    headers: { 'X-Popper-Token': t },
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: { drafts: BoundDraftRow[] }; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data.drafts
}

export async function fetchBoundDraft(studyId: string, draftId: string): Promise<CreatedBoundDraft> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/research/${encodeURIComponent(studyId)}/bound-drafts/${encodeURIComponent(draftId)}`, {
    headers: { 'X-Popper-Token': t },
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: CreatedBoundDraft; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}

export async function reviewBoundDraft(studyId: string, draftId: string, reason: string): Promise<{ status: string }> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/research/${encodeURIComponent(studyId)}/bound-drafts/${encodeURIComponent(draftId)}/review`, {
    method: 'POST', headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', reason }),
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: { status: string }; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}

export async function createBoundDraft(studyId: string, spec: {
  schema_version: '1.0'; title: string
  sections: Array<{ heading: string; blocks: Array<
    { kind: 'claim'; claim_id: string } | { kind: 'author_note'; text: string }
  > }>
}): Promise<CreatedBoundDraft> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/research/${encodeURIComponent(studyId)}/bound-drafts`, {
    method: 'POST', headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', spec }),
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: CreatedBoundDraft; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}

export type FollowupPreview = {
  parent_study_id: string; parent_manifest_sha256: string
  allowed_test_root: string; project_parent: string; run_parent: string
  existing_candidates: Record<string, unknown>[]
}
export type CreatedFollowup = {
  project: string; research_run: string; study_id: string; phase: string
  research_initialized: boolean; candidate_count: number; budget: number
  replayed?: boolean
}

export async function fetchFollowupPreview(studyId: string): Promise<FollowupPreview> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/research/${encodeURIComponent(studyId)}/followup-preview`, {
    headers: { 'X-Popper-Token': t },
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: FollowupPreview; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}

export async function createFollowup(request: {
  parent_study_id: string; expected_parent_manifest_sha256: string
  new_project_name: string; new_run_name: string; new_test_path: string
  candidates: Record<string, unknown>[]; budget: number; objective: string
}): Promise<CreatedFollowup> {
  const t = await sessionToken()
  const res = await fetch('/api/v1/research/followups', {
    method: 'POST', headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', ...request }),
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: CreatedFollowup; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}

export type DesignDetail = {
  study_id: string; design_id: string; state_version: number
  design_sha256: string; design: ResearchDesign; hypothesis_version_matches: boolean
  execution_readiness: {
    controller_routing_ready: boolean; execution_ready: boolean
    registered_candidate_match: string | null; registered_design_route_ready: boolean
    registered_design_reasons: string[]; reasons: string[]; not_checked: string[]
  }
}
export type DesignExecutionJob = {
  job_id: string; research_run?: string; design_id: string; design_sha256: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted' | 'cancelled'
  attempt: number; cancel_requested: number
  observation_id: string | null; error: string | null
  created: string; updated: string
}

export async function fetchDesignJobs(): Promise<DesignExecutionJob[]> {
  const t = await sessionToken()
  const res = await fetch('/api/v1/design-jobs', { headers: { 'X-Popper-Token': t } })
  const body = (await res.json().catch(() => ({}))) as {
    data?: { jobs: DesignExecutionJob[] }; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data.jobs
}

export async function fetchDesignDetail(studyId: string, designId: string): Promise<DesignDetail> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/designs/${encodeURIComponent(studyId)}/${encodeURIComponent(designId)}`, {
    headers: { 'X-Popper-Token': t },
  })
  const body = (await res.json().catch(() => ({}))) as { data?: DesignDetail; error?: { message?: string } }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}

export async function executeDesign(detail: DesignDetail): Promise<DesignExecutionJob> {
  const t = await sessionToken()
  const res = await fetch('/api/v1/design-jobs', {
    method: 'POST', headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', study_id: detail.study_id,
      design_id: detail.design_id, expected_version: detail.state_version,
      design_sha256: detail.design_sha256 }),
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: { job: DesignExecutionJob }; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data.job
}

export async function fetchDesignJob(jobId: string): Promise<DesignExecutionJob> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/design-jobs/${encodeURIComponent(jobId)}`, {
    headers: { 'X-Popper-Token': t },
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: { job: DesignExecutionJob }; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data.job
}

export async function cancelDesignJob(jobId: string): Promise<DesignExecutionJob> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/design-jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: 'POST', headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', reason: 'user requested cancellation' }),
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: { job: DesignExecutionJob }; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data.job
}

export async function retryDesignJob(job: DesignExecutionJob): Promise<DesignExecutionJob> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/design-jobs/${encodeURIComponent(job.job_id)}/retry`, {
    method: 'POST', headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', expected_attempt: job.attempt,
      design_sha256: job.design_sha256 }),
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: { job: DesignExecutionJob }; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data.job
}

export async function fetchRevision(revisionId: string): Promise<RevisionDetail> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/revisions/${encodeURIComponent(revisionId)}`, {
    headers: { 'X-Popper-Token': t },
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: RevisionDetail
    error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}

export async function migrateRevisionDiff(revisionId: string): Promise<RevisionDetail> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/revisions/${encodeURIComponent(revisionId)}/migrate-diff`, {
    method: 'POST', headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0' }),
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: RevisionDetail; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}

export type RevisionWorktree = {
  research_run: string; worktree_id: string; idempotency_key: string
  revision_id: string; git_commit: string; path: string
  status: 'provisioning' | 'active' | 'review_required' | 'submitted' | 'executing' | 'executed' | 'execution_failed' | 'execution_cancelled' | 'removed' | 'failed'
  created: string; removed: string | null; error: string | null
  proposal_sha256: string | null; successor_revision_id: string | null; submitted: string | null
  execution_run_id: string | null; execution_error: string | null; executed: string | null
}

export type RevisionExecutionJob = {
  research_run?: string; job_id: string; worktree_id: string; revision_id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'
  phase: string; progress: number; cancel_requested: boolean
  run_id: string | null; error: string | null
  created: string; updated: string; started: string | null; finished: string | null
}

export async function fetchRevisionWorktrees(): Promise<{
  worktrees: RevisionWorktree[]
  jobs: RevisionExecutionJob[]
  registries: Array<{ research_run: string; integrity: { ok: boolean; events?: number; head?: string; reason?: string } }>
  job_registries: Array<{ research_run: string; integrity: { ok: boolean; events?: number; head?: string; reason?: string } }>
  trusted_local: boolean
}> {
  const t = await sessionToken()
  const res = await fetch('/api/v1/worktrees', { headers: { 'X-Popper-Token': t } })
  const body = (await res.json().catch(() => ({}))) as {
    data?: {
      worktrees: RevisionWorktree[]
      jobs: RevisionExecutionJob[]
      registries: Array<{ research_run: string; integrity: { ok: boolean; events?: number; head?: string; reason?: string } }>
      job_registries: Array<{ research_run: string; integrity: { ok: boolean; events?: number; head?: string; reason?: string } }>
      trusted_local: boolean
    }
    error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}

export async function createRevisionWorktree(revisionId: string): Promise<RevisionWorktree> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/revisions/${encodeURIComponent(revisionId)}/worktrees`, {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', idempotency_key: `web-worktree-${crypto.randomUUID()}` }),
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: RevisionWorktree; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}

export async function removeRevisionWorktree(worktreeId: string): Promise<RevisionWorktree> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/worktrees/${encodeURIComponent(worktreeId)}/remove`, {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0' }),
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: RevisionWorktree; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}

async function postWorktreeAction(
  worktreeId: string, action: 'proposal' | 'submit', payload: Record<string, unknown>,
): Promise<RevisionWorktree> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/worktrees/${encodeURIComponent(worktreeId)}/${action}`, {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', ...payload }),
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: RevisionWorktree; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}

export async function proposeWorktreeRevision(worktreeId: string): Promise<RevisionWorktree> {
  return postWorktreeAction(worktreeId, 'proposal', {})
}

export async function submitWorktreeRevision(
  worktreeId: string, proposalSha256: string,
): Promise<RevisionWorktree> {
  return postWorktreeAction(worktreeId, 'submit', { proposal_sha256: proposalSha256 })
}

export async function executeWorktreeRevision(worktreeId: string): Promise<{
  worktree: RevisionWorktree; job: RevisionExecutionJob
}> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/worktrees/${encodeURIComponent(worktreeId)}/execute`, {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0' }),
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: { worktree: RevisionWorktree; job: RevisionExecutionJob }; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}

export async function fetchRevisionJob(jobId: string): Promise<RevisionExecutionJob & {
  development_diagnostics?: Record<string, unknown>
  development_diagnostics_error?: Record<string, unknown>
}> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/revision-jobs/${encodeURIComponent(jobId)}`, {
    headers: { 'X-Popper-Token': t },
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: { job: RevisionExecutionJob; development_diagnostics?: Record<string, unknown>; development_diagnostics_error?: Record<string, unknown> }; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return { ...body.data.job,
    ...(body.data.development_diagnostics ? { development_diagnostics: body.data.development_diagnostics } : {}),
    ...(body.data.development_diagnostics_error ? { development_diagnostics_error: body.data.development_diagnostics_error } : {}),
  }
}

export async function cancelRevisionJob(jobId: string, reason = 'user requested cancellation'): Promise<RevisionExecutionJob> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/revision-jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: 'POST', headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', reason }),
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: { job: RevisionExecutionJob }; error?: { message?: string }
  }
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data.job
}

export type CampaignStage = {
  key: string
  label: string
  src: string
  status: 'done' | 'active' | 'todo' | 'warn'
  evidence: { k: string; v: string }[]
}

export type CampaignApproval = {
  run_dir: string
  status: string
  step: string
  required: string
  reason: string | null
  history: { step: string; outcome: string }[]
  evidence: {
    diff?: string
    proposal?: { bytes: number; summary?: unknown }
  }
  job: { status: 'idle' | 'completed' | 'failed'; decision?: string; error?: string }
}

export type Campaign = {
  objective: string | null
  phase: string
  phase_label: string
  stages: CampaignStage[]
  campaign: CampaignApproval | null
}

export async function fetchCampaign(): Promise<Campaign> {
  const t = await sessionToken()
  const res = await fetch('/api/campaign', { headers: { 'X-Popper-Token': t } })
  const body = (await res.json()) as Campaign & { error?: string }
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`)
  return body
}

export async function postCampaignDecision(
  action: 'approve' | 'reject',
  reason?: string,
): Promise<{ ok?: boolean; decision?: string; error?: string }> {
  const t = await sessionToken()
  const res = await fetch('/api/campaign', {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, reason }),
  })
  const body = (await res.json().catch(() => ({}))) as { ok?: boolean; decision?: string; error?: string }
  if (!res.ok) throw new Error(body.error || `审批失败：HTTP ${res.status}`)
  return body
}

// ---- 工作区 / 稿件物化 ----
export type WorkspaceOp =
  | { op: 'workspace_switch'; wid: string }
  | { op: 'workspace_remove'; wid: string }
  | { op: 'folder_create'; name: string }
  | { op: 'folder_rename'; fid: string; name: string }
  | { op: 'folder_remove'; fid: string }
  | { op: 'task_create'; fid: string; title?: string }
  | { op: 'task_rename'; fid: string; quid: string; title: string }
  | { op: 'task_toggle'; fid: string; quid: string; done: boolean }
  | { op: 'task_remove'; fid: string; quid: string }
  | { op: 'task_move'; from_fid: string; quid: string; to_fid: string }

export type WorkspaceState = import('./types').Workspace
export type Deliverable = import('./types').Deliverable

export async function fetchWorkspace(): Promise<WorkspaceState> {
  const t = await sessionToken()
  const res = await fetch('/api/workspace', { headers: { 'X-Popper-Token': t } })
  const body = (await res.json()) as WorkspaceState & { error?: string }
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`)
  return body
}

export async function postWorkspaceOp(op: WorkspaceOp): Promise<{ folders: unknown[] }> {
  const t = await sessionToken()
  const res = await fetch('/api/workspace', {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify(op),
  })
  const body = (await res.json().catch(() => ({}))) as { error?: string; folders?: unknown[] }
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`)
  return { folders: body.folders ?? [] }
}

type WorkspaceMutation = Exclude<WorkspaceOp,
  { op: 'workspace_switch'; wid: string } | { op: 'workspace_remove'; wid: string }>

export async function postWorkspaceCommand(
  op: WorkspaceMutation,
  expectedVersion: number,
): Promise<{ operation: string; state_version: number }> {
  const t = await sessionToken()
  const { op: action, ...payload } = op
  const res = await fetch('/api/v1/commands/workspace', {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      schema_version: '1.0',
      idempotency_key: `web-${crypto.randomUUID()}`,
      expected_version: expectedVersion,
      action,
      payload,
    }),
  })
  const body = (await res.json().catch(() => ({}))) as {
    data?: { operation: string; state_version: number }
    error?: { message?: string }
  }
  if (!res.ok || !body.data) {
    throw new Error(body.error?.message || `HTTP ${res.status}`)
  }
  return body.data
}

export type MaterializeTemplate = 'md' | 'tex' | 'docx' | 'pdf'
export type MaterializeDisclosure = 'nature' | 'acm' | 'ieee' | null

// ---- 自由对话（独立于事件溯源链；未配置时后端拒绝发送） ----
export async function fetchChat(): Promise<ChatState> {
  const t = await sessionToken()
  const res = await fetch('/api/chat', { headers: { 'X-Popper-Token': t } })
  const body = (await res.json()) as ChatState & { error?: string }
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`)
  return body
}

export type ToolAuditEvent = {
  seq: number
  event_id: string
  session_id: string
  turn_id: string
  call_id: string
  kind: string
  actor: string
  trace_id: string
  projection_sha256: string
  immutable_sha256: string | null
  previous_hash: string
  hash: string
  created: string
}

export type ToolAuditPage = {
  events: ToolAuditEvent[]
  cursor: number
  has_more: boolean
  integrity: { ok: boolean; events: number; head?: string; reason?: string }
}

export async function fetchToolAuditPage(sessionId: string, after = 0): Promise<ToolAuditPage> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/sessions/${encodeURIComponent(sessionId)}/tool-events?after=${after}&limit=100`, {
    headers: { 'X-Popper-Token': t },
  })
  const envelope = (await res.json().catch(() => ({}))) as {
    data?: ToolAuditPage
    error?: { message?: string }
  }
  if (!res.ok || !envelope.data) throw new Error(envelope.error?.message || `HTTP ${res.status}`)
  return envelope.data
}

export async function selectChatSession(sessionId: string, workspaceId?: string): Promise<ChatState> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/sessions/${encodeURIComponent(sessionId)}/select`, {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', workspace_id: workspaceId }),
  })
  const envelope = (await res.json().catch(() => ({}))) as {
    data?: ChatState
    error?: { message?: string }
  }
  if (!res.ok || !envelope.data) throw new Error(envelope.error?.message || `HTTP ${res.status}`)
  return envelope.data
}

export async function setSessionTokenBudget(
  sessionId: string, tokenBudget: number | null,
): Promise<ChatState> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/sessions/${encodeURIComponent(sessionId)}/budget`, {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', token_budget: tokenBudget }),
  })
  const body = (await res.json().catch(() => ({}))) as { error?: { message?: string } }
  if (!res.ok) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return fetchChat()
}

export async function setSessionCostBudget(
  sessionId: string, costBudgetMicros: number | null,
): Promise<ChatState> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/sessions/${encodeURIComponent(sessionId)}/cost-budget`, {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', cost_budget_micros: costBudgetMicros }),
  })
  const body = (await res.json().catch(() => ({}))) as { error?: { message?: string } }
  if (!res.ok) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return fetchChat()
}

export async function createChatSession(title: string, workspaceId?: string): Promise<ChatState> {
  const t = await sessionToken()
  const res = await fetch('/api/v1/sessions', {
    method: 'POST',
    signal: AbortSignal.timeout(20_000),
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', title, harness: 'chat', workspace_id: workspaceId }),
  })
  const envelope = (await res.json().catch(() => ({}))) as {
    data?: { session_id: string }
    error?: { message?: string }
  }
  if (!res.ok || !envelope.data) throw new Error(envelope.error?.message || `HTTP ${res.status}`)
  return selectChatSession(envelope.data.session_id, workspaceId)
}

export async function fetchSessionIndex(): Promise<import('./sessionNavigation').SessionIndex> {
  const t = await sessionToken()
  const res = await fetch('/api/v1/sessions?scope=all', { headers: { 'X-Popper-Token': t } })
  const body = await res.json()
  if (!res.ok || !body.data) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return body.data
}
export async function updateSessionMetadata(session: import('./sessionNavigation').SessionSummary, change: { title: string } | { status: 'active' | 'archived' }): Promise<void> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/sessions/${encodeURIComponent(session.session_id)}/metadata`, {
    method: 'POST', headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', workspace_id: session.workspace_id, expected_updated: session.updated, ...change }),
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(body.error?.message || `HTTP ${res.status}`)
}

async function postToolAction(
  callId: string,
  action: 'decision' | 'execute' | 'cancel',
  payload: Record<string, unknown>,
): Promise<ChatState> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/tool-calls/${encodeURIComponent(callId)}/${action}`, {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', ...payload }),
  })
  const envelope = (await res.json().catch(() => ({}))) as { error?: { message?: string } }
  if (!res.ok) throw new Error(envelope.error?.message || `HTTP ${res.status}`)
  return fetchChat()
}

export async function approveAndExecuteTool(callId: string): Promise<ChatState> {
  await postToolAction(callId, 'decision', { decision: 'approve', reason: '用户在工作台批准' })
  return postToolAction(callId, 'execute', {})
}

export async function executeApprovedTool(callId: string): Promise<ChatState> {
  return postToolAction(callId, 'execute', {})
}

export async function continueAfterTools(turnId: string): Promise<ChatState> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/turns/${encodeURIComponent(turnId)}/continue`, {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0' }),
  })
  const envelope = (await res.json().catch(() => ({}))) as { error?: { message?: string } }
  if (!res.ok) throw new Error(envelope.error?.message || `HTTP ${res.status}`)
  return fetchChat()
}

export async function rejectTool(callId: string): Promise<ChatState> {
  return postToolAction(callId, 'decision', { decision: 'reject', reason: '用户在工作台拒绝' })
}

export async function cancelTool(callId: string): Promise<ChatState> {
  return postToolAction(callId, 'cancel', { reason: '用户在工作台取消' })
}

export async function postChat(text: string, currentSessionId: string | null, workspaceId?: string): Promise<ChatState> {
  const t = await sessionToken()
  let sessionId = currentSessionId
  if (!sessionId) {
    const created = await fetch('/api/v1/sessions', {
      method: 'POST',
      signal: AbortSignal.timeout(20_000),
      headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
      body: JSON.stringify({ schema_version: '1.0', title: '研究对话', harness: 'chat', workspace_id: workspaceId }),
    })
    const envelope = (await created.json().catch(() => ({}))) as {
      data?: { session_id: string }
      error?: { message?: string }
    }
    if (!created.ok || !envelope.data) {
      throw new Error(envelope.error?.message || `HTTP ${created.status}`)
    }
    sessionId = envelope.data.session_id
  }
  const res = await fetch(`/api/v1/sessions/${encodeURIComponent(sessionId)}/turns`, {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      schema_version: '1.0',
      idempotency_key: `web-turn-${crypto.randomUUID()}`,
      workspace_id: workspaceId,
      text,
    }),
  })
  const body = (await res.json().catch(() => ({}))) as { error?: { message?: string } }
  if (!res.ok) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return fetchChat()
}

export async function cancelChatTurn(turnId: string): Promise<ChatState> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/turns/${encodeURIComponent(turnId)}/cancel`, {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', reason: '用户在工作台停止生成' }),
  })
  const body = (await res.json().catch(() => ({}))) as { error?: { message?: string } }
  if (!res.ok) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return fetchChat()
}

export async function retryChatTurn(turnId: string): Promise<ChatState> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/turns/${encodeURIComponent(turnId)}/retry`, {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      schema_version: '1.0', idempotency_key: `web-retry-${crypto.randomUUID()}`,
    }),
  })
  const body = (await res.json().catch(() => ({}))) as { error?: { message?: string } }
  if (!res.ok) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return fetchChat()
}

export async function steerChatTurn(turnId: string, text: string): Promise<ChatState> {
  const t = await sessionToken()
  const res = await fetch(`/api/v1/turns/${encodeURIComponent(turnId)}/steer`, {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      schema_version: '1.0', idempotency_key: `web-steer-${crypto.randomUUID()}`, text,
    }),
  })
  const body = (await res.json().catch(() => ({}))) as { error?: { message?: string } }
  if (!res.ok) throw new Error(body.error?.message || `HTTP ${res.status}`)
  return fetchChat()
}

export type TerminalReply = {
  id: string
  root?: string
  cursor?: number
  output?: string
  alive: boolean
  reset?: boolean
}

export async function terminalRequest(payload: Record<string, unknown>): Promise<TerminalReply> {
  const t = await sessionToken()
  const res = await fetch('/api/terminal', {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  const body = (await res.json().catch(() => ({}))) as TerminalReply & { error?: string }
  if (!res.ok) throw new Error(body.error || `终端请求失败：HTTP ${res.status}`)
  return body
}


export async function materialize(
  manuscript: import('./types').Manuscript,
  template: MaterializeTemplate,
  disclosure: MaterializeDisclosure,
): Promise<{ status: string; item: Deliverable }> {
  const t = await sessionToken()
  const res = await fetch('/api/materialize', {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ manuscript, template, disclosure }),
  })
  const body = (await res.json().catch(() => ({}))) as { error?: string; status?: string; item?: Deliverable }
  if (!res.ok || !body.item) throw new Error(body.error || `HTTP ${res.status}`)
  return { status: body.status ?? 'materialized', item: body.item }
}

export function fileUrl(item: string): string {
  return `/api/file?item=${encodeURIComponent(item)}`
}

export async function fetchFileText(item: string): Promise<{ text: string; sha256: string }> {
  const t = await sessionToken()
  const res = await fetch(fileUrl(item), { headers: { 'X-Popper-Token': t } })
  const body = await res.text()
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try { msg = (JSON.parse(body) as { error?: string }).error ?? msg } catch { /* ignore */ }
    throw new Error(msg)
  }
  const sha256 = res.headers.get('X-Popper-Content-SHA256')
  if (!sha256 || !/^[0-9a-f]{64}$/.test(sha256)) throw new Error('服务端未提供稿件内容摘要')
  return { text: body, sha256 }
}

export async function saveFileText(item: string, content: string, expectedSha256: string): Promise<string> {
  const t = await sessionToken()
  const res = await fetch('/api/file', {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ item, content, expected_sha256: expectedSha256 }),
  })
  const body = (await res.json().catch(() => ({}))) as { error?: string; sha256?: string }
  if (!res.ok) throw new Error(body.error || `写回失败：HTTP ${res.status}`)
  if (!body.sha256 || !/^[0-9a-f]{64}$/.test(body.sha256)) throw new Error('服务端未确认新稿件摘要')
  return body.sha256
}

export type ProjectFileEntry = { path: string; bytes: number; editable: boolean; reason: string | null }
export type ProjectFileList = { workspace_id: string; files: ProjectFileEntry[]; truncated: boolean; limits: { max_bytes: number; max_files: number } }
export type ProjectFileText = { workspace_id: string; path: string; content: string; sha256: string; bytes: number; editable: boolean }
export type ProjectFilePreview = { workspace_id: string; path: string; before_sha256: string; after_sha256: string; before_bytes: number; after_bytes: number; diff: string; changed: boolean }

async function projectFilesRequest<T>(url: string, init: RequestInit = {}): Promise<T> {
  const t = await sessionToken()
  const response = await fetch(url, { ...init, headers: { 'X-Popper-Token': t, ...init.headers } })
  const body = (await response.json().catch(() => ({}))) as {
    data?: T; error?: { message?: string }
  }
  if (!response.ok) throw new Error(body.error?.message || `项目文件请求失败：HTTP ${response.status}`)
  if (!body.data) throw new Error('项目文件响应缺少数据')
  return body.data
}

export function listProjectFiles(workspaceId: string): Promise<ProjectFileList> {
  return projectFilesRequest<ProjectFileList>(`/api/v1/project-files?workspace_id=${encodeURIComponent(workspaceId)}`)
}

export function readProjectFile(workspaceId: string, path: string): Promise<ProjectFileText> {
  return projectFilesRequest<ProjectFileText>(`/api/v1/project-files?workspace_id=${encodeURIComponent(workspaceId)}&path=${encodeURIComponent(path)}`)
}

export function saveProjectFile(workspaceId: string, path: string, content: string, expectedSha256: string): Promise<{ sha256: string; bytes: number }> {
  return projectFilesRequest('/api/v1/project-files', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', workspace_id: workspaceId, path, content, expected_sha256: expectedSha256 }),
  })
}

export function previewProjectFile(workspaceId: string, path: string, content: string, expectedSha256: string): Promise<ProjectFilePreview> {
  return projectFilesRequest('/api/v1/project-files/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', workspace_id: workspaceId, path, content, expected_sha256: expectedSha256 }),
  })
}

export type InitialRevisionSpec = {
  study_id: string; expected_state_version: number; hypothesis_id: string
  design_id: string; path: string; expected_sha256: string; replacement: string
}
export type InitialRevisionPreview = {
  study_id: string; hypothesis_id: string; design_id: string; path: string
  expected_state_version: number; proposal_sha256: string; diff: string; effect: string
}
export type InitialRevisionCreated = {
  revision_id: string; manifest_sha256: string; study_id: string; design_id: string
  status: 'immutable_unexecuted'; proposal_sha256: string
}
export function previewInitialRevision(spec: InitialRevisionSpec): Promise<InitialRevisionPreview> {
  return projectFilesRequest('/api/v1/research/revision-drafts/preview', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', spec }),
  })
}
export function submitInitialRevision(spec: InitialRevisionSpec, proposalSha256: string): Promise<InitialRevisionCreated> {
  return projectFilesRequest('/api/v1/research/revision-drafts/submit', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', spec, proposal_sha256: proposalSha256 }),
  })
}
export async function executeInitialRevision(revisionId: string, manifestSha256: string): Promise<RevisionExecutionJob> {
  const t = await sessionToken()
  const response = await fetch(`/api/v1/revisions/${encodeURIComponent(revisionId)}/execute`, {
    method: 'POST', headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema_version: '1.0', manifest_sha256: manifestSha256 }),
  })
  const body = (await response.json().catch(() => ({}))) as {
    data?: { job: RevisionExecutionJob }; error?: { message?: string }
  }
  if (!response.ok || !body.data) throw new Error(body.error?.message || `HTTP ${response.status}`)
  return body.data.job
}

export async function adjudicate(riskId: string, verdict: 'allow' | 'reject', reason: string): Promise<import('./types').Adjudication> {
  const t = await sessionToken()
  const res = await fetch('/api/adjudicate', {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ risk_id: riskId, verdict, reason }),
  })
  const body = (await res.json().catch(() => ({}))) as { error?: string } & import('./types').Adjudication
  if (!res.ok) throw new Error(body.error || `裁定失败：HTTP ${res.status}`)
  return body as import('./types').Adjudication
}
