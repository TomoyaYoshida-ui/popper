import type { PopperState } from './types'

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
  state?: PopperState
  workspace?: WorkspaceState
  campaign?: Campaign
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
  })
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

export async function fetchArbor(): Promise<{ trees: ArborTree[]; runs: string[]; node_status_labels: Record<string, string> }> {
  const t = await sessionToken()
  const res = await fetch('/api/arbor', { headers: { 'X-Popper-Token': t } })
  const body = (await res.json()) as { trees: ArborTree[]; runs: string[]; node_status_labels: Record<string, string> } & { error?: string }
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`)
  return body
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

export type MaterializeTemplate = 'md' | 'tex' | 'docx' | 'pdf'
export type MaterializeDisclosure = 'nature' | 'acm' | 'ieee' | null

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

export async function fetchFileText(item: string): Promise<string> {
  const t = await sessionToken()
  const res = await fetch(fileUrl(item), { headers: { 'X-Popper-Token': t } })
  const body = await res.text()
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try { msg = (JSON.parse(body) as { error?: string }).error ?? msg } catch { /* ignore */ }
    throw new Error(msg)
  }
  return body
}

export async function saveFileText(item: string, content: string): Promise<void> {
  const t = await sessionToken()
  const res = await fetch('/api/file', {
    method: 'POST',
    headers: { 'X-Popper-Token': t, 'Content-Type': 'application/json' },
    body: JSON.stringify({ item, content }),
  })
  const body = (await res.json().catch(() => ({}))) as { error?: string }
  if (!res.ok) throw new Error(body.error || `写回失败：HTTP ${res.status}`)
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