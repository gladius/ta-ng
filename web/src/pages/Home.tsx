import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { get, type Job } from '../api'
import { count, Failed, Loading, Page, PassRows } from '../ui'

/** The suite at a glance: one tile per module. */
export default function Home() {
  const jobs = useQuery({ queryKey: ['model-audits', 'home'], queryFn: () => get<Job[]>('/audit/models?with_summary=1&per_page=4') })
  return (
    <Page crumbs={[{ label: 'Overview' }]}>
      <div className="head"><h1>Overview</h1></div>
      <div className="tiles">
        <section className="panel tile">
          <div className="tile-head"><h2>Recent model audits</h2><Link to="/audit/models">All audits</Link></div>
          {jobs.isPending ? <Loading /> : jobs.isError ? <Failed error={jobs.error} /> : !jobs.data.length ? (
            <p className="muted">No audits yet.</p>
          ) : jobs.data.map((j) => (
            <div key={j.id} className="tile-row">
              <div className="spread"><Link className="mono" to={`/audit/models/${j.id}`}>{j.id}</Link><span className="muted">{count(j.items_total, 'request')}</span></div>
              <PassRows summary={j.summary} />
            </div>
          ))}
        </section>
        <section className="panel tile">
          <div className="tile-head"><h2>Model audit</h2><Link to="/audit/models">All audits</Link></div>
          <div className="row"><Link className="btn primary" to="/usecases">Pick a usecase</Link><Link className="btn" to="/models">Model catalog</Link></div>
        </section>
      </div>
    </Page>
  )
}
