import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { get } from '../api'
import { Failed, Loading, Page, when } from '../ui'

type Run = { status: 'queued' | 'running' | 'done' | 'failed'; error: string | null; attempts: number; note: string | null
  queued_at: string | null; started_at: string | null; finished_at: string | null; seconds: number | null }
type Job = { name: string; does: string; every_h: number; enabled: boolean; last_run: string | null; next_run: string | null; task: Run | null; runs: Run[] }

const took = (s: number | null) => s == null ? '—' : s < 60 ? `${s} s` : s < 3600 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`

const STATE: Record<string, string> = { queued: 'waiting for a worker', running: 'running', done: 'done', failed: 'failed' }
const NAME: Record<string, string> = { usecases_list: "Usecases list", process_days: "Process the log's new days" }

// a log line's kind, for its colour: trouble, a usecase stopped with calls left, BigQuery progress, a finished step, a round
const kind = (msg: string) =>
  /error|refused|timed? ?out|failed:|[1-9]\d* (model calls )?failed/i.test(msg) ? 'bad'
    : /gave up|round cap/.test(msg) ? 'warn'
      : /statements done/.test(msg) ? 'dim'
        : /^done\b|^loop done|: done,|accepted|placed \d|described/.test(msg) ? 'good'
          : /^round \d/.test(msg) ? 'round' : ''
const when_ = (r: Run) => r.started_at ? when(r.started_at) : STATE[r.status]

/** The runs' logs in a dialog, as a terminal: time, day, the line coloured by kind; a filter, problems only, copy;
 *  while a run is live it follows the end (the page's refresh brings new lines). Earlier runs one click away. */
function Logs({ runs, title }: { runs: Run[]; title: string }) {
  const ref = useRef<HTMLDialogElement>(null)
  const pre = useRef<HTMLPreElement>(null)
  const [pick, setPick] = useState(0)
  const [find, setFind] = useState('')
  const [bad, setBad] = useState(false)
  const run = runs[pick] ?? runs[0]
  const all = (run?.note ?? '').trimEnd().split('\n')
  const running = run?.status === 'running'
  const lines = all.map((l) => {
    const m = /^(\d\d:\d\d:\d\d) (?:(\d{4}-\d\d-\d\d): )?(.*)$/.exec(l)
    return m ? { t: m[1], day: m[2] ?? '', msg: m[3] } : { t: '', day: '', msg: l }
  }).filter((l) => (!bad || kind(l.msg) === 'bad') && (!find || (l.day + ' ' + l.msg).toLowerCase().includes(find.toLowerCase())))
  const problems = all.filter((l) => kind(l) === 'bad').length
  useEffect(() => { if (running && ref.current?.open && pre.current) pre.current.scrollTop = pre.current.scrollHeight }, [all.length, running])
  return (
    <>
      <button type="button" className="btn small" onClick={() => { setPick(0); ref.current?.showModal() }}>
        Log{problems > 0 && <span className="tlog-bad"> · {problems}</span>}
      </button>
      <dialog ref={ref} className="tlog" onClick={(e) => { if (e.target === e.currentTarget) ref.current?.close() }}>
        <div className="tlog-head">
          <b>{title}</b>
          {runs.length > 1
            ? <select className="input" value={pick} onChange={(e) => setPick(Number(e.target.value))} aria-label="Run">
                {runs.map((r, i) => <option key={i} value={i}>{when_(r)} · {STATE[r.status]}{r.seconds != null ? ` · ${took(r.seconds)}` : ''}</option>)}
              </select>
            : <span className="muted">{STATE[run.status]}{run.seconds != null && ` · ${took(run.seconds)}`}</span>}
          <input className="input" placeholder="Filter" value={find} onChange={(e) => setFind(e.target.value)} />
          <label className="pick"><input type="checkbox" checked={bad} onChange={(e) => setBad(e.target.checked)} /> problems only ({problems})</label>
          <button type="button" className="btn small" onClick={() => navigator.clipboard?.writeText(run?.note ?? '')}>Copy</button>
          <button type="button" className="btn small" onClick={() => ref.current?.close()}>Close</button>
        </div>
        <pre ref={pre} className="term">
          {lines.length === 0 && <span className="dim">no lines{find || bad ? ' match' : ''}</span>}
          {lines.map((l, i) => (
            <div key={i} className={'k-' + (kind(l.msg) || 'plain') + (running && i === lines.length - 1 && !find && !bad ? ' now' : '')}>
              <span className="t">{l.t}</span>{l.day && <span className="d">{l.day}</span>}<span className="m">{l.msg}</span>
            </div>
          ))}
          {running && <div className="cursor">▍</div>}
        </pre>
      </dialog>
    </>
  )
}

/** The scheduled jobs: when each last ran and how it went, when it runs next. One instance runs each job. */
export default function Schedules() {
  const q = useQuery({ queryKey: ['schedules'], queryFn: () => get<{ jobs: Job[] }>('/suite/schedules'),
    refetchInterval: (query) => (query.state.data?.jobs.some((j) => j.task && ['queued', 'running'].includes(j.task.status)) ? 4000 : 30000) })
  const post = useMutation({
    mutationFn: ({ name, body }: { name: string; body?: object }) => get<{ jobs: Job[] }>(`/suite/schedules/${name}${body ? '' : '/run'}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) }),
    onSuccess: () => q.refetch() })

  return (
    <Page crumbs={[{ label: 'Schedules' }]}>
      <div className="head">
        <div><h1>Schedules</h1></div>
      </div>
      {q.isPending ? <Loading /> : q.isError ? <Failed error={q.error} /> : (
        <section className="panel">
          <table className="grid">
            <thead><tr><th>Job</th><th>Every</th><th>Last run</th><th>Outcome</th><th>Log</th><th>Next run</th><th /></tr></thead>
            <tbody>
              {q.data.jobs.map((j) => {
                const busy = !!j.task && ['queued', 'running'].includes(j.task.status)
                return (
                  <tr key={j.name}>
                    <td className="job"><b>{NAME[j.name] ?? j.name}</b><div className="muted">{j.does}</div></td>
                    <td className="nowrap">{j.every_h} h</td>
                    <td className="nowrap">{j.task?.started_at ? when(j.task.started_at) : <span className="muted">{!j.task ? 'never' : j.task.status === 'queued' ? 'waiting' : '—'}</span>}
                      {j.task?.started_at && <div className="muted">{j.task.status === 'running' ? `${took(j.task.seconds)} so far` : `took ${took(j.task.seconds)}`}</div>}</td>
                    <td>{j.task ? <span className={'chip' + (j.task.status === 'failed' ? ' warn' : '')} title={j.task.error ?? undefined}>{STATE[j.task.status]}</span> : <span className="muted">—</span>}
                      {j.task?.error && <div className="muted">{j.task.error}</div>}
                      {j.task?.note && j.task.status === 'running' && <div className="muted">{j.task.note.trimEnd().split('\n').pop()}</div>}
                      {j.runs.length > 1 && <div className="muted runs">earlier: {j.runs.slice(1).map((r, i) => <span key={i} className="nowrap">{r.started_at ? when(r.started_at) : r.status === 'queued' ? 'waiting' : '—'} · {STATE[r.status]} · {took(r.seconds)}</span>)}</div>}</td>
                    <td className="nowrap">{j.runs.some((r) => r.note) ? <Logs runs={j.runs.filter((r) => r.note)} title={NAME[j.name] ?? j.name} /> : <span className="muted">—</span>}</td>
                    <td className="nowrap">{!j.enabled ? <span className="muted">off</span> : j.next_run && j.next_run.includes('T') ? when(j.next_run) : j.next_run}</td>
                    <td className="row nowrap">
                      <button className="btn" disabled={busy || post.isPending} onClick={() => post.mutate({ name: j.name })}>Run now</button>
                      <button className="btn" disabled={post.isPending} onClick={() => post.mutate({ name: j.name, body: { enabled: !j.enabled } })}>{j.enabled ? 'Turn off' : 'Turn on'}</button>
                    </td>
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
