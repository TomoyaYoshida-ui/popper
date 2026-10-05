import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ChatState, Workspace } from '../src/types'

const fake = vi.hoisted(() => ({ root: 'A', sid: 's-A', version: 1, failCreation: false,
  patch: null as null | ((patch: Record<string, unknown>) => void), delayedChat: null as Promise<unknown> | null,
  create: vi.fn(), select: vi.fn(), switch: vi.fn() }))
const workspace = () => ({ current_root: fake.root, roots: [{ wid: 'A', root: 'C:/A', name: 'Project A' }, { wid: 'B', root: 'C:/B', name: 'Project B' }] }) as Workspace
const chat = () => ({ workspace_id: fake.root, navigation_version: fake.version, session_id: fake.sid, sessions: [], turns: [], tool_calls: [] }) as unknown as ChatState
vi.mock('../src/api', async (original) => ({ ...await original<typeof import('../src/api')>(),
  fetchWorkspace: async () => workspace(), fetchState: async () => ({ state: {}, terminal_capability: { available: false } }),
  fetchCampaign: async () => null, fetchResearch: async () => ({ runs: [] }), fetchChat: async () => { const delayed = fake.delayedChat; fake.delayedChat = null; return delayed ? await delayed : chat() },
  fetchSessionIndex: async () => ({ projects: [] }),
  subscribeEvents: (patch: (patch: Record<string, unknown>) => void) => { fake.patch = patch; return () => undefined },
  postWorkspaceOp: async (command: { wid: string }) => { fake.switch(command); fake.root = command.wid; fake.sid = `s-${command.wid}`; fake.version++ },
  selectChatSession: async (sid: string, root: string) => { fake.select(sid, root); fake.sid = sid; fake.version++; return chat() },
  createChatSession: async (title: string, root: string) => { fake.create(title, root); fake.sid = 'created'; fake.version++; if (fake.failCreation) throw new Error('创建响应超时'); return chat() },
}))
vi.mock('../src/components/WorkbenchShell', () => ({ WorkbenchShell: (props: {
  workspace: Workspace | null; chat: ChatState | null; transition: string | null; onSwitchProject: (id: string) => void;
  onSelectSession: (sid: string, root: string) => void; onNewSession: (root: string) => void; onRetry: () => void
}) => <div><output data-testid="context">{props.workspace?.current_root}:{props.chat?.session_id}</output><output data-testid="transition">{props.transition}</output>
  <button onClick={() => props.onSwitchProject('B')}>Project B</button><button onClick={() => props.onSwitchProject('A')}>Project A</button>
  <button onClick={() => props.onSelectSession('s-A2', 'A')}>A second session</button>
  <button onClick={() => props.onNewSession('B')}>New in B</button><button onClick={() => props.onNewSession('A')}>New in A</button><button onClick={props.onRetry}>Reload</button></div> }))
import App from '../src/App'

beforeEach(() => { fake.root = 'A'; fake.sid = 's-A'; fake.version = 1; fake.failCreation = false; fake.patch = null; fake.delayedChat = null; fake.switch.mockClear(); fake.create.mockClear(); fake.select.mockClear() })
describe('session switch coordination', () => {
  it('reconciles timed-out creation once and rejects older SSE after reconciliation', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getByTestId('context').textContent).toBe('A:s-A'))
    const stale = chat()
    fake.failCreation = true
    fireEvent.click(screen.getByText('New in A'))
    await waitFor(() => expect(screen.getByTestId('context').textContent).toBe('A:created'))
    expect(fake.create).toHaveBeenCalledOnce()
    act(() => fake.patch?.({ chat: stale }))
    expect(screen.getByTestId('context').textContent).toBe('A:created')
  })
  it('creates once in the explicitly selected project even with repeated clicks', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getByTestId('context').textContent).toBe('A:s-A'))
    fireEvent.click(screen.getByText('New in B')); fireEvent.click(screen.getByText('New in B'))
    await waitFor(() => expect(screen.getByTestId('context').textContent).toBe('B:created'))
    expect(fake.switch).toHaveBeenCalledOnce(); expect(fake.create).toHaveBeenCalledOnce()
    expect(fake.create).toHaveBeenCalledWith('新研究会话', 'B')
  })
  it('rejects SSE snapshots from before same-project session selection', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getByTestId('context').textContent).toBe('A:s-A'))
    const stale = chat()
    fireEvent.click(screen.getByText('A second session'))
    await waitFor(() => expect(screen.getByTestId('context').textContent).toBe('A:s-A2'))
    act(() => fake.patch?.({ chat: stale }))
    expect(screen.getByTestId('context').textContent).toBe('A:s-A2')
    act(() => fake.patch?.({ context: { workspace_id: 'A', navigation_version: 1 }, workspace: { ...workspace(), current_root: 'B' } }))
    expect(screen.getByTestId('context').textContent).toBe('A:s-A2')
  })
  it('discards an old HTTP response after leaving and returning to the same project', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getByTestId('context').textContent).toBe('A:s-A'))
    let finish!: (value: ChatState) => void
    const stale = { ...chat(), session_id: 'late-session' }
    fake.delayedChat = new Promise((resolve) => { finish = resolve })
    fireEvent.click(screen.getByText('Reload'))
    fireEvent.click(screen.getByText('Project B'))
    await waitFor(() => expect(screen.getByTestId('context').textContent).toBe('B:s-B'))
    fireEvent.click(screen.getByText('Project A'))
    await waitFor(() => expect(screen.getByTestId('context').textContent).toBe('A:s-A'))
    await act(async () => { finish(stale) })
    expect(screen.getByTestId('context').textContent).toBe('A:s-A')
  })
})
