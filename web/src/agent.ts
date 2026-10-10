import { useParams, useSearchParams } from 'react-router-dom'

// One agent's audit (app/web/suite.py): where its pages live, and the shapes they read.

export type Ids = { source: string; ws: string; project: string }
const enc = encodeURIComponent

/** The agent's pages in this app, and its JSON under /api. */
export const page = (a: Ids, step: string, query = '') => `/agents/${enc(a.source)}/${enc(a.ws)}/${enc(a.project)}/${step}${query}`
export const api = (a: Ids, step: string, query = '') => `/suite/agents/${enc(a.source)}/${enc(a.ws)}/${enc(a.project)}/${step}${query}`
/** The ZIP to keep or hand on (markdown). */
export const download = (a: Ids, what: 'report' | 'profile', snap: string) => '/api' + api(a, `${what}.zip`, `?snap=${enc(snap)}`)

type DgMode = 'commercial' | 'open-weight'
export const dgmodeOf = (v: string | null): DgMode => (v === 'open-weight' ? 'open-weight' : 'commercial')
/** The query that keeps an audit in open-weight mode across pages ('' in the default, commercial mode). */
export const modeQuery = (dgmode: DgMode, sep = '?') => (dgmode === 'open-weight' ? `${sep}dgmode=open-weight` : '')

/** An agent page's ids, snapshot and downgrade mode, from its URL. */
export function useAgent() {
  const { source = '', ws = '', project = '' } = useParams()
  const [params] = useSearchParams()
  return { a: { source, ws, project }, snap: params.get('snap') ?? '', dgmode: dgmodeOf(params.get('dgmode')) }
}

/** Breadcrumbs from the audit's start to this agent's `step`, keeping the mode. */
export const crumbs = (a: Ids, step: string, dgmode: DgMode) => [
  { label: 'Agent audits', to: '/sources' + modeQuery(dgmode) },
  { label: a.source, to: `/sources/${enc(a.source)}${modeQuery(dgmode)}` },
  { label: 'agents', to: `/sources/${enc(a.source)}/${enc(a.ws)}${modeQuery(dgmode)}` },
  { label: `${a.project} · ${step}` },
]

type Row = {
  key: string; node: string; graph_path: string; model: string; calls: number; cost: number
  downgrade: boolean; downgrade_to: string | null; downgrade_usd: number; opportunity: number; testable: boolean
}
export type Funnel = {
  agent: string; calls: number; dgmode: DgMode; downgrade_total: number; graphs: string[]; revision: string | null
  rows: Row[]; total: number; traces: number; wins: number
}

/** One answer in a proof's evidence, and whether it fits the reference set. */
export type Run = { preserved: boolean; reason: string; votes: string; output: string }
/** One sampled input of a downgrade proof: the original's own answers and the cheaper model's, each judged. */
export type EvidenceRow = {
  input: string; system: string; kept: number; k: number; verdict: 'SAFE' | 'NOT-SAFE'; note: string; unverified: boolean
  self_rate: string; orig_runs: Run[]; samples: Run[]
}
type Proven = {
  key: string; node: string; graph_path: string; model: string; cost: number; downgrade_usd: number; error?: string
  downgrade?: { cheaper: string; verdict: string; n: number; reason?: string; inputs: EvidenceRow[] }
}
export type Proof = { results: Proven[]; total: number; downgrade_usd: number; dgmode?: DgMode }

export type ProfileNode = {
  node: string; kind: 'llm' | 'tool' | 'retriever' | string; op?: string; model?: string; tier?: string
  cost?: { per_basis: number | null }; unavailable?: string; summary?: string; shape?: string; calls: number
  avg_in?: number; avg_out?: number; error_rate?: number; avg_ms?: number; opp?: { downgrade_to?: string; downgrade_usd?: number }
}
export type Profile = {
  header: {
    agent: string; cost_basis: number; total_cost_basis: number; opportunity_total: number; opp_wins: number
    call_sites: number; tool_count: number; retriever_count: number; traces: number; subgraphs: string[]; error_rate: number
    revisions?: string[]; price_incomplete?: boolean
  }
  deployment?: { kind?: string; summary?: string } | null
  hotspots?: { costliest?: { node: string; per_basis: number }; busiest: { node: string; calls: number }; slowest?: { node: string; avg_ms: number } } | null
  sections: { name: string; graph_path: string; nodes: ProfileNode[] }[]
}
