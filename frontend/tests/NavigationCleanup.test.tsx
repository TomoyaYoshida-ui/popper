import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { TaskHistory } from '../src/components/TaskHistory'
import { ConfirmProvider, useConfirmation } from '../src/components/ConfirmProvider'
import { defaultLayout, resourceTab, restoreLayout } from '../src/workbench'
import type { Workspace } from '../src/types'

function ConfirmFixture({ decision }: { decision: (ok: boolean) => void }) {
  const confirm = useConfirmation()
  const [resolved, setResolved] = useState('waiting')
  return <><button onClick={() => { void confirm({ title: '执行合成修订？', description: '失败也可能消费预算。', details: [{ label: '对象', value: 'synthetic.py' }] }).then(ok => { decision(ok); setResolved(String(ok)) }) }}>打开确认</button><output>{resolved}</output></>
}
describe('navigation cleanup and contextual confirmations', () => {
  it('migrates obsolete overview tabs, keeps the active destination and does not discard file tabs', () => {
    const layout = defaultLayout()
    const overview = { kind: 'overview', id: 'overview::current', title: '项目概览' }
    const file = resourceTab({ kind: 'file', ref: 'draft.py' })
    const restored = restoreLayout(JSON.stringify({ ...layout, tabs: [layout.tabs[0], overview, file], activeTab: overview.id }), 1440)
    expect(restored.tabs.map(tab => tab.kind)).toEqual(['study', 'file'])
    expect(restored.activeTab).toBe(layout.tabs[0].id)
    expect(resourceTab({ kind: 'quests', title: '任务备忘' }).title).toBe('历史任务')
  })
  it('renders old task data read-only without mutation or project switching controls', () => {
    const workspace = { folders: [{ fid: 'f1', name: '保留文件夹', tasks: [{ quid: 'q1', title: '旧任务', done: true, created: '2026-09-30T01:00:00Z' }] }] } as Workspace
    render(<TaskHistory workspace={workspace} />)
    expect(screen.getByText('旧任务')).toBeTruthy()
    expect(screen.getByText('已完成')).toBeTruthy()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
  })
  it('shows scope, traps focus and cancels without executing when Escape is pressed', async () => {
    const decision = vi.fn()
    render(<ConfirmProvider><ConfirmFixture decision={decision} /></ConfirmProvider>)
    const trigger = screen.getByRole('button', { name: '打开确认' })
    trigger.focus(); fireEvent.click(trigger)
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.getByText('synthetic.py')).toBeTruthy()
    const cancel = screen.getByRole('button', { name: '取消' })
    expect(document.activeElement).toBe(cancel)
    fireEvent.keyDown(cancel, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(screen.getByRole('button', { name: '确认执行' }))
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    await waitFor(() => expect(decision).toHaveBeenCalledWith(false))
    expect(document.activeElement).toBe(trigger)
  })
  it('resolves confirmation only once on a repeated click', async () => {
    const decision = vi.fn()
    render(<ConfirmProvider><ConfirmFixture decision={decision} /></ConfirmProvider>)
    fireEvent.click(screen.getByRole('button', { name: '打开确认' }))
    const approve = screen.getByRole('button', { name: '确认执行' })
    fireEvent.click(approve); fireEvent.click(approve)
    await waitFor(() => expect(decision).toHaveBeenCalledTimes(1))
    expect(decision).toHaveBeenCalledWith(true)
  })
})
import { researchForProject } from '../src/workbench'
import type { ResearchState } from '../src/api'

it('does not present a previous project response as the current research context', () => {
  const state = { runs: [{ run_dir: 'A/run', ok: true, manifest: { project: 'C:\\A', study_id: 'A' } },
    { run_dir: 'B/run', ok: true, manifest: { project: 'c:/B/', study_id: 'B' } }], trusted_local: true, status_labels: {} } as ResearchState
  expect(researchForProject(state, 'C:/B')?.runs.map((run) => run.manifest?.study_id)).toEqual(['B'])
  expect(researchForProject(state, undefined)).toBeNull()
})
