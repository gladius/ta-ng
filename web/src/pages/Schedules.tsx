import { useMutation, useQuery } from '@tanstack/react-query'
import { get } from '../api'
import { Failed, Loading, Page, when } from '../ui'

type Job = {
  name: string; does: string; every_h: number; enabled: boolean; last_run: string | null; next_run: string | null
  task: { status: 'queued' | 'running' | 'done' | 'failed'; error: string | null; attempts: number; finished_at: string | null } | null
}

const STATE: Record<string, string> = { queued: 'waiting for a worker', running: 'running', done: 'done', failed: 'failed' }
const NAME: Record<string, string> = { usecases_list: "Usecases list", process_days: "Process the log's new days" }

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
            <thead><tr><th>Job</th><th>Every</th><th>Last run</th><th>Outcome</th><th>Next run</th><th /></tr></thead>
            <tbody>
              {q.data.jobs.map((j) => {
                const busy = !!j.task && ['queued', 'running'].includes(j.task.status)
                return (
                  <tr key={j.name}>
                    <td><b>{NAME[j.name] ?? j.name}</b><div className="muted">{j.does}</div></td>
                    <td className="nowrap">{j.every_h} h</td>
                    <td className="nowrap">{j.last_run ? when(j.last_run) : <span className="muted">never</span>}</td>
                    <td>{j.task ? <span className={'chip' + (j.task.status === 'failed' ? ' warn' : '')} title={j.task.error ?? undefined}>
                      {STATE[j.task.status]}{j.task.finished_at && ' ' + when(j.task.finished_at)}</span> : <span className="muted">—</span>}
                      {j.task?.error && <div className="muted">{j.task.error}</div>}</td>
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
