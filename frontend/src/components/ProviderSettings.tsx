import { useEffect, useRef, useState } from 'react'
import { fetchProviderConfig, saveProviderConfig, testProviderConfig, type ProviderConfig } from '../api'
import { currencyToMicros, microsToCurrency } from '../money'

export function ProviderSettings({ workspaceId }: { workspaceId: string }) {
  const [open, setOpen] = useState(false)
  const [config, setConfig] = useState<ProviderConfig | null>(null)
  const [endpoint, setEndpoint] = useState('')
  const [model, setModel] = useState('')
  const [key, setKey] = useState('')
  const [tokens, setTokens] = useState('')
  const [cost, setCost] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const identity = useRef(workspaceId)
  identity.current = workspaceId
  const first = useRef<HTMLInputElement>(null)
  useEffect(() => {
    const show = () => { setOpen(true); setMessage('') }
    window.addEventListener('popper:provider-settings', show)
    return () => window.removeEventListener('popper:provider-settings', show)
  }, [])
  useEffect(() => { setOpen(false); setKey(''); setConfig(null) }, [workspaceId])
  useEffect(() => {
    if (!open) return
    const origin = workspaceId
    let alive = true
    setBusy(true)
    fetchProviderConfig().then((value) => {
      if (!alive || identity.current !== origin) return
      setConfig(value); setEndpoint(value.base_url); setModel(value.model)
      setTokens(value.token_budget?.toString() ?? ''); setCost(microsToCurrency(value.cost_budget_micros))
      first.current?.focus()
    }).catch((reason) => { if (alive) setMessage((reason as Error).message) })
      .finally(() => { if (alive) setBusy(false) })
    const previousFocus = document.activeElement as HTMLElement | null
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setOpen(false); setKey(''); return }
      if (event.key !== 'Tab') return
      const dialog = document.querySelector('.provider-dialog')
      const fields = dialog ? [...dialog.querySelectorAll<HTMLElement>('input, button:not(:disabled)')] : []
      const firstField = fields[0], lastField = fields[fields.length - 1]
      if (event.shiftKey && document.activeElement === firstField) { event.preventDefault(); lastField?.focus() }
      else if (!event.shiftKey && document.activeElement === lastField) { event.preventDefault(); firstField?.focus() }
    }
    window.addEventListener('keydown', close)
    return () => { alive = false; window.removeEventListener('keydown', close); if (previousFocus?.isConnected) previousFocus.focus() }
  }, [open, workspaceId])
  if (!open) return null
  const save = async () => {
    const origin = workspaceId
    try {
      if (tokens && !/^[1-9]\d*$/.test(tokens)) throw new Error('Token 预算须为正整数')
      const amount = currencyToMicros(cost)
      setBusy(true); setMessage('')
      const value = await saveProviderConfig({ workspace_id: config?.workspace_id ?? workspaceId,
        base_url: endpoint, model, ...(key ? { api_key: key } : {}),
        token_budget: tokens ? Number(tokens) : null, cost_budget_micros: amount })
      if (identity.current !== origin) return
      setConfig(value); setKey(''); setMessage('配置已保存，后续请求使用新配置。')
      window.dispatchEvent(new Event('popper:provider-changed'))
    } catch (reason) { if (identity.current === origin) setMessage((reason as Error).message) }
    finally { if (identity.current === origin) setBusy(false) }
  }
  const probe = async () => {
    setBusy(true); setMessage('')
    try { await testProviderConfig(); setMessage('连接测试通过。') }
    catch (reason) { setMessage((reason as Error).message) }
    finally { setBusy(false) }
  }
  return <div className="provider-overlay"><section className="box provider-dialog" role="dialog" aria-modal="true" aria-labelledby="provider-title">
    <h2 id="provider-title">模型与预算</h2>
    {config?.source === 'cli' && <p className="notice">当前使用命令行配置。保存的是工作区默认配置；显式命令行参数优先。</p>}
    <label>API 端点<input ref={first} value={endpoint} onChange={(event) => setEndpoint(event.target.value)} placeholder="https://…/v1" /></label>
    <label>模型名称<input value={model} onChange={(event) => setModel(event.target.value)} placeholder="qwen3.8-flash" /></label>
    <label>本次运行的 API 密钥<input type="password" autoComplete="off" value={key} onChange={(event) => setKey(event.target.value)} placeholder={config?.credential_available ? '已有可用凭据，留空沿用' : '输入 API 密钥'} /></label>
    <p className="muted">密钥仅保存在本次服务进程中。重启后使用环境凭据或重新输入。</p>
    <label>新会话 Token 预算<input inputMode="numeric" value={tokens} onChange={(event) => setTokens(event.target.value)} placeholder="留空使用会话默认值" /></label>
    <label>费用预算<input inputMode="decimal" value={cost} onChange={(event) => setCost(event.target.value)} placeholder="例如 10.50" /></label>
    <p className="muted">费用预算需要会话定价表才能执行；保存金额不会自动建立定价表。</p>
    {message && <p role="status">{message}</p>}
    <div className="provider-actions"><button type="button" disabled={busy} onClick={() => { setOpen(false); setKey('') }}>关闭</button>
      <button type="button" disabled={busy || !config?.configured} onClick={() => { void probe() }}>测试已保存配置</button>
      <button type="button" disabled={busy || !endpoint || !model} onClick={() => { void save() }}>保存配置</button></div>
  </section></div>
}
