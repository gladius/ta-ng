import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { get } from '../api'
import { Failed, Loading, Page } from '../ui'

type Model = {
  model: string; provider: string | null; tier: string | null; mode: string; input: number | null; output: number | null
  context_window: number | null; retire_date: string | null; open_weight: boolean; family: string | null; host: string | null
  confidence: string | null; purpose: string | null; note: string | null; available: boolean | null
}
type Report = {
  source: string; has_gateway: boolean; models: Model[]
  uncatalogued: { model_name: string; underlying: string }[]; gateway_map: { id: string; chosen: string; flavours: string[] }[]
}
type Path = { to: string; to_status?: string; tier?: string; value?: string; from: { label?: string; re: string }[] }

/** The model catalog (config/models.json) with what the gateway actually serves. */
export function Models() {
  const q = useQuery({ queryKey: ['models'], queryFn: () => get<Report>('/suite/models') })
  return (
    <Page crumbs={[{ label: 'Model catalog' }]}>
      <div className="head">
        <div>
          <h1>Model catalog</h1>
          <p className="meta">The reference catalog (<span className="mono">config/models.json</span>) consolidated with what the gateway serves. Availability source: {q.data?.source ?? '…'}.</p>
        </div>
        <Link className="btn" to="/models/migrations/google">Google migration paths</Link>
      </div>
      {q.isPending ? <Loading /> : q.isError ? <Failed error={q.error} /> : <>
        {!q.data.has_gateway && <div className="note">No gateway is configured here, so availability is unknown and nothing is gated on it.</div>}
        <section className="panel">
          <div className="scroll">
            <table className="grid">
              <thead><tr><th>Model</th><th>Provider</th><th>Tier</th><th>Mode</th><th className="num">In $/1M</th><th className="num">Out $/1M</th><th className="num">Context</th><th>Availability</th></tr></thead>
              <tbody>
                {q.data.models.map((m) => (
                  <tr key={m.model}>
                    <td>
                      <b>{m.model}</b>
                      {m.retire_date && <span className="chip warn">retires {m.retire_date}</span>}
                      {m.open_weight && <span className="chip" title={m.note ?? ''}>open · {m.family}{m.host && ` · ${m.host}`}{m.confidence && ` · ${m.confidence}`}</span>}
                      {m.purpose && <span className="chip">{m.purpose}</span>}
                    </td>
                    <td>{m.provider ?? '—'}</td><td>{m.tier ?? '—'}</td><td>{m.mode}</td>
                    <td className="num mono">{m.input ?? '—'}</td><td className="num mono">{m.output ?? '—'}</td>
                    <td className="num mono">{m.context_window ? m.context_window.toLocaleString('en-US') : '—'}</td>
                    <td className="nowrap">{m.available == null ? <span className="muted">unknown</span> : m.available ? <span className="pill">available</span> : <span className="muted">not deployed</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
        {q.data.gateway_map.length > 0 && (
          <section className="panel">
            <div className="panel-head"><h2>Gateway call names</h2><span className="muted">catalog id → the deployment name called on this gateway</span></div>
            <div className="scroll">
              <table className="grid">
                <thead><tr><th>Catalog id</th><th>Calls</th><th>Names seen</th></tr></thead>
                <tbody>{q.data.gateway_map.map((g) => <tr key={g.id}><td><b>{g.id}</b></td><td className="mono">{g.chosen}</td><td className="mono muted">{g.flavours.join(', ')}</td></tr>)}</tbody>
              </table>
            </div>
          </section>
        )}
        {q.data.uncatalogued.length > 0 && (
          <section className="panel">
            <div className="panel-head"><h2>Served but not in the catalog</h2><span className="muted">add them to config/models.json to price and audit them</span></div>
            <div className="scroll">
              <table className="grid">
                <thead><tr><th>Underlying model</th><th>Deployed as</th></tr></thead>
                <tbody>{q.data.uncatalogued.map((u) => <tr key={u.underlying + u.model_name}><td className="mono">{u.underlying}</td><td className="mono">{u.model_name}</td></tr>)}</tbody>
              </table>
            </div>
          </section>
        )}
      </>}
    </Page>
  )
}

/** A vendor's recommended migration paths (config/migrations/<provider>.json). */
export function Migrations() {
  const { provider = '' } = useParams()
  const q = useQuery({ queryKey: ['migrations', provider], queryFn: () => get<Path[]>(`/suite/migrations/${encodeURIComponent(provider)}`) })
  const name = provider.charAt(0).toUpperCase() + provider.slice(1)
  return (
    <Page crumbs={[{ label: 'Model catalog', to: '/models' }, { label: `${name} migration paths` }]}>
      <div className="head"><div>
        <h1>{name} recommended migration paths</h1>
        <p className="meta">Whatever model an agent runs today, {name} recommends consolidating onto a current GA target. This is the preferred recommendation in the tier downgrade; the same-provider step down is the fallback.</p>
      </div></div>
      {q.isPending ? <Loading /> : q.isError ? <Failed error={q.error} /> : !q.data.length ? (
        <p className="empty">No migration paths configured for {provider} (<span className="mono">config/migrations/{provider}.json</span>).</p>
      ) : q.data.map((p) => (
        <section key={p.to} className="panel pad block">
          <div className="row"><span className="pill">→ {p.to}</span>{p.to_status && <span className="chip">{p.to_status}</span>}{p.tier && <span className="muted small">{p.tier}</span>}</div>
          {p.value && <p className="muted">{p.value}</p>}
          <div className="row"><span className="muted small">migrate from</span>{p.from.map((s) => <span key={s.re} className="chip">{s.label || s.re}</span>)}</div>
        </section>
      ))}
    </Page>
  )
}
