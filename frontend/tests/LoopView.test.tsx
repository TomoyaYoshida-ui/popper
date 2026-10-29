import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import * as api from '../src/api'
import type { ResearchState } from '../src/api'
import { LoopView } from '../src/components/LoopView'

vi.mock('../src/api', async (original) => ({ ...await original<typeof api>(),
  fetchArbor: vi.fn(async () => ({ trees: [], runs: [], node_status_labels: {} })),
  fetchBoundDrafts: vi.fn(async () => []), fetchDesignJobs: vi.fn(async () => []),
  fetchRevisionWorktrees: vi.fn(async () => ({ worktrees: [], jobs: [], registries: [], job_registries: [] })),
  createBoundDraft: vi.fn(), fetchResearchEvidence: vi.fn(),
  writeResumeCheckpoint: vi.fn() }))
vi.mock('../src/components/StudyLifecyclePanel', () => ({ StudyLifecyclePanel: () => <div>研究下一步</div> }))
vi.mock('../src/components/ConfirmProvider', () => ({ useConfirmation: () => vi.fn(async () => true) }))
const base = { workspaceKey: 'project-A', selectedRunDir: null, onSelectRun: vi.fn(), onResearchChanged: vi.fn() }
describe('Study summary and resource navigation', () => {
  beforeEach(() => { vi.clearAllMocks() })
  it('distinguishes legacy project results from Research Claims without an overview page', () => {
    render(<LoopView {...base} research={{ runs: [], status_labels: {}, trusted_local: true }}
      projectSummary={{ name: 'Wisconsin', phaseLabel: '实验完成', legacyResult: { delta: 0.0263, threshold: 0.02, scope: '固定划分' } }} />)
    expect(screen.getByRole('heading', { name: '尚未登记 Research Study' })).toBeTruthy()
    expect(screen.getByText(/不是 Research Study 的独立确认 Claim/)).toBeTruthy()
    expect(screen.getByText('项目实验状态：实验完成')).toBeTruthy()
    expect(screen.getAllByText('popper demo smoke').length).toBeGreaterThan(0)
    expect(screen.getByText(/--trusted-local/)).toBeTruthy()
    expect(api.fetchArbor).not.toHaveBeenCalled()
  })
  it('opens generated bound drafts by explicit Study identity and keeps the raw technical panels collapsed', async () => {
    const open = vi.fn()
    const run = { ok: true, run_dir: 'C:/synthetic/study-1', state_version: 2, manifest: { study_id: 'S-synthetic', project: 'C:/synthetic' },
      study: { status: 'concluded', question: 'Synthetic ODE question' }, claims: [{ entity_id: 'clm_one', status: 'supported_in_scope', metric: 'convergence_order',
        oriented_delta: 1, min_meaningful_effect: 0.5, observation_refs: ['obs_one'], decision_id: 'dec_one' }],
      observations: [], revisions: [], hypotheses: [], decisions: [], reflections: [], budget: { cap: 4, spent: 4, reserved: 0, available: 0 } }
    const research = { runs: [run], status_labels: { study: { concluded: '已结论' } }, trusted_local: true } as unknown as ResearchState
    vi.mocked(api.createBoundDraft).mockResolvedValue({ status: 'draft_needs_review', draft_id: 'draft_one', claim_count: 1,
      draft_dir: 'draft', evidence_dir: 'evidence', draft_spec_path: 'spec.json', manuscript_path: 'manuscript.md', manuscript: '# immutable', publication_attested: false })
    render(<LoopView {...base} research={research} onOpenResource={open} />)
    expect(screen.getByText(/技术信息、源码与溯源/).closest('details')?.open).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: '保存可审校草稿' }))
    await waitFor(() => expect(open).toHaveBeenCalledWith({ kind: 'manuscript', ref: 'draft_one', studyId: 'S-synthetic', title: '研究结果草稿' }))
    expect(screen.queryByLabelText('人工审校说明')).toBeNull()
    expect(screen.queryByText('# immutable')).toBeNull()
    const claimLink = screen.getByRole('button', { name: 'convergence_order 结论 ↗' })
    expect(claimLink.title).toBe('clm_one')
    expect(screen.getByText('clm_one').closest('details')?.open).toBe(false)
    expect(screen.getByText('obs_one').closest('details')?.open).toBe(false)
    expect(screen.getByText('dec_one').closest('details')?.open).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: '在证据标签核验 ↗' }))
    expect(open).toHaveBeenCalledWith({ kind: 'evidence', studyId: 'S-synthetic', title: '研究证据' })
    expect(api.fetchResearchEvidence).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: '核验证据并生成附录' })).toBeNull()
  })
  it('never substitutes another Study while an explicit target is still loading', () => {
    const old = { ok: true, run_dir: 'C:/synthetic/old', manifest: { study_id: 'S-old' }, study: { question: 'Wrong old study', status: 'active' } }
    render(<LoopView {...base} requestedStudyId="S-new" research={{ runs: [old], trusted_local: true, status_labels: {} } as unknown as ResearchState} />)
    expect(screen.getByText('尚未读取到指定 Study')).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'Wrong old study' })).toBeNull()
    expect(screen.queryByText('研究下一步')).toBeNull()
  })
  it('shows the current project and real lifecycle phase while keeping identifiers in folded details', () => {
    const run = { ok: true, run_dir: 'C:/synthetic/study-1', state_version: 7, events_total: 12,
      manifest: { study_id: 'S-phase', project: 'C:/synthetic' }, lifecycle_phase: 'ready_for_confirmation',
      study: { question: 'A long research question should remain readable body text', status: 'active' },
      claims: [], observations: [], hypotheses: [], revisions: [], decisions: [], reflections: [] }
    render(<LoopView {...base} research={{ runs: [run], trusted_local: true, status_labels: { study: { active: '进行中' } } } as unknown as ResearchState}
      projectSummary={{ name: 'Synthetic project', description: 'Project context' }} />)
    expect(screen.getByText('当前项目 · Synthetic project')).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'study-1' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: run.study.question })).toBeNull()
    expect(within(screen.getByLabelText('Study 摘要')).getByText('待独立确认')).toBeTruthy()
    expect(within(screen.getByLabelText('Study 摘要')).queryByText('进行中')).toBeNull()
    const details = screen.getByText('S-phase').closest('details')
    expect(details?.open).toBe(false)
    expect(within(details!).getByText('v7')).toBeTruthy()
    expect(within(details!).getByText('12 条')).toBeTruthy()
  })
  it('surfaces status_brief read_as and treats inconclusive as evidence gap not failure', () => {
    const run = {
      ok: true, run_dir: 'C:/synthetic/study-brief', state_version: 3, events_total: 4,
      manifest: { study_id: 'S-brief', project: 'C:/synthetic' }, lifecycle_phase: 'concluded',
      study: { question: 'Brief summary question', status: 'concluded' },
      claims: [], observations: [{ entity_id: 'obs_1' }], hypotheses: [], revisions: [], decisions: [], reflections: [],
      status_brief: {
        phase: 'concluded', study_status: 'concluded', integrity_ok: true, observations: 1,
        latest_judgment: { scope: 'confirmation', new_judgment: 'inconclusive', reason: 'delta below threshold' },
        claim_status: null, budget: null,
        read_as: '演示完成：phase=concluded 且 integrity_ok。科研判断与描述性 claim 可能不一致；以 judgment 三态为准，inconclusive 不是失败。',
        resume: { last_runtime_step: null, last_action_kind: null, checkpoint: null },
      },
    }
    render(<LoopView {...base} research={{ runs: [run], trusted_local: true, status_labels: { study: { concluded: '已结论' } } } as unknown as ResearchState} />)
    expect(within(screen.getByLabelText('Study 摘要')).getByText('通过')).toBeTruthy()
    expect(within(screen.getByLabelText('Study 摘要')).getByText('证据不足（不是失败）')).toBeTruthy()
    expect(screen.getByRole('status').textContent).toMatch(/inconclusive 不是失败/)
    expect(screen.queryByRole('note')).toBeNull()
  })
  it('surfaces resume checkpoint as unverified note and hides empty resume', () => {
    const withCheckpoint = {
      ok: true, run_dir: 'C:/synthetic/study-resume', state_version: 1, events_total: 1,
      manifest: { study_id: 'S-resume', project: 'C:/synthetic' }, lifecycle_phase: 'researching',
      study: { question: 'Resume question', status: 'active' },
      claims: [], observations: [], hypotheses: [], revisions: [], decisions: [], reflections: [],
      status_brief: {
        phase: 'researching', study_status: 'active', integrity_ok: true, observations: 0,
        latest_judgment: null, claim_status: null, budget: null,
        read_as: '尚未结束：继续 research run / confirm。',
        resume: { last_runtime_step: 2, last_action_kind: 'execute_candidate', checkpoint: 'next: confirm' },
      },
    }
    const { rerender } = render(<LoopView {...base} research={{ runs: [withCheckpoint], trusted_local: true, status_labels: { study: { active: '进行中' } } } as unknown as ResearchState} />)
    expect(screen.getByRole('note').textContent).toMatch(/续作意图（未验证笔记）：next: confirm/)
    expect(screen.getByRole('note').textContent).toMatch(/上次动作 execute_candidate/)
    expect(screen.getByRole('note').textContent).toMatch(/步进 2/)
    const emptyResume = {
      ...withCheckpoint,
      status_brief: {
        ...withCheckpoint.status_brief,
        resume: { last_runtime_step: null, last_action_kind: null, checkpoint: null },
      },
    }
    rerender(<LoopView {...base} research={{ runs: [emptyResume], trusted_local: true, status_labels: { study: { active: '进行中' } } } as unknown as ResearchState} />)
    expect(screen.queryByRole('note')).toBeNull()
  })
  it('writes resume checkpoint via bounded form and surfaces save errors', async () => {
    const onResearchChanged = vi.fn()
    const run = {
      ok: true, run_dir: 'C:/synthetic/study-resume-write', state_version: 1, events_total: 0,
      manifest: { study_id: 'S-write', project: 'C:/synthetic' }, lifecycle_phase: 'researching',
      study: { question: 'Write question', status: 'active' },
      claims: [], observations: [], hypotheses: [], revisions: [], decisions: [], reflections: [],
      status_brief: {
        phase: 'researching', study_status: 'active', integrity_ok: true, observations: 0,
        latest_judgment: null, claim_status: null, budget: null,
        read_as: '尚未结束：继续 research run / confirm。',
        resume: { last_runtime_step: null, last_action_kind: null, checkpoint: null },
      },
    }
    vi.mocked(api.writeResumeCheckpoint).mockResolvedValueOnce({
      id: 'MEM-abc', text: 'next: confirm', run_dir: run.run_dir, study_id: 'S-write',
      trust: 'unverified_session_notes',
    })
    render(<LoopView {...base} onResearchChanged={onResearchChanged}
      research={{ runs: [run], trusted_local: true, status_labels: { study: { active: '进行中' } } } as unknown as ResearchState} />)
    expect(screen.getByText('未验证笔记，不能当证据')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('续作意图'), { target: { value: 'next: confirm' } })
    fireEvent.click(screen.getByRole('button', { name: '保存续作意图' }))
    await waitFor(() => expect(api.writeResumeCheckpoint).toHaveBeenCalledWith({
      study_id: 'S-write', text: 'next: confirm',
    }))
    await waitFor(() => expect(onResearchChanged).toHaveBeenCalled())
    vi.mocked(api.writeResumeCheckpoint).mockRejectedValueOnce(new Error('续作意图不能为空'))
    fireEvent.change(screen.getByLabelText('续作意图'), { target: { value: 'retry' } })
    fireEvent.click(screen.getByRole('button', { name: '保存续作意图' }))
    await waitFor(() => expect(screen.getByText('续作意图不能为空')).toBeTruthy())
  })
})
