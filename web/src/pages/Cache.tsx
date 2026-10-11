import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { Bar, BarChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { get, post } from '../api'
import {
  CAUSE, cachedShare, saved, ttlName, usecaseName, type Cause, type Diagnosis, type Overview, type Site, type SiteModel,
  type SitePage, type Totals, type Ttl, type UsecasePage,
} from '../cache'
import { count, Failed, Kpi, Loading, money, Page, pct } from '../ui'

const usd = (v: number | null | undefined) => money(v, 2)
const INK = '#000000', GREY = '#B9B5B1', GOLD = '#F7B500', RED = '#EE0000'

/** Paid and saved per day: what caching did over the window. */
function DailyChart({ days }: { days: Record<string, Totals> }) {
  const data = Object.keys(days).sort().map((d) => ({ day: d.slice(5), paid: +days[d].cost.toFixed(2), saved: +saved(days[d]).toFixed(2) }))
  return (
    <div className="chart">
      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke="#EFEDEA" />
          <XAxis dataKey="day" tick={{ fontSize: 11 }} tickLine={false} axisLine={false} />
          <YAxis tick={{ fontSize: 11 }} tickLine={false} axisLine={false} width={44} tickFormatter={(v) => '$' + v} />
          <Tooltip formatter={(v: number, name: string) => [usd(v), name === 'paid' ? 'paid' : 'saved by cache']} />
          <Bar dataKey="paid" stackId="a" fill={INK} isAnimationActive={false} />
          <Bar dataKey="saved" stackId="a" fill={GREY} radius={[3, 3, 0, 0]} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
      <div className="legend"><span><i style={{ background: INK }} />paid</span><span><i style={{ background: GREY }} />saved by cache</span></div>
    </div>
  )
}

/** A usecase's share of prompt tokens read from cache, against what its fixed parts allow. */
function ShareBar({ t, could }: { t: Totals; could?: number }) {
  const now = cachedShare(t)
  return (
    <div className="share" title={`${pct(now)} of prompt tokens read from cache${could != null ? `, ${pct(could)} possible` : ''}`}>
      <span className="share-track">{could != null && <i style={{ width: 100 * Math.min(1, could) + '%' }} />}<b style={{ width: 100 * now + '%' }} /></span>
      <span className="mono">{pct(now)}</span>
    </div>
  )
}

function CauseChip({ cause, proof }: { cause: Cause; proof?: Diagnosis['proof'] }) {
  return <span className={'chip cause-' + cause}>{CAUSE[cause]}{proof === 'proof' && ' · needs proof'}{proof === 'safe' && ' · safe fix'}</span>
}

function Built({ at, days }: { at: string | null; days: number }) {
  return <span className="muted">last {days} days{at && `, built ${at.slice(0, 16).replace('T', ' ')} UTC`}</span>
}

/** Org level: paid, saved, still possible; per usecase. */
export function CacheOverview() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['cache', 'overview'], queryFn: () => get<Overview>('/suite/cache/overview'),
    refetchInterval: (query) => (query.state.data?.status.running ? 5000 : false) })
  const refresh = useMutation({ mutationFn: () => post<unknown>('/suite/cache/refresh'), onSuccess: () => qc.invalidateQueries({ queryKey: ['cache'] }) })
  const crumbs = [{ label: 'Cache' }, { label: 'Cache analysis' }]
  if (q.isPending) return <Page crumbs={crumbs}><Loading /></Page>
  if (q.isError) {
    return (
      <Page crumbs={crumbs}>
        <div className="head"><h1>Cache analysis</h1><button className="btn primary" onClick={() => refresh.mutate()} disabled={refresh.isPending}>Build the report</button></div>
        <Failed error={q.error} />
      </Page>
    )
  }
  const o = q.data
  const total = o.total
  return (
    <Page crumbs={crumbs}>
      <div className="head">
        <div><h1>Cache analysis</h1><Built at={o.built_at} days={o.window} /></div>
        <button className="btn" onClick={() => refresh.mutate()} disabled={refresh.isPending || o.status.running}>{o.status.running ? 'Refreshing…' : 'Refresh'}</button>
      </div>
      {o.status.error && <p className="error" role="alert">The last refresh failed: {o.status.error}</p>}
      <div className="kpis">
        <Kpi label="Paid for prompts and answers" value={usd(total.cost)} note={count(total.calls, 'call')} />
        <Kpi label="Saved by caching" value={usd(saved(total))} note={`${pct(cachedShare(total))} of prompt tokens read from cache`} />
        <Kpi label="Still possible" value={usd(o.saving)} note="from the fixes below" best={o.saving > 0 ? 'opportunity' : undefined} />
        <Kpi label="Usecases" value={o.usecases.length} note={count(o.usecases.reduce((n, u) => n + u.sites, 0), 'call-site')} />
      </div>
      <section className="panel"><div className="panel-head"><b>Paid and saved, per day</b></div><DailyChart days={o.daily} /></section>
      <section className="panel">
        <div className="panel-head"><b>By usecase</b><span className="muted">ranked by spend</span></div>
        <div className="scroll">
          <table className="grid">
            <thead><tr><th>Usecase</th><th className="num">Calls</th><th>Read from cache</th><th className="num">Paid</th><th className="num">Saved</th><th className="num">Still possible</th><th>Why not more</th></tr></thead>
            <tbody>
              {o.usecases.map((u) => (
                <tr key={u.usecase}>
                  <td><Link to={`/cache/usecases/${encodeURIComponent(u.usecase)}`}>{usecaseName(u.usecase)}</Link><div className="muted">{count(u.sites, 'call-site')}</div></td>
                  <td className="num mono">{u.total.calls.toLocaleString('en-US')}</td>
                  <td><ShareBar t={u.total} /></td>
                  <td className="num mono">{usd(u.total.cost)}</td>
                  <td className="num mono">{usd(saved(u.total))}</td>
                  <td className="num mono">{u.saving ? <b>{usd(u.saving)}</b> : <span className="muted">—</span>}</td>
                  <td><div className="row">{u.causes.filter((c) => c !== 'ok').map((c) => <CauseChip key={c} cause={c} />)}</div></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </Page>
  )
}

const siteName = (s: Site) => (s.anchors[0] ? s.anchors[0].slice(0, 60) : s.site)

/** One usecase: its call-sites. */
export function CacheUsecase() {
  const { usecase = '' } = useParams()
  const q = useQuery({ queryKey: ['cache', 'usecase', usecase], queryFn: () => get<UsecasePage>(`/suite/cache/usecases/${encodeURIComponent(usecase)}`) })
  const crumbs = [{ label: 'Cache' }, { label: 'Cache analysis', to: '/cache' }, { label: usecaseName(usecase) }]
  if (q.isPending) return <Page crumbs={crumbs}><Loading /></Page>
  if (q.isError) return <Page crumbs={crumbs}><Failed error={q.error} /></Page>
  const u = q.data
  return (
    <Page crumbs={crumbs}>
      <div className="head"><div><h1>{usecaseName(u.usecase)}</h1><Built at={u.built_at} days={u.window} /></div></div>
      <div className="kpis">
        <Kpi label="Paid" value={usd(u.total.cost)} note={count(u.total.calls, 'call')} />
        <Kpi label="Saved by caching" value={usd(saved(u.total))} note={`${pct(cachedShare(u.total))} of prompt tokens`} />
        <Kpi label="Still possible" value={usd(u.saving)} best={u.saving > 0 ? 'opportunity' : undefined} />
        <Kpi label="Call-sites" value={u.sites.length} note={u.sites.some((s) => s.unsure) ? 'some grouping unsure' : 'grouping confident'} />
      </div>
      <section className="panel"><div className="panel-head"><b>Paid and saved, per day</b></div><DailyChart days={u.days} /></section>
      <section className="panel">
        <div className="panel-head"><b>Call-sites</b><span className="muted">a call-site is one prompt template; its first fixed line names it</span></div>
        <div className="scroll">
          <table className="grid">
            <thead><tr><th>Call-site</th><th>Models</th><th className="num">Calls</th><th>Read from cache</th><th className="num">Paid</th><th className="num">Still possible</th><th>Diagnosis</th></tr></thead>
            <tbody>
              {u.sites.map((s) => {
                const diags = Object.values(s.models).map((m) => m.diagnosis).filter((d): d is Diagnosis => !!d)
                const could = Math.max(...diags.map((d) => d.achievable_share), 0)
                return (
                  <tr key={s.site}>
                    <td><Link to={`/cache/sites/${s.site}`}>{siteName(s)}</Link>{s.unsure && <span className="chip warn">grouping unsure</span>}<div className="muted mono">{s.site}</div></td>
                    <td>{Object.keys(s.models).map((m) => <div key={m} className="nowrap">{m}</div>)}</td>
                    <td className="num mono">{s.total.calls.toLocaleString('en-US')}</td>
                    <td><ShareBar t={s.total} could={could} /></td>
                    <td className="num mono">{usd(s.total.cost)}</td>
                    <td className="num mono">{s.saving ? <b>{usd(s.saving)}</b> : <span className="muted">—</span>}</td>
                    <td><div className="row">{[...new Set(diags.map((d) => d.cause))].map((c) => <CauseChip key={c} cause={c} proof={diags.find((d) => d.cause === c)?.proof} />)}</div></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </section>
    </Page>
  )
}

/** The request as a bar: what leads, what varies, and how far the cache reaches today. */
function PrefixBar({ s, m }: { s: Site; m: SiteModel }) {
  const { static_chars: st, dynamic_chars: dy, static_after_dynamic: after } = s.layout
  const lead = st * (1 - after), tail = st * after, all = lead + dy + tail || 1
  const nowShare = cachedShare(m), could = m.diagnosis?.achievable_share ?? 0
  return (
    <div className="pbar-wrap">
      <div className="pbar" role="img" aria-label={`fixed ${Math.round(lead)} characters first, ${Math.round(dy)} per-call, ${Math.round(tail)} fixed after`}>
        {lead > 0 && <span className="fixed" style={{ width: 100 * lead / all + '%' }}>fixed</span>}
        {dy > 0 && <span className="dyn" style={{ width: 100 * dy / all + '%' }}>per-call</span>}
        {tail > 0 && <span className="fixed" style={{ width: 100 * tail / all + '%' }}>fixed</span>}
        <i className="now" style={{ left: 100 * Math.min(1, nowShare) + '%' }} title={`read from cache today: ${pct(nowShare)}`} />
        {could > nowShare + 0.02 && <i className="could" style={{ left: 100 * Math.min(1, could) + '%' }} title={`possible: ${pct(could)}`} />}
      </div>
      <div className="legend">
        <span><i style={{ background: INK }} />fixed text</span><span><i style={{ background: GREY }} />changes per call</span>
        <span><i className="tick" />read from cache today ({pct(nowShare)})</span>{could > nowShare + 0.02 && <span><i className="tick" style={{ background: RED }} />possible ({pct(could)})</span>}
      </div>
    </div>
  )
}

/** Time since the previous call that shared the prefix, with the provider's lifetime lines. */
function GapChart({ t }: { t: Ttl }) {
  const order = ['first', '1m', '5m', '15m', '1h', '6h', '24h', 'more']
  const label: Record<string, string> = { first: 'first', '1m': '≤1 min', '5m': '≤5 min', '15m': '≤15 min', '1h': '≤1 h', '6h': '≤6 h', '24h': '≤24 h', more: '>24 h' }
  const secs: Record<string, number> = { '1m': 60, '5m': 300, '15m': 900, '1h': 3600, '6h': 21600, '24h': 86400 }
  const data = order.map((k) => ({ gap: label[k], calls: t.histogram[k] ?? 0, key: k }))
  const lineAt = (ttl: number) => data.findIndex((d) => secs[d.key] === ttl)
  return (
    <div className="chart">
      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={data} margin={{ top: 22, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke="#EFEDEA" />
          <XAxis dataKey="gap" tick={{ fontSize: 11 }} tickLine={false} axisLine={false} />
          <YAxis tick={{ fontSize: 11 }} tickLine={false} axisLine={false} width={44} />
          <Tooltip formatter={(v: number) => [v.toLocaleString('en-US'), 'calls']} />
          <Bar dataKey="calls" fill={INK} radius={[3, 3, 0, 0]} isAnimationActive={false} />
          {lineAt(t.default_ttl) >= 0 && <ReferenceLine x={label[order[lineAt(t.default_ttl)]]} stroke={GOLD} strokeWidth={2} label={{ value: `lifetime ${ttlName(t.default_ttl)}`, fontSize: 11, position: 'top' }} />}
          {t.best_ttl !== t.default_ttl && lineAt(t.best_ttl) >= 0 && <ReferenceLine x={label[order[lineAt(t.best_ttl)]]} stroke={RED} strokeWidth={2} label={{ value: `best ${ttlName(t.best_ttl)}`, fontSize: 11, position: 'top' }} />}
        </BarChart>
      </ResponsiveContainer>
      <p className="muted pad">Calls left of the lifetime line read the prefix from cache; calls right of it wrote it again. {t.beyond_default.toLocaleString('en-US')} of {t.calls.toLocaleString('en-US')} calls came after the {ttlName(t.default_ttl)} lifetime.</p>
    </div>
  )
}

function TtlTable({ t }: { t: Ttl }) {
  const opts = Object.entries(t.options).map(([k, v]) => ({ ttl: Number(k), ...v }))
  if (opts.length < 2) return <p className="muted pad">{t.provider} offers no other lifetime for this cache.</p>
  return (
    <table className="grid">
      <thead><tr><th>Lifetime</th><th className="num">Reads</th><th className="num">Writes</th><th className="num">Prefix cost</th><th></th></tr></thead>
      <tbody>
        {opts.map((o) => (
          <tr key={o.ttl}>
            <td>{ttlName(o.ttl)}{o.ttl === t.default_ttl && <span className="muted"> (now)</span>}</td>
            <td className="num mono">{o.reads.toLocaleString('en-US')}</td><td className="num mono">{o.writes.toLocaleString('en-US')}</td>
            <td className="num mono">{usd(o.cost)}</td>
            <td>{o.ttl === t.best_ttl && t.saving > 0 && <span className="best">saves {usd(t.saving)}</span>}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** One call-site: the diagnosis per model, the prefix bar, the gaps and the lifetime what-if. */
export function CacheSite() {
  const { site = '' } = useParams()
  const q = useQuery({ queryKey: ['cache', 'site', site], queryFn: () => get<SitePage>(`/suite/cache/sites/${site}`) })
  const crumbs = [{ label: 'Cache' }, { label: 'Cache analysis', to: '/cache' }]
  if (q.isPending) return <Page crumbs={crumbs}><Loading /></Page>
  if (q.isError) return <Page crumbs={crumbs}><Failed error={q.error} /></Page>
  const s = q.data
  return (
    <Page crumbs={[...crumbs, { label: usecaseName(s.usecase), to: `/cache/usecases/${encodeURIComponent(s.usecase)}` }, { label: 'Call-site' }]}>
      <div className="head"><div><h1>{siteName(s)}</h1><span className="muted mono">{s.site}</span> <Built at={s.built_at} days={s.window} /></div></div>
      <div className="kpis">
        <Kpi label="Paid" value={usd(s.total.cost)} note={count(s.total.calls, 'call')} />
        <Kpi label="Saved by caching" value={usd(saved(s.total))} note={`${pct(cachedShare(s.total))} of prompt tokens`} />
        <Kpi label="Still possible" value={usd(s.saving)} best={s.saving > 0 ? 'opportunity' : undefined} />
        <Kpi label="Prompt versions" value={s.versions.length} note={s.versions.length > 1 ? `${s.versions[0].first} → ${s.versions[s.versions.length - 1].last}` : 'unchanged in the window'} />
      </div>
      {Object.values(s.models).map((m) => (
        <section key={m.model} className="panel">
          <div className="panel-head"><b>{m.model}</b><span className="muted">{count(m.calls, 'call')} · {usd(m.cost)} paid · {pct(cachedShare(m))} read from cache</span></div>
          {m.diagnosis && (
            <div className="pad diag">
              <div className="row"><CauseChip cause={m.diagnosis.cause} proof={m.diagnosis.proof} />{m.diagnosis.saving > 0 && <b>worth {usd(m.diagnosis.saving)} over the window</b>}</div>
              <p>{m.diagnosis.suggestion}</p>
              <p className="muted">
                Fixed part {m.diagnosis.evidence.fixed_tokens.toLocaleString('en-US')} tokens ({m.diagnosis.evidence.fixed_leading_tokens.toLocaleString('en-US')} of them leading) of {m.diagnosis.evidence.avg_prompt_tokens.toLocaleString('en-US')} per call;
                {' '}{m.diagnosis.evidence.provider} caches from {m.diagnosis.evidence.cache_min.toLocaleString('en-US')} tokens; {m.diagnosis.evidence.tool_sets} tool set{m.diagnosis.evidence.tool_sets === 1 ? '' : 's'}; {count(m.diagnosis.evidence.versions, 'version')}.
                {m.diagnosis.proof === 'proof' && ' Changing the prompt can change answers: the fix is proven on this model with the validation engine before anyone applies it.'}
              </p>
            </div>
          )}
          <div className="pad"><PrefixBar s={s} m={m} /></div>
          {m.ttl && (
            <div className="two">
              <div><div className="panel-head"><b>Time since the previous call with this prefix</b></div><GapChart t={m.ttl} /></div>
              <div><div className="panel-head"><b>Lifetime what-if</b><span className="muted">from these gaps</span></div><TtlTable t={m.ttl} /></div>
            </div>
          )}
        </section>
      ))}
      <section className="panel"><div className="panel-head"><b>Paid and saved, per day</b></div><DailyChart days={s.days} /></section>
    </Page>
  )
}
