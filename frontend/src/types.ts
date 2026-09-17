// /api/state 的响应类型（与 popper/server.py 契约对齐）

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

export type Mode7Entry = { mode: string; status: 'hit' | 'clean'; evidence: string | null }

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
  job: { status: string; action?: string; error?: string; result?: unknown }
  trusted_local: boolean
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
export type Quest = { quid: string; title: string; created: string; done: boolean }
export type Folder = { fid: string; name: string; created: string; tasks: Quest[] }
export type Deliverable = {
  id: string
  filename: string
  template: string
  disclosure: string | null
  created: string
  bytes: number
}
export type Workspace = {
  schema_version: string
  meta: { next_quest_counter: number; seed?: { folder_name: string; phase?: string | null; seeded_at: string } }
  folders: Folder[]
  deliverables: Deliverable[]
  // 稿件模板与 AI 披露口径中文标签：后端单一下发，前端不再硬编码或回退
  template_labels: Record<string, string>
  disclosure_labels: Record<string, string>
  // 合法模板与披露口径取值清单：后端单一下发，前端不再硬编码
  valid_templates: string[]
  valid_disclosures: string[]
}
export type Section = { heading: string; body: string }
export type Manuscript = { title: string; abstract: string; sections: Section[] }