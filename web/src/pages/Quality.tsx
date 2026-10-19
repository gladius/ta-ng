import { useMutation, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { get } from '../api'
import { Failed, Loading, Page } from '../ui'

type Item = { kind: string; usecase: string | null; site: string | null; name: string | null; ok: boolean; detail: Record<string, unknown> }
type Report = { day: string | null; kinds: Record<string, { checked: number; failed: number }>; failures: Item[]
  days: string[]; running: string[]; trust: { level: 'trustworthy' | 'mostly' | 'not yet'; reasons: string[] } | null }
const LEVEL = { trustworthy: 'Trustworthy', mostly: 'Mostly trustworthy', 'not yet': 'Not trustworthy yet' }

const KIND: Record<string, string> = {
  consistency: 'Numbers add up', placement: 'Calls placed in a call-site', unpriced: 'Calls with a price',
  fingerprint: 'Every call-site recognisable', named: 'Every call-site named', mixed: 'One template per call-site',
  same_name: 'No two call-sites alike', models: 'One model per call-site', volume: 'Steady volume',
}
const site = (i: Item) => i.site ? <Link to={`/usecases/${encodeURIComponent(i.usecase ?? 'None')}/callsites/${i.site}`}>{i.name ?? i.site}</Link> : null

function detail(i: Item): string {
  const d = i.detail
  switch (i.kind) {
    case 'placement': return `${Math.round(100 * (d.share as number))}% placed (${d.placed} of ${d.chat_calls})`
    case 'unpriced': return `${Math.round(100 * (d.share as number))}% without a price (${d.unpriced})`
    case 'consistency': return `calls ${d.calls} ≠ placed ${d.placed} + not placed ${d.unplaced} + other format ${d.other_format} + twice ${d.repeated}`
    case 'fingerprint': return 'nothing to recognise it by'
    case 'named': return 'no name'
    case 'mixed': return d.why ? `more than one template: ${d.why}` : 'more than one template'
    case 'same_name': return `“${d.name}” also names ${(d.others as string[]).length} other call-site${(d.others as string[]).length > 1 ? 's' : ''} here`

    case 'models': return Object.entries(d.calls as Record<string, number>).map(([m, n]) => `${m} ${n}`).join(' · ')
    case 'volume': return `${d.today} calls, ${d.previous_day} the day before`
    default: return d.error ? String(d.error) : ''
  }
}

/** The audit of the audit: after a day is processed, how far to trust it. */
export default function Quality() {
  const [day, setDay] = useState<string>('')
  const q = useQuery({ queryKey: ['quality', day], queryFn: () => get<Report>('/suite/quality' + (day ? `?day=${day}` : '')),
    refetchInterval: (query) => (query.state.data?.running.length ? 5000 : false) })
  const run = useMutation({ mutationFn: (d: string) => get<unknown>(`/suite/quality/run?day=${d}`, { method: 'POST' }), onSuccess: () => q.refetch() })
  const crumbs = [{ label: 'Audit quality' }]
  if (q.isPending) return <Page crumbs={crumbs}><Loading /></Page>
  if (q.isError) return <Page crumbs={crumbs}><Failed error={q.error} /></Page>
  const r = q.data
  const target = day || r.day || r.days[0] || ''
  const busy = r.running.includes(target) || run.isPending
  return (
    <Page crumbs={crumbs}>
      <div className="head">
        <div><h1>Audit quality</h1><span className="muted">{r.day ? `day ${r.day}` : 'no day checked yet'}</span></div>
        <div className="row">
          {r.days.length > 0 && <select className="input" value={target} onChange={(e) => setDay(e.target.value)}>{r.days.map((d) => <option key={d} value={d}>{d}</option>)}</select>}
          <button className="btn" disabled={!target || busy} onClick={() => run.mutate(target)}>{busy ? 'Checking…' : 'Run the checks'}</button>
        </div>
      </div>
      {r.trust && <p className={'trust ' + r.trust.level.replace(' ', '-')}><b>{LEVEL[r.trust.level]}</b> · {r.trust.reasons.join(' · ')}</p>}
      <section className="panel">
        <table className="grid">
          <thead><tr><th>Check</th><th className="num">Checked</th><th className="num">Failed</th></tr></thead>
          <tbody>
            {Object.entries(r.kinds).map(([k, v]) => (
              <tr key={k}><td>{KIND[k] ?? k}</td><td className="num mono">{v.checked}</td><td className="num mono">{v.failed ? <b>{v.failed}</b> : 0}</td></tr>
            ))}
          </tbody>
        </table>
      </section>
      {r.failures.length > 0 && (
        <section className="panel">
          <div className="panel-head"><b>To look at</b><span className="muted">{r.failures.length}</span></div>
          <table className="grid">
            <thead><tr><th>Check</th><th>Usecase</th><th>Call-site</th><th>What</th></tr></thead>
            <tbody>
              {r.failures.map((i, n) => <tr key={n}><td>{KIND[i.kind] ?? i.kind}</td><td>{i.usecase}</td><td>{site(i)}</td><td>{detail(i)}</td></tr>)}
            </tbody>
          </table>
        </section>
      )}
    </Page>
  )
}
