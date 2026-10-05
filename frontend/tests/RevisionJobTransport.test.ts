import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

describe('Revision Job HTTP envelope', () => {
  beforeEach(() => { vi.resetModules() })
  afterEach(() => { vi.unstubAllGlobals() })
  async function read(data: unknown) {
    const fetch = vi.fn(async (path: string) => ({ ok: true, json: async () => path === '/api/session' ? { token: 'synthetic-local-token' } : { data } }))
    vi.stubGlobal('fetch', fetch)
    const { fetchRevisionJob } = await import('../src/api')
    const result = await fetchRevisionJob('JOBR-test')
    expect(fetch.mock.calls[1][0]).toBe('/api/v1/revision-jobs/JOBR-test')
    return result
  }
  it('keeps optional host diagnostics from the real envelope instead of dropping them with job extraction', async () => {
    const diagnostics = { scope: 'dev', study_id: 'S-test', job_id: 'JOBR-test', evidence_sha256: 'hash', candidate: { trajectory_error: 62885 } }
    expect(await read({ job: { job_id: 'JOBR-test', status: 'succeeded' }, development_diagnostics: diagnostics }))
      .toEqual({ job_id: 'JOBR-test', status: 'succeeded', development_diagnostics: diagnostics })
  })
  it('keeps a local diagnostic failure without replacing the real job state', async () => {
    const error = { reason: 'trusted receipt changed', verified: false, scope: 'dev' }
    expect(await read({ job: { job_id: 'JOBR-test', status: 'failed' }, development_diagnostics_error: error }))
      .toEqual({ job_id: 'JOBR-test', status: 'failed', development_diagnostics_error: error })
  })
  it('retains the existing response shape when optional fields are absent', async () => {
    const job = { job_id: 'JOBR-test', status: 'running', progress: 50 }
    expect(await read({ job })).toEqual(job)
  })
})
