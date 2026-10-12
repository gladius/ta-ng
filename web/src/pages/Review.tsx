import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { get, type Result, type ResultsPage } from '../api'
import { answerKind, change, Failed, Loading, Markdown, money, ms, Page, Squares, verdictWord, vsPct } from '../ui'

const avg = (xs: (number | null)[]) => {
  const v = xs.filter((x): x is number => x != null)
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null
}
const vs = (v: number | null, base: number | null) => { const p = vsPct(v, base); return p == null ? '' : change(p) + ' vs original' }
const kind = answerKind

/** A model's answer as it reads: markdown rendered (never raw HTML), or the error when the call failed. */
function AnswerText({ output, error }: { output: string; error: string | null }) {
  return <div className="answer-text">{error ? <span className="muted">{error}</span> : <Markdown text={output} />}</div>
}

/** Review a batch request by request: the list on the left, one request's evidence on the right. */
export default function Review() {
  const { jobId = '', '*': requestId = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const [find, setFind] = useState('')
  const list = useQuery({
    queryKey: ['review-list', jobId, find],
    queryFn: () => get<ResultsPage>(`/validations/${jobId}/results?per_page=200&q=${encodeURIComponent(find)}`),
  })
  const one = useQuery({
    queryKey: ['result', jobId, requestId],
    queryFn: () => get<Result>(`/validations/${jobId}/results/${encodeURIComponent(requestId)}`),   // the route param comes back decoded
  })
  const r = one.data
  const cand = r?.candidates.find((c) => c.label === params.get('cand')) ?? r?.candidates[0]
  const firstOdd = cand ? Math.max(0, cand.runs.findIndex((a) => kind(a) !== 'match')) : 0
  const picked = Number(params.get('answer') ?? firstOdd)
  const answer = cand?.runs[picked]
  const original = r?.reference.runs.find((x) => x.agrees && !x.error) ?? r?.reference.runs[0]
  const origCost = r ? avg(r.reference.runs.filter((x) => !x.recorded).map((x) => x.cost_usd)) : null
  const origLatency = r ? avg(r.reference.runs.map((x) => x.latency_ms)) : null

  return (
    <Page fill crumbs={[{ label: 'Model audit' }, { label: 'Validations', to: '/validations' }, { label: jobId, to: `/validations/${jobId}` }, { label: requestId }]}>
      <div className="review">
        <section className="review-list" aria-label="Requests">
          <label htmlFor="review-find" className="sr">Find a request</label>
          <input id="review-find" className="input" type="search" placeholder="Find a request" value={find} onChange={(e) => setFind(e.target.value)} />
          {list.isPending ? <Loading /> : list.isError ? <Failed error={list.error} /> : (
            <ul>
              {list.data.results.map((x) => (
                <li key={x.id}>
                  <Link to={`/validations/${jobId}/requests/${encodeURIComponent(x.id)}${cand ? '?cand=' + encodeURIComponent(cand.label) : ''}`}
                    aria-current={x.id === requestId ? 'page' : undefined}>
                    <span className="spread"><span className="mono">{x.id}</span><span className="muted">original {x.reference.agree ?? '?'}/{x.reference.of}</span></span>
                    <span className="row">{x.candidates.map((c) => <span key={c.label} className="mini-runs">{c.label} <Squares runs={c.runs} /></span>)}</span>
                  </Link>
                </li>
              ))}
              {list.data.total > list.data.results.length && <li className="muted pad">First {list.data.results.length} of {list.data.total}; find one by id.</li>}
            </ul>
          )}
        </section>

        <div className="review-main">
          {one.isPending ? <Loading /> : one.isError ? <Failed error={one.error} /> : r && cand && (
            <>
              <div className="head">
                <div>
                  <h1 className="mono">{r.id}</h1>
                  <p className="meta">original {r.model} · its own answers agree {r.reference.agree ?? '?'} of {r.reference.of}</p>
                </div>
                <div className="seg" role="group" aria-label="Candidate">
                  {r.candidates.map((c) => (
                    <button key={c.label} type="button" aria-pressed={c.label === cand.label} onClick={() => setParams({ cand: c.label })}>
                      {c.label} · {verdictWord[c.verdict]}
                    </button>
                  ))}
                </div>
              </div>
              <p className={'verdict v-' + cand.verdict}><b>{cand.label} {verdictWord[cand.verdict]}.</b> {cand.judge_reason || cand.reason}{cand.judge_reason && <span className="muted"> ({cand.reason})</span>}</p>

              {r.request && (
                <section className="block">
                  <h2 className="eyebrow">What was sent</h2>
                  <div className="sent">
                    {r.request.messages.map((m, i) => <p key={i}><b>{m.role}</b> {m.content}</p>)}
                    {r.request.tools.length > 0 && <p><b>tools</b> {r.request.tools.join(', ')}</p>}
                  </div>
                </section>
              )}

              <section className="block">
                <div className="spread">
                  <h2 className="eyebrow">{cand.label}'s {cand.runs.length} answers</h2>
                  <div className="row">
                    {cand.runs.map((a, i) => (
                      <button key={i} type="button" className="answer-chip" aria-pressed={i === picked}
                        onClick={() => setParams({ cand: cand.label, answer: String(i) })}>
                        <i className={'a-' + kind(a)} />{i + 1} {kind(a)}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="pair">
                  <article>
                    <span className="muted">The original's answer · {original?.recorded ? 'the logged one' : 'a re-run'}</span>
                    {original && <AnswerText output={original.output} error={original.error} />}
                  </article>
                  {answer && (
                    <article className="picked">
                      <span className="muted">{cand.label} · answer {picked + 1}{answer.model && answer.model !== cand.model ? ` · answered by ${answer.model}` : ''}{answer.degraded ? ' · ran without a setting the model refused' : ''}</span>
                      <AnswerText output={answer.output} error={answer.error} />
                    </article>
                  )}
                </div>
                {answer && (
                  <div className={'judged k-' + kind(answer)}>
                    <i className={'a-' + kind(answer)} aria-hidden="true" />
                    <div>
                      <b>{kind(answer)[0].toUpperCase() + kind(answer).slice(1)}{answer.votes && ` · ${answer.votes} judges`}</b>
                      {answer.judges.map((v, i) => <p key={i}>{v.reason}</p>)}
                      {!answer.judges.length && answer.reason && <p>{answer.reason}</p>}
                    </div>
                  </div>
                )}
              </section>

              {r.reference.runs.length > 1 && (
                <section className="block">
                  <h2 className="eyebrow">The original's own answers</h2>
                  <ul className="plain">
                    {r.reference.runs.map((x, i) => (
                      <li key={i}><i className={x.agrees ? 'a-match' : 'a-unclear'} /> {x.recorded ? 'logged answer' : `re-run ${i + 1 - r.reference.runs.filter((y, j) => y.recorded && j < i).length}`}{x.agrees ? ' · agrees' : ' · differs: ' + x.reason}</li>
                    ))}
                  </ul>
                </section>
              )}

              <div className="facts">
                <span>{cand.label} on this request · <b className="mono">{money(cand.cost_usd)}</b> per call {vs(cand.cost_usd, origCost)}</span>
                <span><b className="mono">{ms(cand.latency_ms)}</b> {vs(cand.latency_ms, origLatency)}</span>
                <span>the original · <span className="mono">{money(origCost)}</span>, <span className="mono">{ms(origLatency)}</span></span>
              </div>
            </>
          )}
        </div>
      </div>
    </Page>
  )
}
