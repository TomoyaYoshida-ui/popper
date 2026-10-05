import type { ChatState } from './types'

type ToolCall = ChatState['tool_calls'][number]
export type ToolDiff = {
  kind: 'file' | 'revision'; source: 'approval_preview' | 'completed_preview_result'
  callId: string; diff: string; path?: string; studyId?: string; stateVersion?: number; proposalSha256?: string
}
const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}

/** Read the exact call's persisted preview; never reconstruct a diff from source arguments. */
export function toolCallDiff(call: ToolCall | undefined): ToolDiff | null {
  if (!call || call.status === 'invalid') return null
  const historical = call.status === 'completed' && call.tool_name === 'research.revision_preview'
  const result = object(call.result)
  const value = historical ? { ...result, ...object(result.data) }
    : object(call.preview?.file_edit ?? call.preview?.revision_preview)
  if (typeof value.diff !== 'string' || !value.diff) return null
  if (historical && (typeof value.path !== 'string' || typeof value.study_id !== 'string'
      || (value.preview_call_id != null && value.preview_call_id !== call.call_id))) return null
  for (const key of ['study_id', 'hypothesis_id', 'design_id'] as const) {
    if (value[key] != null && call.arguments[key] != null && value[key] !== call.arguments[key]) return null
  }
  const studyId = typeof value.study_id === 'string' ? value.study_id
    : !historical && typeof call.arguments.study_id === 'string' ? call.arguments.study_id : undefined
  return { kind: !historical && call.preview?.file_edit ? 'file' : 'revision',
    source: historical ? 'completed_preview_result' : 'approval_preview', callId: call.call_id,
    diff: value.diff, path: typeof value.path === 'string' ? value.path : undefined, studyId,
    stateVersion: typeof value.expected_state_version === 'number' && Number.isSafeInteger(value.expected_state_version)
      ? value.expected_state_version : undefined,
    proposalSha256: typeof value.proposal_sha256 === 'string' ? value.proposal_sha256 : undefined }
}
