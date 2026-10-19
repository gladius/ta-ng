import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Bar, BarChart, CartesianGrid, Cell, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { get, post } from '../api'
import { PromptMap } from '../PromptMap'
import {
  CAUSE, reuse, saved, siteName, ttlName, usecaseName, type Cause, type Overview, type Proof, type Site, type SitePage,
  type Totals, type Trend, type Ttl, type UsecasePage,
} from '../cache'
import { count, Failed, Kpi, Loading, money, Page, pct } from '../ui'

const usd = (v: number | null | undefined) => money(v, 2)
const INK = '#000000', RED = '#EE0000', GREY = '#9A9693'
const WINDOWS = [7, 30, 90] as const

/** The last `n` days of a per-day series. */
const lastDays = (days: Record<string, Totals>, n: number) => Object.keys(days).sort().slice(-n).map((d) => [d, days[d]] as const)
const NUMS = ['calls', 'prompt_tokens', 'cached_tokens', 'written_tokens', 'completion_tokens', 'calls_cached', 'cost', 'cost_nocache', 'calls_with_tools', 'ms_hit', 'ms_miss', 'timed_hit', 'timed_miss'] as const
const sumDays = (days: Record<string, Totals>, n: number): Totals => {
  const out: Totals = { calls: 0, prompt_tokens: 0, cached_tokens: 0, written_tokens: 0, completion_tokens: 0, calls_cached: 0, cost: 0, cost_nocache: 0, calls_with_tools: 0, unsure: false, ms_hit: 0, ms_miss: 0, timed_hit: 0, timed_miss: 0 }
  for (const [, d] of lastDays(days, n)) for (const k of NUMS) (out as Record<string, number | boolean>)[k] = ((out[k] as number) || 0) + ((d[k] as number) || 0)
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
      {[...new Set([...WINDOWS.filter((w) => w < max), max])].map((w) => <button key={w} type="button" aria-pressed={Math.min(n, max) === w} onClick={() => set(w)}>{w} days</button>)}
    </div>
  )
}

/** One opportunity or drop: what, where, what to do, what it's worth. */
/** Org level: how much is reused, is it getting better, where is the money left. */
export function CacheOverview() {
  const qc = useQueryClient()
  const [n0, setN] = useState<number>(30)
  const [find, setFind] = useState('')
  const [page, setPage] = useState(0)
  const PER = 25
  const q = useQuery({ queryKey: ['cache', 'overview'], queryFn: () => get<Overview>('/audit/cache/overview'),
    refetchInterval: (query) => (query.state.data?.status.running ? 5000 : false) })
  const refresh = useMutation({ mutationFn: () => post<unknown>('/audit/cache/refresh'), onSuccess: () => qc.invalidateQueries({ queryKey: ['cache'] }) })
  const crumbs = [{ label: 'Agentic workloads' }, { label: 'Cache audit' }]
  if (q.isPending) return <Page crumbs={crumbs}><Loading /></Page>
  if (q.isError) {
    return (
      <Page crumbs={crumbs}>
        <div className="head"><h1>Cache audit</h1><button className="btn primary" onClick={() => refresh.mutate()} disabled={refresh.isPending}>Analyse all usecases</button></div>
        <Failed error={q.error} />
      </Page>
    )
  }
  const o = q.data
  const n = Math.min(n0, o.window)
  const t = sumDays(o.daily, n)
  const shown = o.usecases.filter((u) => !find || usecaseName(u.usecase).toLowerCase().includes(find.toLowerCase()))
  const pageRows = shown.slice(page * PER, page * PER + PER)
  return (
    <Page crumbs={crumbs}>
      <div className="head">
        <div><h1>Cache audit</h1><Built at={o.built_at} /></div>
        <div className="row"><WindowPick n={n} set={setN} max={o.window} /><button className="btn" onClick={() => refresh.mutate()} disabled={refresh.isPending || o.status.running || !o.status.days.missing.length} title={o.status.days.missing.length ? `process ${o.status.days.missing.length} day(s) of the log not processed yet` : 'every day of the window is processed'}>{o.status.running ? `Processing ${o.status.days.queued.length} day(s)…` : o.status.days.missing.length ? `Process ${o.status.days.missing.length} new day(s)` : 'Up to date'}</button></div>
      </div>
      {o.status.error && <p className="error" role="alert">The last refresh failed: {o.status.error}</p>}
      <div className="kpis">
        <Kpi label="Reused from cache" value={pct(reuse(t))} note={<>of prompt tokens, last {n} days <TrendWord t={o.trend} /></>} />
        <Kpi label="Calls that reused something" value={pct(t.calls ? t.calls_cached / t.calls : 0)} note={count(t.calls, 'call')} />
        <Kpi label="Saved by caching" value={usd(saved(t))} note="estimated, catalog prices" />
        <Kpi label="Still possible" value={usd(o.saving)} note={`over the ${o.window}-day window`} best={o.saving > 0 ? 'opportunity' : undefined} />
      </div>
      <section className="panel"><div className="panel-head"><b>Reused from cache, per day</b></div><ReuseTrend days={o.daily} n={n} /></section>
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
                  <td><Link to={`/audit/cache/usecases/${encodeURIComponent(u.usecase)}`}>{usecaseName(u.usecase)}</Link><div className="muted">{count(u.sites, 'call-site')}{u.unsure && ' · few calls'}</div></td>
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

/** Prompt tokens three ways: read from the cache, written to it, plain. */
function SplitBar({ t }: { t: Totals }) {
  const all = t.prompt_tokens || 1, read = t.cached_tokens / all, write = t.written_tokens / all, plain = Math.max(0, 1 - read - write)
  return (
    <div className="split" title={`${pct(read)} read from the cache, ${pct(write)} written to it, ${pct(plain)} plain`}>
      <span className="split-track"><b className="read" style={{ width: 100 * read + '%' }} /><b className="write" style={{ width: 100 * write + '%' }} /></span>
      <span className="muted nowrap">{pct(read)} read · {pct(write)} written</span>
    </div>
  )
}

/** Latency of calls that hit the cache against calls that missed, when both were timed. */
function timeSaved(t: Totals): { hit: number; miss: number } | null {
  if (!t.timed_hit || !t.timed_miss || !t.ms_hit || !t.ms_miss) return null
  return { hit: t.ms_hit / t.timed_hit, miss: t.ms_miss / t.timed_miss }
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
function SiteCard({ s, usecase }: { s: Site; usecase: string | null }) {
  return (
    <section className="panel site-card">
      <div className="spread">
        <div><Link to={`/audit/cache/sites/${s.site}`}><b>{siteName(s)}</b></Link>{s.profile && <span className="muted"> · {s.profile.does}</span>} <Link className="muted" to={`/usecases/${encodeURIComponent(usecase ?? 'None')}/callsites/${s.site}`}>the call-site</Link></div>
        {s.story.worth > 0 && <b className="nowrap">{usd(s.story.worth)}</b>}
      </div>
      <div className="row"><CauseChip cause={s.story.cause} proof={s.story.proof} />{s.unsure && <span className="chip warn">few calls</span>}<TrendWord t={s.trend} /><SplitBar t={s.total} /></div>
      <p>{s.story.now} {s.story.why}</p>
      <p><b>{s.story.action}</b></p>
    </section>
  )
}

/** One usecase: its reuse, trend, and each call-site as a card. */
export function CacheUsecase() {
  const { usecase = '' } = useParams()
  const [n0, setN] = useState<number>(30)
  const q = useQuery({ queryKey: ['cache', 'usecase', usecase], queryFn: () => get<UsecasePage>(`/audit/cache/usecases/${encodeURIComponent(usecase)}`) })
  const crumbs = [{ label: 'Agentic workloads' }, { label: 'Cache audit', to: '/audit/cache' }, { label: usecaseName(usecase) }]
  if (q.isPending) return <Page crumbs={crumbs}><Loading /></Page>
  if (q.isError) return <Page crumbs={crumbs}><Failed error={q.error} /></Page>
  const u = q.data
  const n = Math.min(n0, u.window)
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
      <h2>{count(u.sites.length, 'call-site')} <span className="muted">the ones with something to fix first</span></h2>
      {u.sites.map((s) => <SiteCard key={s.site} s={s} usecase={u.usecase} />)}
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
      <p className="muted">Gold: reused today ({pct(now)}){could > now + 0.02 && <>; red: possible ({pct(could)})</>}.</p>
    </div>
  )
}

/** Time since the previous call with the same prefix, against the cache lifetime. */
/** One sentence: would a longer lifetime pay, is caching a loss, or can the lifetime not be set at all. */
function ttlVerdict(t: Ttl, days: number): string {
  const late = t.beyond_default - (t.histogram.first ?? 0)
  const choices = Object.keys(t.options).map(Number).filter((x) => x > 0)
  if (t.loss > 0) return `Caching costs more than it saves: the prompt is written far more often than it is read back. Not marking it saves ${usd(t.loss)} over ${days} days.`
  if (choices.length < 2) return `${t.provider === 'google' ? 'Gemini' : 'This provider'}'s cache lifetime is fixed at ${ttlName(t.default_ttl)}; ${late.toLocaleString('en-US')} calls came too late to reuse the prompt.`
  if (t.best_ttl !== t.default_ttl && t.saving > 0) {
    const more = (t.options[t.best_ttl]?.reads ?? 0) - (t.options[t.default_ttl]?.reads ?? 0)
    return `A ${ttlName(t.best_ttl)} lifetime would save ${usd(t.saving)} over ${days} days: ${more.toLocaleString('en-US')} more calls would reuse the prompt.`
  }
  return late > 0 ? `A longer lifetime wouldn't pay: the ${late.toLocaleString('en-US')} late calls don't cover the extra cost of longer-lived writes.` : `Every repeat call comes within ${ttlName(t.default_ttl)}: the lifetime is not the limit.`
}

/** Time since the previous call with the same prompt: black reused it (within the lifetime), red came too late, grey
    was the first call with that prompt (no lifetime helps). */
function GapChart({ t }: { t: Ttl }) {
  const order = ['first', '1m', '5m', '15m', '1h', '6h', '24h', 'more']
  const label: Record<string, string> = { first: 'first ever', '1m': '≤1 min', '5m': '≤5 min', '15m': '≤15 min', '1h': '≤1 h', '6h': '≤6 h', '24h': '≤24 h', more: '>24 h' }
  const secs: Record<string, number> = { '1m': 60, '5m': 300, '15m': 900, '1h': 3600, '6h': 21600, '24h': 86400, more: Infinity }
  const data = order.map((k) => ({ gap: label[k], calls: t.histogram[k] ?? 0, key: k }))
  const fill = (k: string) => (k === 'first' ? GREY : secs[k] <= t.default_ttl ? INK : RED)
  return (
    <div className="chart">
      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={data} margin={{ top: 12, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke="#EFEDEA" />
          <XAxis dataKey="gap" tick={{ fontSize: 11 }} tickLine={false} axisLine={false} />
          <YAxis tick={{ fontSize: 11 }} tickLine={false} axisLine={false} width={44} />
          <Tooltip formatter={(v: number) => [v.toLocaleString('en-US'), 'calls']} />
          <Bar dataKey="calls" radius={[3, 3, 0, 0]} isAnimationActive={false}>
            {data.map((d) => <Cell key={d.key} fill={fill(d.key)} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
      <p className="muted pad"><span className="swatch" style={{ background: INK }} /> reused (within {ttlName(t.default_ttl)}) · <span className="swatch" style={{ background: RED }} /> too late · <span className="swatch" style={{ background: GREY }} /> first call with this prompt</p>
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
            <td>{o.ttl === t.best_ttl && t.saving > 0 && <span className="best">saves {usd(t.saving)}</span>}{o.ttl === 0 && t.loss > 0 && <span className="best">saves {usd(t.loss)}</span>}</td>
          </tr>
        ))}
        <tr><td className="muted">today, as logged</td><td className="num muted">—</td><td className="num muted">—</td><td className="num mono">{usd(t.current.cost)}</td><td>{t.loss > 0 && <span className="chip warn">caching is a loss</span>}</td></tr>
      </tbody>
    </table>
  )
}

/** One call-site: what it is, what happens now, why, what to do; then the evidence. */
export function CacheSite() {
  const { site = '' } = useParams()
  const [n0, setN] = useState<number>(30)
  const q = useQuery({ queryKey: ['cache', 'site', site], queryFn: () => get<SitePage>(`/audit/cache/sites/${site}`) })
  const crumbs = [{ label: 'Agentic workloads' }, { label: 'Cache audit', to: '/audit/cache' }]
  if (q.isPending) return <Page crumbs={crumbs}><Loading /></Page>
  if (q.isError) return <Page crumbs={crumbs}><Failed error={q.error} /></Page>
  const s = q.data
  const n = Math.min(n0, s.window)
  const t = sumDays(s.days, n)
  const main = Object.values(s.models).sort((a, b) => b.calls - a.calls)[0]
  const lifetimeMatters = !!main?.ttl
  return (
    <Page crumbs={[...crumbs, { label: usecaseName(s.usecase), to: `/audit/cache/usecases/${encodeURIComponent(s.usecase)}` }, { label: siteName(s) }]}>
      <div className="head">
        <div>
          <h1>{siteName(s)}</h1>
          {s.profile && <p className="lede">{s.profile.does}</p>}
          <div className="row">
            {Object.keys(s.models).map((m) => <span key={m} className="chip mono">{m}</span>)}
            <span className="chip">{count(s.versions.length, 'prompt version')}</span>
            {s.unsure && <span className="chip warn">few calls</span>}
            <Link to={`/usecases/${encodeURIComponent(s.usecase ?? 'None')}/callsites/${s.site}`}>the call-site</Link>
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
        <div className="pad"><h2>What happens now</h2><p>{s.story.now} {s.story.why}</p>
          {s.grams ? <PromptMap grams={s.grams} models={Object.fromEntries(Object.entries(s.models).sort((a, b) => b[1].calls - a[1].calls).map(([m, v]) => [m, {
            provider: v.diagnosis?.evidence.provider ?? 'unknown', cache_min: v.diagnosis?.evidence.cache_min ?? 1024, calls: v.calls, calls_cached: v.calls_cached,
            prompt_tokens: v.prompt_tokens, cached_tokens: v.cached_tokens, written_tokens: v.written_tokens }]))} toolSets={s.layout.tool_sets} /> : <PrefixBar s={s} />}</div>
        <div className="pad"><h2>What to do</h2><div className="row"><CauseChip cause={s.story.cause} proof={s.story.proof} /></div><p><b>{s.story.action}</b>{s.story.worth > 0 && <> Worth about <b>{usd(s.story.worth)}</b> over {s.window} days at today's traffic.</>}</p>
        </div>
      </section>
      <h2>Evidence</h2>
      <section className="panel">
        <div className="panel-head"><b>By model</b></div>
        <table className="grid">
          <thead><tr><th>Model</th><th className="num">Calls</th><th>Reused</th><th>Prompt tokens</th><th>Latency, hit vs miss</th><th className="num">Paid</th><th>Diagnosis</th></tr></thead>
          <tbody>
            {Object.values(s.models).sort((a, b) => b.calls - a.calls).map((m) => { const ts = timeSaved(m); return (
              <tr key={m.model}><td className="mono">{m.model}</td><td className="num mono">{m.calls.toLocaleString('en-US')}</td><td><ShareBar share={reuse(m)} could={m.diagnosis?.achievable_share} /></td><td><SplitBar t={m} /></td>
                <td className="mono">{ts ? <>{(ts.hit / 1000).toFixed(1)} s vs {(ts.miss / 1000).toFixed(1)} s{ts.miss > ts.hit && <span className="muted"> · {pct(1 - ts.hit / ts.miss)} faster</span>}</> : <span className="muted">—</span>}</td>
                <td className="num mono">{usd(m.cost)}</td><td><CauseChip cause={m.diagnosis?.cause ?? null} proof={m.diagnosis?.proof} /></td></tr>) })}
          </tbody>
        </table>
      </section>
      {lifetimeMatters && main.ttl && (
        <section className="panel">
          <div className="panel-head"><b>Cache lifetime</b><span className="muted">{main.model}</span></div>
          <p className="pad"><b>{ttlVerdict(main.ttl, n)}</b></p>
          <div className="two"><div><GapChart t={main.ttl} /></div><div><TtlTable t={main.ttl} /></div></div>
        </section>
      )}
      <section className="panel"><div className="panel-head"><b>Reused from cache, per day</b></div><ReuseTrend days={s.days} n={n} /></section>
    </Page>
  )
}
