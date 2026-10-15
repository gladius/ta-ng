import { useQueries, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { candidates, get, type JobStatus, type LabelSummary, type ResultsPage } from '../api'
import { AnswersBar, change, count, Failed, Kpi, Legend, Loading, money, ms, Page, pct, Squares, Status, verdictWord, VsBar, when } from '../ui'

const PER_PAGE = 50
const SHOW = [['all', 'All'], ['problems', 'Needs a look'], ['split', 'Candidates disagree']] as const
type Show = (typeof SHOW)[number][0]

/** The batch's conclusion in plain words, written from its numbers (no model call). */
function summarise(rows: LabelSummary[]): string[] {
  const cands = candidates(rows)
  if (!cands.length) return []
  const n = Math.max(...cands.map((s) => s.requests))
  const names = (xs: LabelSummary[]) => xs.map((s) => s.label).join(xs.length > 2 ? ', ' : ' and ')
  const clean = cands.filter((s) => s.requests && s.within === s.requests)
  const out = [clean.length === cands.length
    ? `${names(clean)} ${clean.length > 1 ? 'behave' : 'behaves'} like the original on ${n === 1 ? 'the one request' : `all ${n} requests`}.`
    : cands.map((s) => `${s.label} passes ${s.within} of ${count(s.requests, 'request')}`).join('; ') + '.']
  const best = [...cands].sort((a, b) => (b.within_rate ?? 0) - (a.within_rate ?? 0)
    || (a.cost_vs_original_pct ?? 0) - (b.cost_vs_original_pct ?? 0))[0]
  if (best.cost_vs_original_pct != null) {
    const c = best.cost_vs_original_pct, l = best.latency_vs_original_pct
    out.push(`${clean.length > 1 ? 'The cheapest of those, ' + best.label + ',' : best.label} costs ${Math.abs(c)}% ${c <= 0 ? 'less' : 'more'} per call`
      + (l == null ? '.' : ` and is ${Math.abs(l)}% ${l <= 0 ? 'faster' : 'slower'}.`))
  }
  const drifts = cands.filter((s) => s.answers?.drift).map((s) => `${s.answers!.drift} of ${s.label}'s answers drifted`)
  if (drifts.length) out.push(drifts.join(', ') + '.')
  const unverified = cands.reduce((t, s) => t + s.unverified, 0)
  if (unverified) out.push(`${unverified} request checks could not be verified; each request's page says why.`)
  return out
}

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
    queryFn: () => get<JobStatus>(`/audit/models/${jobId}`),
    refetchInterval: (query) => (query.state.data && ['queued', 'running'].includes(query.state.data.status) ? 5000 : false),
  })
  const live = !!job.data && ['queued', 'running'].includes(job.data.status)
  const results = useQuery({
    queryKey: ['results', jobId, show, page, q, job.data?.items_done],
    queryFn: () => get<ResultsPage>(`/audit/models/${jobId}/results?show=${show}&page=${page}&per_page=${PER_PAGE}&q=${encodeURIComponent(q)}`),
    enabled: !!job.data,
  })
  const counts = useQueries({
    queries: SHOW.map(([k]) => ({
      queryKey: ['count', jobId, k, q, job.data?.items_done],
      queryFn: () => get<ResultsPage>(`/audit/models/${jobId}/results?show=${k}&per_page=1&q=${encodeURIComponent(q)}`),
      enabled: !!job.data,
    })),
  })

  const crumbs = [{ label: 'Agentic workloads' }, { label: 'Model audits', to: '/audit/models' }, { label: jobId, to: `/audit/models/${jobId}` }, { label: 'Requests' }]
  if (job.isPending) return <Page crumbs={crumbs}><Loading /></Page>
  if (job.isError) return <Page crumbs={crumbs}><Failed error={job.error} /></Page>
  const j = job.data
  const base = j.summary.find((s) => s.label === 'original')
  const cands = candidates(j.summary)
  const saving = [...cands].filter((s) => s.cost_vs_original_pct != null).sort((a, b) => a.cost_vs_original_pct! - b.cost_vs_original_pct!)[0]
  const sentences = summarise(j.summary)
  const pages = Math.max(1, Math.ceil((results.data?.total ?? 0) / PER_PAGE))

  return (
    <Page crumbs={crumbs}>
      <div className="head">
        <div>
          <div className="title"><h1>{j.id}</h1><Status status={j.status} /></div>
          <p className="meta">
            {j.items_done} of {j.items_total} requests · judge {j.judge_model} · {when(j.created_at)}{live && ' · live'}
          </p>
        </div>
        <div className="row">
          <a className="btn" href={`/api/audit/models/${j.id}/results.csv`}>Download CSV</a>
          <a className="btn primary" href={`/api/audit/models/${j.id}/results.json`}>JSON</a>
        </div>
      </div>

      {(j.stop_reason || j.error || j.callback?.error || j.unpriced_calls > 0) && (
        <div className="note" role="status">
          {j.stop_reason && <p>{j.stop_reason}</p>}
          {j.error && <p>{j.status === 'failed' ? 'Failed: ' : ''}{j.error}</p>}
          {(j.status === 'failed' || j.status === 'stopped') && j.items_done < j.items_total && (
            <p>The {j.items_total - j.items_done} unfinished requests run again with <span className="mono">POST /api/audit/models/{j.id}/resume</span>, optionally with a higher <span className="mono">max_budget_usd</span>.</p>
          )}
          {j.callback?.error && <p>The callback could not be delivered: {j.callback.error}</p>}
          {j.unpriced_calls > 0 && <p>{j.unpriced_calls} model calls have no catalog price, so spend and costs are understated.</p>}
        </div>
      )}

      {sentences.length > 0 && (
        <section className="summary" aria-label="Summary">
          <div className="summary-head"><span className="dot" aria-hidden="true" />Summary</div>
          <p>{sentences.join(' ')}</p>
        </section>
      )}

      <div className="kpis">
        <Kpi label="Requests" value={j.items_done} note={j.items_done === j.items_total ? 'all finished' : `of ${j.items_total}`} />
        <Kpi label="Labels passing every request" value={`${cands.filter((s) => s.requests && s.within === s.requests).length} of ${cands.length}`}
          note={cands.map((s) => `${s.label} ${s.within}/${s.requests}`).join(' · ')} />
        <Kpi label="Largest saving" best value={saving ? change(saving.cost_vs_original_pct) : '—'} note={saving ? `${saving.label}, cost per call` : 'no prices yet'} />
        <Kpi label="Spent on this audit" value={money(j.spend_usd, 2)} note={j.max_budget_usd == null ? 'no budget limit' : `of ${money(j.max_budget_usd, 2)}`} />
      </div>

      {cands.length > 0 && (
        <section className="panel">
          <div className="panel-head"><h2>By label</h2></div>
          <div className="scroll">
            <table className="grid">
              <thead><tr><th>Label</th><th>Answers</th><th>Requests</th><th>Cost / call</th><th>Latency</th></tr></thead>
              <tbody>
                {base && (
                  <tr className="base-row">
                    <td><b>original</b></td>
                    <td className="muted">baseline · its own answers agree {pct(base.avg_consistency)}</td>
                    <td className="muted">{base.requests}</td>
                    <td><VsBar value={base.avg_cost_usd} base={base.avg_cost_usd} shown={money(base.avg_cost_usd)} isBase /></td>
                    <td><VsBar value={base.avg_latency_ms} base={base.avg_latency_ms} shown={ms(base.avg_latency_ms)} isBase /></td>
                  </tr>
                )}
                {cands.map((s) => (
                  <tr key={s.label}>
                    <td><b>{s.label}</b></td>
                    <td>{s.answers ? <AnswersBar a={s.answers} /> : <span className="muted">average fit {pct(s.avg_fit_rate)}</span>}</td>
                    <td className="nowrap">
                      <b>{s.within}</b> pass <span className="muted">of {s.requests}</span>
                      {s.outside > 0 && <div className="muted">{count(s.outside, 'drift')}</div>}
                      {s.unverified > 0 && <div className="muted">{s.unverified} not verified</div>}
                    </td>
                    <td><VsBar value={s.avg_cost_usd} base={base?.avg_cost_usd ?? null} shown={money(s.avg_cost_usd)} /></td>
                    <td><VsBar value={s.avg_latency_ms} base={base?.avg_latency_ms ?? null} shown={ms(s.avg_latency_ms)} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Legend />
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
              <thead><tr><th>Request</th><th>Original agrees</th>{cands.map((s) => <th key={s.label}>{s.label}</th>)}</tr></thead>
              <tbody>
                {results.data.results.map((r) => {
                  const url = `/audit/models/${j.id}/requests/${encodeURIComponent(r.id)}`   // ids may hold / ? # %
                  return (
                    <tr key={r.id}>
                      <td className="mono"><Link to={url}>{r.id}</Link></td>
                      <td className="mono">{r.reference.agree ?? '?'}/{r.reference.of}</td>
                      {cands.map((s) => {
                        const c = r.candidates.find((x) => x.label === s.label)
                        return (
                          <td key={s.label}>
                            {c ? (
                              <Link className={'cell v-' + c.verdict} to={`${url}?cand=${encodeURIComponent(c.label)}`} title={c.judge_reason || c.reason}>
                                {verdictWord[c.verdict]} <Squares runs={c.runs} />
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
