import { useQuery } from '@tanstack/react-query'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { get, type Result, type ResultsPage } from '../api'
import { answerKind, change, Failed, Loading, Markdown, money, ms, Page, pct, verdictWord, vsPct } from '../ui'

type Run = Result['reference']['runs'][number] | Result['candidates'][number]['runs'][number]
const mean = (xs: (number | null | undefined)[]) => {
  const v = xs.filter((x): x is number => x != null)
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null
}
/** One model's average call on this request: tokens, reasoning, cost, latency. */
const figures = (runs: Run[]) => {
  const ok = runs.filter((x) => !x.error && x.usage)
  return {
    input: mean(ok.map((x) => x.usage!.input + x.usage!.cache_read + x.usage!.cache_write)),
    output: mean(ok.map((x) => x.usage!.output)),
    reasoning: ok.some((x) => x.usage!.reasoning != null) ? mean(ok.map((x) => x.usage!.reasoning ?? 0)) : null,
    cost: mean(ok.map((x) => x.cost_usd)), latency: mean(ok.map((x) => x.latency_ms)),
  }
}
const n = (v: number | null) => (v == null ? '—' : Math.round(v).toLocaleString('en-US'))
const kind = answerKind

/** A model's answer as it reads: markdown rendered (never raw HTML), or the error when the call failed. */
function AnswerText({ output, error }: { output: string; error: string | null }) {
  return <div className="answer-text">{error ? <span className="muted">{error}</span> : <Markdown text={output} />}</div>
}

/** Review a batch request by request: the list on the left, one request's evidence on the right. */
export default function Review() {
  const { auditId: jobId = '', '*': requestId = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const list = useQuery({                                   // the audit's requests in order, for previous / next
    queryKey: ['review-list', jobId],
    queryFn: () => get<ResultsPage>(`/audit/models/${jobId}/results?per_page=200`),
  })
  const ids = list.data?.results.map((x) => x.id) ?? []
  const at = ids.indexOf(requestId)
  const one = useQuery({
    queryKey: ['result', jobId, requestId],
    queryFn: () => get<Result>(`/audit/models/${jobId}/results/${encodeURIComponent(requestId)}`),   // the route param comes back decoded
  })
  const r = one.data
  const cand = r?.candidates.find((c) => c.label === params.get('cand')) ?? r?.candidates[0]
  const judged = cand?.verdict !== 'not_judged'
  const go = (patch: Record<string, string>) => setParams({ ...Object.fromEntries(params), ...patch })
  const firstOdd = cand && judged ? Math.max(0, cand.runs.findIndex((a) => kind(a) !== 'match')) : 0
  const picked = Number(params.get('answer') ?? firstOdd)
  const answer = cand?.runs[picked]
  const refs = r?.reference.runs ?? []
  const logged = refs.find((x) => x.recorded)
  const basePicked = Number(params.get('base') ?? Math.max(0, refs.findIndex((x) => x.agrees && !x.error)))
  const original = refs[basePicked]
  const fresh = refs.filter((x) => !x.recorded)
  const base = figures(fresh.length ? fresh : refs)
  const mine = cand ? figures(cand.runs) : null
  const settings = Object.entries(r?.request?.settings ?? {})
  const to = (id: string) => `/audit/models/${jobId}/requests/${encodeURIComponent(id)}${cand ? '?cand=' + encodeURIComponent(cand.label) : ''}`
  const vs = (v: number | null, b: number | null) => { const p = vsPct(v, b); return p == null ? null : <div><b>{p === 0 ? 'same' : change(p)}</b></div> }

  return (
    <Page crumbs={[{ label: 'Agentic workloads' }, { label: 'Model audits', to: '/audit/models' }, { label: jobId, to: `/audit/models/${jobId}` }, { label: requestId }]}>
        <div className="review-main">
          {one.isPending ? <Loading /> : one.isError ? <Failed error={one.error} /> : r && cand && mine && (
            <>
              {at >= 0 && (
                <div className="pager">
                  {at > 0 && <Link className="btn" to={to(ids[at - 1])}>‹ Previous</Link>}
                  <span className="muted">Request {at + 1} of {list.data!.total}</span>
                  {at < ids.length - 1 && <Link className="btn" to={to(ids[at + 1])}>Next ›</Link>}
                </div>
              )}
              <div className="head">
                <div>
                  <h1 className="mono">{r.id}</h1>
                  <p className="meta">baseline {r.model}</p>
                </div>
                <div className="seg" role="group" aria-label="Candidate">
                  {r.candidates.map((c) => (
                    <button key={c.label} type="button" aria-pressed={c.label === cand.label} onClick={() => setParams({ cand: c.label })}>
                      {c.label} · {c.verdict !== 'not_judged' && pct(c.fit_rate)} {verdictWord[c.verdict]}
                    </button>
                  ))}
                </div>
              </div>
              <p className={'verdict v-' + cand.verdict}>
                <b>{cand.label} {verdictWord[cand.verdict]}{cand.verdict === 'within' || cand.verdict === 'outside' ? `: ${cand.kept} of ${cand.of} answers fit.` : '.'}</b>{' '}
                {cand.verdict === 'unverified' ? cand.reason : judged && cand.judge_reason}
                {judged && r.reference.agree != null && <div className="muted">{r.model} gave the same answer {r.reference.agree} of {r.reference.of} times.</div>}
              </p>
              {judged && logged && !logged.agrees && (
                <p className="verdict"><b>The re-runs do not reproduce the logged answer.</b> {logged.reason}</p>
              )}

              <section className="panel">
                <div className="scroll">
                  <table className="grid">
                    <thead><tr><th>Average per call, this request</th><th className="num">Input tokens</th><th className="num">Output tokens</th><th className="num">of which reasoning</th><th className="num">Cost</th><th className="num">Latency</th></tr></thead>
                    <tbody>
                      <tr className="base-row">
                        <td><b>{r.model}</b><div className="muted">baseline</div></td>
                        <td className="num mono">{n(base.input)}</td><td className="num mono">{n(base.output)}</td><td className="num mono">{n(base.reasoning)}</td>
                        <td className="num mono">{money(base.cost)}</td><td className="num mono">{ms(base.latency)}</td>
                      </tr>
                      <tr>
                        <td><b>{cand.label}</b></td>
                        <td className="num"><span className="mono">{n(mine.input)}</span>{vs(mine.input, base.input)}</td>
                        <td className="num"><span className="mono">{n(mine.output)}</span>{vs(mine.output, base.output)}</td>
                        <td className="num"><span className="mono">{n(mine.reasoning)}</span>{vs(mine.reasoning, base.reasoning)}</td>
                        <td className="num"><span className="mono">{money(mine.cost)}</span>{vs(mine.cost, base.cost)}</td>
                        <td className="num"><span className="mono">{ms(mine.latency)}</span>{vs(mine.latency, base.latency)}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </section>

              {r.request && (
                <section className="block">
                  <h2 className="eyebrow">What was sent</h2>
                  <div className="sent">
                    {r.request.messages.map((m, i) => <p key={i}><b>{m.role}</b> {m.content}</p>)}
                    {r.request.tools.length > 0 && <p><b>tools</b> {r.request.tools.join(', ')}</p>}
                    {settings.length > 0 && <p><b>settings</b> <span className="mono">{settings.map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`).join(' · ')}</span></p>}
                  </div>
                </section>
              )}

              <section className="block">
                <div className="pair">
                  <article>
                    <div className="spread">
                      <b>{r.model} <span className="muted">baseline</span></b>
                      <div className="row">
                        {refs.map((x, i) => (
                          <button key={i} type="button" className="answer-chip" aria-pressed={i === basePicked} onClick={() => go({ base: String(i) })}>
                            {judged && <i className={x.error ? 'a-unclear' : x.agrees ? 'a-match' : 'a-drift'} />}{x.recorded ? 'logged' : i + 1}
                          </button>
                        ))}
                      </div>
                    </div>
                    {original && <AnswerText output={original.output} error={original.error} />}
                    {judged && original && !original.agrees && !original.error && <p className="note-line"><b>Unlike its other answers.</b> {original.reason}</p>}
                  </article>
                  {answer && (
                    <article className="picked">
                      <div className="spread">
                        <b>{cand.label}</b>
                        <div className="row">
                          {cand.runs.map((a, i) => (
                            <button key={i} type="button" className="answer-chip" aria-pressed={i === picked} onClick={() => go({ cand: cand.label, answer: String(i) })}>
                              {judged && <i className={'a-' + kind(a)} />}{i + 1}{judged && ' ' + kind(a)}
                            </button>
                          ))}
                        </div>
                      </div>
                      {(answer.degraded || (answer.model && answer.model !== cand.model)) && (
                        <span className="muted">{answer.model && answer.model !== cand.model ? `answered by ${answer.model}` : ''}{answer.degraded ? ` ran without ${answer.dropped?.length ? answer.dropped.join(', ') : 'a setting'}, which the model refused` : ''}</span>
                      )}
                      <AnswerText output={answer.output} error={answer.error} />
                    </article>
                  )}
                </div>
                {judged && answer && (
                  <div className={'judged k-' + kind(answer)}>
                    <i className={'a-' + kind(answer)} aria-hidden="true" />
                    <div>
                      <b>Answer {picked + 1}: {kind(answer)}{answer.judges.length > 1 && answer.votes && ` · ${answer.votes.replace('/', ' of ')} checks agree`}</b>
                      {answer.judges.length > 1 ? <ol>{answer.judges.map((v, i) => <li key={i}>{v.reason}</li>)}</ol>
                        : <p>{answer.judges[0]?.reason ?? answer.reason}</p>}
                    </div>
                  </div>
                )}
              </section>
            </>
          )}
        </div>
    </Page>
  )
}
