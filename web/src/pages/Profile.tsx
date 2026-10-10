import { useQuery } from '@tanstack/react-query'
import { Link, Navigate } from 'react-router-dom'
import { ApiError, get } from '../api'
import { api, crumbs, download, modeQuery, page, useAgent, type Profile as ProfileData, type ProfileNode } from '../agent'
import { count, Failed, Kpi, Loading, Markdown, money, Page, pct } from '../ui'

/** One call-site: what it is and does, how much it runs and costs, and its cheaper option if it has one. */
function Node({ n, basis }: { n: ProfileNode; basis: string }) {
  const llm = n.kind === 'llm'
  return (
    <article className={'node node-' + n.kind}>
      <div className="row node-top">
        <span className="op">{n.op || n.kind}</span>
        <b>{n.node}</b>
        {llm && <span className="mono muted small">{n.model}</span>}
        {llm && n.tier && <span className="chip">{n.tier}</span>}
      </div>
      {n.summary ? <div className="node-sum"><Markdown text={n.summary} /></div>
        : llm && <p className="muted">{n.unavailable ? `Analysis unavailable: ${n.unavailable}` : `Shape: ${n.shape}.`}</p>}
      <div className="node-metrics">
        <span><b>{n.calls}</b> calls</span>
        {llm && <span>in <b>{(n.avg_in ?? 0).toLocaleString('en-US')}</b> → out <b>{(n.avg_out ?? 0).toLocaleString('en-US')}</b> tokens</span>}
        {llm && n.cost?.per_basis != null && <span><b>{money(n.cost.per_basis, 2)}</b> / {basis}</span>}
        {!llm && n.avg_ms ? <span><b>{n.avg_ms}</b> ms avg</span> : null}
        {n.error_rate ? <span className="bad">errors {pct(n.error_rate)}</span> : null}
        {n.opp?.downgrade_to && <span className="chip">↓ {n.opp.downgrade_to} · ~{money(n.opp.downgrade_usd ?? 0, 2)}</span>}
      </div>
    </article>
  )
}

/** What the deployment does: measured facts per call-site, plus the advisory per-node analysis. */
export default function Profile() {
  const { a, snap, dgmode } = useAgent()
  const skip = new URLSearchParams(location.search).get('skip') === '1'
  const q = useQuery({ queryKey: ['profile', a.source, a.ws, a.project, snap], queryFn: () => get<{ profile: ProfileData; analysed: boolean }>(api(a, 'profile', `?snap=${snap}`)), retry: false })
  const where = crumbs(a, 'profile', dgmode)

  // Redirect only on a fresh answer: a cached one may be out of date, and analysing again is paid.
  if (q.isFetching && (q.error || (q.data && !q.data.analysed && !skip))) return <Page crumbs={where}><Loading /></Page>
  if (q.error instanceof ApiError && q.error.status === 404) return <Navigate replace to={page(a, 'build', `?next=profile${modeQuery(dgmode, '&')}`)} />
  if (q.data && !q.data.analysed && !skip) return <Navigate replace to={page(a, 'analyze', `?snap=${snap}${modeQuery(dgmode, '&')}`)} />
  if (q.isPending) return <Page crumbs={where}><Loading /></Page>
  if (q.isError) return <Page crumbs={where}><Failed error={q.error} /></Page>

  const p = q.data.profile, h = p.header, d = p.deployment
  const basis = `${Math.round(h.cost_basis / 1000)}k calls`
  return (
    <Page crumbs={where}>
      <div className="head">
        <div>
          <p className="eyebrow">Deployment profile · <span className="mono">{h.agent}</span></p>
          <h1>{d?.summary && d.kind ? d.kind : 'What this deployment does'}</h1>
        </div>
        <div className="row">
          <a className="btn" href={download(a, 'profile', snap)}>Download profile (.zip)</a>
          <Link className="btn primary" to={page(a, 'select', `?snap=${snap}${modeQuery(dgmode, '&')}`)}>Audit savings ›</Link>
        </div>
      </div>
      {d?.summary ? <section className="summary"><Markdown text={d.summary} /></section>
        : <p className="meta"><Link to={page(a, 'analyze', `?snap=${snap}`)}>Describe each call-site</Link> (one model call each).</p>}

      <div className="kpis">
        <Kpi label={`Cost / ${basis}`} value={money(h.total_cost_basis, 2)} />
        {h.opportunity_total > 0 && <Kpi label={`Opportunity / ${basis}`} value={money(h.opportunity_total, 2)} best={count(h.opp_wins, 'node')} />}
        <Kpi label="Call-sites" value={h.call_sites} note={`${count(h.tool_count, 'tool')} · ${count(h.retriever_count, 'retriever')}`} />
        <Kpi label="Traces" value={h.traces.toLocaleString('en-US')} note={`errors ${pct(h.error_rate)}`} />
      </div>

      {p.hotspots && (
        <p className="meta row">
          {p.hotspots.costliest && <span><b>Costliest:</b> {p.hotspots.costliest.node} ({money(p.hotspots.costliest.per_basis, 2)})</span>}
          <span><b>Busiest:</b> {p.hotspots.busiest.node} ({p.hotspots.busiest.calls}×)</span>
          {p.hotspots.slowest && <span><b>Slowest:</b> {p.hotspots.slowest.node} ({p.hotspots.slowest.avg_ms} ms)</span>}
          {h.revisions?.length ? <span><b>Versions:</b> {h.revisions.join(', ')}</span> : null}
        </p>
      )}
      {h.price_incomplete && <div className="note">Some models are not in the price catalog, so their cost shows "—". Add them to <span className="mono">config/models.json</span>.</div>}

      {p.sections.map((s) => (
        <section key={s.name + s.graph_path} className="block">
          <h2 className="section-title">{s.name} {s.graph_path && <span className="muted small">subgraph</span>}</h2>
          {s.nodes.map((n) => <Node key={n.node + (n.model ?? '')} n={n} basis={basis} />)}
        </section>
      ))}
    </Page>
  )
}
