import { useEffect, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { terminalRequest } from '../api'

export function TerminalView() {
  const host = useRef<HTMLDivElement | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [connected, setConnected] = useState(false)
  const [terminalRoot, setTerminalRoot] = useState<string | null>(null)

  useEffect(() => {
    if (!host.current) return
    let disposed = false
    let sessionId: string | null = null
    let cursor = 0
    let timer: number | null = null
    let inputChain = Promise.resolve()
    const term = new Terminal({
      cursorBlink: true,
      fontFamily: 'Cascadia Mono, Consolas, monospace',
      fontSize: 13,
      theme: { background: '#090c10', foreground: '#d6deeb', cursor: '#ff8a3d', selectionBackground: '#314158' },
      scrollback: 5000,
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(host.current)
    fit.fit()
    term.writeln('\x1b[38;5;208mStarting Popper ConPTY terminal…\x1b[0m')

    const poll = async () => {
      if (disposed || !sessionId) return
      try {
        const reply = await terminalRequest({ op: 'read', id: sessionId, cursor })
        if (reply.reset) {
          term.clear()
          term.writeln('\r\n\x1b[33m[earlier terminal output truncated]\x1b[0m')
        }
        if (reply.output) term.write(reply.output)
        cursor = reply.cursor ?? cursor
        if (!reply.alive) {
          term.writeln('\r\n\x1b[31m[process exited]\x1b[0m')
          setConnected(false)
          return
        }
      } catch (reason) {
        if (!disposed) setError((reason as Error).message)
      }
      if (!disposed) timer = window.setTimeout(() => { void poll() }, 60)
    }

    void terminalRequest({ op: 'create', cols: term.cols, rows: term.rows }).then((reply) => {
      if (disposed) {
        void terminalRequest({ op: 'close', id: reply.id })
        return
      }
      sessionId = reply.id
      setTerminalRoot(reply.root ?? null)
      cursor = reply.cursor ?? 0
      if (reply.output) term.write(reply.output)
      setConnected(true)
      void poll()
    }).catch((reason) => setError((reason as Error).message))

    const input = term.onData((data) => {
      if (!sessionId || disposed) return
      inputChain = inputChain.then(() => terminalRequest({ op: 'input', id: sessionId, data })).then(() => undefined)
        .catch((reason) => { if (!disposed) setError((reason as Error).message) })
    })
    const resize = new ResizeObserver(() => {
      fit.fit()
      if (sessionId && !disposed) {
        void terminalRequest({ op: 'resize', id: sessionId, cols: term.cols, rows: term.rows })
          .catch((reason) => setError((reason as Error).message))
      }
    })
    resize.observe(host.current)

    return () => {
      disposed = true
      if (timer != null) window.clearTimeout(timer)
      resize.disconnect()
      input.dispose()
      term.dispose()
      if (sessionId) void terminalRequest({ op: 'close', id: sessionId })
    }
  }, [])

  return (
    <div className="terminal-panel">
      <div className="terminal-bar">
        <span className="terminal-dot red" /><span className="terminal-dot yellow" /><span className="terminal-dot green" />
        <span className="terminal-title" title={terminalRoot ?? undefined}>
          PowerShell · ConPTY{terminalRoot ? ` · ${terminalRoot}` : ''}
        </span>
        <span className={`terminal-state ${connected ? 'on' : ''}`}>{connected ? 'connected' : 'disconnected'}</span>
      </div>
      {error && <div className="terminal-error">{error}</div>}
      <div className="terminal-host" ref={host} />
    </div>
  )
}
