import { act, renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { useChatRequests } from '../src/useChatRequests'
import type { ChatState } from '../src/types'
describe('Chat request context', () => {
  it('never revives a late request after leaving and returning to the same project', async () => {
    const update = vi.fn()
    const hook = renderHook(({ root }) => useChatRequests(root, 'S', update, vi.fn(), vi.fn()), { initialProps: { root: 'A' } })
    let finish!: (chat: ChatState) => void, first!: Promise<boolean>
    act(() => { first = hook.result.current(() => new Promise(resolve => { finish = resolve })) })
    hook.rerender({ root: 'B' }); hook.rerender({ root: 'A' })
    await act(async () => { finish({ session_id: 'S' } as ChatState); await first })
    expect(update).not.toHaveBeenCalled()
  })
  it('deduplicates writes and ignores late responses after session switching', async () => {
    const update = vi.fn(), error = vi.fn(), pending = vi.fn()
    const hook = renderHook(({ session }) => useChatRequests('project', session, update, error, pending),
      { initialProps: { session: 'old' } })
    let finish!: (chat: ChatState) => void
    const request = vi.fn(() => new Promise<ChatState>(resolve => { finish = resolve }))
    let first!: Promise<boolean>
    act(() => { first = hook.result.current(request) })
    expect(await hook.result.current(request)).toBe(false)
    expect(request).toHaveBeenCalledTimes(1)
    hook.rerender({ session: 'new' })
    await act(async () => { finish({ session_id: 'old' } as ChatState); await first })
    expect(update).not.toHaveBeenCalled()
  })
  it('does not put an old project error into the new project', async () => {
    const error = vi.fn(), update = vi.fn()
    const hook = renderHook(({ root }) => useChatRequests(root, 'S', update, error, vi.fn()), { initialProps: { root: 'old' } })
    let fail!: (error: Error) => void, first!: Promise<boolean>
    act(() => { first = hook.result.current(() => new Promise((_, reject) => { fail = reject })) })
    hook.rerender({ root: 'new' })
    await act(async () => { fail(new Error('old failure')); await first })
    expect(error).not.toHaveBeenCalled()
  })
})
