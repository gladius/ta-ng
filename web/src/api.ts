import { useEffect, useRef } from 'react'

// The /api calls the pages use. Shapes follow /openapi.json (app/web/public.py, app/web/model_audits.py), which a test keeps true to
// what the API returns.

export type Verdict = 'within' | 'outside' | 'unverified'
export type Answers = { match: number; acceptable: number; drift: number; unclear: number }

export type LabelSummary = {
  label: string
  models: string[]
  requests: number
  answers: Answers | null
  within: number
  outside: number
  unverified: number
  avg_fit_rate?: number | null
  avg_consistency?: number | null
  avg_latency_ms: number | null
  avg_cost_usd: number | null
  within_rate?: number | null
  cost_vs_original_pct?: number | null
  latency_vs_original_pct?: number | null
}

export type Job = {
  id: string
  status: 'queued' | 'running' | 'done' | 'stopped' | 'failed'
  created_at: string
  updated_at: string
  items_total: number
  items_done: number
  spend_usd: number
  max_budget_usd: number | null
  judge_model: string
  stop_reason: string | null
  error: string | null
  workers: number
  unpriced_calls: number
  summary?: LabelSummary[] | null
}

/** A batch's candidate labels, without the original's baseline row. */
export const candidates = (summary?: LabelSummary[] | null) => (summary ?? []).filter((s) => s.label !== 'original')

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function get<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch('/api' + path, { headers: { Accept: 'application/json' }, ...init })
  if (!r.ok) {
    let why = r.statusText
    try {
      const body = await r.json()
      why = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch { /* not JSON: keep the status text */ }
    throw new ApiError(r.status, why)
  }
  return r.json() as Promise<T>
}

type Routing = { label: string; served_model: string | null; requests: number; within: number; outside: number; unverified: number }

export type JobStatus = Job & {
  spec: { reference_runs: number; candidate_runs: number; first_runs?: number; votes: number; judge_model: string }
  summary: LabelSummary[]
  routing: Routing[]
  callback: { sent_at: string | null; error: string | null } | null
}

type Usage = { input: number; output: number; cache_read: number; cache_write: number }
type Call = { output: string; model: string | null; usage: Usage | null; latency_ms: number | null; cost_usd: number | null; error: string | null }
type OriginalAnswer = Call & { recorded: boolean | null; agrees: boolean; reason: string }
export type Category = 'match' | 'acceptable' | 'drift' | 'unclear'
export type CandidateAnswer = Call & {
  degraded: boolean | null; kept: boolean; category: Category | null; votes: string; reason: string
  judges: { kept: boolean | null; reason: string }[]
}
type CandidateResult = {
  label: string; model: string; verdict: Verdict; reason: string; judge_reason: string; kept: number; of: number
  fit_rate: number | null; answers: Answers | null; latency_ms: number | null; cost_usd: number | null; runs: CandidateAnswer[]
}
export type Result = {
  id: string; model: string; spend_usd: number
  reference: { agree: number | null; of: number; consistency: number | null; runs: OriginalAnswer[] }
  candidates: CandidateResult[]
  request?: { messages: { role: string; content: string }[]; tools: string[] } | null
}
export type ResultsPage = { page: number; per_page: number; total: number; results: Result[] }

export const post = <T,>(path: string) => get<T>(path, { method: 'POST' })

/** Listen to a server-sent event stream while `path` is set, one handler per event name; `error` when it drops. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Handlers = Record<string, (data: any) => void>
const FINAL = ['done', 'failed', 'complete']
export function useStream(path: string | null, handlers: Handlers) {
  const ref = useRef(handlers)
  useEffect(() => { ref.current = handlers })
  useEffect(() => {
    if (!path) return
    const es = new EventSource('/api' + path)
    let ended = false
    for (const name of Object.keys(ref.current).filter((n) => n !== 'error')) {
      es.addEventListener(name, (e) => {
        if (FINAL.includes(name)) { ended = true; es.close() }
        ref.current[name]?.(JSON.parse((e as MessageEvent).data))
      })
    }
    es.onerror = () => { if (!ended) { ended = true; es.close(); ref.current.error?.({ msg: 'The connection dropped.' }) } }
    return () => { ended = true; es.close() }
  }, [path])
}
