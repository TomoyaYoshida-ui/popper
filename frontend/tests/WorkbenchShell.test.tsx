import { StrictMode, useEffect, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ChatState, Workspace } from '../src/types'
import type { EditorController } from '../src/workbench'
import { defaultLayout, openTab } from '../src/workbench'
import { WorkbenchShell } from '../src/components/WorkbenchShell'

const lifecycle = { mounted: vi.fn(), unmounted: vi.fn() }
vi.mock('../src/components/TerminalView', () => ({ TerminalView: () => {
  useEffect(() => { lifecycle.mounted(); return () => { lifecycle.unmounted() } }, [])
  return <div>live shell session</div>
} }))
const workspace = { current_root: 'project-A', roots: [{ wid: 'project-A', root: 'C:/A', name: 'Project A' }, { wid: 'project-B', root: 'C:/B', name: 'Project B' }] } as Workspace
const chat = { session_id: 'session-A', sessions: [{ session_id: 'session-A', title: 'Research chat', status: 'active', updated: '2026-09-30T00:00:00Z' }] } as ChatState
function FakeEditor({ register, save, discard }: { register: (controller: EditorController) => void; save: () => Promise<boolean>; discard: () => Promise<void> }) {
  const [text, setText] = useState('base')
  useEffect(() => { register({ dirty: text !== 'base', label: 'model.py', save, discard }) })
  return <input aria-label="file content" value={text} onChange={(event) => setText(event.target.value)} />
}
function fixture(save = vi.fn(async () => true)) {
  const discard = vi.fn(async () => undefined)
  const switchProject = vi.fn(async () => undefined)
  render(<WorkbenchShell workspace={workspace} chat={chat} research={null} error={null} connected terminalAvailable
    onRetry={vi.fn()} onDismissError={vi.fn()} onNewSession={vi.fn(async () => undefined)} onSelectSession={vi.fn(async () => undefined)} onSwitchProject={switchProject} onMaterialize={vi.fn()}
    renderChat={(_context, open) => <button type="button" onClick={() => open({ kind: 'file', ref: 'model.py' })}>open model</button>}
    renderTab={(tab, context) => tab.kind === 'file' ? <FakeEditor register={context.registerEditor} save={save} discard={discard} /> : <div>study content</div>}
    renderLogs={() => <p>execution logs</p>} renderAudit={() => <p>audit records</p>} />)
  return { save, discard, switchProject }
}
describe('workbench interactions', () => {
  it('reveals the active tab after opening a resource and after viewport resizing', () => {
    const previous = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')
    const scroll = vi.fn()
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scroll })
    try {
      fixture()
      scroll.mockClear()
      fireEvent.click(screen.getByText('open model'))
      expect(scroll).toHaveBeenCalledWith({ block: 'nearest', inline: 'nearest', behavior: 'auto' })
      scroll.mockClear()
      fireEvent(window, new Event('resize'))
      expect(scroll).toHaveBeenCalledOnce()
      expect(screen.getByRole('tab', { name: /model.py/ }).getAttribute('aria-selected')).toBe('true')
    } finally {
      if (previous) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', previous)
      else Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView')
    }
  })
  it('shares the right explorer across file tabs and hides it without remounting the editor', () => {
    fixture()
    fireEvent.click(screen.getByText('open model'))
    const editor = screen.getByLabelText('file content')
    fireEvent.change(editor, { target: { value: 'unsaved' } })
    const explorer = screen.getByRole('complementary', { name: '项目文件目录' })
    fireEvent.click(screen.getByRole('button', { name: '收起文件目录' }))
    expect(explorer.hidden).toBe(true)
    expect(screen.getByLabelText('file content')).toBe(editor)
    fireEvent.click(screen.getByRole('button', { name: '展开文件目录' }))
    expect(explorer.hidden).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: '项目文件', exact: true }))
    expect(screen.getByRole('tab', { name: /打开文件/ })).toBeTruthy()
    expect(screen.getByRole('complementary', { name: '项目文件目录' })).toBe(explorer)
    fireEvent.click(screen.getByText('open model'))
    expect(screen.getAllByRole('tab', { name: /model.py/ })).toHaveLength(1)
    expect(screen.getByLabelText('file content')).toBe(editor)
    expect((editor as HTMLInputElement).value).toBe('unsaved')
  })
  it('keeps session positioning separate from sidebar expansion and reuses Study navigation', () => {
    fixture()
    const expand = screen.getByRole('button', { name: '展开会话列表' })
    fireEvent.click(screen.getByRole('button', { name: 'Agent 会话' }))
    expect(screen.getByRole('button', { name: '展开会话列表' })).toBe(expand)
    fireEvent.click(expand)
    fireEvent.click(screen.getByRole('button', { name: 'Agent 会话' }))
    expect(screen.getByRole('button', { name: '收起侧栏' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '返回当前 Study' }))
    expect(screen.getAllByRole('tab', { name: 'Study' })).toHaveLength(1)
    expect(screen.queryByText('项目概览')).toBeNull()
  })
  it('supports arrow-key tab navigation and traps focus in the unsaved dialog', async () => {
    fixture()
    fireEvent.click(screen.getByText('open model'))
    const model = screen.getByRole('tab', { name: /model.py/ })
    model.focus(); fireEvent.keyDown(model, { key: 'ArrowLeft' })
    expect(screen.getByRole('tab', { name: 'Study' }).getAttribute('aria-selected')).toBe('true')
    expect(document.activeElement).toBe(screen.getByRole('tab', { name: 'Study' }))
    fireEvent.click(model)
    fireEvent.change(screen.getByLabelText('file content'), { target: { value: 'changed' } })
    fireEvent.click(screen.getByRole('button', { name: '关闭 model.py' }))
    const cancel = screen.getByRole('button', { name: '取消' })
    cancel.focus(); fireEvent.keyDown(cancel, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(screen.getByRole('button', { name: '保存后继续' }))
    fireEvent.keyDown(document.activeElement!, { key: 'Tab' })
    expect(document.activeElement).toBe(cancel)
  })
  it('restores stored tabs under StrictMode without overwriting them on mount', async () => {
    const stored = openTab(defaultLayout(), { kind: 'file', ref: 'restored.py' })
    localStorage.setItem('popper-workbench:v1:project-A:session-A', JSON.stringify(stored))
    render(<StrictMode><WorkbenchShell workspace={workspace} chat={chat} research={null} error={null} connected terminalAvailable={false}
      onRetry={vi.fn()} onDismissError={vi.fn()} onNewSession={vi.fn(async () => undefined)} onSelectSession={vi.fn(async () => undefined)} onSwitchProject={vi.fn(async () => undefined)} onMaterialize={vi.fn()}
      renderChat={() => <p>chat</p>} renderTab={(tab) => <p>{tab.title}</p>} renderLogs={() => null} renderAudit={() => null} /></StrictMode>)
    expect(screen.getByRole('tab', { name: /restored.py/ })).toBeTruthy()
    await waitFor(() => expect(JSON.parse(localStorage.getItem('popper-workbench:v1:project-A:session-A')!).activeTab).toBe(stored.activeTab))
  })
  it('keeps edits when switching tags and lets cancel or discard protect closure', async () => {
    const { discard } = fixture()
    fireEvent.click(screen.getByText('open model'))
    fireEvent.change(screen.getByLabelText('file content'), { target: { value: 'changed' } })
    fireEvent.click(screen.getByRole('tab', { name: 'Study' }))
    fireEvent.click(screen.getByRole('tab', { name: /model.py/ }))
    expect((screen.getByLabelText('file content') as HTMLInputElement).value).toBe('changed')
    fireEvent.click(screen.getByRole('button', { name: '关闭 model.py' }))
    expect(screen.getByRole('dialog')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '关闭 model.py' }))
    fireEvent.click(screen.getByRole('button', { name: '放弃修改' }))
    await waitFor(() => expect(screen.queryByRole('tab', { name: /model.py/ })).toBeNull())
    expect(discard).toHaveBeenCalledOnce()
  })
  it('does not close a file when saving requires review or fails', async () => {
    fixture(vi.fn(async () => false))
    fireEvent.click(screen.getByText('open model'))
    fireEvent.change(screen.getByLabelText('file content'), { target: { value: 'changed' } })
    fireEvent.click(screen.getByRole('button', { name: '关闭 model.py' }))
    fireEvent.click(screen.getByRole('button', { name: '保存后继续' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByRole('tab', { name: /model.py/ })).toBeTruthy()
    expect(screen.getByRole('status').textContent).toContain('核对差异')
  })
  it('guards project switching before any server mutation', async () => {
    const { switchProject } = fixture()
    fireEvent.click(screen.getByText('open model'))
    fireEvent.change(screen.getByLabelText('file content'), { target: { value: 'changed' } })
    fireEvent.click(screen.getByRole('button', { name: '展开会话列表' }))
    fireEvent.click(screen.getByRole('button', { name: 'Project B', exact: true }))
    expect(switchProject).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '取消', exact: true }))
    expect(switchProject).not.toHaveBeenCalled()
    expect((screen.getByLabelText('file content') as HTMLInputElement).value).toBe('changed')
    fireEvent.click(screen.getByRole('button', { name: 'Project B', exact: true }))
    fireEvent.click(screen.getByRole('button', { name: '放弃修改' }))
    await waitFor(() => expect(switchProject).toHaveBeenCalledWith('project-B'))
  })
  it('retains the terminal instance when collapsed or showing logs', async () => {
    lifecycle.mounted.mockClear(); lifecycle.unmounted.mockClear(); fixture()
    fireEvent.click(screen.getByRole('button', { name: '终端', exact: true }))
    await waitFor(() => expect(screen.getByText('live shell session')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: '收起底部面板' }))
    expect(lifecycle.unmounted).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '运行日志', exact: true }))
    expect(screen.getByText('execution logs')).toBeTruthy()
    expect(lifecycle.mounted).toHaveBeenCalledOnce()
    expect(lifecycle.unmounted).not.toHaveBeenCalled()
  })
  it('preserves editor DOM, dirty contents and terminal when only the session changes', async () => {
    lifecycle.mounted.mockClear(); lifecycle.unmounted.mockClear()
    const props = { workspace, research: null, error: null, connected: true, terminalAvailable: true,
      onRetry: vi.fn(), onDismissError: vi.fn(), onNewSession: vi.fn(async () => undefined), onSelectSession: vi.fn(async () => undefined), onSwitchProject: vi.fn(async () => undefined), onMaterialize: vi.fn(),
      renderChat: () => <p>chat</p>, renderTab: (_tab: unknown, context: { registerEditor: (controller: EditorController) => void }) => <FakeEditor register={context.registerEditor} save={async () => true} discard={async () => undefined} />,
      renderLogs: () => null, renderAudit: () => null }
    const view = render(<WorkbenchShell {...props} chat={chat} />)
    fireEvent.change(screen.getByLabelText('file content'), { target: { value: 'unsaved' } })
    const input = screen.getByLabelText('file content')
    fireEvent.click(screen.getByRole('button', { name: '终端', exact: true }))
    await waitFor(() => expect(lifecycle.mounted).toHaveBeenCalledOnce())
    view.rerender(<WorkbenchShell {...props} chat={{ ...chat, session_id: 'other-session' }} />)
    expect(screen.getByLabelText('file content')).toBe(input)
    expect((input as HTMLInputElement).value).toBe('unsaved')
    expect(lifecycle.unmounted).not.toHaveBeenCalled()
    expect(lifecycle.mounted).toHaveBeenCalledOnce()
    expect(screen.getByLabelText('有未保存内容')).toBeTruthy()
  })
})
