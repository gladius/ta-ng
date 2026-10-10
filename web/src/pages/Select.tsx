import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import { ApiError, get } from '../api'
import { api, crumbs, modeQuery, page, useAgent, type Funnel } from '../agent'
import { Failed, Loading, money, Page } from '../ui'

const TOP = 5

/** Choose which call-sites to audit: the costliest five with something to test start ticked; change any by hand. */
export default function Select() {
  const { a, snap, dgmode } = useAgent()
  const [, setParams] = useSearchParams()
  const go = useNavigate()
  const q = useQuery({ queryKey: ['select', a.source, a.ws, a.project, snap, dgmode], queryFn: () => get<Funnel>(api(a, 'select', `?snap=${snap}&dgmode=${dgmode}`)), retry: false })
  const [picked, setPicked] = useState<Set<string> | null>(null)       // null: the default, the top five
  const [find, setFind] = useState('')
  const [graph, setGraph] = useState('')

  if (q.error instanceof ApiError && q.error.status === 404) return <Navigate replace to={page(a, 'build', `?next=select${modeQuery(dgmode, '&')}`)} />
  const where = crumbs(a, 'choose', dgmode)
  if (q.isPending) return <Page crumbs={where}><Loading /></Page>
  if (q.isError) return <Page crumbs={where}><Failed error={q.error} /></Page>
  const f = q.data
  const chosen = picked ?? new Set(f.rows.slice(0, TOP).filter((r) => r.testable && r.opportunity > 0).map((r) => r.key))
  const toggle = (key: string) => {
    const next = new Set(chosen)
    if (next.has(key)) next.delete(key); else next.add(key)
    setPicked(next)
  }
  const rows = f.rows.filter((r) => (!graph || r.graph_path === graph) && (!find || r.node.toLowerCase().includes(find.toLowerCase())))

  return (
    <Page crumbs={where}>
      <div className="head">
        <div>
          <div className="title"><h1>Choose what to audit</h1>{f.revision && <span className="chip mono" title="Audited on one version: the latest deployment revision">rev {f.revision.slice(0, 12)}</span>}</div>
          <p className="meta">
            {f.traces === 0 ? 'No traces for this agent yet, so there is nothing to audit.'
              : <><b>{f.rows.length} call-sites</b> from <b>{f.traces} traces</b>{f.revision && ' (latest revision only)'}, costliest first. The ticked ones are re-run live; only those are proven.</>}
          </p>
        </div>
        <Link className="btn" to={page(a, 'profile', `?snap=${snap}${modeQuery(dgmode, '&')}`)}>Deployment profile</Link>
      </div>

      {!f.rows.length ? (
        <p className="empty">{f.traces ? `The latest revision has ${f.traces} traces but no LLM call-sites to audit.` : 'Nothing to audit yet.'}</p>
      ) : (
        <section className="panel">
          <div className="panel-head">
            <div className="seg" role="group" aria-label="Downgrade targets">
              <button type="button" aria-pressed={dgmode === 'commercial'} onClick={() => setParams({ snap })}>Commercial · one tier down</button>
              <button type="button" aria-pressed={dgmode === 'open-weight'} onClick={() => setParams({ snap, dgmode: 'open-weight' })}>Open-weight · same tier</button>
            </div>
            {(f.graphs.length > 0 || f.rows.length > 12) && (
              <div className="row">
                {f.rows.length > 12 && <><label htmlFor="sel-find" className="sr">Filter call-sites</label><input id="sel-find" className="input" type="search" placeholder="Filter call-sites by name" value={find} onChange={(e) => setFind(e.target.value)} /></>}
                {f.graphs.length > 0 && (
                  <select className="input" aria-label="Graph" value={graph} onChange={(e) => setGraph(e.target.value)}>
                    <option value="">All graphs</option>
                    {f.graphs.map((g) => <option key={g} value={g}>{g}</option>)}
                  </select>
                )}
              </div>
            )}
          </div>
          <div className="scroll">
            <table className="grid">
              <thead><tr><th style={{ width: 36 }} /><th>Call-site</th><th>Model</th><th className="num">Cost / {Math.round(f.calls / 1000)}k calls</th><th>{dgmode === 'open-weight' ? 'Open-weight peer' : 'One tier down'}</th></tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.key} className={r.testable ? '' : 'dim'}>
                    <td><input type="checkbox" aria-label={'Audit ' + r.node} checked={chosen.has(r.key)} disabled={!r.testable} onChange={() => toggle(r.key)} /></td>
                    <td><b>{r.node}</b>{r.graph_path && <span className="muted small"> {r.graph_path}</span>}</td>
                    <td className="mono muted">{r.model}</td>
                    <td className="num mono">{money(r.cost, 0)}</td>
                    <td>{r.downgrade_to ? <span className="chip">↓ {r.downgrade_to}</span> : !r.testable && <span className="muted small">nothing to test</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pad spread">
            <span className="muted">{chosen.size ? `${chosen.size} selected` : 'none selected'}</span>
            <button type="button" className="btn primary" disabled={!chosen.size}
              onClick={() => go(page(a, 'audit', `?snap=${snap}&dgmode=${dgmode}&keys=${encodeURIComponent([...chosen].join(','))}`))}>
              Audit selected →
            </button>
          </div>
        </section>
      )}
    </Page>
  )
}
