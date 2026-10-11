import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { get } from '../api'
import { usecaseName } from '../cache'
import { count, Failed, Loading, Page, when } from '../ui'

type Profile = {
  site: string; usecase: string | null; name: string; does: string; task: string; output: string
  fixed: string | null; per_call: string | null; model: string; tokens: number | null; created_at: string | null
}
type Listing = { page: number; per_page: number; total: number; profiles: Profile[] }

/** Every call-site the profiler has named: what it is, where it runs, when it was profiled. */
export default function CallSites() {
  const [params, setParams] = useSearchParams()
  const page = Math.max(1, Number(params.get('page')) || 1)
  const usecase = params.get('usecase') ?? ''
  const [q, setQ] = useState(params.get('q') ?? '')
  const query = new URLSearchParams({ page: String(page), per_page: '50', ...(usecase && { usecase }), ...(q && { q }) })
  const list = useQuery({ queryKey: ['profiles', page, usecase, q], queryFn: () => get<Listing>(`/suite/profiles?${query}`) })
  const crumbs = [{ label: 'Call-sites' }, { label: 'Profiles' }]
  return (
    <Page crumbs={crumbs}>
      <div className="head">
        <div><h1>Call-site profiles</h1><span className="muted">one prompt template each, named by the profiler from a real request; shared by every module</span></div>
        <input className="input" placeholder="Find by name, purpose or usecase" value={q} onChange={(e) => { setQ(e.target.value); setParams({ ...(usecase && { usecase }), q: e.target.value }) }} />
      </div>
      {usecase && <p className="muted">In usecase <b>{usecaseName(usecase)}</b> · <Link to="/profiles">all usecases</Link></p>}
      {list.isPending ? <Loading /> : list.isError ? <Failed error={list.error} /> : !list.data.profiles.length ? (
        <p className="empty">No call-site profiled yet. A cache refresh profiles every new call-site.</p>
      ) : (
        <section className="panel">
          <div className="panel-head"><b>{count(list.data.total, 'call-site')}</b><span className="muted">newest first</span></div>
          <div className="scroll">
            <table className="grid">
              <thead><tr><th>Call-site</th><th>Usecase</th><th>Task</th><th>Same every call</th><th>Changes per call</th><th>Profiled</th></tr></thead>
              <tbody>
                {list.data.profiles.map((p) => (
                  <tr key={p.site}>
                    <td><Link to={`/cache/sites/${p.site}`}><b>{p.name}</b></Link><div className="muted">{p.does}</div></td>
                    <td><Link to={`/profiles?usecase=${encodeURIComponent(p.usecase ?? 'None')}`}>{usecaseName(p.usecase)}</Link></td>
                    <td><span className="chip">{p.task}</span> <span className="chip">{p.output} out</span></td>
                    <td className="muted">{p.fixed}</td>
                    <td className="muted">{p.per_call || '—'}</td>
                    <td className="muted nowrap">{p.created_at ? when(p.created_at) : '—'}<div className="mono">{p.model}</div></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {list.data.total > list.data.per_page && (
            <div className="pager pad">
              <button className="btn" disabled={page === 1} onClick={() => setParams({ ...(usecase && { usecase }), ...(q && { q }), page: String(page - 1) })}>‹ Previous</button>
              <span className="muted">page {page} of {Math.ceil(list.data.total / list.data.per_page)}</span>
              <button className="btn" disabled={page * list.data.per_page >= list.data.total} onClick={() => setParams({ ...(usecase && { usecase }), ...(q && { q }), page: String(page + 1) })}>Next ›</button>
            </div>
          )}
        </section>
      )}
    </Page>
  )
}
