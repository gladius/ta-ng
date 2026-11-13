import { useQueries, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { candidates, get, PREFIX, type ResultsPage } from '../api'
import { usecaseName } from '../cache'
import { STATUS, STRATEGY_NAME, VerdictChip, type Audit } from './ModelAudits'
import { AnswersBar, BatchTotals, change, count, Failed, Kpi, Legend, Loading, money, ms, Page, pct, Squares, verdictWord, VsBar, when } from '../ui'

const PER_PAGE = 50
const SHOW = [['all', 'All'], ['problems', 'Needs a look'], ['split', 'Candidates disagree']] as const
type Show = (typeof SHOW)[number][0]

/** One model audit, the whole of it on one page: the batch on each model (tokens, reasoning, cost, answers that hold),
    the averages per call, each call-site's verdict, then every request, paged, with search and filters. */
export default function Batch() {
  const { auditId: jobId = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const show = (SHOW.find(([k]) => k === params.get('show'))?.[0] ?? 'all') as Show
  const page = Math.max(1, Number(params.get('page')) || 1)
  const q = params.get('q') ?? ''
  const [typed, setTyped] = useState(q)
  const set = (next: Record<string, string>) => setParams({ show, page: '1', ...(q ? { q } : {}), ...next })

  const job = useQuery({
    queryKey: ['job', jobId],
    queryFn: () => get<Audit>(`/audit/models/${jobId}`),
    refetchInterval: (query) => (query.state.data && ['sampling', 'queued', 'running'].includes(query.state.data.status) ? 5000 : false),
  })
  const live = !!job.data && ['sampling', 'queued', 'running'].includes(job.data.status)
  const results = useQuery({
    queryKey: ['results', jobId, show, page, q, job.data?.items_done],
    queryFn: () => get<ResultsPage>(`/audit/models/${jobId}/results?show=${show}&page=${page}&per_page=${PER_PAGE}&q=${encodeURIComponent(q)}`),
    enabled: !!job.data && job.data.status !== 'sampling',
  })
  const counts = useQueries({
    queries: SHOW.map(([k]) => ({
      queryKey: ['count', jobId, k, q, job.data?.items_done],
      queryFn: () => get<ResultsPage>(`/audit/models/${jobId}/results?show=${k}&per_page=1&q=${encodeURIComponent(q)}`),
      enabled: !!job.data && job.data.status !== 'sampling',
    })),
  })

  const crumbs = [{ label: 'Agentic workloads' }, { label: 'Model audits', to: '/audit/models' }, { label: jobId }]
  if (job.isPending) return <Page crumbs={crumbs}><Loading /></Page>
  if (job.isError) return <Page crumbs={crumbs}><Failed error={job.error} /></Page>
  const j = job.data
  const base = j.summary.find((s) => s.label === 'original')
  const cands = candidates(j.summary)
  const baseModel = base?.models.length === 1 ? base.models[0] : null      // one model behind every request: name it
  const vs = baseModel ?? 'original'
  const n = (v?: number | null) => (v == null ? '—' : Math.round(v).toLocaleString())
  const reasoning = (s: { avg_reasoning_tokens?: number | null; avg_output_tokens?: number | null }) =>
    s.avg_reasoning_tokens == null ? '—' : `${n(s.avg_reasoning_tokens)}${s.avg_output_tokens ? ` (${Math.round((s.avg_reasoning_tokens / s.avg_output_tokens) * 100)}%)` : ''}`
  const sites = Object.keys(j.samples ?? {}).filter(Boolean)                 // call-sites of a sampled audit; none for sent requests
  const picked = Object.fromEntries(Object.entries(j.samples ?? {}).flatMap(([st, xs]) => xs.map((x) => [x.id, { st, why: x.why }])))
  const judged = j.spec?.judge !== false
  const most = [...cands].filter((s) => s.output_tokens_vs_original_pct != null).sort((a, b) => b.output_tokens_vs_original_pct! - a.output_tokens_vs_original_pct!)[0]
  const saving = [...cands].filter((s) => s.cost_vs_original_pct != null).sort((a, b) => a.cost_vs_original_pct! - b.cost_vs_original_pct!)[0]
  const pages = Math.max(1, Math.ceil((results.data?.total ?? 0) / PER_PAGE))

  return (
    <Page crumbs={crumbs}>
      <div className="head">
        <div>
          <div className="title"><h1>{cands.length ? `${cands.map((c) => c.label).join(', ')} vs ${vs}` : `Model audit ${j.id}`}</h1>
            <span className={'pill pill-' + (j.status === 'sampling' ? 'running' : j.status)}>{STATUS[j.status] ?? j.status}</span></div>
          <p className="meta">
            {j.id} · {j.scope === 'requests' ? count(j.items_total, 'sent request') : usecaseName(j.usecase)} · {STRATEGY_NAME[j.strategy] ?? j.strategy} · {j.items_done} of {j.items_total} requests done
            {j.spec && (j.spec.judge === false ? ' · not judged' : ` · judge ${j.spec.judge_model}`)}{j.spec?.max_output_tokens ? ` · output cap ${j.spec.max_output_tokens.toLocaleString()}` : ''} · started {when(j.created_at)} by {j.trigger}{live && ' · live'}
          </p>
        </div>
        <div className="row">
          <a className="btn" href={`${PREFIX}/api/audit/models/${j.id}/results.csv`}>Download CSV</a>
          <a className="btn" href={`${PREFIX}/api/audit/models/${j.id}/results.json`}>JSON</a>
        </div>
      </div>

      {(j.stop_reason || j.error || j.callback?.error || j.unpriced_calls > 0 || j.status === 'sampling') && (
        <div className="note" role="status">
          {j.stop_reason && <p>{j.stop_reason}</p>}
          {j.error && <p>{j.status === 'failed' ? 'Failed: ' : ''}{j.error}</p>}
          {(j.status === 'failed' || j.status === 'stopped') && j.items_done < j.items_total && (
            <p>{j.items_total - j.items_done} unfinished · resume: <span className="mono">POST {PREFIX}/api/audit/models/{j.id}/resume</span></p>
          )}
          {j.status === 'sampling' && <p>Sampling requests from the log…</p>}
          {j.callback?.error && <p>Callback failed: {j.callback.error}</p>}
          {j.unpriced_calls > 0 && <p>{j.unpriced_calls} calls without a catalog price: spend is understated.</p>}
        </div>
      )}


      <div className="kpis">
        <Kpi label="Requests" value={j.items_done} note={j.items_done === j.items_total ? 'all finished' : `of ${j.items_total}`} />
        {judged ? <Kpi label="Models passing every request" value={`${cands.filter((s) => s.requests && s.within === s.requests).length} of ${cands.length}`}
          note={cands.map((s) => `${s.label} ${s.within}/${s.requests}`).join(' · ')} />
          : <Kpi label="Output tokens" value={most ? change(most.output_tokens_vs_original_pct) : '—'} note={most ? `${most.label} vs ${vs}` : 'answers not judged'} />}
        <Kpi label={saving && saving.cost_vs_original_pct! > 0 ? `Cost vs ${vs}` : 'Largest saving'} best={!!saving && saving.cost_vs_original_pct! <= 0} value={saving ? change(saving.cost_vs_original_pct) : '—'} note={saving ? `${saving.label}, cost per call` : 'no prices yet'} />
        <Kpi label="Spent on this audit" value={money(j.spend_usd, 2)} note={j.max_budget_usd == null ? 'no budget limit' : `of ${money(j.max_budget_usd, 2)}`} />
      </div>

      <BatchTotals summary={j.summary} />

      {cands.length > 0 && (
        <section className="panel">
          <div className="panel-head"><h2>Average per call</h2></div>
          <div className="scroll">
            <table className="grid">
              <thead><tr><th>Model</th><th>Answers</th><th className="num">Input tokens</th><th>Output tokens</th><th className="num">of which reasoning</th><th>Cost / call</th><th>Latency</th></tr></thead>
              <tbody>
                {base && (
                  <tr className="base-row">
                    <td className="nowrap"><b>{baseModel ?? 'original'}</b><div className="muted">{baseModel ? 'baseline' : `baseline · ${base.models.join(', ')}`}</div></td>
                    <td className="muted">{judged ? `its own answers agree ${pct(base.avg_consistency)}` : 'not judged'}</td>
                    <td className="num mono">{n(base.avg_input_tokens)}</td>
                    <td><VsBar value={base.avg_output_tokens ?? null} base={base.avg_output_tokens ?? null} shown={n(base.avg_output_tokens)} isBase /></td>
                    <td className="num nowrap"><span className="mono">{reasoning(base)}</span><div className="muted">answer {n(base.avg_output_chars)} chars</div></td>
                    <td><VsBar value={base.avg_cost_usd} base={base.avg_cost_usd} shown={money(base.avg_cost_usd)} isBase /></td>
                    <td><VsBar value={base.avg_latency_ms} base={base.avg_latency_ms} shown={ms(base.avg_latency_ms)} isBase /></td>
                  </tr>
                )}
                {cands.map((s) => (
                  <tr key={s.label}>
                    <td className="nowrap"><b>{s.label}</b></td>
                    <td>{!judged ? <span className="muted">not judged</span> : s.answers ? <AnswersBar a={s.answers} /> : <span className="muted">average fit {pct(s.avg_fit_rate)}</span>}</td>
                    <td className="num mono">{n(s.avg_input_tokens)}</td>
                    <td><VsBar value={s.avg_output_tokens ?? null} base={base?.avg_output_tokens ?? null} shown={n(s.avg_output_tokens)} vs={vs} /></td>
                    <td className="num nowrap"><span className="mono">{reasoning(s)}</span><div className="muted">answer {n(s.avg_output_chars)} chars</div></td>
                    <td><VsBar value={s.avg_cost_usd} base={base?.avg_cost_usd ?? null} shown={money(s.avg_cost_usd)} vs={vs} /></td>
                    <td><VsBar value={s.avg_latency_ms} base={base?.avg_latency_ms ?? null} shown={ms(s.avg_latency_ms)} vs={vs} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {judged && <Legend />}
        </section>
      )}

      {j.routing.length > 0 && (
        <section className="panel">
          <div className="panel-head"><h2>Where routers sent requests</h2></div>
          <div className="scroll">
            <table className="grid">
              <thead><tr><th>Label</th><th>Answered by</th><th className="num">Requests</th><th className="num">Pass</th><th className="num">Drift</th><th className="num">Not verified</th></tr></thead>
              <tbody>
                {j.routing.map((r) => (
                  <tr key={r.label + r.served_model}>
                    <td><b>{r.label}</b></td><td className="mono">{r.served_model ?? '—'}</td>
                    <td className="num">{r.requests}</td><td className="num">{r.within}</td><td className="num">{r.outside}</td><td className="num">{r.unverified}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {sites.length > 0 && j.verdicts && (
        <section className="panel">
          <div className="panel-head"><h2>By call-site</h2><span className="muted">{count(sites.length, 'call-site')}</span></div>
          <div className="scroll">
            <table className="grid">
              <thead><tr><th>Call-site</th><th className="num">Sampled</th>{cands.map((s) => <th key={s.label}>{s.label}</th>)}</tr></thead>
              <tbody>
                {sites.map((st) => (
                  <tr key={st}>
                    <td><Link to={`/usecases/${encodeURIComponent(j.usecase ?? 'None')}/callsites/${st}`}>{j.site_names[st] ?? st}</Link></td>
                    <td className="num">{j.samples[st].length}</td>
                    {cands.map((s) => { const v = j.verdicts?.[st]?.[s.label]; return <td key={s.label}>{v ? <VerdictChip v={v} /> : <span className="muted">—</span>}</td> })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="panel">
        <div className="panel-head">
          <h2>Requests</h2>
          <div className="row">
            <form className="find" onSubmit={(e) => { e.preventDefault(); set({ q: typed }) }}>
              <label htmlFor="find-request" className="sr">Find a request</label>
              <input id="find-request" type="search" placeholder="Find a request id" value={typed} onChange={(e) => setTyped(e.target.value)} />
            </form>
            <div className="seg" role="group" aria-label="Show">
              {SHOW.map(([k, name], i) => (
                <button key={k} type="button" aria-pressed={show === k} onClick={() => set({ show: k })}>
                  {name} {counts[i].data?.total ?? ''}
                </button>
              ))}
            </div>
          </div>
        </div>
        {results.isPending ? <Loading /> : results.isError ? <Failed error={results.error} /> : !results.data.results.length ? (
          <p className="empty pad">{live && j.items_done === 0 ? 'No requests finished yet.' : 'No requests match.'}</p>
        ) : (
          <div className="scroll">
            <table className="grid">
              <thead><tr><th>Request</th>{sites.length > 0 && <><th>Call-site</th><th>Why picked</th></>}{judged && <th>{baseModel ?? 'Original'} agrees with itself</th>}{cands.map((s) => <th key={s.label}>{s.label}</th>)}</tr></thead>
              <tbody>
                {results.data.results.map((r) => {
                  const url = `/audit/models/${j.id}/requests/${encodeURIComponent(r.id)}`   // ids may hold / ? # %
                  return (
                    <tr key={r.id}>
                      <td className="mono"><Link to={url}>{r.id}</Link></td>
                      {sites.length > 0 && <><td>{j.site_names[picked[r.id]?.st] ?? '—'}</td><td className="muted">{picked[r.id]?.why ?? ''}</td></>}
                      {judged && <td className="mono">{r.reference.agree ?? '?'}/{r.reference.of}</td>}
                      {cands.map((s) => {
                        const c = r.candidates.find((x) => x.label === s.label)
                        return (
                          <td key={s.label}>
                            {c ? (
                              <Link className={'cell v-' + c.verdict} to={`${url}?cand=${encodeURIComponent(c.label)}`} title={c.judge_reason || c.reason}>
                                {verdictWord[c.verdict]} {judged && <Squares runs={c.runs} />}
                              </Link>
                            ) : <span className="muted">—</span>}
                          </td>
                        )
                      })}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
        <div className="pager pad">
          {page > 1 && <button className="btn" onClick={() => set({ page: String(page - 1) })}>‹ Previous</button>}
          <span className="muted">Page {page} of {pages} · {count(results.data?.total ?? 0, 'request')}</span>
          {page < pages && <button className="btn" onClick={() => set({ page: String(page + 1) })}>Next ›</button>}
        </div>
      </section>
    </Page>
  )
}
