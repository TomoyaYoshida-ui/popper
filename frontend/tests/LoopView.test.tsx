import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import * as api from '../src/api'
import type { ResearchState } from '../src/api'
import { LoopView } from '../src/components/LoopView'

vi.mock('../src/api', async (original) => ({ ...await original<typeof api>(),
  fetchArbor: vi.fn(async () => ({ trees: [], runs: [], node_status_labels: {} })),
  fetchBoundDrafts: vi.fn(async () => []), fetchDesignJobs: vi.fn(async () => []),
  fetchRevisionWorktrees: vi.fn(async () => ({ worktrees: [], jobs: [], registries: [], job_registries: [] })),
  createBoundDraft: vi.fn(), fetchResearchEvidence: vi.fn() }))
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
})
