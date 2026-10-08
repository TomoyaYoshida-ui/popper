// /api/state 的响应类型（与 popper/server.py 契约对齐）

export type EntityRef = { kind: string; id: string }

export type Metric = { name: string; direction: 'min' | 'max' }

export type RunResult = {
  run_id: string
  split: 'dev' | 'test'
  config: Record<string, unknown>
  mean: number
  std: number
  n_seeds: number
  per_seed: { seed: number; value: number }[]
  metric: Metric
  environment?: Record<string, unknown>
  evaluator_id?: string
  evaluator_hash?: string
  elapsed_seconds?: number
  trust?: string
}

export type RunRow = { id: string; split: string; status: string }

export type Event = { seq: number; kind: string; payload: Record<string, unknown> }

export type RiskEntry = {
  risk_id: string
  status: 'pass' | 'warn' | 'fail'
  reason: string
  provisional?: boolean
  user_adjudication?: boolean
}

export type Mode7Entry = { mode: string; status: 'hit' | 'clean' | 'unknown'; evidence: string | null }

export type Review = {
  rejection_side: {
    risks: Record<string, RiskEntry>
    summary: { counts: Record<string, number>; status: string }
  }
  fraud_side: {
    modes: Mode7Entry[]
    summary: { count: number; hits: string[]; status: string }
  }
  adjudications?: Record<string, { verdict: 'allow' | 'reject'; reason: string; at: string }>
}

export type Adjudication = { verdict: 'allow' | 'reject'; reason: string; at: string }

export type Claim = {
  claim_id: string
  status: string
  delta: number
  unit: string
  threshold: number
  evidence_ids: string[]
  scope: string
}

export type Spec = {
  name: string
  objective: string
  metric: Metric
  baseline: Record<string, unknown>
  budget: number
  min_improvement: number
  seeds?: number[]
  candidates?: Record<string, unknown>[]
}

export type ExperimentState = {
  phase: string
  spec: Spec
  claim: Claim | null
  selected?: Record<string, unknown>
  selected_dev_run?: string
  environment?: Record<string, unknown>
  evaluator_id?: string
  evaluator_hash?: string
}

export type PopperState = {
  mode: 'experiment' | 'reproduction'
  state: ExperimentState | Record<string, unknown>
  results: RunResult[]
  runs: RunRow[]
  events: Event[]
  review: Review | null
  paper?: { title: string; authors: string[]; year: number; doi: string; claim: { id: string; text: string; reported_value: number; metric: { name: string; unit: string } } }
  protocol?: { target_claim: { absolute_tolerance: number }; dataset: { name: string } }
  result?: Record<string, unknown> | null
  artifacts?: Array<{ path: string; sha256: string; bytes: number }>
  research_projection?: {
    kind: 'reproduction'; study: { entity_id: string; question: string; status: string }
    runs: Array<{ entity_id: string; status: string; revision_id: string }>
    observations: Array<{ entity_id: string; observed_value: number; reported_value: number; absolute_difference: number; absolute_tolerance: number; outcome: string; metric: { name: string; unit: string } }>
    decisions: Array<{ entity_id: string; action: string; observation_refs: string[] }>
    experiment_graph: { nodes: Array<{ kind: string; id: string }>; edges: unknown[]; integrity: { ok: boolean; issues: Array<{ code: string; detail?: string }> } }
  }
  job: { status: string; action?: string; error?: string; result?: unknown }
  trusted_local: boolean
  terminal_capability: { available: boolean; reason: string | null }
  // 实验状态机阶段标签：后端 popper/server.py PHASES 单一定义下发
  phases: Record<string, string>
  // 事件 kind→中文标签：后端单一下发，前端不再硬编码或回退
  event_names: Record<string, string>
  // campaign 阶段状态→中文标签：后端单一下发，前端不再硬编码或回退
  stage_status_labels: Record<string, string>
  // 评审门禁 R1-R7 / 状态 / 7-mode 中文标签：后端单一下发，前端不再硬编码或回退
  risk_names: Record<string, string>
  gate_status_labels: Record<string, string>
  mode7_names: Record<string, string>
}

// /api/workspace 与 /api/materialize（与 popper/workspace.py、server.py 契约对齐）
export type Quest = { quid: string; entity_ref: EntityRef; title: string; created: string; done: boolean }
export type Folder = { fid: string; name: string; created: string; tasks: Quest[] }
export type Deliverable = {
  id: string
  filename: string
  template: string
  disclosure: string | null
  created: string
  bytes: number
}
// 多工作区根：注册表由后端持久化（主根 = serve 的项目目录，永在不可移除）
export type WorkspaceRoot = {
  wid: string
  root: string
  name: string
  primary: boolean
  registered: string
  current: boolean
  entity_ref: EntityRef
}
export type Workspace = {
  environment_label?: string | null
  schema_version: string
  meta: { next_quest_counter: number; state_version: number; seed?: { folder_name: string; phase?: string | null; seeded_at: string } }
  folders: Folder[]
  deliverables: Deliverable[]
  // 多工作区：已登记根清单 + 当前根 wid（Quest/产物跟随当前根）
  roots: WorkspaceRoot[]
  current_root: string
  // 稿件模板与 AI 披露口径中文标签：后端单一下发，前端不再硬编码或回退
  template_labels: Record<string, string>
  disclosure_labels: Record<string, string>
  // 合法模板与披露口径取值清单：后端单一下发，前端不再硬编码
  valid_templates: string[]
  valid_disclosures: string[]
}
export type Section = { heading: string; body: string }
export type Manuscript = { title: string; abstract: string; sections: Section[] }

// /api/chat：由持久化 Agent Session 投影为兼容聊天消息
export type ChatMessage = {
  id: string
  role: 'user' | 'assistant'
  text: string
  ts: string
  error?: { type: string; message: string }
}
export type ChatState = {
  navigation_version?: number
  workspace_id?: string | null
  configured: boolean
  model: string | null
  event_integrity: { ok: boolean; events: number; head?: string; reason?: string }
  tool_event_integrity: { ok: boolean; events: number; head?: string; reason?: string }
  session_id: string | null
  // 续推理协调状态独立于工具和科研作业终态；旧响应可以省略。
  continuation?: {
    state: 'waiting_approval' | 'waiting_job' | 'ready' | 'blocked'
    reason?: string
    source_turn_id?: string
    root_turn_id?: string
    correction_attempts?: number
    max_corrections?: number
  }
  sessions: Array<{
    session_id: string
    title: string
    harness: string
    harness_version: string | null
    model: string | null
    token_budget: number | null
    price_catalog_id: string | null
    cost_budget_micros: number | null
    status: 'active' | 'archived'
    created: string
    updated: string
  }>
  turns: Array<{
    turn_id: string
    session_id: string
    idempotency_key: string
    actor: string
    trace_id: string
    parent_turn_id: string | null
    user_message: string
    status: 'running' | 'completed' | 'failed' | 'interrupted'
    assistant_message: string | null
    assistant_draft: string
    request_progress?: { state: 'waiting' | 'receiving' | 'retrying' | 'failed' | 'completed'; elapsed_seconds: number } | null
    error: { type: string; message: string } | null
    cancel_requested: number
    created: string
    updated: string
  }>
  jobs: Array<{
    job_id: string
    session_id: string
    turn_id: string
    status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'
    attempt: number
    max_attempts: number
    worker_id: string | null
    lease_expires: string | null
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    model_name: string | null
    price_catalog_id: string | null
    cost_micros: number
    reserved_cost_micros: number
    error: { type: string; message: string } | null
    created: string
    updated: string
  }>
  usage: {
    jobs: number
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    reserved_tokens: number
    token_budget: number | null
    remaining_tokens: number | null
    cost_micros: number
    currency: string | null
    reserved_cost_micros: number
    cost_budget_micros: number | null
    remaining_cost_micros: number | null
  }
  pricing_catalogs: Array<{
    catalog_id: string
    name: string
    currency: string
    prices: Array<{
      model: string
      prompt_micros_per_million: number
      completion_micros_per_million: number
    }>
    actor: string
    created: string
  }>
  harnesses: Array<{
    name: string
    display_name: string
    protocol_version: string
    capabilities: string[]
    model: string | null
  }>
  tool_calls: Array<{
    call_id: string
    session_id: string
    turn_id: string
    tool_name: string
    arguments: Record<string, unknown>
    risk: 'read' | 'disclose' | 'write' | 'execute'
    status: 'pending_approval' | 'approved' | 'rejected' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted' | 'invalid'
    executed?: boolean
    original_tool_name?: string
    raw_arguments?: unknown
    validation_error?: { type?: string; code?: string; message: string; original_tool_name?: string; raw_arguments?: unknown; executed?: boolean }
    proposed_by: string
    trace_id: string
    decided_by: string | null
    decision_reason: string | null
    result: unknown
    preview?: { revision_preview?: Record<string, unknown>; ok: boolean; reason?: string | null; file_list?: Record<string, unknown>; file_read?: Record<string, unknown>; file_edit?: { path: string; diff: string; before_sha256: string; after_sha256: string; changed: boolean }; state_version?: number; version_matches?: boolean; design?: Record<string, unknown> | null; research_status?: Record<string, unknown> | null; study_action?: Record<string, unknown> | null; bound_draft?: Record<string, unknown> | null; job?: Record<string, unknown> | null; approval_matches?: boolean; binding?: Record<string, unknown> | null; observation?: Record<string, unknown> | null; run?: Record<string, unknown> | null; verification_level?: string; limitations?: string[]; design_sha256?: string | null; design_sha256_matches?: boolean; budget_effect?: string; hypothesis?: Record<string, unknown> | null; hypothesis_version_matches?: boolean; study?: Record<string, unknown> | null; proposal?: Record<string, unknown> | null; proposal_sha256?: string | null; execution_ready?: boolean; execution_readiness?: { controller_routing_ready: boolean; execution_ready: boolean; registered_candidate_match?: string | null; registered_design_route_ready?: boolean; registered_design_reasons?: string[]; reasons: string[]; not_checked: string[] } }
    error: { type: string; message: string } | null
    created: string
    updated: string
  }>
  messages: ChatMessage[]
  // 界面文案（未配置提示/空态/AI 标注/错误标题/composer 占位）：后端单一下发
  labels: Record<string, string>
}
