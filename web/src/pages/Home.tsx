import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { candidates, get, type Job } from '../api'
import { count, Failed, Loading, Page, PassRows } from '../ui'

type PriceGap = { model: string; catalog: { input: number; output: number }; gateway: { input: number; output: number } }
type Item = { module: string; title: string; detail: string; to: string; rank: number }
const SHOWN = 8

/** What needs someone's attention across the modules, most important first. */
function attention(jobs: Job[], gaps: PriceGap[]): Item[] {
  const items: Item[] = []
  for (const j of jobs) {
    const to = `/validations/${j.id}`
    if (j.status === 'failed') items.push({ rank: 0, module: 'Validations', title: `${j.id} failed.`, detail: j.error ?? '', to })
    if (j.status === 'stopped') items.push({ rank: 0, module: 'Validations', title: `${j.id} stopped.`, detail: j.stop_reason ?? '', to })
    const cands = candidates(j.summary)
    for (const s of cands.filter((s) => s.outside)) {
      items.push({ rank: 1, module: 'Validations', title: `${j.id}: ${s.label} drifts on ${s.outside} of ${count(s.requests, 'request')}.`,
        detail: 'The batch page lists where.', to: to + '?show=problems' })
    }
    const unverified = cands.reduce((t, s) => t + s.unverified, 0)
    if (unverified) items.push({ rank: 2, module: 'Validations', title: `${j.id}: ${unverified} request checks could not be verified.`,
      detail: "Each request's page says why.", to: to + '?show=problems' })
  }
  for (const g of gaps) {
    items.push({ rank: 1, module: 'Prices', title: `${g.model} is priced $${g.catalog.input} / $${g.catalog.output} in the catalog;`,
      detail: `the gateway charges $${g.gateway.input} / $${g.gateway.output} per million tokens, so its costs here are off.`, to: '/models' })
  }
  return items.sort((a, b) => a.rank - b.rank)
}

/** The suite at a glance: what needs attention, then one tile per module. */
export default function Home() {
  const jobs = useQuery({ queryKey: ['batches', 'home'], queryFn: () => get<Job[]>('/validations?with_summary=1&per_page=20') })
  const gaps = useQuery({ queryKey: ['price-gaps'], queryFn: () => get<PriceGap[]>('/suite/price-gaps') })
  const items = jobs.data && gaps.data ? attention(jobs.data, gaps.data) : []
  return (
    <Page crumbs={[{ label: 'Overview' }]}>
      <div className="head">
        <div>
          <h1>Overview</h1>
          <p className="meta">What the suite found, across its modules.</p>
        </div>
      </div>

      <section className="panel attention" aria-label="Needs attention">
        <div className="attention-head"><span className="dot" aria-hidden="true" /><h2>Needs attention</h2><span className="muted">most important first</span></div>
        {jobs.isPending || gaps.isPending ? <Loading /> : jobs.isError ? <Failed error={jobs.error} /> : gaps.isError ? <Failed error={gaps.error} /> : !items.length ? (
          <p className="muted">Nothing right now.</p>
        ) : (
          <>
            {items.slice(0, SHOWN).map((x, i) => {
              return (
                <Link key={i} className="attention-row" to={x.to}>
                  <span className="attention-module">{x.module}</span>
                  <span className="attention-text"><b>{x.title}</b> <span className="muted">{x.detail}</span></span>
                  <span aria-hidden="true" className="muted">→</span>
                </Link>
              )
            })}
            {items.length > SHOWN && <p className="muted">and {items.length - SHOWN} more in the batches below.</p>}
          </>
        )}
      </section>

      <div className="tiles">
        <section className="panel tile">
          <div className="tile-head"><h2>Model validations</h2><Link to="/validations">All batches</Link></div>
          {jobs.isPending ? <Loading /> : jobs.isError ? <Failed error={jobs.error} /> : !jobs.data.length ? (
            <p className="muted">No batches yet. Teams send them through the API.</p>
          ) : jobs.data.slice(0, 4).map((j) => (
            <div key={j.id} className="tile-row">
              <div className="spread"><Link className="mono" to={`/validations/${j.id}`}>{j.id}</Link><span className="muted">{count(j.items_total, 'request')}</span></div>
              <PassRows summary={j.summary} />
            </div>
          ))}
        </section>
        <section className="panel tile">
          <div className="tile-head"><h2>Agent audits</h2></div>
          <p className="muted">Pick an agent from a data source to find its call-sites, then check whether a cheaper tier or an open-weight model keeps its behaviour.</p>
          <div className="row"><Link className="btn primary" to="/sources">Audit an agent</Link><Link className="btn" to="/models">Model catalog</Link></div>
        </section>
        <section className="panel tile">
          <div className="tile-head"><h2>Cache</h2><span className="chip">later</span></div>
          <p className="muted">Which call-sites could cache their prompts, why they don't, and what it would save.</p>
        </section>
      </div>
    </Page>
  )
}
