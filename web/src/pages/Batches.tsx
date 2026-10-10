import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { get, type Job } from '../api'
import { Failed, Loading, money, Page, PassRows, Status, when } from '../ui'

const PER_PAGE = 50

/** Every validation batch, newest first, each with how its labels did. */
export default function Batches() {
  const [params, setParams] = useSearchParams()
  const page = Math.max(1, Number(params.get('page')) || 1)
  const q = useQuery({
    queryKey: ['batches', page],
    queryFn: () => get<Job[]>(`/validations?with_summary=1&page=${page}&per_page=${PER_PAGE}`),
    refetchInterval: (query) => (query.state.data?.some((j) => j.status === 'queued' || j.status === 'running') ? 5000 : false),
  })
  return (
    <Page crumbs={[{ label: 'Models' }, { label: 'Validations' }]}>
      <div className="head">
        <div>
          <h1>Validations</h1>
          <p className="meta">Batches sent through the API. Each request is re-run on its original model and on every candidate, and each candidate is judged against the range of answers the original itself gives.</p>
        </div>
        <a className="btn" href="/docs">API reference</a>
      </div>
      {q.isPending ? <Loading /> : q.isError ? <Failed error={q.error} /> : !q.data.length ? (
        <p className="empty">No validations yet.</p>
      ) : (
        <section className="panel">
          <div className="scroll">
            <table className="grid">
              <thead>
                <tr><th>Batch</th><th>Status</th><th>Requests</th><th>Labels</th><th className="num">Spent</th><th>Started</th></tr>
              </thead>
              <tbody>
                {q.data.map((j) => (
                  <tr key={j.id}>
                    <td className="mono"><Link to={j.id}>{j.id}</Link></td>
                    <td><Status status={j.status} /></td>
                    <td>{j.items_done} <span className="muted">of {j.items_total}</span></td>
                    <td><PassRows summary={j.summary} /></td>
                    <td className="num mono">{money(j.spend_usd, 4)}</td>
                    <td className="muted">{when(j.created_at).split(' ').map((x) => <span key={x} className="nowrap">{x} </span>)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      <div className="pager">
        {page > 1 && <button className="btn" onClick={() => setParams({ page: String(page - 1) })}>‹ Newer</button>}
        {q.data?.length === PER_PAGE && <button className="btn" onClick={() => setParams({ page: String(page + 1) })}>Older ›</button>}
      </div>
    </Page>
  )
}
