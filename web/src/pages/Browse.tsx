import { useQuery } from '@tanstack/react-query'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { get } from '../api'
import { dgmodeOf, modeQuery, page } from '../agent'
import { Failed, Loading, Page } from '../ui'

type Source = { id: string; name: string }
type Workspace = { id: string; name: string }
type Agents = { name: string; agents: { id: string; name: string; runs?: number | null }[] }

/** The downgrade mode the audit was opened in (Tier downgrade or Open-weight), kept through the browse pages. */
function useMode() {
  const [params] = useSearchParams()
  return dgmodeOf(params.get('dgmode'))
}

export function Sources() {
  const mode = useMode()
  const ow = mode === 'open-weight'
  const q = useQuery({ queryKey: ['sources'], queryFn: () => get<Source[]>('/suite/sources') })
  return (
    <Page crumbs={[{ label: 'Agent audits' }]}>
      <div className="head"><div>
        <h1>{ow ? 'Open-weight audit' : 'Tier downgrade audit'}</h1>
        <p className="meta">Pick a data source, then an agent. Its recorded calls are grouped by call-site, and the ones you choose are re-run on {ow ? 'a same-tier open-weight model' : 'a cheaper tier'} to prove whether the behaviour holds.</p>
      </div></div>
      {q.isPending ? <Loading /> : q.isError ? <Failed error={q.error} /> : !q.data.length ? (
        <p className="empty">No data source has credentials on this server.</p>
      ) : (
        <div className="tiles">
          {q.data.map((d) => (
            <Link key={d.id} className="panel tile link-tile" to={`/sources/${encodeURIComponent(d.id)}${modeQuery(mode)}`}><h2>{d.name}</h2></Link>
          ))}
        </div>
      )}
    </Page>
  )
}

export function Workspaces() {
  const { source = '' } = useParams()
  const mode = useMode()
  const q = useQuery({ queryKey: ['workspaces', source], queryFn: () => get<Workspace[]>(`/suite/sources/${encodeURIComponent(source)}/workspaces`) })
  return (
    <Page crumbs={[{ label: 'Agent audits', to: '/sources' + modeQuery(mode) }, { label: source }]}>
      <div className="head"><div><h1>Workspaces</h1><p className="meta">{source}</p></div></div>
      {q.isPending ? <Loading /> : q.isError ? <Failed error={q.error} /> : !q.data.length ? (
        <p className="empty">No workspaces: check this source's credentials, or it may be unreachable.</p>
      ) : (
        <section className="panel">
          {q.data.map((w) => (
            <Link key={w.id} className="list-row" to={`/sources/${encodeURIComponent(source)}/${encodeURIComponent(w.id)}${modeQuery(mode)}`}>
              <b>{w.name}</b><span className="mono muted">{w.id}</span>
            </Link>
          ))}
        </section>
      )}
    </Page>
  )
}

export function Agents() {
  const { source = '', ws = '' } = useParams()
  const mode = useMode()
  const audit = '?next=select' + modeQuery(mode, '&')
  const q = useQuery({ queryKey: ['agents', source, ws], queryFn: () => get<Agents>(`/suite/sources/${encodeURIComponent(source)}/workspaces/${encodeURIComponent(ws)}/agents`) })
  return (
    <Page crumbs={[{ label: 'Agent audits', to: '/sources' + modeQuery(mode) }, { label: source, to: `/sources/${encodeURIComponent(source)}${modeQuery(mode)}` }, { label: q.data?.name ?? ws }]}>
      <div className="head"><div><h1>Agents</h1><p className="meta">{q.data?.name ?? ws}</p></div></div>
      {q.isPending ? <Loading /> : q.isError ? <Failed error={q.error} /> : !q.data.agents.length ? (
        <p className="empty">No agents in this workspace.</p>
      ) : (
        <section className="panel">
          <div className="scroll">
            <table className="grid">
              <thead><tr><th>Agent</th><th className="num">Runs</th><th /></tr></thead>
              <tbody>
                {q.data.agents.map((a) => {
                  const ids = { source, ws, project: a.name }
                  return (
                    <tr key={a.id}>
                      <td><Link to={page(ids, 'build', audit)}><b>{a.name}</b></Link></td>
                      <td className="num">{a.runs ?? '—'}</td>
                      <td className="num nowrap"><Link to={page(ids, 'build', '?next=profile' + modeQuery(mode, '&'))}>Profile</Link> · <Link to={page(ids, 'build', audit)}>Audit ›</Link></td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </Page>
  )
}
