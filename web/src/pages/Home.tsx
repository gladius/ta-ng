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
          <div className="tile-head"><h2>Model audit</h2><Link to="/model-audits">All audits</Link></div>
          <p className="muted">Would a cheaper, open-weight or newly released model hold on a usecase's call-sites? Sampled from the log, re-run and judged with evidence.</p>
          <div className="row"><Link className="btn primary" to="/usecases">Pick a usecase</Link><Link className="btn" to="/models">Model catalog</Link></div>
        </section>
        <section className="panel tile">
          <div className="tile-head"><h2>Cache audit</h2><Link to="/cache">Cache audit</Link></div>
          <p className="muted">What caching saves today per usecase and call-site, why it isn't more, and what each fix is worth.</p>
        </section>
      </div>
    </Page>
  )
}
