import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ProjectSessionTree } from '../src/components/ProjectSessionTree'
import { filterProjects, recentSessions, type SessionIndex } from '../src/sessionNavigation'
import { defaultLayout, openTab, projectLayoutKey, restoreProjectLayout } from '../src/workbench'

const index: SessionIndex = { projects: ['A', 'B'].map((id) => ({ workspace_id: id, name: `项目 ${id}`, error: null,
  sessions: [{ workspace_id: id, session_id: `session-${id}`, title: `研究 ${id}`, status: 'active', updated: `2026-09-30T00:00:0${id === 'A' ? 1 : 2}Z`, running: id === 'A', pending_approvals: id === 'B' ? 1 : 0 }] })) }
const props = { index, currentProject: 'A', currentSession: 'session-A', loading: false, error: null, busy: false, onRetry: vi.fn(), onOpen: vi.fn(), onCreate: vi.fn() }
describe('project-owned session navigation', () => {
  it('expands without switching and opens children with explicit ownership', () => {
    const open = vi.fn(), create = vi.fn()
    render(<ProjectSessionTree {...props} onOpen={open} onCreate={create} />)
    fireEvent.click(screen.getByRole('button', { name: '展开 项目 B 的会话' }))
    expect(open).not.toHaveBeenCalled()
    fireEvent.keyDown(screen.getByRole('button', { name: '收起 项目 B 的会话' }), { key: 'ArrowLeft' })
    expect(screen.getByRole('button', { name: '展开 项目 B 的会话' }).getAttribute('aria-expanded')).toBe('false')
    fireEvent.keyDown(screen.getByRole('button', { name: '展开 项目 B 的会话' }), { key: 'ArrowRight' })
    fireEvent.click(screen.getAllByRole('button', { name: /研究 B/ })[0])
    expect(open).toHaveBeenCalledWith('B', 'session-B')
    fireEvent.click(screen.getByRole('button', { name: '项目 B', exact: true }))
    expect(open).toHaveBeenLastCalledWith('B')
    fireEvent.click(screen.getByRole('button', { name: '在 项目 B 新建会话' }))
    expect(create).toHaveBeenCalledWith('B')
    expect(screen.getAllByText('运行中')[0]).toBeTruthy()
    expect(screen.getAllByText('待审批')[0]).toBeTruthy()
    expect(screen.getByText('最近会话').closest('details')?.open).toBe(false)
  })
  it('searches across projects without losing attribution and limits recents to ten', () => {
    render(<ProjectSessionTree {...props} />)
    fireEvent.change(screen.getByLabelText('搜索项目与会话'), { target: { value: '研究 B' } })
    expect(screen.getByRole('button', { name: '项目 B', exact: true })).toBeTruthy()
    expect(screen.queryByRole('button', { name: '项目 A', exact: true })).toBeNull()
    expect(filterProjects(index, '项目 A')[0].sessions).toHaveLength(1)
    const large = { projects: [{ ...index.projects[0], sessions: Array.from({ length: 15 }, (_, i) => ({ ...index.projects[0].sessions[0], session_id: `${i}` })) }] }
    expect(recentSessions(large)).toHaveLength(10)
  })
  it('distinguishes loading, no results and per-project read failures', () => {
    const view = render(<ProjectSessionTree {...props} loading />)
    expect(screen.getByRole('status').textContent).toContain('正在读取')
    expect(screen.queryByText('尚无会话')).toBeNull()
    view.rerender(<ProjectSessionTree {...props} index={{ projects: [{ ...index.projects[0], sessions: [], error: '读取失败' }] }} />)
    expect(screen.getByText('读取失败')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('搜索项目与会话'), { target: { value: 'unmatched' } })
    expect(screen.getByText('没有匹配的项目或会话')).toBeTruthy()
  })
  it('keeps rename input on conflict and prevents duplicate mutation clicks', async () => {
    let reject!: (reason: Error) => void
    const change = vi.fn(() => new Promise<void>((_resolve, failed) => { reject = failed }))
    render(<ProjectSessionTree {...props} onChange={change} />)
    fireEvent.click(screen.getAllByLabelText('研究 A 的操作')[0])
    fireEvent.click(screen.getAllByText('重命名')[0])
    fireEvent.change(screen.getByLabelText('会话名称'), { target: { value: '新标题' } })
    fireEvent.click(screen.getByText('保存名称')); fireEvent.submit(screen.getByLabelText('会话名称').closest('form')!)
    expect(change).toHaveBeenCalledOnce()
    reject(new Error('版本冲突'))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('版本冲突'))
    expect((screen.getByLabelText('会话名称') as HTMLInputElement).value).toBe('新标题')
    expect((screen.getAllByText('归档会话')[0] as HTMLButtonElement).disabled).toBe(true)
  })
  it('offers restore for archived sessions but never permanent deletion', () => {
    render(<ProjectSessionTree {...props} onChange={vi.fn()} index={{ projects: [{ ...index.projects[0], sessions: [{ ...index.projects[0].sessions[0], status: 'archived', running: false }] }] }} />)
    fireEvent.click(screen.getByText('已归档 (1)'))
    fireEvent.click(screen.getByLabelText('研究 A 的操作'))
    expect(screen.getByText('恢复会话')).toBeTruthy()
    expect(screen.queryByText('删除会话')).toBeNull()
  })
  it('disambiguates equal project names using registered directories', () => {
    render(<ProjectSessionTree {...props} index={{ projects: index.projects.map((project) => ({ ...project, name: '同名项目', root: `C:/fixture-${project.workspace_id}` })) }} />)
    fireEvent.click(screen.getByRole('button', { name: '同名项目 · fixture-B', exact: true }))
    expect(props.onOpen).toHaveBeenCalledWith('B')
    expect(screen.getByText('fixture-A')).toBeTruthy()
  })
})
describe('project layout migration', () => {
  it('prioritizes selected legacy preferences, merges by recency and leaves legacy keys intact', () => {
    const first = { ...openTab(defaultLayout(), { kind: 'file', ref: 'a.py' }), chatWidth: 520, selectedStudy: 'ODE', maximized: true }
    const second = openTab(openTab(defaultLayout(), { kind: 'file', ref: 'a.py' }), { kind: 'file', ref: 'b.py' })
    localStorage.setItem('popper-workbench:v1:A:s1', JSON.stringify(first))
    localStorage.setItem('popper-workbench:v1:A:s2', JSON.stringify(second))
    const result = restoreProjectLayout(localStorage, 'A', 's1', [{ session_id: 's2', updated: 'newer' }, { session_id: 's1', updated: 'older' }], 1440)
    expect(result.chatWidth).toBe(520); expect(result.activeTab).toBe(first.activeTab); expect(result.selectedStudy).toBe('ODE')
    expect(result.tabs.map((tab) => tab.ref).filter(Boolean)).toEqual(['a.py', 'b.py'])
    expect(result.mobileView).toBe('work'); expect(result.maximized).toBe(true)
    expect(localStorage.getItem('popper-workbench:v1:A:s2')).toBe(JSON.stringify(second))
    expect(localStorage.getItem(projectLayoutKey('A'))).toBeNull()
    localStorage.setItem(projectLayoutKey('A'), JSON.stringify(result))
    expect(restoreProjectLayout(localStorage, 'A', 's2', [], 760).chatWidth).toBe(520)
  })
})
