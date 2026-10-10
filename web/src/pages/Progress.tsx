import { useEffect, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { post, useStream } from '../api'
import { api, crumbs, modeQuery, page, useAgent } from '../agent'
import { Page } from '../ui'

/** A long step in progress: what it is doing now, how far along, and what went wrong if it failed. */
function Progress({ eyebrow, title, stage, pct, error, children }: {
  eyebrow: string; title: ReactNode; stage: string; pct: number; error?: ReactNode; children?: ReactNode
}) {
  return (
    <section className="progress" aria-live="polite">
      <p className="eyebrow">{eyebrow}</p>
      {!error && <div className="spinner" aria-hidden="true" />}
      <h1>{title}</h1>
      <p className="stage">{stage}</p>
      <div className="bar"><div style={{ width: Math.max(4, pct) + '%' }} /></div>
      {children}
      {error && <div className="error" role="alert">{error}</div>}
    </section>
  )
}

/** Build a snapshot of the agent (its traces, grouped by call-site), then open the next step on it. */
export function Build() {
  const { a, dgmode } = useAgent()
  const [params] = useSearchParams()
  const next = params.get('next') === 'profile' ? 'profile' : 'select'
  const go = useNavigate()
  const [snap, setSnap] = useState<string | null>(null)
  const [stage, setStage] = useState('Starting…')
  const [pct, setPct] = useState(8)
  const [error, setError] = useState('')
  useEffect(() => {
    post<{ snap: string }>(api(a, 'snapshots')).then((r) => setSnap(r.snap)).catch((e) => setError(e.message))
  }, [a.source, a.ws, a.project]) // eslint-disable-line react-hooks/exhaustive-deps
  useStream(snap ? api(a, 'build-stream', `?snap=${snap}`) : null, {
    stage: (d) => { setStage(d.msg); setPct(d.pct ?? 8) },
    done: (d) => go(page(a, next, `?snap=${d.snap}${modeQuery(dgmode, '&')}`), { replace: true }),
    failed: (d) => setError(d.msg),
    error: (d) => setError(d.msg),
  })
  return (
    <Page crumbs={crumbs(a, 'building', dgmode)}>
      <Progress eyebrow="Agent audit · read-only" title={<>Building <span className="accent">{a.project}</span></>} stage={error ? 'Could not build it.' : stage} pct={pct}
        error={error && <>{error} · <a href={location.href}>retry</a></>} />
    </Page>
  )
}

/** Read each call-site once to describe what it does (paid), then open the profile. */
export function Analyze() {
  const { a, snap, dgmode } = useAgent()
  const go = useNavigate()
  const queries = useQueryClient()
  const [stage, setStage] = useState('Starting…')
  const [pct, setPct] = useState(8)
  const [error, setError] = useState('')
  useStream(api(a, 'comprehend-stream', `?snap=${snap}`), {
    stage: (d) => { setStage(d.msg); setPct(d.pct ?? 8) },
    done: () => {                           // drop the cached "not analysed yet", or the profile would send us back
      queries.removeQueries({ queryKey: ['profile', a.source, a.ws, a.project, snap] })
      go(page(a, 'profile', `?snap=${snap}${modeQuery(dgmode, '&')}`), { replace: true })
    },
    failed: (d) => setError(d.msg),
    error: (d) => setError(d.msg),
  })
  return (
    <Page crumbs={crumbs(a, 'analysing', dgmode)}>
      <Progress eyebrow="Deployment profile · per-node analysis" title={<>Analysing <span className="accent">{a.project}</span></>} stage={error ? 'Could not analyse it.' : stage} pct={pct}
        error={error && <>{error} · <a href={location.href}>retry</a> · <Link to={page(a, 'profile', `?snap=${snap}&skip=1`)}>view the profile without it</Link></>} />
    </Page>
  )
}

type AuditRow = { node: string; state: 'run' | 'done'; msg: string }

/** Prove the chosen call-sites live (paid), one row each, then open the report. */
export function Audit() {
  const { a, snap, dgmode } = useAgent()
  const [params] = useSearchParams()
  const go = useNavigate()
  const queries = useQueryClient()
  const [rows, setRows] = useState<Record<string, AuditRow>>({})
  const [total, setTotal] = useState(0)
  const [error, setError] = useState('')
  const done = Object.values(rows).filter((r) => r.state === 'done').length
  useStream(api(a, 'prove-stream', `?snap=${snap}&keys=${encodeURIComponent(params.get('keys') ?? '')}&dgmode=${dgmode}`), {
    start: (d) => setTotal(d.total ?? 0),
    step: (d) => setRows((r) => ({ ...r, [d.key]: { node: d.node, state: 'run', msg: d.msg || 'auditing…' } })),
    done_node: (d) => setRows((r) => ({ ...r, [d.key]: { node: d.node, state: 'done', msg: 'audited' } })),
    complete: (d) => {
      if (d.stale) return setError('The selection went stale: reload the call-sites and audit again.')
      queries.removeQueries({ queryKey: ['report', a.source, a.ws, a.project, snap] })   // no cached "not audited yet"
      go(page(a, 'report', `?snap=${snap}${modeQuery(dgmode, '&')}`), { replace: true })
    },
    failed: (d) => setError(d.msg),
    error: (d) => setError(d.msg),
  })
  return (
    <Page crumbs={crumbs(a, 'auditing', dgmode)}>
      <Progress eyebrow="Agent audit · live re-run" title={<>Auditing <span className="accent">{a.project}</span></>}
        stage={error ? 'The audit stopped.' : `${done} of ${total} audited`} pct={total ? (100 * done) / total : 4}
        error={error && <>{error} · <Link to={page(a, 'select', `?snap=${snap}${modeQuery(dgmode, '&')}`)}>back to the call-sites</Link></>}>
        <ul className="audit-rows">
          {Object.entries(rows).map(([k, r]) => (
            <li key={k} className={'st-' + r.state}><span className="st-dot" aria-hidden="true" />{r.node} <span className="muted">— {r.msg}</span></li>
          ))}
        </ul>
      </Progress>
    </Page>
  )
}
