import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ProviderSettings } from '../src/components/ProviderSettings'
import { fetchProviderConfig, saveProviderConfig, testProviderConfig } from '../src/api'

vi.mock('../src/api', () => ({ fetchProviderConfig: vi.fn(), saveProviderConfig: vi.fn(), testProviderConfig: vi.fn() }))
const configured = { schema_version: '1.0', base_url: 'https://provider.example/v1', model: 'configured-model',
  configured: true, credential_available: true, credential_storage: 'process_or_environment_only', source: 'workspace',
  workspace_id: 'workspace-a', token_budget: 50000, cost_budget_micros: 10500000 }

describe('provider settings', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(fetchProviderConfig).mockResolvedValue(configured)
    vi.mocked(saveProviderConfig).mockResolvedValue(configured)
    vi.mocked(testProviderConfig).mockResolvedValue({ ok: true, model: configured.model })
  })
  it('saves normal currency amounts and clears the process-only key', async () => {
    render(<ProviderSettings workspaceId="workspace-a" />)
    act(() => window.dispatchEvent(new Event('popper:provider-settings')))
    await screen.findByDisplayValue('configured-model')
    fireEvent.change(screen.getByLabelText('本次运行的 API 密钥'), { target: { value: 'example-process-key' } })
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
    await waitFor(() => expect(saveProviderConfig).toHaveBeenCalledWith(expect.objectContaining({
      api_key: 'example-process-key', token_budget: 50000, cost_budget_micros: 10500000 })))
    await waitFor(() => expect((screen.getByLabelText('本次运行的 API 密钥') as HTMLInputElement).value).toBe(''))
    expect(localStorage.getItem('api_key')).toBeNull()
    expect(testProviderConfig).not.toHaveBeenCalled()
  })
  it('closes and discards credentials when the workspace changes', async () => {
    const view = render(<ProviderSettings workspaceId="workspace-a" />)
    act(() => window.dispatchEvent(new Event('popper:provider-settings')))
    await screen.findByDisplayValue('configured-model')
    fireEvent.change(screen.getByLabelText('本次运行的 API 密钥'), { target: { value: 'temporary' } })
    view.rerender(<ProviderSettings workspaceId="workspace-b" />)
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
