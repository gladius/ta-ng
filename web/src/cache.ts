// The cache module's JSON (/api/audit/cache, app/web/cache.py).

export type Totals = {
  calls: number; prompt_tokens: number; cached_tokens: number; written_tokens: number; completion_tokens: number
  calls_cached: number; cost: number; cost_nocache: number; calls_with_tools: number; unsure: boolean
}
export type Cause = 'ok' | 'no_mark' | 'below_min' | 'dynamic_first' | 'tools_vary' | 'gaps' | 'sparse' | 'versions_churn' | 'unexplained'
export type Proof = 'safe' | 'proof' | null
export type Diagnosis = {
  cause: Cause; saving: number; suggestion: string; proof: Proof; achievable_share: number
  evidence: { provider: string; calls: number; avg_prompt_tokens: number; fixed_tokens: number; fixed_leading_tokens: number
    cache_min: number; cached_share: number; achievable_share: number; tool_sets: number; versions: number; static_after_dynamic: number }
}
export type Ttl = {
  provider: string; options: Record<string, { reads: number; writes: number; cost: number }>
  current: { cached_tokens: number; written_tokens: number; cost: number }
  default_ttl: number; best_ttl: number; saving: number; calls: number; beyond_default: number
  histogram: Record<string, number>
}
export type Trend = { recent: number; before: number; delta: number } | null
export type Profile = { name: string; does: string; task: string; output: string; fixed: string; per_call: string } | null
export type Story = { now: string; why: string; action: string; worth: number; share: number; proof: Proof; cause: Cause | null }
export type SiteModel = Totals & { model: string; ttl: Ttl | null; diagnosis: Diagnosis | null }
export type Site = {
  site: string; anchors: string[]; unsure: boolean; versions: { version: string; first: string; last: string; days: number }[]
  layout: { static_chars: number; dynamic_chars: number; static_after_dynamic: number; tool_sets: number }
  models: Record<string, SiteModel>; days: Record<string, Totals>; total: Totals; saving: number
  profile: Profile; story: Story; trend: Trend
}
export type Overview = {
  built_at: string | null; window: number; daily: Record<string, Totals>; total: Totals; saving: number; trend: Trend
  usecases: { usecase: string; total: Totals; saving: number; trend: Trend; sites: number; unsure: boolean; causes: Cause[] }[]
  status: { running: boolean; error: string | null; days: { window: number; done: string[]; queued: string[]; missing: string[] } }
}
export type UsecasePage = { built_at: string | null; window: number; usecase: string; total: Totals; days: Record<string, Totals>; trend: Trend; saving: number; sites: Site[] }
export type SitePage = Site & { built_at: string | null; window: number; usecase: string }

export const CAUSE: Record<Cause, string> = {
  ok: 'reusing well', no_mark: 'no cache mark', below_min: 'shared part too small', dynamic_first: 'per-call content first',
  tools_vary: 'tool list varies', gaps: 'cache expires too soon', sparse: 'calls too far apart', versions_churn: 'prompt changes often',
  unexplained: 'not reused, unexplained',
}
export const ttlName = (secs: number) => (secs < 3600 ? `${secs / 60} min` : secs < 86400 ? `${secs / 3600} h` : `${secs / 86400} d`)
/** The share of prompt tokens reused from cache. */
export const reuse = (t: Totals) => (t.prompt_tokens ? t.cached_tokens / t.prompt_tokens : 0)
/** What caching saved: the no-cache cost minus the logged cost (0 when the model has no catalog price). */
export const saved = (t: Totals) => Math.max(0, (t.cost_nocache || 0) - (t.cost || 0))
export const usecaseName = (u: string | null) => (u === 'None' || u == null ? '(no usecase tag)' : u)
export const siteName = (s: { profile: Profile; anchors: string[]; site: string }) => s.profile?.name || (s.anchors[0] ? s.anchors[0].slice(0, 60) : s.site)
