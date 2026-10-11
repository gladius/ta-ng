import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { get } from '../api'
import { usecaseName } from '../cache'
import { count, Failed, Loading, money, Page, pct, when } from '../ui'

export type Verdict = { requests: number; within: number; outside: number; unverified: number; pass_rate: number; cost_vs_original_pct: number | null; latency_vs_original_pct: number | null; fit_rate: number | null; verdict: 'passes' | 'drifts' | 'not verified' }
export type Check = {
  id: string; created_at: string; trigger: string; usecase: string | null; site: string | null; preset: string; candidates: string[]
  samples: Record<string, { id: string; why: string; prompt_tokens: number; completion_tokens: number }[]>
  job_id: string | null; max_budget_usd: number | null; status: string; error: string | null
  summary: Record<string, Record<string, Verdict>> | null; finished_at: string | null
}
type CheckPage = Check & { job: { id: string; status: string; items_total: number; items_done: number; spend_usd: number; stop_reason: string | null } | null; site_names: Record<string, string> }

export const PRESET: Record<string, string> = { cheaper: 'cheaper tier of each call-site\'s model', open_weight: 'open-weight swap in the same tier', release: 'a release (one named model)', custom: 'models you name' }
const STATUS: Record<string, string> = { sampling: 'Sampling', queued: 'Queued', running: 'Running', done: 'Done', failed: 'Failed', stopped: 'Stopped' }

export function VerdictChip({ v }: { v: Verdict }) {
  return <span className={'chip ' + (v.verdict === 'passes' ? 'cause-ok' : v.verdict === 'drifts' ? 'cause-dynamic_first' : '')}>{v.verdict} · {v.within} of {v.requests}</span>
}

/** Start a check for a usecase or a call-site: preset, candidates, samples, budget, with the estimate. */
export function CheckDialog({ usecase, site, onStarted }: { usecase: string | null; site?: string; onStarted?: (id: string) => void }) {
  const [open, setOpen] = useState(false)
  const [preset, setPreset] = useState('cheaper')
  const [models, setModels] = useState('')
  const [samples, setSamples] = useState(8)
  const [budget, setBudget] = useState(10)
  const nCand = preset === 'custom' || preset === 'release' ? Math.max(1, models.split(',').filter((x) => x.trim()).length) : 1
  const est = useQuery({ queryKey: ['check-estimate', usecase, site, samples, nCand], enabled: open,
    queryFn: () => get<{ usd: number }>(`/suite/checks/estimate?${new URLSearchParams({ usecase: usecase ?? 'None', ...(site && { site }), samples: String(samples), candidates: String(nCand) })}`) })
  const start = useMutation({
    mutationFn: () => get<Check>('/suite/checks', { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ usecase: usecase ?? 'None', site, preset, candidates: models.split(',').map((x) => x.trim()).filter(Boolean), samples, max_budget_usd: budget }) }),
    onSuccess: (c) => { setOpen(false); onStarted?.(c.id) },
  })
  if (!open) return <button className="btn primary" onClick={() => setOpen(true)}>Check models…</button>
  return (
    <div className="panel pad dialog">
      <h2>Check models {site ? 'for this call-site' : 'for every call-site of this usecase'}</h2>
      <p className="muted">Samples each call-site's requests by size (smallest, largest, the quartiles, the heaviest answers, a few random), re-runs the original and each candidate on them, and judges the answers with evidence. Nothing in production changes.</p>
      <div className="row">
        <label>Candidates <select className="input" value={preset} onChange={(e) => setPreset(e.target.value)}>{Object.entries(PRESET).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
        {(preset === 'custom' || preset === 'release') && <label>Models <input className="input" placeholder="gpt-5.5, claude-haiku-4-5" value={models} onChange={(e) => setModels(e.target.value)} /></label>}
        <label>Samples per call-site <input className="input" type="number" min={1} max={50} value={samples} onChange={(e) => setSamples(Number(e.target.value))} /></label>
        <label>Budget, $ <input className="input" type="number" min={1} step={1} value={budget} onChange={(e) => setBudget(Number(e.target.value))} /></label>
      </div>
      <p className="muted">Estimated cost: {est.data ? money(est.data.usd, 2) : '…'} (the budget stops the check once reached).</p>
      {start.isError && <p className="error" role="alert">{start.error instanceof Error ? start.error.message : String(start.error)}</p>}
      <div className="row"><button className="btn primary" disabled={start.isPending} onClick={() => start.mutate()}>Start</button><button className="btn" onClick={() => setOpen(false)}>Cancel</button></div>
    </div>
  )
}

/** Every check, newest first. */
export function Checks() {
  const q = useQuery({ queryKey: ['checks'], queryFn: () => get<Check[]>('/suite/checks'),
    refetchInterval: (query) => (query.state.data?.some((c) => ['sampling', 'queued', 'running'].includes(c.status)) ? 5000 : false) })
  const crumbs = [{ label: 'Models' }, { label: 'Checks' }]
  return (
    <Page crumbs={crumbs}>
      <div className="head"><div><h1>Model checks</h1><span className="muted">would a cheaper, open-weight or newly released model hold on a usecase's call-sites? Started from a usecase or call-site page, by the API, or by the router.</span></div></div>
      {q.isPending ? <Loading /> : q.isError ? <Failed error={q.error} /> : !q.data.length ? <p className="empty">No checks yet. Open a usecase and press "Check models…".</p> : (
        <section className="panel">
          <table className="grid">
            <thead><tr><th>Check</th><th>Scope</th><th>Candidates</th><th>Status</th><th>Result</th><th>Started</th></tr></thead>
            <tbody>
              {q.data.map((c) => {
                const vs = c.summary ? Object.values(c.summary).flatMap((by) => Object.values(by)) : []
                return (
                  <tr key={c.id}>
                    <td className="mono"><Link to={`/checks/${c.id}`}>{c.id}</Link><div className="muted">{c.trigger}</div></td>
                    <td><Link to={`/usecases/${encodeURIComponent(c.usecase ?? 'None')}`}>{usecaseName(c.usecase)}</Link>{c.site && <div className="muted">one call-site</div>}</td>
                    <td>{PRESET[c.preset]}{c.candidates.length > 0 && <div className="mono muted">{c.candidates.join(', ')}</div>}</td>
                    <td><span className={'pill pill-' + (c.status === 'done' ? 'done' : c.status === 'failed' ? 'failed' : 'running')}>{STATUS[c.status] ?? c.status}</span>{c.error && <div className="muted">{c.error.slice(0, 80)}</div>}</td>
                    <td>{vs.length ? `${vs.filter((v) => v.verdict === 'passes').length} of ${vs.length} pass` : <span className="muted">—</span>}</td>
                    <td className="muted nowrap">{when(c.created_at)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </section>
      )}
    </Page>
  )
}

/** One check: per call-site and candidate, the verdict with the evidence link. */
export function CheckDetail() {
  const { checkId = '' } = useParams()
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['checks', checkId], queryFn: () => get<CheckPage>(`/suite/checks/${checkId}`),
    refetchInterval: (query) => (['sampling', 'queued', 'running'].includes(query.state.data?.status ?? '') ? 5000 : false) })
  const crumbs = [{ label: 'Models' }, { label: 'Checks', to: '/checks' }, { label: checkId }]
  if (q.isPending) return <Page crumbs={crumbs}><Loading /></Page>
  if (q.isError) return <Page crumbs={crumbs}><Failed error={q.error} /></Page>
  const c = q.data
  const sites = Object.keys(c.samples)
  return (
    <Page crumbs={crumbs}>
      <div className="head">
        <div><h1>Check {c.id}</h1><span className="muted">{usecaseName(c.usecase)} · {PRESET[c.preset]}{c.candidates.length > 0 && ` (${c.candidates.join(', ')})`} · started {when(c.created_at)} by {c.trigger}</span></div>
        <div className="row"><span className={'pill pill-' + (c.status === 'done' ? 'done' : c.status === 'failed' ? 'failed' : 'running')}>{STATUS[c.status] ?? c.status}</span>{c.job && <Link className="btn" to={`/validations/${c.job.id}`}>Evidence (job {c.job.id})</Link>}</div>
      </div>
      {c.error && <p className="error" role="alert">{c.error}</p>}
      {c.job && <p className="muted">{c.job.items_done} of {c.job.items_total} sampled requests done · spent {money(c.job.spend_usd, 2)}{c.max_budget_usd != null && ` of ${money(c.max_budget_usd, 2)}`}{c.job.stop_reason && ` · ${c.job.stop_reason}`}</p>}
      {sites.map((st) => {
        const by = c.summary?.[st] ?? {}
        return (
          <section key={st} className="panel">
            <div className="panel-head"><b><Link to={`/usecases/${encodeURIComponent(c.usecase ?? 'None')}/callsites/${st}`}>{c.site_names[st]}</Link></b><span className="muted">{count(c.samples[st].length, 'sampled request')}: {[...new Set(c.samples[st].map((s) => s.why))].join(', ')}</span></div>
            {Object.keys(by).length ? (
              <table className="grid">
                <thead><tr><th>Candidate</th><th>Verdict</th><th className="num">Pass rate</th><th className="num">Fit of answers</th><th className="num">Cost vs original</th><th className="num">Latency vs original</th></tr></thead>
                <tbody>
                  {Object.entries(by).map(([cand, v]) => (
                    <tr key={cand}><td className="mono">{cand}</td><td><VerdictChip v={v} /></td><td className="num mono">{pct(v.pass_rate)}</td><td className="num mono">{v.fit_rate == null ? '—' : pct(v.fit_rate)}</td>
                      <td className="num mono">{v.cost_vs_original_pct == null ? '—' : (v.cost_vs_original_pct > 0 ? '+' : '') + v.cost_vs_original_pct + '%'}</td>
                      <td className="num mono">{v.latency_vs_original_pct == null ? '—' : (v.latency_vs_original_pct > 0 ? '+' : '') + v.latency_vs_original_pct + '%'}</td></tr>
                  ))}
                </tbody>
              </table>
            ) : <p className="muted pad">{c.status === 'done' ? 'No candidate was run for this call-site (no cheaper or open-weight model for its original in the catalog).' : 'Waiting for the job…'}</p>}
          </section>
        )
      })}
      <button className="btn" onClick={() => qc.invalidateQueries({ queryKey: ['checks'] })}>Refresh</button>
    </Page>
  )
}
