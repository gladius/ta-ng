import { useState } from 'react'
import type { Grams } from './cache'

/** The prompt as squares: one per word 4-gram of a sample call, in prompt order. Grey squares are in (nearly) every
 *  call of the call-site, red ones vary. The gold edge is where the chosen model's cache minimum falls: the faded
 *  squares before it are too few tokens to cache on their own. Shares come from the day script (MAP_SAMPLE calls). */
export function PromptMap({ grams, mins }: { grams: Grams; mins: Record<string, number> }) {
  const models = Object.keys(mins)
  const [pick, setPick] = useState(models[0] ?? '')
  if (!grams || !grams.shares) return null
  const words = grams.words.split(' ')
  const digits = Array.from(grams.shares, (d) => Number(d))
  const n = digits.length
  if (!n) return null
  // the squares: at most 1,500; beyond that one square stands for several grams (the lowest share of the group wins)
  const per = Math.max(1, Math.ceil(n / 1500))
  const cells: { i: number; varied: boolean; share: number }[] = []
  for (let i = 0; i < n; i += per) {
    const group = digits.slice(i, i + per)
    const low = Math.min(...group)
    cells.push({ i, varied: low < 9, share: low / 10 })
  }
  // tokens, estimated: the prompt's characters spread over its masked words, four characters a token
  const masked = words.reduce((a, w) => a + w.length + 1, 0) || 1
  const tokensAt = (gram: number) => {
    const upto = words.slice(0, Math.min(words.length, gram + 4)).reduce((a, w) => a + w.length + 1, 0)
    return Math.round((grams.chars * upto / masked) / 4)
  }
  const total = Math.round(grams.chars / 4)
  const min = pick ? mins[pick] ?? 1024 : 1024
  const firstVaried = digits.findIndex((d) => d < 9)
  const fixedLead = firstVaried < 0 ? total : tokensAt(firstVaried) - Math.round(grams.chars * 5 / masked / 4)
  const edge = cells.findIndex((c) => tokensAt(c.i) >= min)
  const same = digits.filter((d) => d >= 9).length
  return (
    <div className="pmap-wrap">
      <div className="pmap-head">
        <span>1 square ≈ {per === 1 ? '1 n-gram' : `${per} n-grams`}, in prompt order · {n.toLocaleString('en-US')} n-grams, about {total.toLocaleString('en-US')} tokens · shares over {grams.n} calls</span>
        {models.length > 1 && (
          <div className="seg" role="group" aria-label="Model for the cache minimum">
            {models.map((m) => <button key={m} type="button" aria-pressed={m === pick} onClick={() => setPick(m)}>{m}</button>)}
          </div>
        )}
      </div>
      <div className="pmap" role="img" aria-label={`${Math.round(100 * same / n)}% of the prompt is the same in every call; the fixed opening is about ${fixedLead} tokens against a cache minimum of ${min}`}>
        {cells.map((c, k) => (
          <i key={c.i} className={[c.varied ? 'varied' : '', tokensAt(c.i) < min ? 'under' : '', k === edge ? 'edge' : ''].join(' ').trim()}
            title={`${words.slice(c.i, c.i + 3 + per).join(' ')}\n${c.varied ? `in ${Math.round(100 * c.share)}% of calls` : 'in every call'} · ≈ token ${tokensAt(c.i).toLocaleString('en-US')}`} />
        ))}
      </div>
      <div className="pmap-facts">
        <span><b>{Math.round(100 * same / n)}%</b> of the prompt is the same in every call</span>
        <span>fixed opening <b>≈ {Math.max(0, fixedLead).toLocaleString('en-US')} tokens</b></span>
        <span>{pick && <>cache minimum on <span className="mono">{pick}</span> <b>{min.toLocaleString('en-US')} tokens</b></>}</span>
        {fixedLead < min
          ? <span className="warn"><b>Below the minimum:</b> the fixed opening alone cannot be cached on this model{firstVaried >= 0 && <>; the first change comes at about token {tokensAt(firstVaried).toLocaleString('en-US')}</>}.</span>
          : <span>The fixed opening clears the minimum: about <b>{Math.min(fixedLead, total).toLocaleString('en-US')} tokens</b> can be reused per call.</span>}
      </div>
      <div className="legend pmap-legend">
        <span><i /> same in every call</span>
        <span><i className="varied" /> varies across calls</span>
        <span><i className="under" /> before the cache minimum</span>
        <span><i className="edge" /> where the minimum falls</span>
      </div>
    </div>
  )
}
