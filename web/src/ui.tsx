import type { ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Link } from 'react-router-dom'
import { candidates, type Answers, type Category, type Job, type LabelSummary, type Verdict } from './api'

type Crumb = { label: string; to?: string }

/** A page inside the shell: the bar with where you are, then the content. `fill`: the page fills the window and
    its panes scroll on their own (the review inbox). */
export function Page({ crumbs, children, fill = false }: { crumbs: Crumb[]; children: ReactNode; fill?: boolean }) {
  return (
    <main className={'page' + (fill ? ' fill' : '')}>
      <div className="topbar">
        <nav className="crumbs" aria-label="Breadcrumb">
          {crumbs.map((c, i) => (
            <span key={i}>{i > 0 && ' / '}{c.to ? <Link to={c.to}>{c.label}</Link> : <b>{c.label}</b>}</span>
          ))}
        </nav>
      </div>
      <div className="content">{children}</div>
    </main>
  )
}

export function Loading() {
  return <p className="empty">Loading…</p>
}

export function Failed({ error }: { error: unknown }) {
  return <p className="error" role="alert">Could not load: {error instanceof Error ? error.message : String(error)}</p>
}

const STATUS: Record<Job['status'], string> = {
  queued: 'Queued', running: 'Running', done: 'Done', stopped: 'Stopped', failed: 'Failed',
}

export function Status({ status }: { status: Job['status'] }) {
  return <span className={'pill pill-' + status}>{STATUS[status]}</span>
}

/** The words people read; the API keeps within / outside / unverified. */
export const verdictWord: Record<Verdict, string> = { within: 'passes', outside: 'drifts', unverified: 'not verified' }

export const money = (v: number | null | undefined, digits = 5) =>
  v == null ? '—' : '$' + v.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
export const ms = (v: number | null | undefined) => (v == null ? '—' : Math.round(v).toLocaleString('en-US') + ' ms')
export const pct = (v: number | null | undefined) => (v == null ? '—' : Math.round(v * 100) + '%')
export const change = (p: number | null | undefined) => (p == null ? '' : (p > 0 ? '+' : p < 0 ? '−' : '±') + Math.abs(p) + '%')
export const when = (iso: string) => iso.slice(0, 16).replace('T', ' ')
export const count = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`
/** A value against the original's, in % (−72 = 72% less); null when either is unknown. */
export const vsPct = (v: number | null, base: number | null) => (v == null || !base ? null : Math.round((v / base - 1) * 100))

type Judged = { category: Category | null; kept: boolean; error: string | null; reason: string }
/** What one answer was judged; older results carry no category, only kept or not. */
export const answerKind = (r: Judged): Category => r.category ?? (r.error ? 'unclear' : r.kept ? 'match' : 'drift')

/** One square per answer, coloured by its category; screen readers get the counts. */
export function Squares({ runs }: { runs: Judged[] }) {
  const kinds = runs.map(answerKind)
  const said = KINDS.map((k) => [k, kinds.filter((x) => x === k).length] as const).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`)
  return (
    <span className="sq" role="img" aria-label={said.join(', ')}>
      {kinds.map((k, i) => <i key={i} className={'a-' + k} title={`answer ${i + 1}: ${k}${runs[i].reason ? ' — ' + runs[i].reason : ''}`} />)}
    </span>
  )
}

/** A value against the original's: the gold tick is the original, the black fill this value on the same scale. */
export function VsBar({ value, base, shown, isBase = false }: { value: number | null; base: number | null; shown: string; isBase?: boolean }) {
  const r = value != null && base ? value / base : null
  const p = vsPct(value, base)
  return (
    <div className="vs">
      <span className="vs-track">
        <b className={isBase ? 'base' : ''} style={{ width: r == null ? 0 : Math.min(r, 1.5) * 66.7 + '%' }} />
        <i />
      </span>
      <span className="mono">{shown}</span>
      <span className={'vs-pc' + (isBase || !p ? '' : ' strong')}>{isBase ? 'baseline' : p == null ? '' : p ? change(p) + ' vs original' : 'same as original'}</span>
    </div>
  )
}

export function Legend() {
  return (
    <div className="legend">
      <span><i className="a-match" />match</span>
      <span><i className="a-acceptable" />acceptable: differs only where the original varies</span>
      <span><i className="a-drift" />drift: changed a decision, value or action, or made something up</span>
      <span><i className="a-unclear" />unclear</span>
      <span><i className="tick" />the original's cost or latency</span>
    </div>
  )
}

/** A headline number; `best` adds a highlighted tag (true: "best"). */
export function Kpi({ label, value, note, best }: { label: string; value: ReactNode; note?: ReactNode; best?: ReactNode }) {
  return (
    <div className="panel kpi">
      <span className="muted">{label}{best && <span className="best">{best === true ? 'best' : best}</span>}</span>
      <span className="kpi-v">{value}</span>
      {note && <span className="muted">{note}</span>}
    </div>
  )
}

const KINDS = ['match', 'acceptable', 'drift', 'unclear'] as const

/** A label's answers split by category: black match, grey acceptable, red drift, outlined unclear. */
export function AnswersBar({ a }: { a: Answers | null }) {
  const n = a ? a.match + a.acceptable + a.drift + a.unclear : 0
  if (!a || !n) return <span className="muted">—</span>
  const parts = KINDS.filter((k) => a[k])
  return (
    <div>
      <div className="abar" title={KINDS.map((k) => `${a[k]} ${k}`).join(' · ')}>
        {parts.map((k) => <span key={k} className={'a-' + k} style={{ width: (100 * a[k]) / n + '%' }} />)}
      </div>
      <div className="abar-cap">{n} answers · {parts.map((k) => `${a[k]} ${k}`).join(', ')}</div>
    </div>
  )
}

/** Model or analysis text as it reads: markdown rendered, never raw HTML. */
export function Markdown({ text }: { text: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
}

/** Each candidate label's pass rate in a batch, with its cost against the original. */
export function PassRows({ summary }: { summary?: LabelSummary[] | null }) {
  return candidates(summary).map((s) => (
    <div key={s.label} className="lrow">
      <b>{s.label}</b>
      <span className="track"><span style={{ width: 100 * (s.within_rate ?? 0) + '%' }} /></span>
      <span>{s.within} of {s.requests} pass</span>
      {s.cost_vs_original_pct != null && <span className="muted">{change(s.cost_vs_original_pct)} cost</span>}
    </div>
  ))
}
