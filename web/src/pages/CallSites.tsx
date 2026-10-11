import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { get } from '../api'
import { CAUSE, usecaseName, type Cause, type Proof } from '../cache'
import { count, Failed, Kpi, Loading, money, Page, pct, when } from '../ui'
import { CheckDialog, VerdictChip, type Verdict } from './Checks'

type Profile = { name: string; does: string; task: string; output: string; fixed: string | null; per_call: string | null; model: string; created_at: string | null } | null
type CacheDecision = { cause: Cause | null; action: string; worth: number; proof: Proof; share: number; models: string[]; built_at: string | null } | null
type Site = {
  site: string; usecase: string | null; name: string; signature: string[]; calls: number; first_day: string; last_day: string
  versions: { version: string; first: string; last: string }[]; static_chars: number; dynamic_chars: number; static_after: number
  tool_sets: number; unsure: boolean; window_days: number; updated_at: string | null; profile: Profile
}
type Models = { candidates: Record<string, Verdict & { check: string; at: string; trigger: string }> } | null
type UsecaseRow = { usecase: string | null; calls: number; first_day: string; last_day: string; sites: number | null; analysed_at: string | null; analysing: boolean; error: string | null }
type Usecases = { page: number; per_page: number; total: number; listed_at: string | null; reloading: boolean; running: string[]; usecases: UsecaseRow[] }
type UsecasePage = UsecaseRow & { sites: (Site & { cache: CacheDecision; decisions: { models?: Models } })[]; analysing: boolean }
type SitePage = Site & { decisions: { cache: CacheDecision; models: Models; router: null } }

const ucPath = (u: string | null) => `/usecases/${encodeURIComponent(u ?? 'None')}`
const sitePath = (u: string | null, site: string) => `${ucPath(u)}/callsites/${site}`

/** Every usecase in the log window, with how many call-sites were found in it. */
export function CallSiteUsecases() {
  const [params, setParams] = useSearchParams()
  const page = Math.max(1, Number(params.get('page')) || 1)
  const [q, setQ] = useState(params.get('q') ?? '')
  const list = useQuery({ queryKey: ['callsites', 'usecases', page, q], queryFn: () => get<Usecases>(`/suite/callsites/usecases?page=${page}&per_page=50${q ? '&q=' + encodeURIComponent(q) : ''}`),
    refetchInterval: (query) => (query.state.data?.reloading || query.state.data?.running.length ? 5000 : false) })
  const crumbs = [{ label: 'Profiles' }, { label: 'Usecases' }]
  return (
    <Page crumbs={crumbs}>
      <div className="head">
        <div><h1>Usecases</h1><span className="muted">each usecase is made of call-sites: one prompt template each, found from the instruction lines its calls share, named by the profiler{list.data?.listed_at && <> · list from {when(list.data.listed_at)}{list.data.reloading && ', reloading'}</>}</span></div>
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
                  <td>{u.analysing || list.data.running.includes(u.usecase ?? '') ? <span className="chip">analysing…</span> : u.error ? <span className="chip warn" title={u.error}>failed</span> : u.analysed_at ? <span className="muted nowrap">{when(u.analysed_at)}</span> : <span className="muted">open to analyse</span>}</td>
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

function CacheLine({ c, site }: { c: CacheDecision; site: string }) {
  if (!c) return <span className="muted">cache: not analysed yet</span>
  return (
    <span>
      <Link to={`/cache/sites/${site}`}>cache</Link>: {c.cause ? <span className={'chip cause-' + c.cause}>{CAUSE[c.cause]}</span> : null} {pct(c.share)} reused
      {c.worth > 0 && <> · <b>{money(c.worth, 2)}</b> possible{c.proof === 'proof' && ', needs proof'}</>}
    </span>
  )
}

/** One usecase: its call-sites as cards. */
export function CallSiteUsecase() {
  const { usecase = '' } = useParams()
  const q = useQuery({ queryKey: ['callsites', 'usecase', usecase], queryFn: () => get<UsecasePage>(`/suite/callsites/usecases/${encodeURIComponent(usecase)}`),
    refetchInterval: (query) => (query.state.data?.analysing ? 4000 : false) })
  const crumbs = [{ label: 'Profiles' }, { label: 'Usecases', to: '/usecases' }, { label: usecaseName(usecase) }]
  if (q.isPending) return <Page crumbs={crumbs}><Loading /></Page>
  if (q.isError) return <Page crumbs={crumbs}><Failed error={q.error} /></Page>
  const u = q.data
  const calls = u.sites.reduce((n, s) => n + s.calls, 0)
  return (
    <Page crumbs={crumbs}>
      <div className="head">
        <div><h1>{usecaseName(u.usecase)}</h1><span className="muted">{u.calls.toLocaleString('en-US')} calls in the window{u.sites.length > 0 && <> · made of {count(u.sites.length, 'call-site')} ({calls.toLocaleString('en-US')} chat calls)</>} · derived from the log, no source code needed{u.analysed_at && <> · analysed {when(u.analysed_at)}</>}</span></div>
        <div className="row"><CheckDialog usecase={u.usecase} /><button className="btn" disabled={u.analysing} onClick={() => get(`/suite/callsites/usecases/${encodeURIComponent(usecase)}/analyse`, { method: 'POST' }).then(() => q.refetch())}>{u.analysing ? 'Analysing…' : 'Analyse again'}</button></div>
      </div>
      {u.analysing && !u.sites.length && <p className="empty">Analysing this usecase from the log: finding its call-sites, naming them, reading their cache behaviour. Usually under a minute.</p>}
      {u.error && <p className="error" role="alert">The last analysis failed: {u.error}</p>}
      {u.sites.map((s) => (
        <section key={s.site} className="panel site-card">
          <div className="spread">
            <div><Link to={sitePath(u.usecase, s.site)}><b>{s.name}</b></Link>{s.profile && <span className="muted"> · {s.profile.does}</span>}</div>
            <span className="mono muted nowrap">{s.calls.toLocaleString('en-US')} calls</span>
          </div>
          <div className="row">
            {s.profile && <><span className="chip">{s.profile.task}</span><span className="chip">{s.profile.output} out</span></>}
            <span className="chip">{count(s.versions.length, 'version')}</span>
            {s.unsure && <span className="chip warn">grouping unsure</span>}
            {!s.profile && <span className="chip">not profiled</span>}
          </div>
          <p><CacheLine c={s.cache} site={s.site} /> · models: {s.decisions?.models ? Object.entries(s.decisions.models.candidates).map(([cand, v]) => <span key={cand}><span className="mono">{cand}</span> <VerdictChip v={v} /> </span>) : 'not checked yet'} · router: no pin</p>
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

/** One call-site: identity (the fingerprint), profile, and what each module decided. */
export function CallSite() {
  const { site = '' } = useParams()
  const q = useQuery({ queryKey: ['callsites', 'site', site], queryFn: () => get<SitePage>(`/suite/callsites/${site}`) })
  const crumbs = [{ label: 'Profiles' }, { label: 'Usecases', to: '/usecases' }]
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
            {s.unsure && <span className="chip warn">grouping unsure</span>}
          </div>
        </div>
        <CheckDialog usecase={s.usecase} site={s.site} />
      </div>
      <div className="kpis">
        <Kpi label="Calls" value={s.calls.toLocaleString('en-US')} note={`last ${s.window_days} days, ${s.first_day} → ${s.last_day}`} />
        <Kpi label="Prompt versions" value={s.versions.length} note={s.versions.length > 1 ? 'revisions seen in the window' : 'unchanged in the window'} />
        <Kpi label="Fingerprint lines" value={s.signature.length} note="stable instruction lines" />
        <Kpi label="Profiled" value={p ? 'yes' : 'no'} note={p ? `${when(p.created_at ?? '')} by ${p.model}` : 'profiled on the next refresh'} />
      </div>

      <section className="panel story">
        <div className="pad">
          <h2>Identity: how its calls are recognised</h2>
          <p>A call belongs to this call-site when its system prompt contains these lines (numbers masked as #). They are the longest lines that at least half of the usecase's calls share; everything else in the prompt is per-call content or a revision.</p>
          <ol className="sig">{s.signature.map((l, i) => <li key={i}><code>{l}</code></li>)}</ol>
          {s.versions.length > 1 && (
            <>
              <h3>Versions</h3>
              <table className="grid"><thead><tr><th>Version</th><th>First seen</th><th>Last seen</th></tr></thead>
                <tbody>{s.versions.map((v) => <tr key={v.version}><td className="mono">{v.version.slice(0, 10)}</td><td>{v.first}</td><td>{v.last}</td></tr>)}</tbody></table>
            </>
          )}
          <h3>Layout</h3>
          <LayoutBar s={s} />
        </div>
        <div className="pad">
          <h2>Profile: what it is</h2>
          {p ? (
            <dl className="facts">
              <dt>Same every call</dt><dd>{p.fixed || '—'}</dd>
              <dt>Changes per call</dt><dd>{p.per_call || '—'}</dd>
              <dt>Task</dt><dd>{p.task}, {p.output} out</dd>
            </dl>
          ) : <p className="muted">Not profiled yet.</p>}
        </div>
        <div className="pad">
          <h2>Decisions</h2>
          <p><b>Cache</b> · {s.decisions.cache ? <CacheLine c={s.decisions.cache} site={s.site} /> : <span className="muted">not analysed yet</span>}</p>
          {s.decisions.cache?.action && <p className="muted">{s.decisions.cache.action}</p>}
          <p><b>Models</b> · {s.decisions.models ? Object.entries(s.decisions.models.candidates).map(([cand, v]) => (
            <span key={cand} className="row"><span className="mono">{cand}</span> <VerdictChip v={v} />{v.cost_vs_original_pct != null && <span className="muted">{v.cost_vs_original_pct}% cost</span>}<Link className="muted" to={`/checks/${v.check}`}>check {when(v.at)}</Link></span>
          )) : <span className="muted">no model checked for this call-site yet</span>}</p>
          <p><b>Router</b> · <span className="muted">no pin</span></p>
        </div>
      </section>
    </Page>
  )
}
