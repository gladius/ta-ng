import { useState } from 'react'
import type { EvidenceRow, Run } from './agent'
import { Markdown } from './ui'

/** A text box that scrolls, with a button to show all of it. */
function Box({ label, text }: { label: string; text: string }) {
  const [all, setAll] = useState(false)
  return (
    <div className="box">
      <div className="box-head"><span>{label}</span><button type="button" onClick={() => setAll(!all)}>{all ? 'collapse' : 'show all'}</button></div>
      <pre className={all ? 'all' : ''}>{text || '(none)'}</pre>
    </div>
  )
}

/** Answers as tabs, each with whether it fits the reference set and the judges' reason. */
function Runs({ runs, name, fits }: { runs: Run[]; name: string; fits: string }) {
  const [i, setI] = useState(0)
  const r = runs[i]
  if (!r) return <p className="muted">No answers recorded.</p>
  return (
    <div className="runs">
      <div className="row">
        {runs.map((x, j) => (
          <button key={j} type="button" className="answer-chip" aria-pressed={j === i} onClick={() => setI(j)}>
            <i className={x.preserved ? 'a-match' : 'a-drift'} />{name} {j + 1} · {x.preserved ? 'fits' : 'differs'}{x.votes && ` (${x.votes})`}
          </button>
        ))}
      </div>
      <div className="answer-text framed"><Markdown text={r.output} /></div>
      <p className="small"><b>{fits}</b> {r.preserved ? 'yes' : 'no'}{r.reason && ` — ${r.reason}`}</p>
    </div>
  )
}

/** Per sampled input: what was sent, the original's own answers (the reference set), and the cheaper model's. */
export default function Evidence({ inputs }: { inputs: EvidenceRow[] }) {
  return (
    <ul className="evidence">
      {inputs.map((iv, n) => (
        <li key={n}>
          <details>
            <summary>
              <span className={'tag ' + (iv.verdict === 'SAFE' ? 'tag-ok' : 'tag-bad')}>{iv.verdict === 'SAFE' ? 'safe' : 'not safe'}</span>
              {iv.unverified && <span className="tag tag-warn" title="A call failed, an output was too long to judge whole, a judge reply was unreadable, or the original was too inconsistent to compare against">not verified</span>}
              <span className="ev-note">{iv.note || 'judged'}</span>
              <span className="mono small nowrap">kept {iv.kept}/{iv.k}{iv.self_rate && ` · original ${iv.self_rate}`}</span>
            </summary>
            <div className="ev-body">
              <h3 className="eyebrow">What was sent</h3>
              <Box label="system" text={iv.system} />
              <Box label="user" text={iv.input} />
              <h3 className="eyebrow">The original's own answers — the reference set{iv.self_rate && ` · agree ${iv.self_rate}`}</h3>
              <Runs runs={iv.orig_runs} name="original" fits="Fits the other originals:" />
              <h3 className="eyebrow">The cheaper model's answers · judged against the reference set</h3>
              <Runs runs={iv.samples} name="re-run" fits="Fits the reference set:" />
            </div>
          </details>
        </li>
      ))}
    </ul>
  )
}
