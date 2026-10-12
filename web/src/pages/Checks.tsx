import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { get, type JobStatus, type ResultsPage } from '../api'
import { usecaseName } from '../cache'
import { AnswersBar, count, Failed, Legend, Loading, money, ms, Page, pct, Squares, verdictWord, VsBar, when } from '../ui'

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

/** A release check: named models against every analysed usecase (or the top N), one check per usecase. */
function ReleaseDialog({ onStarted }: { onStarted: () => void }) {
  const [open, setOpen] = useState(false)
  const [models, setModels] = useState('')
  const [top, setTop] = useState(20)
  const [samples, setSamples] = useState(8)
  const [budget, setBudget] = useState(5)
  const start = useMutation({
    mutationFn: () => get<{ checks: string[]; usecases: string[] }>('/suite/checks/release', { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ candidates: models.split(',').map((x) => x.trim()).filter(Boolean), top, samples, max_budget_usd: budget }) }),
    onSuccess: () => { setOpen(false); onStarted() },
  })
  if (!open) return <button className="btn primary" onClick={() => setOpen(true)}>Check a release…</button>
  return (
    <div className="panel pad dialog">
      <h2>Check a release across usecases</h2>
      <p className="muted">One check per analysed usecase, biggest first: the named model against each call-site's current model on sampled requests. The budget is per usecase.</p>
      <div className="row">
        <label>Models <input className="input" placeholder="gpt-5.5" value={models} onChange={(e) => setModels(e.target.value)} /></label>
        <label>Top usecases by calls <input className="input" type="number" min={1} max={500} value={top} onChange={(e) => setTop(Number(e.target.value))} /></label>
        <label>Samples per call-site <input className="input" type="number" min={1} max={50} value={samples} onChange={(e) => setSamples(Number(e.target.value))} /></label>
        <label>Budget per usecase, $ <input className="input" type="number" min={1} step={1} value={budget} onChange={(e) => setBudget(Number(e.target.value))} /></label>
      </div>
      {start.isError && <p className="error" role="alert">{start.error instanceof Error ? start.error.message : String(start.error)}</p>}
      <div className="row"><button className="btn primary" disabled={start.isPending || !models.trim()} onClick={() => start.mutate()}>Start</button><button className="btn" onClick={() => setOpen(false)}>Cancel</button></div>
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
      <div className="head"><div><h1>Model checks</h1><span className="muted">would a cheaper, open-weight or newly released model hold on a usecase's call-sites? Started from a usecase or call-site page, here for a release, by the API, or by the router.</span></div><ReleaseDialog onStarted={() => q.refetch()} /></div>
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

/** One check: per call-site, the same evidence the validation page shows: answers by category, the original's
    own agreement, cost and latency against it, and every sampled request with why it was picked and how each
    candidate did, linking to the request's review. */
export function CheckDetail() {
  const { checkId = '' } = useParams()
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['checks', checkId], queryFn: () => get<CheckPage>(`/suite/checks/${checkId}`),
    refetchInterval: (query) => (['sampling', 'queued', 'running'].includes(query.state.data?.status ?? '') ? 5000 : false) })
  const jobId = q.data?.job?.id
  const job = useQuery({ queryKey: ['job', jobId], queryFn: () => get<JobStatus>(`/validations/${jobId}`), enabled: !!jobId,
    refetchInterval: (query) => (query.state.data && ['queued', 'running'].includes(query.state.data.status) ? 5000 : false) })
  const results = useQuery({ queryKey: ['results', jobId, 'check', job.data?.items_done], queryFn: () => get<ResultsPage>(`/validations/${jobId}/results?per_page=200`), enabled: !!jobId })
  const crumbs = [{ label: 'Models' }, { label: 'Checks', to: '/checks' }, { label: checkId }]
  if (q.isPending) return <Page crumbs={crumbs}><Loading /></Page>
  if (q.isError) return <Page crumbs={crumbs}><Failed error={q.error} /></Page>
  const c = q.data
  const j = job.data
  const rows = results.data?.results ?? []
  const spec = j?.spec
  return (
    <Page crumbs={crumbs}>
      <div className="head">
        <div><h1>Check {c.id}</h1><span className="muted">{usecaseName(c.usecase)} · {PRESET[c.preset]}{c.candidates.length > 0 && ` (${c.candidates.join(', ')})`} · started {when(c.created_at)} by {c.trigger}</span></div>
        <div className="row"><span className={'pill pill-' + (c.status === 'done' ? 'done' : c.status === 'failed' ? 'failed' : 'running')}>{STATUS[c.status] ?? c.status}</span>{c.job && <Link className="btn" to={`/validations/${c.job.id}`}>Validation batch {c.job.id}</Link>}</div>
      </div>
      {c.error && <p className="error" role="alert">{c.error}</p>}
      {j && spec && (
        <p className="muted">Each sampled request: the original re-run {spec.reference_runs}× beside its logged answer, each candidate {spec.candidate_runs}×; judge {spec.judge_model}, {spec.votes} votes when an answer isn't a pass. {j.items_done} of {j.items_total} done · spent {money(j.spend_usd, 2)}{c.max_budget_usd != null && ` of ${money(c.max_budget_usd, 2)}`}{j.stop_reason && ` · ${j.stop_reason}`}</p>
      )}
      {Object.keys(c.samples).map((st) => {
        const ids = new Set(c.samples[st].map((s) => s.id))
        const why = Object.fromEntries(c.samples[st].map((s) => [s.id, s.why]))
        const mine = rows.filter((r) => ids.has(r.id))
        const labels = [...new Set(mine.flatMap((r) => r.candidates.map((x) => x.label)))]
        const agg = labels.map((label) => {
          const cs = mine.map((r) => r.candidates.find((x) => x.label === label)).filter((x): x is NonNullable<typeof x> => !!x)
          const answers = cs.reduce((a, x) => { for (const k of ['match', 'acceptable', 'drift', 'unclear'] as const) a[k] += x.answers?.[k] ?? 0; return a }, { match: 0, acceptable: 0, drift: 0, unclear: 0 })
          const avg = (xs: (number | null)[]) => { const v = xs.filter((x): x is number => x != null); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null }
          return { label, requests: cs.length, within: cs.filter((x) => x.verdict === 'within').length, outside: cs.filter((x) => x.verdict === 'outside').length,
            unverified: cs.filter((x) => x.verdict === 'unverified').length, answers, cost: avg(cs.map((x) => x.cost_usd)), lat: avg(cs.map((x) => x.latency_ms)) }
        })
        const baseCost = mine.length ? mine.map((r) => r.reference.runs.filter((x) => !x.recorded && x.cost_usd != null).map((x) => x.cost_usd as number)).flat() : []
        const baseLat = mine.length ? mine.map((r) => r.reference.runs.filter((x) => !x.recorded && x.latency_ms != null).map((x) => x.latency_ms as number)).flat() : []
        const bc = baseCost.length ? baseCost.reduce((a, b) => a + b, 0) / baseCost.length : null
        const bl = baseLat.length ? baseLat.reduce((a, b) => a + b, 0) / baseLat.length : null
        const cons = mine.filter((r) => r.reference.consistency != null)
        const consistency = cons.length ? cons.reduce((a, r) => a + (r.reference.consistency as number), 0) / cons.length : null
        return (
          <section key={st} className="panel">
            <div className="panel-head"><b><Link to={`/usecases/${encodeURIComponent(c.usecase ?? 'None')}/callsites/${st}`}>{c.site_names[st]}</Link></b><span className="muted">{count(c.samples[st].length, 'sampled request')}</span></div>
            {mine.length > 0 && (
              <>
                <div className="scroll">
                  <table className="grid">
                    <thead><tr><th>Model</th><th>Answers</th><th>Requests</th><th>Cost / call</th><th>Latency</th></tr></thead>
                    <tbody>
                      <tr className="base-row"><td><b>original</b></td><td className="muted">baseline · its own answers agree {pct(consistency)}</td><td className="muted">{mine.length}</td>
                        <td><VsBar value={bc} base={bc} shown={money(bc)} isBase /></td><td><VsBar value={bl} base={bl} shown={ms(bl)} isBase /></td></tr>
                      {agg.map((s) => (
                        <tr key={s.label}><td><b>{s.label}</b></td><td><AnswersBar a={s.answers} /></td>
                          <td className="nowrap"><b>{s.within}</b> pass <span className="muted">of {s.requests}</span>{s.outside > 0 && <div className="muted">{count(s.outside, 'drift')}</div>}{s.unverified > 0 && <div className="muted">{s.unverified} not verified</div>}</td>
                          <td><VsBar value={s.cost} base={bc} shown={money(s.cost)} /></td><td><VsBar value={s.lat} base={bl} shown={ms(s.lat)} /></td></tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <Legend />
                <div className="scroll">
                  <table className="grid">
                    <thead><tr><th>Sampled request</th><th>Why picked</th><th>Original agrees</th>{labels.map((l) => <th key={l}>{l}</th>)}</tr></thead>
                    <tbody>
                      {mine.map((r) => {
                        const url = `/validations/${c.job!.id}/requests/${encodeURIComponent(r.id)}`
                        return (
                          <tr key={r.id}>
                            <td className="mono"><Link to={url}>{r.id.slice(0, 18)}</Link></td>
                            <td className="muted">{why[r.id]}</td>
                            <td className="mono">{r.reference.agree ?? '?'}/{r.reference.of}</td>
                            {labels.map((l) => { const x = r.candidates.find((y) => y.label === l); return (
                              <td key={l}>{x ? <Link className={'cell v-' + x.verdict} to={`${url}?cand=${encodeURIComponent(l)}`} title={x.judge_reason || x.reason}>{verdictWord[x.verdict]} <Squares runs={x.runs} /></Link> : <span className="muted">—</span>}</td>) })}
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              </>
            )}
            {!mine.length && <p className="muted pad">{c.status === 'done' ? 'No candidate was run for this call-site (no cheaper or open-weight model for its original in the catalog).' : 'Waiting for the job…'}</p>}
          </section>
        )
      })}
      <button className="btn" onClick={() => qc.invalidateQueries({ queryKey: ['checks'] })}>Refresh</button>
    </Page>
  )
}
