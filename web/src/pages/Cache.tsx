import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Bar, BarChart, CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { get, post } from '../api'
import {
  CAUSE, reuse, saved, siteName, ttlName, usecaseName, type Brief, type Cause, type Overview, type Proof, type Site, type SitePage,
  type Totals, type Trend, type Ttl, type UsecasePage,
} from '../cache'
import { count, Failed, Kpi, Loading, money, Page, pct } from '../ui'

const usd = (v: number | null | undefined) => money(v, 2)
const INK = '#000000', GOLD = '#F7B500', RED = '#EE0000'
const WINDOWS = [7, 14, 30] as const

/** The last `n` days of a per-day series. */
const lastDays = (days: Record<string, Totals>, n: number) => Object.keys(days).sort().slice(-n).map((d) => [d, days[d]] as const)
const NUMS = ['calls', 'prompt_tokens', 'cached_tokens', 'written_tokens', 'completion_tokens', 'calls_cached', 'cost', 'cost_nocache', 'calls_with_tools'] as const
const sumDays = (days: Record<string, Totals>, n: number): Totals => {
  const out: Totals = { calls: 0, prompt_tokens: 0, cached_tokens: 0, written_tokens: 0, completion_tokens: 0, calls_cached: 0, cost: 0, cost_nocache: 0, calls_with_tools: 0, unsure: false }
  for (const [, d] of lastDays(days, n)) for (const k of NUMS) out[k] += d[k] || 0
  return out
}

/** Reuse share per day: the one trend a reader needs. */
function ReuseTrend({ days, n }: { days: Record<string, Totals>; n: number }) {
  const data = lastDays(days, n).map(([d, t]) => ({ day: d.slice(5), reuse: Math.round(100 * reuse(t)) }))
  return (
    <div className="chart">
      <ResponsiveContainer width="100%" height={180}>
        <LineChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke="#EFEDEA" />
          <XAxis dataKey="day" tick={{ fontSize: 11 }} tickLine={false} axisLine={false} />
          <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} tickLine={false} axisLine={false} width={40} tickFormatter={(v) => v + '%'} />
          <Tooltip formatter={(v: number) => [v + '%', 'reused from cache']} />
          <Line type="monotone" dataKey="reuse" stroke={INK} strokeWidth={2} dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
      <p className="muted pad">Share of prompt tokens reused from cache, per day. A step down means something broke the prefix (a prompt change, a deploy).</p>
    </div>
  )
}

function TrendWord({ t }: { t: Trend }) {
  if (!t) return null
  const d = Math.round(100 * t.delta)
  return <span className={d <= -5 ? 'trend down' : d >= 5 ? 'trend up' : 'trend'}>{d > 0 ? '▲' : d < 0 ? '▼' : '='} {Math.abs(d)} pts vs the week before</span>
}

function CauseChip({ cause, proof }: { cause: Cause | null; proof?: Proof }) {
  if (!cause) return null
  return <span className={'chip cause-' + cause}>{CAUSE[cause]}{proof === 'proof' && ' · needs proof'}{proof === 'safe' && ' · safe fix'}</span>
}

function Built({ at }: { at: string | null }) {
  return at ? <span className="muted">built {at.slice(0, 16).replace('T', ' ')} UTC</span> : null
}

function WindowPick({ n, set, max }: { n: number; set: (n: number) => void; max: number }) {
  return (
    <div className="seg" role="group" aria-label="Window">
      {WINDOWS.filter((w) => w <= Math.max(max, 7)).map((w) => <button key={w} type="button" aria-pressed={n === w} onClick={() => set(w)}>{w} days</button>)}
    </div>
  )
}

/** One opportunity or drop: what, where, what to do, what it's worth. */
function BriefRow({ b }: { b: Brief }) {
  return (
    <div className="brief">
      <div className="spread">
        <span><Link to={`/cache/sites/${b.site}`}><b>{b.name}</b></Link> <span className="muted">in {usecaseName(b.usecase)} · {count(b.calls, 'call')} · {pct(b.share)} reused</span></span>
        {b.worth > 0 && <b className="nowrap">{usd(b.worth)}</b>}
      </div>
      <div className="row"><CauseChip cause={b.cause} proof={b.proof} />{b.trend && <TrendWord t={b.trend} />}</div>
      <p>{b.action}</p>
    </div>
  )
}

/** Org level: how much is reused, is it getting better, where is the money left. */
export function CacheOverview() {
  const qc = useQueryClient()
  const [n, setN] = useState<number>(14)
  const [find, setFind] = useState('')
  const [page, setPage] = useState(0)
  const PER = 25
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
  const t = sumDays(o.daily, n)
  const well = o.usecases.filter((u) => u.saving === 0 && u.causes.every((c) => c === 'ok' || c === 'below_min' || c === 'sparse'))
  const shown = o.usecases.filter((u) => !find || usecaseName(u.usecase).toLowerCase().includes(find.toLowerCase()))
  const pageRows = shown.slice(page * PER, page * PER + PER)
  return (
    <Page crumbs={crumbs}>
      <div className="head">
        <div><h1>Cache analysis</h1><Built at={o.built_at} /></div>
        <div className="row"><WindowPick n={n} set={setN} max={o.window} /><button className="btn" onClick={() => refresh.mutate()} disabled={refresh.isPending || o.status.running}>{o.status.running ? 'Refreshing…' : 'Refresh'}</button></div>
      </div>
      {o.status.error && <p className="error" role="alert">The last refresh failed: {o.status.error}</p>}
      <div className="kpis">
        <Kpi label="Reused from cache" value={pct(reuse(t))} note={<>of prompt tokens, last {n} days <TrendWord t={o.trend} /></>} />
        <Kpi label="Calls that reused something" value={pct(t.calls ? t.calls_cached / t.calls : 0)} note={count(t.calls, 'call')} />
        <Kpi label="Saved by caching" value={usd(saved(t))} note="estimated, catalog prices" />
        <Kpi label="Still possible" value={usd(o.saving)} note={`over the ${o.window}-day window, from the fixes below`} best={o.saving > 0 ? 'opportunity' : undefined} />
      </div>
      <section className="panel"><div className="panel-head"><b>Reused from cache, per day</b></div><ReuseTrend days={o.daily} n={n} /></section>
      <div className="two-col">
        <section className="panel">
          <div className="panel-head"><b>Biggest opportunities</b><span className="muted">what's left, ranked by value</span></div>
          {o.opportunities.length ? o.opportunities.map((b) => <BriefRow key={b.site} b={b} />) : <p className="muted pad">Nothing left to fix.</p>}
        </section>
        <section className="panel">
          <div className="panel-head"><b>Dropped this week</b><span className="muted">reuse fell 5 points or more</span></div>
          {o.dropped.length ? o.dropped.map((b) => <BriefRow key={b.site} b={b} />) : <p className="muted pad">No call-site dropped.</p>}
          <div className="panel-head"><b>Doing well</b><span className="muted">{count(well.length, 'usecase')} with nothing to fix</span></div>
        </section>
      </div>
      <section className="panel">
        <div className="panel-head">
          <b>All usecases</b>
          <div className="row"><input className="input" placeholder="Find a usecase" value={find} onChange={(e) => { setFind(e.target.value); setPage(0) }} /><span className="muted">{shown.length} of {o.usecases.length}</span></div>
        </div>
        <div className="scroll">
          <table className="grid">
            <thead><tr><th>Usecase</th><th className="num">Calls</th><th>Reused</th><th>Trend</th><th className="num">Still possible</th><th>Why not more</th></tr></thead>
            <tbody>
              {pageRows.map((u) => (
                <tr key={u.usecase}>
                  <td><Link to={`/cache/usecases/${encodeURIComponent(u.usecase)}`}>{usecaseName(u.usecase)}</Link><div className="muted">{count(u.sites, 'call-site')}{u.unsure && ' · grouping unsure'}</div></td>
                  <td className="num mono">{u.total.calls.toLocaleString('en-US')}</td>
                  <td><ShareBar share={reuse(u.total)} /></td>
                  <td><TrendWord t={u.trend} /></td>
                  <td className="num mono">{u.saving ? <b>{usd(u.saving)}</b> : <span className="muted">—</span>}</td>
                  <td><div className="row">{u.causes.filter((c) => c !== 'ok').map((c) => <CauseChip key={c} cause={c} />)}</div></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {shown.length > PER && (
          <div className="pager pad">
            <button className="btn" disabled={page === 0} onClick={() => setPage(page - 1)}>‹ Previous</button>
            <span className="muted">page {page + 1} of {Math.ceil(shown.length / PER)}</span>
            <button className="btn" disabled={(page + 1) * PER >= shown.length} onClick={() => setPage(page + 1)}>Next ›</button>
          </div>
        )}
      </section>
    </Page>
  )
}

function ShareBar({ share, could }: { share: number; could?: number }) {
  return (
    <div className="share" title={`${pct(share)} of prompt tokens reused${could != null ? `, ${pct(could)} possible` : ''}`}>
      <span className="share-track">{could != null && <i style={{ width: 100 * Math.min(1, could) + '%' }} />}<b style={{ width: 100 * share + '%' }} /></span>
      <span className="mono">{pct(share)}</span>
    </div>
  )
}

/** One call-site as a card: what it is, what happens, what to do. */
function SiteCard({ s }: { s: Site }) {
  return (
    <section className="panel site-card">
      <div className="spread">
        <div><Link to={`/cache/sites/${s.site}`}><b>{siteName(s)}</b></Link>{s.profile && <span className="muted"> · {s.profile.does}</span>}</div>
        {s.story.worth > 0 && <b className="nowrap">{usd(s.story.worth)}</b>}
      </div>
      <div className="row"><CauseChip cause={s.story.cause} proof={s.story.proof} />{s.unsure && <span className="chip warn">grouping unsure</span>}<TrendWord t={s.trend} /></div>
      <p>{s.story.now} {s.story.why}</p>
      <p><b>{s.story.action}</b></p>
    </section>
  )
}

/** One usecase: its reuse, trend, and each call-site as a card. */
export function CacheUsecase() {
  const { usecase = '' } = useParams()
  const [n, setN] = useState<number>(14)
  const q = useQuery({ queryKey: ['cache', 'usecase', usecase], queryFn: () => get<UsecasePage>(`/suite/cache/usecases/${encodeURIComponent(usecase)}`) })
  const crumbs = [{ label: 'Cache' }, { label: 'Cache analysis', to: '/cache' }, { label: usecaseName(usecase) }]
  if (q.isPending) return <Page crumbs={crumbs}><Loading /></Page>
  if (q.isError) return <Page crumbs={crumbs}><Failed error={q.error} /></Page>
  const u = q.data
  const t = sumDays(u.days, n)
  return (
    <Page crumbs={crumbs}>
      <div className="head"><div><h1>{usecaseName(u.usecase)}</h1><Built at={u.built_at} /></div><WindowPick n={n} set={setN} max={u.window} /></div>
      <div className="kpis">
        <Kpi label="Reused from cache" value={pct(reuse(t))} note={<>of prompt tokens, last {n} days <TrendWord t={u.trend} /></>} />
        <Kpi label="Calls" value={t.calls.toLocaleString('en-US')} note={`${pct(t.calls ? t.calls_cached / t.calls : 0)} reused something`} />
        <Kpi label="Saved by caching" value={usd(saved(t))} note="estimated, catalog prices" />
        <Kpi label="Still possible" value={usd(u.saving)} note={`over ${u.window} days`} best={u.saving > 0 ? 'opportunity' : undefined} />
      </div>
      <section className="panel"><div className="panel-head"><b>Reused from cache, per day</b></div><ReuseTrend days={u.days} n={n} /></section>
      <h2>{count(u.sites.length, 'call-site')} <span className="muted">a call-site is one prompt template this usecase sends; the ones with something to fix come first</span></h2>
      {u.sites.map((s) => <SiteCard key={s.site} s={s} />)}
    </Page>
  )
}

/** The request as a bar: the part that is the same every call, the part that changes, and how far reuse reaches. */
function PrefixBar({ s }: { s: Site }) {
  const { static_chars: st, dynamic_chars: dy, static_after_dynamic: after } = s.layout
  const lead = st * (1 - after), tail = st * after, all = lead + dy + tail || 1
  const now = s.story.share
  const could = Math.max(...Object.values(s.models).map((m) => m.diagnosis?.achievable_share ?? 0), 0)
  return (
    <div className="pbar-wrap">
      <div className="pbar" role="img" aria-label={`${Math.round(100 * lead / all)}% same every call first, ${Math.round(100 * dy / all)}% different every call, ${Math.round(100 * tail / all)}% same every call after`}>
        {lead > 0 && <span className="fixed" style={{ width: 100 * lead / all + '%' }}>same every call</span>}
        {dy > 0 && <span className="dyn" style={{ width: 100 * dy / all + '%' }}>different every call</span>}
        {tail > 0 && <span className="fixed" style={{ width: 100 * tail / all + '%' }}>same every call</span>}
        <i className="now" style={{ left: 100 * Math.min(1, now) + '%' }} />
        {could > now + 0.02 && <i className="could" style={{ left: 100 * Math.min(1, could) + '%' }} />}
      </div>
      <p className="muted">
        A prompt is read left to right. The cache can only reuse the part that is the same every call <em>and</em> comes before anything that differs.
        The gold mark is how much is reused today ({pct(now)}){could > now + 0.02 && <>; the red mark is how much could be ({pct(could)})</>}.
      </p>
    </div>
  )
}

/** Time since the previous call with the same prefix, against the cache lifetime. */
function GapChart({ t }: { t: Ttl }) {
  const order = ['first', '1m', '5m', '15m', '1h', '6h', '24h', 'more']
  const label: Record<string, string> = { first: 'first ever', '1m': '≤1 min', '5m': '≤5 min', '15m': '≤15 min', '1h': '≤1 h', '6h': '≤6 h', '24h': '≤24 h', more: '>24 h' }
  const secs: Record<string, number> = { '1m': 60, '5m': 300, '15m': 900, '1h': 3600, '6h': 21600, '24h': 86400 }
  const data = order.map((k) => ({ gap: label[k], calls: t.histogram[k] ?? 0, key: k }))
  const at = (ttl: number) => data.find((d) => secs[d.key] === ttl)?.gap
  return (
    <div className="chart">
      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={data} margin={{ top: 22, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke="#EFEDEA" />
          <XAxis dataKey="gap" tick={{ fontSize: 11 }} tickLine={false} axisLine={false} />
          <YAxis tick={{ fontSize: 11 }} tickLine={false} axisLine={false} width={44} />
          <Tooltip formatter={(v: number) => [v.toLocaleString('en-US'), 'calls']} />
          <Bar dataKey="calls" fill={INK} radius={[3, 3, 0, 0]} isAnimationActive={false} />
          {at(t.default_ttl) && <ReferenceLine x={at(t.default_ttl)} stroke={GOLD} strokeWidth={2} label={{ value: `cache lasts ${ttlName(t.default_ttl)}`, fontSize: 11, position: 'top' }} />}
          {t.best_ttl !== t.default_ttl && at(t.best_ttl) && <ReferenceLine x={at(t.best_ttl)} stroke={RED} strokeWidth={2} label={{ value: `with ${ttlName(t.best_ttl)}`, fontSize: 11, position: 'top' }} />}
        </BarChart>
      </ResponsiveContainer>
      <p className="muted pad">How long after the previous call with the same prompt each call came. Calls left of the gold line found the prompt still in cache; calls right of it had to write it again ({t.beyond_default.toLocaleString('en-US')} of {t.calls.toLocaleString('en-US')}).</p>
    </div>
  )
}

function TtlTable({ t }: { t: Ttl }) {
  const opts = Object.entries(t.options).map(([k, v]) => ({ ttl: Number(k), ...v }))
  return (
    <table className="grid">
      <thead><tr><th>Cache lifetime</th><th className="num">Calls that reuse</th><th className="num">Calls that write</th><th className="num">Cost of the prefix</th><th></th></tr></thead>
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

/** One call-site: what it is, what happens now, why, what to do; then the evidence. */
export function CacheSite() {
  const { site = '' } = useParams()
  const [n, setN] = useState<number>(14)
  const q = useQuery({ queryKey: ['cache', 'site', site], queryFn: () => get<SitePage>(`/suite/cache/sites/${site}`) })
  const crumbs = [{ label: 'Cache' }, { label: 'Cache analysis', to: '/cache' }]
  if (q.isPending) return <Page crumbs={crumbs}><Loading /></Page>
  if (q.isError) return <Page crumbs={crumbs}><Failed error={q.error} /></Page>
  const s = q.data
  const t = sumDays(s.days, n)
  const main = Object.values(s.models).sort((a, b) => b.calls - a.calls)[0]
  const lifetimeMatters = main?.ttl && (Object.keys(main.ttl.options).length > 1 || s.story.cause === 'gaps' || s.story.cause === 'sparse')
  return (
    <Page crumbs={[...crumbs, { label: usecaseName(s.usecase), to: `/cache/usecases/${encodeURIComponent(s.usecase)}` }, { label: siteName(s) }]}>
      <div className="head">
        <div>
          <h1>{siteName(s)}</h1>
          {s.profile && <p className="lede">{s.profile.does}</p>}
          <div className="row">
            {s.profile && <><span className="chip">{s.profile.task}</span><span className="chip">{s.profile.output} out</span></>}
            {Object.keys(s.models).map((m) => <span key={m} className="chip mono">{m}</span>)}
            <span className="chip">{count(s.versions.length, 'prompt version')}</span>
            {s.unsure && <span className="chip warn">grouping unsure</span>}
          </div>
        </div>
        <WindowPick n={n} set={setN} max={s.window} />
      </div>
      <div className="kpis">
        <Kpi label="Reused from cache" value={pct(reuse(t))} note={<>of prompt tokens, last {n} days <TrendWord t={s.trend} /></>} />
        <Kpi label="Calls" value={t.calls.toLocaleString('en-US')} note={`${pct(t.calls ? t.calls_cached / t.calls : 0)} reused something`} />
        <Kpi label="Saved by caching" value={usd(saved(t))} note="estimated, catalog prices" />
        <Kpi label="Still possible" value={usd(s.story.worth)} note={`over ${s.window} days`} best={s.story.worth > 0 ? 'opportunity' : undefined} />
      </div>
      <section className="panel story">
        <div className="pad"><h2>What happens now</h2><p>{s.story.now} {s.story.why}</p><PrefixBar s={s} /></div>
        <div className="pad"><h2>What to do</h2><div className="row"><CauseChip cause={s.story.cause} proof={s.story.proof} /></div><p><b>{s.story.action}</b>{s.story.worth > 0 && <> Worth about <b>{usd(s.story.worth)}</b> over {s.window} days at today's traffic.</>}</p>
          {s.story.proof === 'proof' && <p className="muted">A prompt change can change answers. Before anyone applies it, the changed prompt is run on the same model against the original and judged; the result shows here.</p>}
        </div>
      </section>
      <h2>Evidence</h2>
      <section className="panel">
        <div className="panel-head"><b>By model</b><span className="muted">the same call-site may be sent to several models; the cache rules differ by provider</span></div>
        <table className="grid">
          <thead><tr><th>Model</th><th className="num">Calls</th><th>Reused</th><th className="num">Paid</th><th>Diagnosis</th></tr></thead>
          <tbody>
            {Object.values(s.models).sort((a, b) => b.calls - a.calls).map((m) => (
              <tr key={m.model}><td className="mono">{m.model}</td><td className="num mono">{m.calls.toLocaleString('en-US')}</td><td><ShareBar share={reuse(m)} could={m.diagnosis?.achievable_share} /></td><td className="num mono">{usd(m.cost)}</td><td><CauseChip cause={m.diagnosis?.cause ?? null} proof={m.diagnosis?.proof} /></td></tr>
            ))}
          </tbody>
        </table>
      </section>
      {lifetimeMatters && main.ttl && (
        <section className="panel">
          <div className="panel-head"><b>Cache lifetime</b><span className="muted">{main.model}</span></div>
          <div className="two"><div><GapChart t={main.ttl} /></div><div><TtlTable t={main.ttl} /></div></div>
        </section>
      )}
      <section className="panel"><div className="panel-head"><b>Reused from cache, per day</b></div><ReuseTrend days={s.days} n={n} /></section>
    </Page>
  )
}
