export type HandoffFields = Record<'goal' | 'constraints' | 'decisions' | 'todo' | 'blockers' | 'next_step', string>
export const HANDOFF_LABELS: Record<keyof HandoffFields, string> = { goal: '任务目标', constraints: '已确认的约束', decisions: '已确认的决定', todo: '待办', blockers: '阻塞与限制', next_step: '下一步' }
export type HandoffSource = { session_id: string; turn_id: string; version: string; kind: string; user_excerpt: string; agent_suggestion: string; status: string; truncated: boolean }
export type HandoffFact = { study_id: string; name: string; state_version: number; status: string; observations: number; claims: Array<{ claim_id: string; status: string }> }
export type ContextRevision = { task_id: string; title: string; status: 'active' | 'completed'; version: number; revision: number; updated: string; body: { fields: HandoffFields; sources: HandoffSource[]; study_ids: string[] } }
export type ContextBinding = { version: number; task_id: string | null; revision: number | null }
export type TaskContext = { workspace_id: string; tasks: ContextRevision[]; sessions: Array<{ session_id: string; title: string; updated: string; status: string }>; binding: ContextBinding; selected: ContextRevision | null; stale?: boolean; facts: HandoffFact[] }
export type ContextPreview = { task_id: string | null; title: string; expected_task_version: number; expected_binding_version: number; source_session_id?: string; source_version?: string; preview_sha256: string; fields: HandoffFields; sources: HandoffSource[]; facts: HandoffFact[]; study_ids: string[]; quality: string; notice?: string }
