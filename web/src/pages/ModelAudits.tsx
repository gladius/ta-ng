import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import Select from 'react-select'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { get, type JobStatus } from '../api'
import { usecaseName } from '../cache'
import { count, Failed, Loading, money, Page, when } from '../ui'

export type Verdict = { requests: number; within: number; outside: number; unverified: number; pass_rate: number; cost_vs_original_pct: number | null; latency_vs_original_pct: number | null; fit_rate: number | null; verdict: 'passes' | 'drifts' | 'not verified' }
export type Audit = Omit<JobStatus, 'status' | 'spec'> & {
  status: 'sampling' | 'queued' | 'running' | 'done' | 'stopped' | 'failed'
  trigger: string; scope: 'usecase' | 'site' | 'requests'; usecase: string | null; site: string | null; strategy: string; models: string[]
  samples: Record<string, { id: string; why: string; prompt_tokens?: number; completion_tokens?: number }[]>
  verdicts: Record<string, Record<string, Verdict>> | null; site_names: Record<string, string>
  spec: JobStatus['spec'] | null
}

export const STRATEGY_NAME: Record<string, string> = { cheaper: 'Cheaper', better_value: 'Better value', release: 'A release', manual: 'Manual', revalidate: 'Re-validate (retired)', drift_watch: 'Drift watch (retired)' }
const OLD_STRATEGY: Record<string, string> = { cheaper: 'cheaper tier of each call-site\'s model', open_weight: 'open-weight swap in the same tier', release: 'a release', custom: 'chosen models', auto: 'Auto' }
export const STATUS: Record<string, string> = { sampling: 'Sampling', queued: 'Queued', running: 'Running', done: 'Done', failed: 'Failed', stopped: 'Stopped' }

export function VerdictChip({ v }: { v: Verdict }) {
  return <span className={'chip ' + (v.verdict === 'passes' ? 'cause-ok' : v.verdict === 'drifts' ? 'cause-dynamic_first' : '')}>{v.verdict} · {v.within} of {v.requests}</span>
}

type Served = { models: { model: string; available: boolean; provider: string; tier: string | null; open_weight: boolean }[] }
type UsecaseOpt = { usecase: string | null; sites: number | null; analysed_at: string | null; calls: number }

/** The one way to start an audit: which usecases (or the one call-site this page is on), which models (any the
    gateway serves, several at once) and/or Auto; samples and budget under "advanced". */
export function AuditDialog({ usecase, site, lockScope = false, onStarted }: { usecase?: string | null; site?: string; lockScope?: boolean; onStarted?: (ids: string[]) => void }) {
  const open = true
  const nav = useNavigate()
  const [usecases, setUsecases] = useState<string[]>(usecase !== undefined ? [usecase ?? 'None'] : [])
  const [models, setModels] = useState<string[]>([])
  const [strategy, setStrategy] = useState('cheaper')
  const strategies = useQuery({ queryKey: ['strategies'], queryFn: () => get<{ strategies: { id: string; does: string }[] }>('/audit/models/strategies'), enabled: open })
  const [advanced, setAdvanced] = useState(false)
  const [samples, setSamples] = useState(12)
  const [budget, setBudget] = useState<number | ''>('')
  const served = useQuery({ queryKey: ['served-models'], queryFn: () => get<Served>('/suite/models'), enabled: open })
  const opts = useQuery({ queryKey: ['usecase-options'], queryFn: () => get<{ usecases: UsecaseOpt[] }>('/suite/callsites/usecases?per_page=200'), enabled: open && !site && !lockScope })
  const auto = strategy !== 'manual' && strategy !== 'release'
  const est = useQuery({ queryKey: ['audit-estimate', usecases, site, samples, models.length + (auto ? 1 : 0)], enabled: open && (!!site || usecases.length > 0),
    queryFn: async () => {
      const n = Math.max(1, models.length + (auto ? 1 : 0))
      const scopes: (string | null)[] = site ? [null] : usecases
      const sums = await Promise.all(scopes.map((u) => get<{ usd: number | null }>(`/audit/models/estimate?${new URLSearchParams({ ...(u != null && { usecase: u }), ...(site && { site }), samples: String(samples), candidates: String(n) })}`)))
      return { usd: sums.reduce((a, b) => a + (b.usd ?? 0), 0), unknown: sums.filter((x) => x.usd == null).length }
    } })
  const prev = useQuery({ queryKey: ['audit-preview', usecases, site, strategy, models], enabled: open && (!!site || usecases.length > 0),
    queryFn: () => get<{ call_sites: { usecase: string | null; site: string; name: string; model: string; candidates: string[] }[]; not_known_yet: string[] }>(
      `/audit/models/preview?${new URLSearchParams({ strategy, usecases: site ? '' : usecases.join(','), ...(site && { site }), models: models.join(',') })}`) })
  const start = useMutation({
    mutationFn: () => get<{ audits: { id: string }[] }>('/audit/models', { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ usecases: site ? [] : usecases, site, strategy, models, samples, ...(budget !== '' && { max_budget_usd: budget }) }) }),
    onSuccess: (r) => { onStarted?.(r.audits.map((a) => a.id)) },
  })
  const available = (served.data?.models ?? []).filter((m) => m.available)
  return (
    <div className="panel pad dialog">
      <h2>New model audit {site ? 'of one call-site' : lockScope ? 'of this usecase' : ''}</h2>
      {!site && !lockScope && (
        <label>Usecases
          <Select isMulti classNamePrefix="rs" placeholder="Choose usecases… (type to search)" closeMenuOnSelect={false}
            options={(opts.data?.usecases ?? []).map((u) => ({ value: u.usecase ?? 'None', label: usecaseName(u.usecase),
              hint: `${u.calls.toLocaleString('en-US')} calls${u.sites ? ` · ${u.sites} call-site${u.sites === 1 ? '' : 's'}` : ''}` }))}
            value={usecases.map((id) => ({ value: id, label: usecaseName(id === 'None' ? null : id) }))}
            onChange={(v) => setUsecases(v.map((o) => o.value))}
            formatOptionLabel={(o: { label: string; hint?: string }, m) => m.context === 'menu' ? <span>{o.label} <span className="muted">{o.hint}</span></span> : o.label} />
          <span className="row"><button type="button" className="btn small" onClick={() => setUsecases((opts.data?.usecases ?? []).filter((u) => u.calls > 0).map((u) => u.usecase ?? 'None'))}>all usecases</button></span>
        </label>
      )}
      <label>Strategy
        <Select classNamePrefix="rs" isSearchable={false}
          options={(strategies.data?.strategies ?? []).map((x) => ({ value: x.id, label: STRATEGY_NAME[x.id] ?? x.id, hint: x.does }))}
          value={{ value: strategy, label: STRATEGY_NAME[strategy] ?? strategy }}
          onChange={(v) => v && setStrategy(v.value)}
          formatOptionLabel={(o: { label: string; hint?: string }, m) => m.context === 'menu' ? <span>{o.label} <span className="muted">· {o.hint}</span></span> : o.label} />
      </label>
      <label>{strategy === 'manual' || strategy === 'release' ? 'Models' : 'Models to add'}
        <Select isMulti classNamePrefix="rs" placeholder={strategy === 'manual' || strategy === 'release' ? 'Choose models… (type to search)' : 'Optional'} closeMenuOnSelect={false}
          options={Object.entries(available.reduce<Record<string, { value: string; label: string }[]>>((g, m) => { (g[m.provider] ??= []).push({ value: m.model, label: `${m.model}${m.tier ? ` · ${m.tier}` : ''}${m.open_weight ? ' · open-weight' : ''}` }); return g }, {})).map(([provider, options]) => ({ label: provider, options }))}
          value={models.map((m) => ({ value: m, label: m }))}
          onChange={(v) => setModels(v.map((o) => o.value))} />
      </label>
      <button className="btn small" onClick={() => setAdvanced(!advanced)}>{advanced ? 'Hide advanced' : 'Advanced'}</button>
      {advanced && (
        <div className="row">
          <label>Samples per call-site <input className="input" type="number" min={1} max={50} value={samples} onChange={(e) => setSamples(Number(e.target.value))} /></label>
          <label>Budget per audit, $ (optional) <input className="input" type="number" min={1} step={1} value={budget} onChange={(e) => setBudget(e.target.value === '' ? '' : Number(e.target.value))} /></label>
        </div>
      )}
      {prev.data && (prev.data.call_sites.length > 0 || prev.data.not_known_yet.length > 0) && (
        <div className="preview">
          <div className="muted">What this will try</div>
          <table className="grid">
            <tbody>
              {prev.data.call_sites.map((c) => (
                <tr key={c.site + c.model}><td>{c.name}{!site && <span className="muted"> · {usecaseName(c.usecase)}</span>}</td><td className="mono">{c.model}</td>
                  <td>{c.candidates.length ? <span className="mono">→ {c.candidates.join(', ')}</span> : <span className="chip warn">nothing to try: no served model for this strategy</span>}</td></tr>
              ))}
              {prev.data.not_known_yet.map((u) => <tr key={u}><td>{usecaseName(u === 'None' ? null : u)}</td><td colSpan={2} className="muted">call-sites found when the audit samples</td></tr>)}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted">Estimated cost: {est.data ? money(est.data.usd, 2) : '…'}{est.data?.unknown ? ` plus ${est.data.unknown} usecase${est.data.unknown === 1 ? '' : 's'} not sized yet` : ''} · {site ? '1 audit' : `${usecases.length} audit${usecases.length === 1 ? '' : 's'}, one per usecase`}</p>
      {start.isError && <p className="error" role="alert">{start.error instanceof Error ? start.error.message : String(start.error)}</p>}
      <div className="row"><button className="btn primary" disabled={start.isPending || (!site && !usecases.length) || ((strategy === 'manual' || strategy === 'release') && !models.length)} onClick={() => start.mutate()}>Start</button><button className="btn" onClick={() => nav(-1)}>Cancel</button></div>
    </div>
  )
}

/** Every model audit, newest first. */
/** The new-audit page: the form on its own, preset from ?usecase= / ?site=. */
export function NewModelAudit() {
  const [params] = useSearchParams()
  const nav = useNavigate()
  const usecase = params.get('usecase') ?? undefined, site = params.get('site') ?? undefined
  return (
    <Page crumbs={[{ label: 'Agentic workloads' }, { label: 'Model audits', to: '/audit/models' }, { label: 'New' }]}>
      <AuditDialog key={(usecase ?? '') + (site ?? '')} usecase={usecase} site={site} onStarted={() => nav('/audit/models')} />
    </Page>
  )
}

export function ModelAudits() {
  const q = useQuery({ queryKey: ['model-audits'], queryFn: () => get<Audit[]>('/audit/models'),
    refetchInterval: (query) => (query.state.data?.some((c) => ['sampling', 'queued', 'running'].includes(c.status)) ? 5000 : false) })
  const crumbs = [{ label: 'Agentic workloads' }, { label: 'Model audits' }]
  return (
    <Page crumbs={crumbs}>
      <div className="head"><h1>Model audits</h1><Link className="btn primary" to="/audit/models/new">New model audit…</Link></div>
      {q.isPending ? <Loading /> : q.isError ? <Failed error={q.error} /> : !q.data.length ? <p className="empty">No model audits yet.</p> : (
        <section className="panel">
          <table className="grid">
            <thead><tr><th>Audit</th><th>Scope</th><th>Strategy</th><th>Status</th><th>Result</th><th>Started</th></tr></thead>
            <tbody>
              {q.data.map((c) => {
                const vs = c.verdicts ? Object.values(c.verdicts).flatMap((by) => Object.values(by)) : []
                return (
                  <tr key={c.id}>
                    <td className="mono"><Link to={`/audit/models/${c.id}`}>{c.id}</Link><div className="muted">{c.trigger}</div></td>
                    <td>{c.scope === 'requests' ? <span>{count(c.items_total, 'request')} sent</span> : <><Link to={`/usecases/${encodeURIComponent(c.usecase ?? 'None')}`}>{usecaseName(c.usecase)}</Link>{c.site && <div className="muted">one call-site</div>}</>}</td>
                    <td>{STRATEGY_NAME[c.strategy] ?? OLD_STRATEGY[c.strategy] ?? c.strategy}{c.models.length > 0 && <div className="mono muted">{c.models.join(', ')}</div>}</td>
                    <td><span className={'pill pill-' + (c.status === 'sampling' ? 'running' : c.status)}>{STATUS[c.status] ?? c.status}</span>{c.error && <div className="muted">{c.error.slice(0, 80)}</div>}</td>
                    <td>{vs.length ? `${vs.filter((v) => v.verdict === 'passes').length} of ${vs.length} pass` : <span className="muted">—</span>}</td>
                    <td className="muted nowrap">{when(c.created_at)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </section>
      )}
    </Page>
  )
}
