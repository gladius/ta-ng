# The day run, end to end

Section B is what branch `audit-loop` now runs; A is what it replaced.

Prod numbers: 15.6M rows a day, about 200 usecases, 51% not chat, 2.2 TB billed for one read of the two JSON columns.
"Day one" = the first run, nothing known yet. "Steady state" = every run after that, when most calls are placed by
fingerprints already known and only new shapes need work.

## A. What ran before (replaced 2026-10-17)

Pure BigQuery word-window grouping (frequency windows, a splitter, Jaccard merges) plus Python linking, versioning and
leftover regrouping. Dropped because it counted words and could not tell instruction from data, and its errors were
silent. Its ledger, gaps, layout and coverage tables stayed.

## B. The proposed run

Rule: BigQuery collapses, the model decides on short digests, SQL counts verify every decision, the store keeps the
reason. The model never reads calls at volume.

| # | Step | Reads / calls | Day one | Steady state |
|---|---|---|---|---|
| B1 | The one read into **narrow temp tables clustered by usecase**: `calls` (numbers, model, usage, cost, latency, layout fields), `heads` (id, usecase, head text ≤ 8000 chars, `norm`, `prefix_key` = hash of the first 400 masked chars) | 2.2 TB | $14, 10 min | same |
| B2 | Place by known fingerprints (today's step 4) | heads.norm, one pass | — | places 95%+ |
| B3a | **Free key** on the calls B2 left: tool set + first 300 masked chars of the head. A key whose whole masked head is identical across its calls is a final call-site, no model (synthetic log: 47% of calls; bench: 9%) | heads, one pass | 1 min | 1 min |
| B3b | **Elimination loop, per usecase**: SQL picks the biggest remaining key, one call of it and two others with the same first 300 masked chars → **one model call** returns everything the call-site needs: name, what it does (the profile), 3–5 literal static phrases (the fingerprint), the static and per-call regions and whether per-call text comes before fixed text (the cache view), a cache suggestion → SQL counts: calls the phrases place (≥ 5, else the model picked data: one retry), calls already placed that they also match (0, else too generic: one retry) → placed calls are removed → next seed. Stop when under 1% is left or 3 seeds in a row place under 5 calls; the rest is "unplaced" | 2 small queries + 1 call per call-site: ~6.5k tokens in (3 heads of ≤ 8000 chars + instructions), ~400 out | ~2000 call-sites (3000 minus the static ones), 10 usecases in parallel: **Haiku 5.5 ~$2, Sonnet 5 ~$50; ~$8 of queries; 30–45 min** | only what known fingerprints leave: cents, minutes |
| B4 | Ledger and gaps (today's 8, 9) | calls | seconds | same |
| B5 | Layout per site: `lcp` of 30 pairs is enough; drop the window scan `lay_*` | heads, 30 rows per site | seconds | same |
| B6 | Download: sites with phrases, names, reasons, counts; 3 samples per site; request ids; coverage; unplaced ≤ 500 per usecase | small | — | — |
| B7 | **Findings agent, per usecase, Opus**: reads the store rows only (ledger, gaps, layout, sites with their cache view, model mix) and writes the finding: spend, trend, cache verdict with $ at stake and cause, model-fit candidates, anomalies, one recommendation naming the rows it rests on | 1 call of ~5k tokens in, ~1.5k out, ~8k thinking per usecase | ~200 calls, **~$35, 5 min** in parallel | same |
| B8 | Arithmetic checks (today's 17 without spot checks) | store | free | same |

Why B3 is cheap: the model reads 3 heads per call-site, once, ever, and that one call also yields the profile and the
cache view, so the separate profile task and the model spot checks go. Each round removes the biggest remaining site.
Every answer is a set of literal phrases that SQL proves or rejects by count, so a wrong answer costs one more small
round, not trust. The 2000 is an assumption (200 usecases × ~15 call-sites, a third fully static); the prod free-pass
count replaces it.

Models (list prices per million tokens; the gateway serves haiku-4-5, sonnet-5, opus-5 today — Haiku 5.5 needs adding):

| Step | Model | Thinking | Why |
|---|---|---|---|
| B3b static region | Haiku 5.5 ($0.10 / $0.50), fallback Sonnet 5 ($3 / $15) on a failed count check | none | extraction with a mechanical check behind it |
| B7 findings | Opus 5 ($5 / $25) | ~8k budget | judgement and writing for a reader; 200 calls a day |
| model audits | as today: candidates re-run, Sonnet 5 judges | none | unchanged |

What makes it trustworthy: for every call-site the store holds the phrases, the model's one-line reason, the day it
was learned, placed count and sibling-overlap count (0 required). Those four things are shown next to the site. A
reader can check any site in one glance. Nothing is placed by similarity, only by literal phrases that were counted.

Determinism: known fingerprints are never changed by the agent; a usecase whose placement falls below 90% gets its
unplaced calls to B3 as new sites. Retiring or merging sites is a separate, rare, reviewed step.

Daily total: day one ~$14 BigQuery + ~$8 queries + $2–50 grouping + $35 findings, about 1 h. Steady state ~$50, 30 min.

## C. What is built (branch audit-loop) and what is next

Built: B1 (src clustered by usecase, one read), B2, B3a as the seed picker, B3b as core/identity/loop.py with one
model call per call-site (profile + phrases + regions + version check) and a second call on three other placed calls
("one template?"; a no is stored as `mixed` on the version), B4–B6, B8 (the arithmetic checks; the model spot checks
are gone). `ONLY_USECASES` limits the run to a few usecases; a day's session is reused for a rerun of the loop (24 h).
Tests: tests/test_loop.py on a fake day and a fake model. Scorer: tools/identity_score.py (paid, the real model).
Scores 2026-10-17 with Sonnet 5, 3 rounds each: synthetic log 14 usecases, every call-site exact, 0 unplaced; hard
bench 16 usecases, every call-site exact and the 15-call site found, 0 unplaced, except the twins (two templates
sharing their first 70%) placed as one call-site. The second look ("one template?") is the guard for that case.

Next, in order:
1. The prod free-pass count (tools/free_pass_prod.sql, counts only) and the choice of two usecases.
2. One prod day on the two usecases; the owners confirm the call-sites.
3. B7, the findings per usecase on Opus, then the executive page.

## D. Open questions

From the earlier code review, still open: the coherence judge fails open (no DIVERGENT and no n/N in the reply counts
as a full pass; it should abstain); in dev without a gateway every target model counts as available.


1. Non-chat 51%: B1 can carry the Gemini-native and Vegas-native heads once we know their keys (structure query pending).
2. Prices: 89% of rows have no logged cost; catalog or gateway `/model/info` (needs your OK).
3. Closed loop (router, cache marks at the gateway): after B7 produces recommendations, not before.
4. Executive page: B7's findings per usecase are its rows. Build it once B7 exists.

## E. The n-gram fingerprint from the other session (2026-10-18): what we found and what to check there

The other session wrote `core/ledger/bq_fingerprint.py` (not in this repo): bucket = tool set + first 300 masked
characters (the `key` column), ML.NGRAMS 4-grams of `norm`, a phrase = a 4-gram found in exactly one bucket of the
usecase and in at least half of that bucket's calls, top 5 by share (at least 3), buckets of 10+ calls only, placed
through `hits()`/`place()` as round -1 before the loop; `prefix_map()` = per token position, share of calls holding
the same word.

The same SQL, transcribed, run here on the two tables whose true call-site is known (no model calls):

| Table | Exact | Failures | Unplaced |
|---|---|---|---|
| hard bench, 16 usecases | 13 usecases exact | twins merged (purity 51%); the 3 usecases with a date first became 7 false sites each ("today is monday", "today is tuesday", ...) | 76 calls in the date usecases |
| synthetic log, 3 days, 14 usecases | 13 usecases exact | plan-advisor became 5 false sites ("customer tom becker", "customer james kim", "current plan global traveler") | 1,184 of 1,943 plan-advisor calls |

The loop on the same tables: everything exact except the twins, 0 unplaced.

Why: the statistic is sound; two design choices are not. (1) The bucket is taken as the call-site, so a changing value
in the first 300 characters (name, weekday, plan, language) splits one template into many buckets. (2) "In exactly one
bucket" then discards every real template phrase, because the template text is in all the pieces, and keeps the value.
The false sites are saved as real and the loop never sees those calls again. Also: top phrases are masked numbers
("# # # #", "# # # and"); sites have no name or profile, so the model call per site stays; site ids hash the day's
phrases, which come from the day's statistics, so the same template gets a new id on another day; about 1,200 4-grams
per call, so a full day is roughly 9 billion n-gram rows a day (~0.7 TB read twice, ~$9, tens of minutes).

What n-grams are good for: the region map. Per site, each 4-gram's share of calls says which words are static and
which are per-call anywhere in the prompt. That beats the token-position map, which shows the whole tail as dynamic
after the first inserted value of varying length. Cheap on a sample of 50 calls per site, no model.

Inside a session on a read-only project (checked on the sandbox 2026-10-18): ML.NGRAMS, TEXT_ANALYZE, COSINE_DISTANCE
work; ML.BAG_OF_WORDS works as a window function; CREATE TEMP MODEL k-means and ML.PREDICT work; ML.TF_IDF is not
available; AI.GENERATE needs the Vertex AI API and roles in the enterprise project and sends prompts outside the
gateway; AI.GENERATE_EMBEDDING needs a CREATE MODEL in a dataset.

### Checks for the other session (paste as is; it reports counts only, no prompt text)

```
Before we wire bq_fingerprint into the loop, run these checks on the temp tables of your last
run and report numbers only (no prompt text, no phrases from customer data). Do not save sites
to the store yet.

1. Per usecase: chat calls, buckets with >= 10 calls (_fp_buckets), sites the pipeline produced
   (_fp_sites), calls placed in round -1, calls left unplaced.

2. Split templates: pairs of buckets that share >= 80% of their frequent 4-grams. Each such
   pair is one template counted as two sites.

   WITH f AS (
     SELECT r.usecase, r.bucket, r.ng
     FROM _fp_ngrams r JOIN _fp_buckets b ON b.usecase = r.usecase AND b.bucket = r.bucket
     GROUP BY r.usecase, r.bucket, r.ng, b.bsz
     HAVING COUNT(DISTINCT r.id) >= 0.5 * b.bsz),
   sz AS (SELECT usecase, bucket, COUNT(*) AS n FROM f GROUP BY 1, 2),
   pairs AS (
     SELECT a.usecase, a.bucket AS b1, b.bucket AS b2, COUNT(*) AS shared, sa.n AS n1, sb.n AS n2
     FROM f a JOIN f b ON a.usecase = b.usecase AND a.ng = b.ng AND a.bucket < b.bucket
     JOIN sz sa ON sa.usecase = a.usecase AND sa.bucket = a.bucket
     JOIN sz sb ON sb.usecase = b.usecase AND sb.bucket = b.bucket
     GROUP BY 1, 2, 3, 5, 6)
   SELECT p.usecase, (SELECT COUNT(*) FROM sz s WHERE s.usecase = p.usecase) AS buckets,
          COUNTIF(shared >= 0.8 * LEAST(n1, n2)) AS pairs_sharing_80pct
   FROM pairs p GROUP BY 1 ORDER BY 3 DESC

3. Thin sites: per site, how many 4-grams passed the "exactly one bucket and >= 50%" filter
   (COUNT(*) per usecase, bucket in _fp_candidates). Report the distribution: sites with < 20,
   20 to 100, > 100. A real template has hundreds; a template split by a value in its opening
   has a handful, and those are the value itself.

4. Number-only phrases: how many sites have 2 or more of their top-5 phrases made of '#'
   tokens with at most one real word (like "# # # #" or "# # # and").

5. Stability: run the pipeline on a second day and report how many site ids from day 1 appear
   again on day 2, and how many are new. Site ids hash the phrases, so a changed phrase set
   means a new site for the same template.

6. For the two chosen usecases only, your own judgement without quoting text: of the sites
   found, how many look like the same template to you (same instructions, different values)?
```

Reading the answer: check 2 nonzero means templates were split and the saved sites are wrong (the sandbox failure);
check 3 with many sites under 20 says the same from the other side; check 5 with few repeats means the store fills
with duplicate sites day after day; all clean means those usecases have no changing value in their first 300
characters today, the hole is still there but not biting, and the region map part is safe either way.

### 2026-10-19: the pre-pass is in

`core/ledger/bq_minhash.py` (the other session's MinHash plan, bound here with three changes: unplaced calls only,
collisions rejected, parameters instead of string building) always runs before the loop's rounds; its call-sites are
placed as round -1 and then described by one model call each (loop.describe: name, profile, regions, cache note,
"one template?" -> mixed), the statistics' phrases staying the fingerprint. No switch: one flow, the model call per
call-site is the same either way, and a merge is flagged, not silent. Phase 2 phrases need 3 real words like phase 1.
Bench with the pre-pass on: every call-site exact except the twins (merged, as every method does), 0 unplaced above
MIN_CALLS, the loop had nothing left to do.

First full day on the synthetic log (14 usecases) found two things the bench had not: a template with a per-call
value in its opening came out as 29 call-sites from its repeated heads (fixed: candidates matching mostly the same
calls are one template, the bigger stays, as in the loop; phase 1 phrases are spread over the head so a value in
the opening touches one of five), and the reader is the last guard: when the model describing a pre-pass site says
it is a version of another (`same_as`), its calls move there and it becomes that site's version (build.move).

The prompt map (site_grams in build.FINISH, `grams` on the call-site): the sample head's word 4-grams in order, each
with the share of 100 calls holding it. The pages draw one square per gram, grey in every call, red varying, faded
before the model's cache minimum (a picker when the site runs on several models), with the fixed opening in tokens
against that minimum. Presence, not position, so a varying-length insertion does not paint the tail red.

Next: one day on the two chosen usecases.
