import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ToolDiffPanel } from '../src/components/ToolDiffPanel'
import { toolCallDiff } from '../src/toolCallDiff'
import type { ChatState } from '../src/types'

const history = { call_id: 'preview-call', tool_name: 'research.revision_preview', status: 'completed',
  arguments: { study_id: 'study-source', hypothesis_id: 'hypothesis-source', design_id: 'design-source',
    replacement: 'DO NOT reconstruct the source diff from this argument.' }, preview: undefined,
  result: { preview_call_id: 'preview-call', study_id: 'study-source', hypothesis_id: 'hypothesis-source',
    design_id: 'design-source', path: 'model.py', expected_state_version: 40,
    proposal_sha256: 'a'.repeat(64), diff: '-original source\n+persisted historical source' },
} as unknown as ChatState['tool_calls'][number]

describe('Tool difference source selection and historical receipts', () => {
  it('uses the completed result and preserves source identity and version without adding approval', () => {
    const { container } = render(<ToolDiffPanel call={history} studyId="study-source" />)
    expect(container.querySelector('pre.source-diff')?.textContent).toBe('-original source\n+persisted historical source')
    expect(screen.getByText(/历史预览回执 · 仅预览源码差异/)).toBeTruthy()
    expect(screen.getByText('来源工具调用').nextElementSibling?.textContent).toBe('preview-call')
    expect(screen.getByText('预览状态版本').nextElementSibling?.textContent).toBe('40')
    expect(screen.getByText('原提案摘要').nextElementSibling?.textContent).toBe('a'.repeat(64))
    expect(container.querySelector('details')).toHaveProperty('open', false)
    expect(screen.queryByText(/请回到对话中的工具卡片批准/)).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
  })
  it('prefers an executed historical preview over a newly computed live preview', () => {
    render(<ToolDiffPanel call={{ ...history, preview: { ok: true, revision_preview: {
      path: 'changed.py', study_id: 'study-source', expected_state_version: 41, diff: 'NEW LIVE DIFF' } } }} />)
    expect(screen.queryByText('NEW LIVE DIFF')).toBeNull()
    expect(screen.getByText(/persisted historical source/)).toBeTruthy()
  })
  it('supports legacy completed receipts without a preview_call_id and envelope results', () => {
    const { preview_call_id: _callId, ...legacy } = history.result as Record<string, unknown>
    expect(toolCallDiff({ ...history, result: { data: legacy } })).toMatchObject({
      source: 'completed_preview_result', callId: 'preview-call', studyId: 'study-source', stateVersion: 40 })
  })
  it.each([
    { preview_call_id: 'other-call' }, { study_id: 'other-study' },
    { hypothesis_id: 'other-hypothesis' }, { design_id: 'other-design' },
  ])('rejects mismatched persisted identity %o', (changed) => {
    const bad = { ...history, result: { ...(history.result as Record<string, unknown>), ...changed } }
    expect(toolCallDiff(bad)).toBeNull()
    render(<ToolDiffPanel call={bad} />)
    expect(screen.getByText(/不会根据工具参数重建或猜测源码差异/)).toBeTruthy()
    expect(screen.queryByText(/persisted historical source/)).toBeNull()
  })
  it('refuses a resource tab bound to another Study', () => {
    render(<ToolDiffPanel call={history} studyId="other-study" />)
    expect(screen.queryByText(/persisted historical source/)).toBeNull()
  })
  it.each(['pending_approval', 'approved', 'invalid', 'failed'] as const)('does not use a historical result for status %s', (status) => {
    expect(toolCallDiff({ ...history, status })).toBeNull()
  })
  it('uses live preview for an active approval without accepting an old result', () => {
    const active = { ...history, status: 'pending_approval', preview: { ok: true, revision_preview: {
      path: 'model.py', study_id: 'study-source', diff: '-current\n+proposed', expected_state_version: 42 } } } as ChatState['tool_calls'][number]
    const { container } = render(<ToolDiffPanel call={active} studyId="study-source" />)
    expect(container.querySelector('pre.source-diff')?.textContent).toBe('-current\n+proposed')
    expect(screen.queryByText(/persisted historical source/)).toBeNull()
    expect(screen.getByText(/请回到对话中的工具卡片批准/)).toBeTruthy()
  })
  it('retains existing file edit preview behavior', () => {
    const file = { ...history, status: 'pending_approval', tool_name: 'workspace.file_replace', arguments: { path: 'notes.py' },
      preview: { ok: true, file_edit: { path: 'notes.py', diff: '-before\n+after', before_sha256: 'before', after_sha256: 'after', changed: true } } } as ChatState['tool_calls'][number]
    expect(toolCallDiff(file)).toMatchObject({ kind: 'file', source: 'approval_preview', path: 'notes.py' })
  })
  it('does not invent differences for another tool, a missing receipt, or missing call', () => {
    expect(toolCallDiff({ ...history, tool_name: 'research.revision_create' })).toBeNull()
    expect(toolCallDiff({ ...history, result: undefined })).toBeNull()
    expect(toolCallDiff(undefined)).toBeNull()
    render(<ToolDiffPanel call={undefined} />)
    expect(screen.getByText(/请打开对应会话/)).toBeTruthy()
  })
})
