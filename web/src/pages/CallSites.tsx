import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { get } from '../api'
import { CAUSE, usecaseName, type Cause, type Grams, type MapModel, type Proof } from '../cache'
import { PromptMap } from '../PromptMap'
import { count, Failed, Kpi, Loading, money, Page, when } from '../ui'
import { type Verdict } from './ModelAudits'

type Profile = { name: string; does: string; task: string; output: string; fixed: string | null; per_call: string | null; model: string; created_at: string | null } | null
type CacheDecision = { cause: Cause | null; action: string; worth: number; proof: Proof; share: number; models: string[]; built_at: string | null } | null
type Site = {
  site: string; usecase: string | null; name: string; signature: string[]; markers: string[]; calls: number; first_day: string; last_day: string
  versions: { version: string; first: string; last: string }[]; static_chars: number; dynamic_chars: number; static_after: number
  tool_sets: number; unsure: boolean; window_days: number; updated_at: string | null; profile: Profile; grams: Grams
}
type Pick = { model: string; cost_vs_original_pct?: number; latency_vs_original_pct?: number; audit: string } | null
type Models = { candidates: Record<string, Verdict & { audit: string; at: string; trigger: string; valid?: boolean }>; cheapest?: Pick; fastest?: Pick } | null
type Days = { window: number; done: string[]; queued: string[]; missing: string[] }
type UsecaseRow = { usecase: string | null; calls: number; first_day: string; last_day: string; sites: number | null; analysed_at: string | null; error: string | null; decisions?: { models?: { sites: number; with_cheaper: number; saving: number; cost: number } } }
type Usecases = { page: number; per_page: number; total: number; listed_at: string | null; reloading: boolean; running: string[]; skipped: string[]; usecases: UsecaseRow[] }
type Coverage = { calls: number; other_format: number; no_usage: number; unpriced: number; repeated: number; placed: number; unplaced: number }
type UsecasePage = UsecaseRow & { sites: (Site & { cache: CacheDecision; decisions: { models?: Models } })[]; analysing: boolean; days: Days; coverage: Coverage }
type SitePage = Site & { decisions: { cache: CacheDecision; models: Models; router: null }; cache_models: Record<string, MapModel> }

const ucPath = (u: string | null) => `/usecases/${encodeURIComponent(u ?? 'None')}`
const sitePath = (u: string | null, site: string) => `${ucPath(u)}/callsites/${site}`

/** Every usecase in the log window, with how many call-sites were found in it. */
export function CallSiteUsecases() {
  const [params, setParams] = useSearchParams()
  const page = Math.max(1, Number(params.get('page')) || 1)
  const [q, setQ] = useState(params.get('q') ?? '')
  const list = useQuery({ queryKey: ['callsites', 'usecases', page, q], queryFn: () => get<Usecases>(`/suite/callsites/usecases?page=${page}&per_page=50${q ? '&q=' + encodeURIComponent(q) : ''}`),
    refetchInterval: (query) => (query.state.data?.reloading || query.state.data?.running.length ? 5000 : false) })
  const crumbs = [{ label: 'Agentic workloads' }, { label: 'Usecases' }]
  return (
    <Page crumbs={crumbs}>
      <div className="head">
        <div><h1>Usecases</h1><span className="muted">{list.data?.listed_at && <>list from {when(list.data.listed_at)}{list.data.reloading && ', reloading'}</>}{!!list.data?.skipped.length && <> · not analysed: usecases like {list.data.skipped.join(', ')}</>}</span></div>
        <div className="row"><input className="input" placeholder="Find a usecase" value={q} onChange={(e) => { setQ(e.target.value); setParams({ q: e.target.value }) }} /><button className="btn" onClick={() => get(`/suite/callsites/usecases?reload=true`).then(() => list.refetch())}>Reload list</button></div>
      </div>
      {list.isPending ? <Loading /> : list.isError ? <Failed error={list.error} /> : !list.data.usecases.length ? (
        <p className="empty">{list.data.reloading ? 'Reading the usecases from the log…' : 'No usecases in the window.'}</p>
      ) : (
        <section className="panel">
          <div className="panel-head"><b>{count(list.data.total, 'usecase')}</b><span className="muted">most calls first</span></div>
          <table className="grid">
            <thead><tr><th>Usecase</th><th className="num">Calls in the window</th><th>Active</th><th className="num">Call-sites</th><th>Analysed</th></tr></thead>
            <tbody>
              {list.data.usecases.map((u) => (
                <tr key={u.usecase ?? 'None'}>
                  <td><Link to={ucPath(u.usecase)}>{usecaseName(u.usecase)}</Link></td>
                  <td className="num mono">{u.calls.toLocaleString('en-US')}</td>
                  <td className="muted nowrap">{u.first_day} → {u.last_day}</td>
                  <td className="num mono">{u.sites ?? '—'}</td>
                  <td>{list.data.running.length ? <span className="chip">processing days…</span> : u.error ? <span className="chip warn" title={u.error}>failed</span> : u.analysed_at ? <span className="muted nowrap">{when(u.analysed_at)}</span> : <span className="muted">no day processed yet</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {list.data.total > list.data.per_page && (
            <div className="pager pad">
              <button className="btn" disabled={page === 1} onClick={() => setParams({ q, page: String(page - 1) })}>‹ Previous</button>
              <span className="muted">page {page} of {Math.ceil(list.data.total / list.data.per_page)}</span>
              <button className="btn" disabled={page * list.data.per_page >= list.data.total} onClick={() => setParams({ q, page: String(page + 1) })}>Next ›</button>
            </div>
          )}
        </section>
      )}
    </Page>
  )
}

/** One usecase: its call-sites as cards. */
export function CallSiteUsecase() {
  const { usecase = '' } = useParams()
  const q = useQuery({ queryKey: ['callsites', 'usecase', usecase], queryFn: () => get<UsecasePage>(`/suite/callsites/usecases/${encodeURIComponent(usecase)}`),
    refetchInterval: (query) => (query.state.data?.analysing ? 4000 : false) })
  const crumbs = [{ label: 'Agentic workloads' }, { label: 'Usecases', to: '/usecases' }, { label: usecaseName(usecase) }]
  if (q.isPending) return <Page crumbs={crumbs}><Loading /></Page>
  if (q.isError) return <Page crumbs={crumbs}><Failed error={q.error} /></Page>
  const u = q.data
  const calls = u.sites.reduce((n, s) => n + s.calls, 0)
  return (
    <Page crumbs={crumbs}>
      <div className="head">
        <div><h1>{usecaseName(u.usecase)}</h1><span className="muted">{u.calls.toLocaleString('en-US')} calls in the window{u.sites.length > 0 && <> · made of {count(u.sites.length, 'call-site')}{calls !== u.calls && ` (${calls.toLocaleString('en-US')} of them chat calls)`}</>}{u.analysed_at && <> · analysed {when(u.analysed_at)}</>}</span></div>
        <div className="row"><Link className="btn primary" to={`/audit/models/new?usecase=${encodeURIComponent(u.usecase ?? 'None')}`}>Audit models</Link><button className="btn" disabled={u.analysing || !u.days.missing.length} title={u.days.missing.length ? `process ${u.days.missing.length} day(s) of the log not processed yet (every usecase)` : 'every day of the window is processed'} onClick={() => get(`/suite/callsites/usecases/${encodeURIComponent(usecase)}/analyse`, { method: 'POST' }).then(() => q.refetch())}>{u.analysing ? `Processing ${u.days.queued.length} day(s)…` : u.days.missing.length ? `Process ${u.days.missing.length} new day(s)` : 'Up to date'}</button></div>
      </div>
      {u.analysing && !u.sites.length && <p className="empty">Processing {u.days.queued.length} day(s) of the log…</p>}
      {!u.analysing && !u.sites.length && u.days.missing.length > 0 && <p className="empty">No day of the log processed yet.</p>}
      {u.coverage?.calls > 0 && <p className="muted">Analysed {u.coverage.placed.toLocaleString('en-US')} of {u.coverage.calls.toLocaleString('en-US')} calls
        {[['other format', u.coverage.other_format], ['no usage logged', u.coverage.no_usage], ['no price', u.coverage.unpriced], ['logged twice', u.coverage.repeated], ['not placed', u.coverage.unplaced]]
          .filter(([, n]) => (n as number) > 0).map(([k, n]) => ` · ${k}: ${(n as number).toLocaleString('en-US')}`).join('')}</p>}
      {u.error && <p className="error" role="alert">The last processing failed: {u.error}</p>}
      {u.sites.map((s) => (
        <section key={s.site} className="panel site-card">
          <div className="spread">
            <div><Link to={sitePath(u.usecase, s.site)}><b>{s.name}</b></Link>{s.profile && <span className="muted"> · {s.profile.does}</span>}</div>
            <span className="mono muted nowrap">{s.calls.toLocaleString('en-US')} calls</span>
          </div>
          <div className="row">
            {s.profile && <><span className="chip">{s.profile.task}</span><span className="chip">{s.profile.output} out</span></>}
            <span className="chip">{count(s.versions.length, 'version')}</span>
            {s.unsure && <span className="chip warn">few calls</span>}
          </div>
        </section>
      ))}
    </Page>
  )
}

/** The fixed vs per-call text of the prompt, in order. */
function LayoutBar({ s }: { s: Site }) {
  const lead = s.static_chars - s.static_after, dyn = s.dynamic_chars, tail = s.static_after, all = lead + dyn + tail || 1
  return (
    <div className="pbar-wrap">
      <div className="pbar" role="img" aria-label={`${Math.round(100 * lead / all)}% same every call first, ${Math.round(100 * dyn / all)}% different every call, ${Math.round(100 * tail / all)}% same every call after`}>
        {lead > 0 && <span className="fixed" style={{ width: 100 * lead / all + '%' }}>same every call</span>}
        {dyn > 0 && <span className="dyn" style={{ width: 100 * dyn / all + '%' }}>different every call</span>}
        {tail > 0 && <span className="fixed" style={{ width: 100 * tail / all + '%' }}>same every call</span>}
      </div>
      <p className="muted">About {s.static_chars.toLocaleString('en-US')} characters are the same every call and {s.dynamic_chars.toLocaleString('en-US')} differ{s.tool_sets > 1 && <>; the tool list comes in {s.tool_sets.toLocaleString('en-US')} different sets</>}.</p>
    </div>
  )
}

/** One call-site: the prompt, how it is recognised, what it is. The audits judge on their own pages. */
export function CallSite() {
  const { site = '' } = useParams()
  const q = useQuery({ queryKey: ['callsites', 'site', site], queryFn: () => get<SitePage>(`/suite/callsites/${site}`) })
  const crumbs = [{ label: 'Agentic workloads' }, { label: 'Usecases', to: '/usecases' }]
  if (q.isPending) return <Page crumbs={crumbs}><Loading /></Page>
  if (q.isError) return <Page crumbs={crumbs}><Failed error={q.error} /></Page>
  const s = q.data
  const p = s.profile
  return (
    <Page crumbs={[...crumbs, { label: usecaseName(s.usecase), to: ucPath(s.usecase) }, { label: s.name }]}>
      <div className="head">
        <div>
          <h1>{s.name}</h1>
          {p && <p className="lede">{p.does}</p>}
          <div className="row">
            {p && <><span className="chip">{p.task}</span><span className="chip">{p.output} out</span></>}
            <span className="chip mono">{s.site}</span>
            {s.unsure && <span className="chip warn">few calls</span>}
          </div>
        </div>
        <Link className="btn primary" to={`/audit/models/new?usecase=${encodeURIComponent(s.usecase ?? 'None')}&site=${s.site}`}>Audit models</Link>
      </div>
      <div className="kpis">
        <Kpi label="Calls" value={s.calls.toLocaleString('en-US')} note={`last ${s.window_days} days, ${s.first_day} → ${s.last_day}`} />
        <Kpi label="Prompt versions" value={s.versions.length} note={s.versions.length > 1 ? 'revisions seen in the window' : 'unchanged in the window'} />
        <Kpi label="Models" value={Object.keys(s.cache_models).length} note={Object.keys(s.cache_models).slice(0, 3).join(', ') || 'none in the window'} />
        <Kpi label="Cache audit" value={s.decisions.cache ? <Link to={`/audit/cache/sites/${s.site}`}>{CAUSE[s.decisions.cache.cause ?? 'ok']}</Link> : 'not yet'} note={s.decisions.cache?.worth ? `${money(s.decisions.cache.worth, 2)} still possible` : 'see the cache audit'} />
      </div>

      <section className="panel story">
        <div className="pad">
          <h2>The prompt</h2>
          {s.grams ? <PromptMap grams={s.grams} models={s.cache_models} /> : <LayoutBar s={s} />}
          <h3>Recognised by</h3>
          <pre className="sig">{s.markers.length ? s.markers.map((m) => `“${m}”`).join('\n') : '—'}</pre>
          {s.versions.length > 1 && (
            <>
              <h3>Versions</h3>
              <table className="grid"><thead><tr><th>Version</th><th>First seen</th><th>Last seen</th></tr></thead>
                <tbody>{s.versions.map((v) => <tr key={v.version}><td className="mono">{v.version.slice(0, 10)}</td><td>{v.first}</td><td>{v.last}</td></tr>)}</tbody></table>
            </>
          )}
        </div>
        <div className="pad">
          <h2>What it is</h2>
          {p ? (
            <dl className="facts">
              <dt>Same every call</dt><dd>{p.fixed || '—'}</dd>
              <dt>Changes per call</dt><dd>{p.per_call || '—'}</dd>
              <dt>Task</dt><dd>{p.task}, {p.output} out</dd>
            </dl>
          ) : <p className="muted">Not profiled yet.</p>}
        </div>
      </section>
    </Page>
  )
}
