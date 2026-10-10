import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get, type Job } from '../api'
import { count, Failed, Loading, Page, PassRows } from '../ui'

/** The suite at a glance: one tile per module. */
export default function Home() {
  const jobs = useQuery({ queryKey: ['batches', 'home'], queryFn: () => get<Job[]>('/validations?with_summary=1&per_page=4') })
  return (
    <Page crumbs={[{ label: 'Overview' }]}>
      <div className="head"><h1>Overview</h1></div>
      <div className="tiles">
        <section className="panel tile">
          <div className="tile-head"><h2>Model validations</h2><Link to="/validations">All batches</Link></div>
          {jobs.isPending ? <Loading /> : jobs.isError ? <Failed error={jobs.error} /> : !jobs.data.length ? (
            <p className="muted">No batches yet. Teams send them through the API.</p>
          ) : jobs.data.map((j) => (
            <div key={j.id} className="tile-row">
              <div className="spread"><Link className="mono" to={`/validations/${j.id}`}>{j.id}</Link><span className="muted">{count(j.items_total, 'request')}</span></div>
              <PassRows summary={j.summary} />
            </div>
          ))}
        </section>
        <section className="panel tile">
          <div className="tile-head"><h2>Agent audits</h2></div>
          <p className="muted">Whether a cheaper tier or an open-weight model keeps an agent's behaviour.</p>
          <div className="row"><Link className="btn primary" to="/sources">Audit an agent</Link><Link className="btn" to="/models">Model catalog</Link></div>
        </section>
        <section className="panel tile">
          <div className="tile-head"><h2>Cache</h2><span className="chip">later</span></div>
          <p className="muted">Which call-sites could cache their prompts, and what it would save.</p>
        </section>
      </div>
    </Page>
  )
}
