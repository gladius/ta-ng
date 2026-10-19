import { useState } from 'react'
import type { Grams, MapModel } from './cache'

const KIND: Record<string, string> = { tools: 'tool schemas', system: 'system prompt', user: 'first message' }

/** The prompt as squares in the provider's cache order: tool schemas, system prompt, first message. One square per
 *  word 4-gram of a sample call. Grey squares are in (nearly) every call of the call-site, red ones vary. The gold
 *  band is what the chosen model actually reused from the cache, from the logged cached tokens; the gold edge is its
 *  cache minimum; the faded squares before it are too few tokens to cache on their own. Under the map, the sample
 *  itself with the varying stretches marked. Shares come from the day script (MAP_SAMPLE calls). */
export function PromptMap({ grams, models }: { grams: Grams; models: Record<string, MapModel> }) {
  const names = Object.keys(models)
  const [pick, setPick] = useState(names[0] ?? '')
  const [open, setOpen] = useState(false)
  if (!grams || !grams.segments?.length) return null
  const segs = grams.segments.filter((s) => s.shares.length > 0)
  if (!segs.length) return null
  // every gram of every segment in order, with its estimated token position (four characters a token)
  type Cell = { seg: number; i: number; share: number; token: number; first: boolean }
  const cells: Cell[] = []
  let tokens = 0
  for (const [k, s] of segs.entries()) {
    const words = s.words.split(' ')
    const masked = words.reduce((a, w) => a + w.length + 1, 0) || 1
    const digits = Array.from(s.shares, (d) => Number(d))
    let upto = 0
    for (const [i, d] of digits.entries()) {
      upto += words[i].length + 1
      cells.push({ seg: k, i, share: d / 10, token: tokens + Math.round((s.chars * upto / masked) / 4), first: i === 0 })
    }
    tokens += Math.round(s.chars / 4)
  }
  const per = Math.max(1, Math.ceil(cells.length / 2000))             // at most 2,000 squares; beyond, one square per `per` grams
  const squares: (Cell & { varied: boolean })[] = []
  for (let i = 0; i < cells.length; i += per) {
    const group = cells.slice(i, i + per)
    squares.push({ ...group[0], share: Math.min(...group.map((c) => c.share)), varied: group.some((c) => c.share < 0.9), first: group.some((c) => c.first) })
  }
  const m = pick ? models[pick] : undefined
  const min = m?.cache_min ?? 1024
  const reused = m && m.calls ? Math.round(m.cached_tokens / m.calls) : 0                 // tokens read from the cache per call, average
  const callsCached = m && m.calls ? m.calls_cached / m.calls : 0
  const firstVaried = cells.findIndex((c) => c.share < 0.9)
  const fixedLead = firstVaried < 0 ? tokens : cells[firstVaried].token
  const same = cells.filter((c) => c.share >= 0.9).length
  const edge = squares.findIndex((c) => c.token >= min)
  const words = (s: number) => segs[s].words.split(' ')
  return (
    <div className="pmap-wrap">
      <div className="pmap-head">
        <span>1 square ≈ {per === 1 ? '4 words' : `${4 * per} words`} · 100 a row · about {tokens.toLocaleString('en-US')} tokens · from {grams.n} calls</span>
        {names.length > 1 && (
          <div className="seg" role="group" aria-label="Model">
            {names.map((n) => <button key={n} type="button" aria-pressed={n === pick} onClick={() => setPick(n)}>{n}</button>)}
          </div>
        )}
      </div>
      {segs.map((sg, k) => {
        const own = squares.map((c, idx) => ({ c, idx })).filter(({ c }) => c.seg === k)
        return (
          <div key={sg.kind} className={'pmap-block k-' + sg.kind}>
            <div className="pmap-seg"><i className={'k-' + sg.kind} /> {KIND[sg.kind] ?? sg.kind} <small>≈ {Math.round(sg.chars / 4).toLocaleString('en-US')} tokens</small></div>
            <div className="pmap" role="img" aria-label={`${KIND[sg.kind] ?? sg.kind}: ${own.filter(({ c }) => !c.varied).length} of ${own.length} squares the same in every call`}>
              {own.map(({ c, idx }) => (
                <i key={idx} className={['k-' + sg.kind, c.varied ? 'varied' : '', c.token < min ? 'under' : '', idx === edge ? 'edge' : '', reused > 0 && c.token <= reused ? 'reused' : ''].join(' ').trim()}
                  title={`${words(c.seg).slice(c.i, c.i + 3 + per).join(' ')}\n${c.varied ? `in ${Math.round(100 * c.share)}% of calls` : 'in every call'} · ≈ token ${c.token.toLocaleString('en-US')}`} />
              ))}
            </div>
          </div>
        )
      })}
      <div className="pmap-facts">
        <span><b>{Math.round(100 * same / cells.length)}%</b> the same in every call</span>
        <span>fixed opening <b>≈ {Math.max(0, fixedLead).toLocaleString('en-US')} tokens</b></span>
        {m && <span>minimum on <span className="mono">{pick}</span> <b>{min.toLocaleString('en-US')}</b></span>}
        {m && <span>reused today <b>≈ {reused.toLocaleString('en-US')} tokens</b> a call{m.calls_cached > 0 && <>, {Math.round(100 * callsCached)}% of calls</>}{reused > tokens && ', into the conversation'}</span>}
        {fixedLead < min
          ? <span className="warn"><b>Below the minimum</b>{firstVaried >= 0 && <>: the first change is at token {cells[firstVaried].token.toLocaleString('en-US')}</>}</span>
          : <span><b>Clears the minimum</b>: about {Math.min(fixedLead, tokens).toLocaleString('en-US')} tokens reusable a call</span>}
      </div>
      <div className="legend pmap-legend">
        <span><i className="k-tools" /> tool schemas</span>
        <span><i className="k-system" /> system prompt</span>
        <span><i className="k-user" /> first message</span>
        <span><i className="varied" /> varies per call</span>
        <span><i className="under" /> under the minimum</span>
        <span><i className="edge" /> minimum</span>
        <span><i className="reused" /> reused today</span>
      </div>
      <button type="button" className="btn small" onClick={() => setOpen(!open)}>{open ? 'Hide the sample' : 'Show a sample call'}</button>
      {open && <Sample segs={segs} />}
    </div>
  )
}

/** The sample head, segment by segment, each word painted by the share of the 4-grams it belongs to. */
function Sample({ segs }: { segs: NonNullable<Grams>['segments'] }) {
  return (
    <div className="pmap-sample">
      {segs.map((s) => {
        const digits = Array.from(s.shares, (d) => Number(d))
        const words = s.words.split(' ')
        // a word varies when every gram through it varies (a fixed word next to a changing stretch keeps one stable
        // gram on its other side); masked words map back onto the raw text in order
        const varies = words.map((_, i) => {
          const through = [i - 3, i - 2, i - 1, i].filter((g) => g >= 0 && g < digits.length)
          return through.length > 0 && through.every((g) => digits[g] < 9)
        })
        const parts: { text: string; varied: boolean }[] = []
        const re = /[A-Za-z0-9]+/g
        let last = 0, wi = 0
        for (const mt of Array.from(s.sample.matchAll(re))) {
          if (wi >= words.length) break
          const before = s.sample.slice(last, mt.index)
          if (before) parts.push({ text: before, varied: false })
          parts.push({ text: mt[0], varied: varies[wi] })
          last = (mt.index ?? 0) + mt[0].length
          wi++
        }
        parts.push({ text: s.sample.slice(last), varied: false })
        return (
          <section key={s.kind}>
            <h4>{KIND[s.kind] ?? s.kind} <span className="muted">{s.chars.toLocaleString('en-US')} characters{s.kind === 'tools' && s.chars >= 8000 ? ', first 8,000 shown' : ''}</span></h4>
            <pre>{parts.map((p, k) => p.varied ? <mark key={k}>{p.text}</mark> : <span key={k}>{p.text}</span>)}</pre>
          </section>
        )
      })}
    </div>
  )
}
