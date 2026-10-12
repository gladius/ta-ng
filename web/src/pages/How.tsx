import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { get } from '../api'
import { Failed, Loading, Page } from '../ui'

type Strategies = {
  strategies: { id: string; does: string }[]
  spec: { reference_runs: number; first_runs: number; candidate_runs: number; votes: number; margin: number; coherence_floor: number; judge_model: string }
  samples: number; valid_days: number
}
export const STRATEGY_NAME: Record<string, string> = { cheaper: 'Cheaper', better_value: 'Better value', release: 'A release', revalidate: 'Re-validate', drift_watch: 'Drift watch', manual: 'Manual' }

function Step({ n, title, children, wide = false }: { n: number; title: string; children: React.ReactNode; wide?: boolean }) {
  return (
    <div className={'step' + (wide ? ' wide' : '')}>
      <div className="step-n">{n}</div>
      <div><b>{title}</b><p className="muted">{children}</p></div>
    </div>
  )
}

/** How a model audit works, drawn from the engine's real settings and the chosen strategy. */
export default function How({ strategy: initial = 'cheaper', spec: given, samples: givenSamples, embedded = false }: { strategy?: string; spec?: Strategies['spec']; samples?: number; embedded?: boolean }) {
  const [strategy, setStrategy] = useState(initial)
  const q = useQuery({ queryKey: ['strategies'], queryFn: () => get<Strategies>('/audit/models/strategies') })
  if (q.isPending) return embedded ? <Loading /> : <Page crumbs={[{ label: 'Model audit' }, { label: 'How a model audit works' }]}><Loading /></Page>
  if (q.isError) return embedded ? <Failed error={q.error} /> : <Page crumbs={[{ label: 'Model audit' }, { label: 'How a model audit works' }]}><Failed error={q.error} /></Page>
  const s = given ?? q.data.spec
  const samples = givenSamples ?? q.data.samples
  const does = q.data.strategies.find((x) => x.id === strategy)?.does ?? ''
  const body = (
    <>
      {!embedded && (
        <div className="head">
          <div><h1>How a model audit works</h1><span className="muted">drawn from the engine's current settings; pick a strategy to see what changes</span></div>
          <div className="seg" role="group" aria-label="Strategy">
            {q.data.strategies.map((x) => <button key={x.id} type="button" aria-pressed={strategy === x.id} onClick={() => setStrategy(x.id)}>{STRATEGY_NAME[x.id] ?? x.id}</button>)}
          </div>
        </div>
      )}
      <div className="flow">
        <Step n={1} title="Scope">The usecases (one audit each) or one call-site. A call-site is one prompt template, found from the instruction lines its calls share; every audit keys on its id.</Step>
        <Step n={2} title={`Sample ${samples} requests per call-site`}>From the log, by size: the smallest and largest prompts, the quartiles between, the two heaviest answers, the rest random. Breadth over depth: drift shows on different requests more than on repeats.</Step>
        <Step n={3} title={`Candidates: ${STRATEGY_NAME[strategy] ?? strategy}`} wide>{does}. Only models the gateway serves are tried. Labels are model names; whoever started the audit (a team, a release, the router, a schedule) is recorded on it.</Step>
        <Step n={4} title={`The original answers ${s.reference_runs}× more`}>Each sampled request is re-sent exactly as logged, all at once. With the logged answer that makes {s.reference_runs + 1} answers: the range of the original's own behaviour. If it agrees with itself less than {Math.round(100 * s.coherence_floor)}% of the time, nothing can be certified on that request.</Step>
        <Step n={5} title={`Each candidate answers ${s.first_runs}×, up to ${s.candidate_runs}×`}>Three answers first; only when one of them isn't a pass does the candidate answer {s.candidate_runs - s.first_runs} more times. A clear pass needs no more.</Step>
        <Step n={6} title={`The judge (${s.judge_model}) reads everything`}>The whole request and every answer, never a cut. An answer identical to one of the original's is a match with no judge call. Otherwise one vote; when it isn't a pass, {s.votes - 1} more votes and the majority decides. Each answer ends as match, acceptable (differs only where the original varies), drift (a changed decision, value or action, or an omission) or unclear.</Step>
        <Step n={7} title="Verdict per request">A candidate passes when its answers fit the original's range at least as often as the original agrees with itself, minus a margin of {Math.round(100 * s.margin)} points. Cost and latency are measured on the same calls.</Step>
        <Step n={8} title="Verdict per call-site, written on the record">Pass rate, fit, cost and latency against the original, per candidate; then the cheapest and the fastest that pass, by measured numbers. Valid for {q.data.valid_days} days and for this prompt version; after that, or after a prompt change, it is re-earned. Every audit keeps its history and links to the evidence (its validation).</Step>
      </div>
      {!embedded && <p className="muted">Start one from <Link to="/audit/models">Model audits</Link> (a usecase or call-site page links there with it selected), or with <span className="mono">POST /api/audit/models</span>.</p>}
    </>
  )
  return embedded ? <div className="flow-wrap">{body}</div> : <Page crumbs={[{ label: 'Model audit' }, { label: 'Model audits', to: '/audit/models' }, { label: 'How it works' }]}>{body}</Page>
}
