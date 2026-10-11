import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { get } from '../api'
import { CAUSE, usecaseName, type Cause, type Proof } from '../cache'
import { count, Failed, Kpi, Loading, money, Page, pct, when } from '../ui'

type Profile = { name: string; does: string; task: string; output: string; fixed: string | null; per_call: string | null; model: string; created_at: string | null } | null
type CacheDecision = { cause: Cause | null; action: string; worth: number; proof: Proof; share: number; models: string[]; built_at: string | null } | null
type Site = {
  site: string; usecase: string | null; name: string; signature: string[]; calls: number; first_day: string; last_day: string
  versions: { version: string; first: string; last: string }[]; static_chars: number; dynamic_chars: number; static_after: number
  tool_sets: number; unsure: boolean; window_days: number; updated_at: string | null; profile: Profile
}
type UsecaseRow = { usecase: string | null; sites: number; calls: number; unsure: number; profiled: number }
type Usecases = { page: number; per_page: number; total: number; usecases: UsecaseRow[] }
type UsecasePage = { usecase: string | null; sites: (Site & { cache: CacheDecision })[] }
type SitePage = Site & { decisions: { cache: CacheDecision; models: null; router: null } }

const ucPath = (u: string | null) => `/callsites/usecases/${encodeURIComponent(u ?? 'None')}`

/** Every usecase in the log window, with how many call-sites were found in it. */
export function CallSiteUsecases() {
  const [params, setParams] = useSearchParams()
  const page = Math.max(1, Number(params.get('page')) || 1)
  const [q, setQ] = useState(params.get('q') ?? '')
  const list = useQuery({ queryKey: ['callsites', 'usecases', page, q], queryFn: () => get<Usecases>(`/suite/callsites/usecases?page=${page}&per_page=50${q ? '&q=' + encodeURIComponent(q) : ''}`) })
  const crumbs = [{ label: 'Call-sites' }]
  return (
    <Page crumbs={crumbs}>
      <div className="head">
        <div><h1>Call-sites</h1><span className="muted">a call-site is one prompt template; the log has no id for it, so one is derived from the instruction lines its calls share</span></div>
        <input className="input" placeholder="Find a usecase" value={q} onChange={(e) => { setQ(e.target.value); setParams({ q: e.target.value }) }} />
      </div>
      {list.isPending ? <Loading /> : list.isError ? <Failed error={list.error} /> : !list.data.usecases.length ? (
        <p className="empty">No call-sites yet: run a refresh on the Cache analysis page.</p>
      ) : (
        <section className="panel">
          <div className="panel-head"><b>{count(list.data.total, 'usecase')}</b><span className="muted">most calls first</span></div>
          <table className="grid">
            <thead><tr><th>Usecase</th><th className="num">Calls</th><th className="num">Call-sites</th><th>Profiled</th><th>Grouping</th></tr></thead>
            <tbody>
              {list.data.usecases.map((u) => (
                <tr key={u.usecase ?? 'None'}>
                  <td><Link to={ucPath(u.usecase)}>{usecaseName(u.usecase)}</Link></td>
                  <td className="num mono">{u.calls.toLocaleString('en-US')}</td>
                  <td className="num mono">{u.sites}</td>
                  <td>{u.profiled} of {u.sites}</td>
                  <td>{u.unsure ? <span className="chip warn">{count(u.unsure, 'call-site')} unsure</span> : <span className="chip">confident</span>}</td>
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
  const q = useQuery({ queryKey: ['callsites', 'usecase', usecase], queryFn: () => get<UsecasePage>(`/suite/callsites/usecases/${encodeURIComponent(usecase)}`) })
  const crumbs = [{ label: 'Call-sites', to: '/callsites' }, { label: usecaseName(usecase) }]
  if (q.isPending) return <Page crumbs={crumbs}><Loading /></Page>
  if (q.isError) return <Page crumbs={crumbs}><Failed error={q.error} /></Page>
  const u = q.data
  const calls = u.sites.reduce((n, s) => n + s.calls, 0)
  return (
    <Page crumbs={crumbs}>
      <div className="head"><div><h1>{usecaseName(u.usecase)}</h1><span className="muted">{count(u.sites.length, 'call-site')} · {calls.toLocaleString('en-US')} calls in the last {u.sites[0]?.window_days} days</span></div></div>
      {u.sites.map((s) => (
        <section key={s.site} className="panel site-card">
          <div className="spread">
            <div><Link to={`/callsites/${s.site}`}><b>{s.name}</b></Link>{s.profile && <span className="muted"> · {s.profile.does}</span>}</div>
            <span className="mono muted nowrap">{s.calls.toLocaleString('en-US')} calls</span>
          </div>
          <div className="row">
            {s.profile && <><span className="chip">{s.profile.task}</span><span className="chip">{s.profile.output} out</span></>}
            <span className="chip">{count(s.versions.length, 'version')}</span>
            {s.unsure && <span className="chip warn">grouping unsure</span>}
            {!s.profile && <span className="chip">not profiled</span>}
          </div>
          <p><CacheLine c={s.cache} site={s.site} /> · models: not validated yet · router: no pin</p>
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
  const crumbs = [{ label: 'Call-sites', to: '/callsites' }]
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
          <p><b>Models</b> · <span className="muted">no cheaper model validated for this call-site yet</span></p>
          <p><b>Router</b> · <span className="muted">no pin</span></p>
        </div>
      </section>
    </Page>
  )
}
