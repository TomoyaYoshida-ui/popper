import { useRef } from 'react'
import type { ChatState } from './types'

/** Reject duplicate writes synchronously and discard responses from a previous context. */
export function useChatRequests(root: string | undefined, session: string | null | undefined,
  onChat: (chat: ChatState) => void, onError: (message: string | null) => void,
  onPending: (pending: boolean) => void) {
  const current = useRef({ root, session, rootEpoch: 0, sessionEpoch: 0 })
  if (current.current.root !== root) current.current.rootEpoch++
  if (current.current.session !== session) current.current.sessionEpoch++
  current.current.root = root; current.current.session = session
  const active = useRef(new Set<string | undefined>())
  return async (request: () => Promise<ChatState>, changesSession = false, reportError = onError): Promise<boolean> => {
    const context = { ...current.current }
    if (active.current.has(context.root)) return false
    active.current.add(context.root)
    onPending(true)
    const sameContext = () => current.current.rootEpoch === context.rootEpoch
      && (changesSession || current.current.sessionEpoch === context.sessionEpoch)
    try {
      const next = await request()
      if (sameContext()) { onChat(next); reportError(null) }
      return true
    } catch (error) {
      if (sameContext()) reportError((error as Error).message)
      return false
    } finally {
      active.current.delete(context.root)
      if (current.current.root === context.root) onPending(false)
    }
  }
}
