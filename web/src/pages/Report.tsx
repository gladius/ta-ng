import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { ApiError, get, post } from '../api'
import { api, crumbs, download, modeQuery, page, useAgent, type Funnel, type Proof } from '../agent'
import Evidence from '../Evidence'
import { count, Failed, Kpi, Loading, money, Page } from '../ui'

/** What the audit proved on this snapshot: verified savings, and every call-site's evidence. */
export default function Report() {
  const { a, snap, dgmode } = useAgent()
  const go = useNavigate()
  const [failed, setFailed] = useState<unknown>(null)
  const q = useQuery({ queryKey: ['report', a.source, a.ws, a.project, snap], queryFn: () => get<{ funnel: Funnel; proof: Proof }>(api(a, 'report', `?snap=${snap}`)), retry: false })
  const where = crumbs(a, 'report', dgmode)

  if (q.isFetching && q.error) return <Page crumbs={where}><Loading /></Page>          // redirect only on a fresh answer
  if (q.error instanceof ApiError && q.error.status === 404) return <Navigate replace to={page(a, 'build', `?next=select${modeQuery(dgmode, '&')}`)} />
  if (q.error instanceof ApiError && q.error.status === 409) return <Navigate replace to={page(a, 'select', `?snap=${snap}${modeQuery(dgmode, '&')}`)} />
  if (q.isPending) return <Page crumbs={where}><Loading /></Page>
  if (q.isError) return <Page crumbs={where}><Failed error={q.error} /></Page>

  const { funnel: f, proof } = q.data
  const mode = proof.dgmode ?? f.dgmode
  const results = proof.results
  const audited = results.length
  const cost = results.reduce((t, r) => t + (r.cost || 0), 0)
  const save = proof.total || 0
  const safe = results.filter((r) => (r.downgrade_usd || 0) > 0).length
  const errored = results.filter((r) => r.error)
  const allFailed = audited > 0 && errored.length === audited
  const per = `per ${f.calls.toLocaleString('en-US')} calls`
  const dg = results.filter((r) => r.downgrade)
  const again = () => post(api(a, 'prove-clear', `?snap=${snap}`))
    .then(() => go(page(a, 'build', `?next=select${modeQuery(mode, '&')}`)), setFailed)

  return (
    <Page crumbs={where}>
      {failed != null && <Failed error={failed} />}
      {errored.length > 0 && (
        <div className="error" role="alert">
          The live re-run failed for {errored.length} of {count(audited, 'call-site')}; those weren't counted.{' '}
          <button type="button" className="link" onClick={again}>Audit again</button>
          <details><summary>Show the error</summary><pre className="small">{errored[0].error}</pre></details>
        </div>
      )}

      <section className="summary verdict-hero">
        <div className="summary-head"><span className="dot" aria-hidden="true" />{mode === 'open-weight' ? 'Open-weight audit' : 'Tier downgrade audit'}<span className="muted mono">{f.agent}</span></div>
        <h1>{allFailed ? 'The audit could not complete.' : save > 0 ? `${money(save, 0)} in verified savings.` : 'No safe savings on the audited call-sites.'}</h1>
        <p>{allFailed ? 'Nothing could be verified: fix the cause and audit again.'
          : save > 0 ? `Proven on a live re-run of real recorded inputs, ${per}.`
            : `${audited === 1 ? 'The audited call-site needs' : 'Each audited call-site needs'} its current model: a verified result.`}</p>
      </section>

      {!allFailed && (
        <div className="kpis">
          <Kpi label="Current cost" value={money(cost, 0)} note={per} />
          <Kpi label="Verified savings" value={money(save, 0)} note={per} best={save > 0 && cost > 0 && `${Math.round((save / cost) * 100)}% off`} />
          <Kpi label="Safe to change" value={<>{safe} <span className="muted small">of {audited}</span></>} />
          <Kpi label="Sampled" value={f.traces} note="traces" />
        </div>
      )}

      <section className="panel">
        <div className="panel-head">
          <h2>{mode === 'open-weight' ? 'Open-weight swap' : 'Model-tier downgrade'}</h2>
          <span className="muted">{dg.length ? `${dg.length} audited` : 'none audited'}</span>
        </div>
        {!dg.length ? <p className="pad muted">None of the audited call-sites had a cheaper model to try.</p> : dg.map((r) => {
          const d = r.downgrade!
          return (
            <details key={r.key} className="finding">
              <summary>
                <b>{r.node}</b>
                <span className="mono muted small">{r.model} → {d.cheaper}</span>
                <span className="grow" />
                <span className={'tag ' + (d.verdict === 'SAFE' ? 'tag-ok' : d.verdict === 'NOT-SAFE' ? 'tag-bad' : 'tag-warn')}>{d.verdict.toLowerCase().replace('-', ' ')}</span>
                <span className="mono nowrap">{d.verdict === 'SAFE' ? '−' + money(r.downgrade_usd, 0) : '—'}</span>
              </summary>
              <div className="finding-body">
                {d.reason && <p className="muted">{d.reason}</p>}
                {d.inputs.length > 0 && <Evidence inputs={d.inputs} />}
              </div>
            </details>
          )
        })}
      </section>

      <div className="row">
        <a className="btn primary" href={download(a, 'report', snap)}>Download report (.zip)</a>
        <button type="button" className="btn" onClick={again}>Audit again from fresh traces</button>
      </div>
      <p className="muted small">Audited {audited} of {count(f.rows.length, 'call-site')}{f.revision && ` on the latest revision (${f.revision.slice(0, 12)})`}.</p>
    </Page>
  )
}
