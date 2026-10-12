import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import Select from 'react-select'
import { Link, useParams } from 'react-router-dom'
import { get, type JobStatus, type ResultsPage } from '../api'
import { usecaseName } from '../cache'
import { AnswersBar, count, Failed, Legend, Loading, money, ms, Page, pct, Squares, verdictWord, VsBar, when } from '../ui'
import { STRATEGY_NAME } from './How'

export type Verdict = { requests: number; within: number; outside: number; unverified: number; pass_rate: number; cost_vs_original_pct: number | null; latency_vs_original_pct: number | null; fit_rate: number | null; verdict: 'passes' | 'drifts' | 'not verified' }
export type Check = {
  id: string; created_at: string; trigger: string; usecase: string | null; site: string | null; preset: string; candidates: string[]
  samples: Record<string, { id: string; why: string; prompt_tokens: number; completion_tokens: number }[]>
  job_id: string | null; max_budget_usd: number | null; status: string; error: string | null
  summary: Record<string, Record<string, Verdict>> | null; finished_at: string | null
}
type CheckPage = Check & { job: { id: string; status: string; items_total: number; items_done: number; spend_usd: number; stop_reason: string | null } | null; site_names: Record<string, string> }

export const PRESET: Record<string, string> = { cheaper: 'cheaper tier of each call-site\'s model', open_weight: 'open-weight swap in the same tier', release: 'a release', custom: 'chosen models', auto: 'Auto' }
const STATUS: Record<string, string> = { sampling: 'Sampling', queued: 'Queued', running: 'Running', done: 'Done', failed: 'Failed', stopped: 'Stopped' }

export function VerdictChip({ v }: { v: Verdict }) {
  return <span className={'chip ' + (v.verdict === 'passes' ? 'cause-ok' : v.verdict === 'drifts' ? 'cause-dynamic_first' : '')}>{v.verdict} · {v.within} of {v.requests}</span>
}

type Served = { models: { model: string; available: boolean; provider: string; tier: string | null; open_weight: boolean }[] }
type UsecaseOpt = { usecase: string | null; sites: number | null; analysed_at: string | null; calls: number }

/** The one way to start a check: which usecases (or the one call-site this page is on), which models (any the
    gateway serves, several at once) and/or Auto; samples and budget under "advanced". */
export function CheckDialog({ usecase, site, lockScope = false, onStarted }: { usecase?: string | null; site?: string; lockScope?: boolean; onStarted?: (ids: string[]) => void }) {
  const [open, setOpen] = useState(false)
  const [usecases, setUsecases] = useState<string[]>(usecase !== undefined ? [usecase ?? 'None'] : [])
  const [models, setModels] = useState<string[]>([])
  const [strategy, setStrategy] = useState('cheaper')
  const strategies = useQuery({ queryKey: ['strategies'], queryFn: () => get<{ strategies: { id: string; does: string }[] }>('/suite/checks/strategies'), enabled: open })
  const [advanced, setAdvanced] = useState(false)
  const [samples, setSamples] = useState(12)
  const [budget, setBudget] = useState<number | ''>('')
  const served = useQuery({ queryKey: ['served-models'], queryFn: () => get<Served>('/suite/models'), enabled: open })
  const opts = useQuery({ queryKey: ['usecase-options'], queryFn: () => get<{ usecases: UsecaseOpt[] }>('/suite/callsites/usecases?per_page=200'), enabled: open && !site && !lockScope })
  const auto = strategy !== 'manual' && strategy !== 'release'
  const est = useQuery({ queryKey: ['check-estimate', usecases, site, samples, models.length + (auto ? 1 : 0)], enabled: open && (!!site || usecases.length > 0),
    queryFn: async () => {
      const n = Math.max(1, models.length + (auto ? 1 : 0))
      const scopes: (string | null)[] = site ? [null] : usecases
      const sums = await Promise.all(scopes.map((u) => get<{ usd: number }>(`/suite/checks/estimate?${new URLSearchParams({ ...(u != null && { usecase: u }), ...(site && { site }), samples: String(samples), candidates: String(n) })}`)))
      return { usd: sums.reduce((a, b) => a + b.usd, 0) }
    } })
  const start = useMutation({
    mutationFn: () => get<{ checks: string[] }>('/suite/checks', { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ usecases: site ? [] : usecases, site, strategy, models, samples, ...(budget !== '' && { max_budget_usd: budget }) }) }),
    onSuccess: (r) => { setOpen(false); onStarted?.(r.checks) },
  })
  if (!open) return <button className="btn primary" onClick={() => setOpen(true)}>Check models…</button>
  const available = (served.data?.models ?? []).filter((m) => m.available)
  return (
    <div className="panel pad dialog">
      <h2>Check models {site ? 'on this call-site' : lockScope ? 'on this usecase' : ''}</h2>
      <p className="muted">Each call-site's requests are sampled by size; the current model and every candidate answer them again; a judge compares the answers with evidence. You get, per call-site: which candidates drift, and the cheapest and fastest that don't, by measured cost and latency. Nothing in production changes.</p>
      {!site && !lockScope && (
        <label>Usecases
          <Select isMulti classNamePrefix="rs" placeholder="Choose usecases… (type to search)" closeMenuOnSelect={false}
            options={(opts.data?.usecases ?? []).map((u) => ({ value: u.usecase ?? 'None', label: usecaseName(u.usecase), isDisabled: !(u.analysed_at && u.sites),
              hint: u.analysed_at && u.sites ? `${u.calls.toLocaleString('en-US')} calls` : 'not analysed yet' }))}
            value={usecases.map((id) => ({ value: id, label: usecaseName(id === 'None' ? null : id) }))}
            onChange={(v) => setUsecases(v.map((o) => o.value))}
            formatOptionLabel={(o: { label: string; hint?: string }, m) => m.context === 'menu' ? <span>{o.label} <span className="muted">{o.hint}</span></span> : o.label} />
          <span className="row"><button type="button" className="btn small" onClick={() => setUsecases((opts.data?.usecases ?? []).filter((u) => u.analysed_at && u.sites).map((u) => u.usecase ?? 'None'))}>all analysed</button></span>
        </label>
      )}
      <label>Strategy
        <Select classNamePrefix="rs" isSearchable={false}
          options={(strategies.data?.strategies ?? []).map((x) => ({ value: x.id, label: STRATEGY_NAME[x.id] ?? x.id, hint: x.does }))}
          value={{ value: strategy, label: STRATEGY_NAME[strategy] ?? strategy }}
          onChange={(v) => v && setStrategy(v.value)}
          formatOptionLabel={(o: { label: string; hint?: string }, m) => m.context === 'menu' ? <span>{o.label} <span className="muted">· {o.hint}</span></span> : o.label} />
        <span className="muted">{strategies.data?.strategies.find((x) => x.id === strategy)?.does} · <Link to="/how">how a check works</Link></span>
      </label>
      <label>{strategy === 'manual' || strategy === 'release' ? 'Models' : 'Models to add'}
        <Select isMulti classNamePrefix="rs" placeholder={strategy === 'manual' || strategy === 'release' ? 'Choose models… (type to search)' : 'Optional: more models to try alongside the strategy\'s picks'} closeMenuOnSelect={false}
          options={Object.entries(available.reduce<Record<string, { value: string; label: string }[]>>((g, m) => { (g[m.provider] ??= []).push({ value: m.model, label: `${m.model}${m.tier ? ` · ${m.tier}` : ''}${m.open_weight ? ' · open-weight' : ''}` }); return g }, {})).map(([provider, options]) => ({ label: provider, options }))}
          value={models.map((m) => ({ value: m, label: m }))}
          onChange={(v) => setModels(v.map((o) => o.value))} />
      </label>
      <button className="btn small" onClick={() => setAdvanced(!advanced)}>{advanced ? 'Hide advanced' : 'Advanced'}</button>
      {advanced && (
        <div className="row">
          <label>Samples per call-site <input className="input" type="number" min={1} max={50} value={samples} onChange={(e) => setSamples(Number(e.target.value))} /></label>
          <label>Budget per check, $ (optional) <input className="input" type="number" min={1} step={1} value={budget} onChange={(e) => setBudget(e.target.value === '' ? '' : Number(e.target.value))} /></label>
        </div>
      )}
      <p className="muted">Estimated cost: {est.data ? money(est.data.usd, 2) : '…'} · {site ? '1 check' : `${usecases.length} check${usecases.length === 1 ? '' : 's'}, one per usecase`}</p>
      {start.isError && <p className="error" role="alert">{start.error instanceof Error ? start.error.message : String(start.error)}</p>}
      <div className="row"><button className="btn primary" disabled={start.isPending || (!site && !usecases.length) || ((strategy === 'manual' || strategy === 'release') && !models.length)} onClick={() => start.mutate()}>Start</button><button className="btn" onClick={() => setOpen(false)}>Cancel</button></div>
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
      <div className="head"><div><h1>Model checks</h1><span className="muted">would a cheaper, open-weight or newly released model hold on a usecase's call-sites? Started from a usecase or call-site page, here for a release, by the API, or by the router. <Link to="/how">How a check works</Link>.</span></div><CheckDialog onStarted={() => q.refetch()} /></div>
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
                    <td>{STRATEGY_NAME[c.preset] ?? PRESET[c.preset] ?? c.preset}{c.candidates.length > 0 && <div className="mono muted">{c.candidates.join(', ')}</div>}</td>
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
        <div><h1>Check {c.id}</h1><span className="muted">{usecaseName(c.usecase)} · {STRATEGY_NAME[c.preset] ?? PRESET[c.preset] ?? c.preset}{c.candidates.length > 0 && ` (${c.candidates.join(', ')})`} · started {when(c.created_at)} by {c.trigger}</span></div>
        <div className="row"><span className={'pill pill-' + (c.status === 'done' ? 'done' : c.status === 'failed' ? 'failed' : 'running')}>{STATUS[c.status] ?? c.status}</span>{c.job && <Link className="btn" to={`/validations/${c.job.id}`}>Validation batch {c.job.id}</Link>}</div>
      </div>
      {c.error && <p className="error" role="alert">{c.error}</p>}
      {j && spec && (
        <p className="muted">Each sampled request: the original re-run {spec.reference_runs}× beside its logged answer, each candidate {spec.first_runs}× (up to {spec.candidate_runs}× when one isn't a pass); judge {spec.judge_model}, {spec.votes} votes when an answer isn't a pass. {j.items_done} of {j.items_total} done · spent {money(j.spend_usd, 2)}{c.max_budget_usd != null && ` of ${money(c.max_budget_usd, 2)}`}{j.stop_reason && ` · ${j.stop_reason}`}</p>
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
