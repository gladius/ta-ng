import { useQueries, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { candidates, get, PREFIX, type ResultsPage } from '../api'
import { usecaseName } from '../cache'
import { STATUS, STRATEGY_NAME, VerdictChip, type Audit } from './ModelAudits'
import { AnswersBar, change, CONSISTENCY, count, Failed, FIT, Legend, Loading, money, Page, pct, verdictWord, vsPct, when } from '../ui'

const PER_PAGE = 50
const MEASURES = [['Output tokens per call', 'avg_output_tokens'], ['Cost per call', 'avg_cost_usd'], ['Latency', 'avg_latency_ms']] as const
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
  const [whole, setWhole] = useState(false)
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
  const delta = (v?: number | null, b?: number | null) => { const p = vsPct(v ?? null, b ?? null); return p == null ? null : <div><b>{p === 0 ? 'same' : change(p)}</b></div> }
  const fact = (name: string, p?: number | null) => p != null && <span className="fact"><span className="muted">{name}</span> <b>{p === 0 ? 'same' : change(p)}</b></span>
  const sites = Object.keys(j.samples ?? {}).filter(Boolean)                 // call-sites of a sampled audit; none for sent requests
  const picked = Object.fromEntries(Object.entries(j.samples ?? {}).flatMap(([st, xs]) => xs.map((x) => [x.id, { st, why: x.why }])))
  const judged = j.spec?.judge !== false
  const pages = Math.max(1, Math.ceil((results.data?.total ?? 0) / PER_PAGE))

  return (
    <Page crumbs={crumbs}>
      <div className="head">
        <div>
          <div className="title"><h1>{cands.length ? `${cands.map((c) => c.label).join(', ')} vs ${vs}` : `Model audit ${j.id}`}</h1>
            <span className={'pill pill-' + (j.status === 'sampling' ? 'running' : j.status)}>{STATUS[j.status] ?? j.status}</span></div>
          <p className="meta">{j.scope === 'requests' ? count(j.items_total, 'sent request') : usecaseName(j.usecase)}{live && ` · ${j.items_done} of ${j.items_total} done · live`}</p>
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


      {cands.length > 0 && (
        <section className="panel lines">
          {cands.map((s) => {
            const tone = !judged || s.judged === false ? null : s.outside > 0 ? 'outside' : s.within === s.requests ? 'within' : 'unverified'
            return (
              <div key={s.label} className="line">
                <b>{s.label}</b>
                {tone ? (
                  <span><i className={'dot d-' + tone} /> matches {vs} on <b>{s.within} of {s.requests}</b> requests
                    {s.outside > 0 && <> · <b>{s.outside}</b> drift</>}{s.unverified > 0 && <> · {s.unverified} not verified</>}</span>
                ) : <span className="muted">answers not judged</span>}
                {fact('output tokens', s.output_tokens_vs_original_pct)}{fact('cost', s.cost_vs_original_pct)}{fact('latency', s.latency_vs_original_pct)}
                {!!s.flags?.length && <ul className="flags">{s.flags.map((f) => <li key={f}>{f}</li>)}</ul>}
              </div>
            )
          })}
          {!!base?.flags?.length && <div className="line"><b>{vs}</b><ul className="flags">{base.flags.map((f) => <li key={f}>{f}</li>)}</ul></div>}
        </section>
      )}

      {base && cands.length > 0 && (
        <section className="panel">
          <div className="panel-head">
            <h2>Comparison{whole && <span className="muted"> · {count(base.requests, 'request')}, each answered once</span>}</h2>
            <div className="seg" role="group" aria-label="Figures">
              <button type="button" aria-pressed={!whole} onClick={() => setWhole(false)}>Per call</button>
              <button type="button" aria-pressed={whole} onClick={() => setWhole(true)}>Whole batch</button>
            </div>
          </div>
          <div className="scroll">
            <table className="grid">
              <thead><tr>
                <th>Model</th>{judged && <th>Answers</th>}
                {<th className="num">Input tokens</th>}
                {<th className="num">Output tokens</th>}
                {<th className="num">of which reasoning</th>}
                {<th className="num">Answer length</th>}
                <th className="num">{whole ? 'Cost' : 'Cost per 1,000 calls'}</th>
                <th className="num">Latency</th>
              </tr></thead>
              <tbody>
                {[base, ...cands].map((s) => {
                  const isBase = s === base
                  const cell = (v: number | null | undefined, b: number | null | undefined, shown: string) => (
                    <td className="num nowrap"><span className="mono">{shown}</span>{!isBase && delta(v, b)}</td>
                  )
                  const k = whole ? s.requests : 1
                  const kb = whole ? base.requests : 1
                  const t = (v?: number | null) => (v == null ? null : v * k)
                  return (
                    <tr key={s.label} className={isBase ? 'base-row' : ''}>
                      <td className="nowrap"><b>{isBase ? vs : s.label}</b>{isBase && <div className="muted">{baseModel ? 'baseline' : `baseline · ${base.models.join(', ')}`}</div>}</td>
                      {judged && <td>{isBase ? <span className="muted" title={CONSISTENCY}>consistency {pct(s.avg_consistency)}</span>
                        : s.judged === false ? <span className="muted">not judged</span>
                        : <><b className="mono" title={FIT}>{pct(s.avg_fit_rate)}</b> {s.answers && <AnswersBar a={s.answers} />}</>}</td>}
                      {cell(t(s.avg_input_tokens), (base.avg_input_tokens ?? 0) * kb, n(t(s.avg_input_tokens)))}
                      {cell(t(s.avg_output_tokens), (base.avg_output_tokens ?? 0) * kb, n(t(s.avg_output_tokens)))}
                      {<td className="num nowrap"><span className="mono">{n(t(s.avg_reasoning_tokens))}</span>
                        {s.avg_reasoning_tokens != null && !!s.avg_output_tokens && <div className="muted">{Math.round((s.avg_reasoning_tokens / s.avg_output_tokens) * 100)}% of output</div>}</td>}
                      {cell(s.avg_output_chars, base.avg_output_chars, s.avg_output_chars == null ? '—' : n(s.avg_output_chars) + ' chars')}
                      {cell(t(s.avg_cost_usd), (base.avg_cost_usd ?? 0) * kb, whole ? money(t(s.avg_cost_usd)) : money(s.avg_cost_usd == null ? null : s.avg_cost_usd * 1000, 2))}
                      {cell(s.avg_latency_ms, base.avg_latency_ms, s.avg_latency_ms == null ? '—' : (s.avg_latency_ms / 1000).toFixed(1) + ' s')}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          {judged && <Legend />}
        </section>
      )}

      {base && cands.length > 0 && (
        <section className="panel">
          <div className="panel-head"><h2>Against {vs}</h2><span className="tick-note"><i />{vs}</span></div>
          <div className="charts">
            {MEASURES.filter(([, k]) => base[k] != null).map(([name, k]) => {
              const rows = [base, ...cands]
              const max = Math.max(...rows.map((s) => s[k] ?? 0)) || 1
              return (
                <div key={k}>
                  <h3 className="eyebrow">{name}</h3>
                  {rows.map((s) => {
                    const v = s[k] ?? null
                    const p = vsPct(v, base[k] ?? null)
                    return (
                      <div className="chart-row" key={s.label}>
                        <span className="chart-name" title={s === base ? vs : s.label}>{s === base ? vs : s.label}</span>
                        <span className="vs-track wide"><b className={s === base ? 'base' : ''} style={{ width: v == null ? 0 : (v / max) * 100 + '%' }} /><i style={{ left: ((base[k] ?? 0) / max) * 100 + '%' }} /></span>
                        <span className={'chart-val' + (s === base ? ' muted' : '')}>{s === base ? 'baseline' : p == null ? '—' : p === 0 ? 'same' : change(p)}</span>
                      </div>
                    )
                  })}
                </div>
              )
            })}
          </div>
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
              <thead><tr><th>Request</th>{sites.length > 0 && <><th>Call-site</th><th>Why picked</th></>}{cands.map((s) => <th key={s.label}>{s.label}</th>)}</tr></thead>
              <tbody>
                {results.data.results.map((r) => {
                  const url = `/audit/models/${j.id}/requests/${encodeURIComponent(r.id)}`   // ids may hold / ? # %
                  return (
                    <tr key={r.id}>
                      <td className="mono"><Link to={url}>{r.id}</Link></td>
                      {sites.length > 0 && <><td>{j.site_names[picked[r.id]?.st] ?? '—'}</td><td className="muted">{picked[r.id]?.why ?? ''}</td></>}
                      {cands.map((s) => {
                        const c = r.candidates.find((x) => x.label === s.label)
                        return (
                          <td key={s.label}>
                            {c ? (
                              <Link className={'cell v-' + c.verdict} to={`${url}?cand=${encodeURIComponent(c.label)}`} title={[judged && `${verdictWord[c.verdict]}: ${c.kept} of ${c.of} answers fit`, c.judge_reason || c.reason].filter(Boolean).join(' · ')}>
                                {c.verdict === 'within' || c.verdict === 'outside' ? <><i className={'dot d-' + c.verdict} role="img" aria-label={verdictWord[c.verdict]} /><span className="mono">{pct(c.fit_rate)}</span></> : verdictWord[c.verdict]}
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
      <p className="meta">
        {j.id} · {STRATEGY_NAME[j.strategy] ?? j.strategy} · {j.items_done} of {j.items_total} requests done
        {j.spec && (j.spec.judge === false ? ' · not judged' : ` · ${j.spec.first_runs ?? j.spec.candidate_runs} to ${j.spec.candidate_runs} answers per model · judge ${j.spec.judge_model}`)}{j.spec?.max_output_tokens ? ` · output cap ${j.spec.max_output_tokens.toLocaleString()}` : ''} · started {when(j.created_at)} by {j.trigger}
        {' · spent '}{money(j.spend_usd, 2)}{j.max_budget_usd != null && ` of ${money(j.max_budget_usd, 2)}`}
      </p>
    </Page>
  )
}
