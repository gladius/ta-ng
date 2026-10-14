# Plan: core (identity, ledger) → cache module → trust router (2026-10-11)

Order agreed: cache module first. All three need the same two things first: a local table shaped like the
Vegas Common log (`docs/vegas-common-log.md`) and traffic in it.

## Step 1 — replica table and traffic (sandbox `trust-router-sandbox`) — DONE 2026-10-11
`tools/llm_log.py` built it: 259k synthetic rows / 14 days in `litellm.llm_log`, view `litellm.v_llm_log` adds the
gateway's real calls. Every row's `synthetic` column holds the truth (call-site, version, session, turn, block sizes,
cached depth, why). Profile matches production's shapes (docs/vegas-common-log.md).
- Table `litellm.llm_log` (generic name; Vegas's column names so the same `BQ_*` settings fit both) (the 12 we use + the two JSON payloads), partitioned
  by `created_timestamp_utc`, clustered by `vegas_usecase_name`.
- View `litellm.v_llm_log` = that table UNION ALL our gateway's `raw_logs` mapped to the same columns, so
  every real call through our gateway (today's and future) shows up in the same shape. The app's `BQ_*` settings
  point at the view.
- Traffic, two layers:
  - **Bulk, free:** a generator writes rows directly, one archetype per usecase shape seen in production
    (UC-01 dynamic-top + per-call tools + 6-turn history; UC-03/04 constant 5k prompt single-turn; UC-09
    numeric template; UC-08 well cached; UC-10 Claude; embedding and Proxy rows to be skipped). Realistic
    timing (sessions, bursts, idle gaps) and usage fields computed from the prefix each call really shares,
    so the cache module's output is checkable against known truth. Sonnet 5 writes the agent definitions and
    user inputs once; the rest is templating.
  - **Real, paid, small:** the same agents called through our gateway on cheap models (gpt-5.4-mini/nano,
    claude-haiku-4-5), logged by the existing Pub/Sub pipeline. Real responses for downgrade / open-weight
    validation and real cache fields. Cost stated before running.
- The gateway logger is fine: rows with empty `messages` are LiteLLM's health checks; our own no-log calls
  never reach BigQuery, so the paid layer must send its calls WITHOUT the no-log flag.

## Step 2 — cache module (phase 1, read-only, no model calls)
- BigQuery aggregates per usecase × call-site × day: prompt tokens paid / read from cache / written; dollars
  paid, saved, missed; call gaps between prefix-sharing calls.
- Pages: overview (paid / cached / missed over time per usecase), ranked opportunities (call-site, fix, $/month,
  risk), per call-site prefix strip (tools | system fixed | system dynamic | history | latest turn, with where
  the cache stops today and where it could) and gap histogram with the TTL lines.
- Suggestions: TTL per provider (Gemini explicit cache TTL, Claude 5 min / 1 h, OpenAI retention) priced by
  replaying the real call timeline; prefix expansion (move fixed text first, cache marks for Claude); multi-turn
  caching. Two classes: no-behaviour-change (safe) vs prompt change (marked "needs proof").
- Prices: logged cost is the baseline; the catalog only for what-ifs. Land the two cost fixes first
  (Sonnet 5 price, `openai/`-prefixed names).

## Step 3 — call-site grouping
- Standalone skeleton method: digits masked, then the lines common across a sample of a usecase's prompts
  (static vs dynamic blocks); call-site = usecase + hash(tool names) + hash(skeleton). Stable templates fall
  out as the special case where the skeleton is the whole prompt.
- Accuracy test: run it on LangSmith traces ignoring their node ids, measure over-splits and merges. Then send
  the script to the production-side Claude to run on 1,000-prompt samples of UC-01 and UC-09, returning only
  group counts and masked skeletons.
- Replaces `_callsite()` in `connectors/bigquery/source.py`; the downgrade and open-weight flows then run on
  BigQuery unchanged. The same skeleton hash is trust router's hot-path fingerprint.

## Step 4 — proof and sampling
- Proof of layout changes through the validation engine (same model, old vs new prompt) plus a cache
  round-trip check (the old `445df77` lever did both; its breaker detection can be recovered from git).
- `POST /api/validations/sample` (usecase, days, N, candidates → a normal job): the answer to "a new model was
  released, is it safe for usecase X", and the daily trend when scheduled.

## Then trust router (own component, gateway side)
Fingerprint from step 3, pins + guards in Redis, LiteLLM hook, canary and rollback; earns pins by calling the
validation API. See `~/Workspace/trust_router/DESIGN.md`.

## Standing rules
Re-runs are faithful (exact logged request, all at once); the judge reads everything whole, never a cut;
gateway changes need the user's OK; paid runs state the cost first and set `max_budget_usd`.

## Architecture (agreed 2026-10-11)
```
core/identity/   call-site grouping (skeleton, versions, tools), sessions, flows (orch id, trace id, history)
core/ledger/     BigQuery aggregates per usecase x call-site x day; price catalog; daily refresh into our DB
core/profiler/   one call per call-site version: task, output type, fixed vs variable (reactivated later)
validation/      engine + judge (unchanged)
modules/cache/   diagnosis, TTL replay, prefix suggestions, rewrite + proof, monitoring (forecast, anomalies)
modules/models/  downgrade / open-weight / new-model (exists: agent audit + validations)
modules/router/  later
web/             React suite; three entry points on the same data: org, usecase, call-site
```
Rules: modules depend on core, never on each other; our DB holds summaries and verdicts, never log rows; one
refresh on demand (the Refresh button / POST /api/suite/cache/refresh; a schedule is a deployment decision, later); per-call-site numbers carry the grouping's confidence.
Audiences: executives (paid / saved / missed, trend, top opportunities), central team (fleet ranking, diagnoses),
usecase teams (their call-sites, charts, suggestions, proof status, before/after).
Visuals per call-site: cache timeline (write / read / miss with the TTL window), gap histogram with the cost-vs-TTL
curve, prefix bar (tools | fixed | dynamic | history | latest; cached-to vs could-cache-to), org->usecase->call-site
treemap, daily trends. BigQuery AI: ARIMA_PLUS spend forecasts, ML.DETECT_ANOMALIES on cache share and cost.
Later on the same ledger: tokenomics (output/reasoning share, max_tokens oversizing, history growth, retries, model
mix, embeddings, latency vs cost).

## Build order (branch `cache-module`), review after each
1. core/identity + core/ledger on the replica, scored against `synthetic` (grouping: over-splits and merges).
   DONE 2026-10-11: `core/identity/skeleton.py` (masked lines, greedy clustering by character-weighted containment
   of the cluster core, merge pass, anchors, daily-skeleton versions), `core/ledger/sites.py` (sample -> `sites`
   table -> SQL assignment of every row into `call_sites` by anchor presence), `core/ledger/daily.py`
   (`ledger_daily`: usecase x site x model x day; logged cost + cost_nocache from the catalog). Score on the replica
   via `tools/identity_score.py --build`: 100 % purity and completeness on all 14 usecases, versions exact; known
   limit: two versions live at once (A/B) read as one. Boundary: nothing in core/ or modules/ knows the test data;
   "synthetic" exists only in tools/llm_log.py (generator), the `synthetic` column, and tools/identity_score.py.
2. modules/cache: diagnosis, TTL replay, prefix suggestions; the three page levels with the charts.
3. Rewrite + proof through the validation engine; monitoring with anomalies and forecasts.
4. Profiler into core; tokenomics views.
Data needs none of: LangSmith (optional accuracy check only), trajectories beyond what orch/trace ids and message
history give, Gemini on the gateway (only for validating Vegas usecases later).

## Production test (next, before step 3) — 2026-10-11
Step 2 DONE on the replica: core/ledger/gaps.py, modules/cache/{rules,ttl,diagnosis,report}.py, core/ledger/refresh.py,
app/web/cache.py, web/src/pages/Cache.tsx (org / usecase / call-site pages). Now run it on the real log:
1. Deploy branch `cache-module` to the production environment with `.env`:
   BQ_TABLE=<project.dataset.vegas_table> BQ_REQUEST_COLUMN=request_payload BQ_RESPONSE_COLUMN=response_payload
   BQ_ID_COLUMN=vegas_common_services_txn_id BQ_USECASE_COLUMN=vegas_usecase_name BQ_TIME_COLUMN=created_timestamp_utc
   BQ_WORK_DATASET=<a dataset the service account may create tables in> (unset = the log's dataset)
2. `POST /api/suite/cache/refresh?days=1` first (sizes the scan: the two JSON columns over ~8M rows/day), then 14.
3. Read the usecase pages: "grouping unsure" chips, the number of call-sites per usecase against the counts in
   docs/vegas-common-log.md (UC-01 is the test), the causes. Expect "unexplained" where the provider's behaviour is
   unknown (UC-03/04): those are findings, not bugs.
Known gaps while testing: Gemini cache-read priced at the catalog's 10 % (Vertex: 25 %), so "saved by cache" is
overstated for Gemini; no profiler yet (what a call-site does); no rewrite/proof, anomalies or forecasts (step 3).

## Core rebuilt in SQL (2026-10-11, after review)
The first core downloaded prompts and clustered them in Python and rebuilt every table per refresh; replaced:
- `core/identity/sql.py`: call-site = hash of a prompt's 10 longest STABLE lines (in >= 50 % of the usecase's prompts,
  40+ chars, numbers masked); a prompt with no stable line uses its TEMPLATE lines (>= 5 %), which separates several
  templates under one usecase. Version = the template lines present, counting only lines that are stable or live for
  a limited run of days (a revision), not a repeating slot value. Layout per site (fixed vs per-call characters, how
  much fixed text sits after the first per-call line, tool sets) from a sample per site, all in SQL. Tables:
  `site_lines`, `call_sites` (by day, built incrementally), `sites_daily`, `site_layout`. Scored exact on the replica;
  whole build ~1 min, refresh ~3 min end to end including ledger, gaps, report.
- Site ids are strings in the API (64-bit hashes lose precision in JavaScript).
- Still to do from the review: Postgres summary tables fed per day (the stored-JSON report goes away, pages paged from
  Postgres); profiler as a core module with its own listing page; incremental ledger/gaps per day.

## Read-only BigQuery (2026-10-12)
Production BigQuery is read-only, so the app creates nothing there. A refresh is ONE script in a BigQuery session
(`core/ledger/build.py`): temp tables only (identity: line shares, every call -> site; per-site layout; the daily
ledger; the gap buckets), then small result sets into TWO app tables: `callsites` (one record per call-site: identity refreshed each run,
profile written once, `decisions` JSON per module; `core/identity/store.py`) and `ledger_daily` (the one time series,
usecase x site x model x day; `core/ledger/tables.py`). Pages read only those (`modules/cache/query.py`). Refresh on the replica: ~80 s end to end. Grouping still
exact. Incremental later = query only the days the app's tables lack (no BigQuery write needed).

## On demand per usecase (2026-10-12)
`usecases` table = the cheap list (usecase, calls, days; reloaded when older than 12 h) plus each usecase's analysis
state and roll-up. Opening a usecase runs the script scoped to it (`Build(days, usecase)`: ~30 s on the replica) and
writes its call-site records (identity, per-model daily series as JSON, profile once, decisions). The org view sums
the records of analysed usecases; "Analyse all usecases" runs the whole window (~65 s here). `ledger_daily` is gone:
the series live on the records. Core tables: `usecases`, `callsites`.

## Models module = Model checks (2026-10-12)
The agent-audit flow (LangSmith snapshots, select/prove/report pages) is retired (branch `langsmith-audit-reference`
keeps it). One pipeline: a check = scope (usecases, one check each, or a call-site) + a strategy (cheaper, better value,
release, re-validate, drift watch, manual; `modules/models/checks.py`) + models named on top; 12 sampled requests per
call-site by size; one validation job (engine unchanged except adaptive candidate runs: 3 first, up to 5 when one isn't
a pass); verdicts per call-site x candidate on the check (history) and on the record (`decisions.models`: latest per
candidate, valid 30 days and for the prompt version; the cheapest and fastest that pass by MEASURED cost and
latency), roll-up on the usecase. Pages: Checks (list, detail with the validation evidence, the one dialog with
chip multi-selects), `/how` explainer drawn from the engine's settings. Palette: black / white / warm greys / red /
gold only, no green.

## Names and paths (2026-10-12)
Model audit (module): a run is a "model audit" (`modules/models/audits.py`, table `model_audits`, column `strategy`,
ids `aud_`), public and documented at `/api/audit/models`; the UI at `/audit/models`, `/audit/models/:id`,
`/audit/models/how`. Cache audit: UI `/audit/cache...`, JSON `/api/audit/cache` (not documented yet). A validation is
the measurement underneath both, still `/api/validations` (public). /openapi.json = validations + model audits.

## One public API: model audits (2026-10-12)
`/api/validations` is gone: an audit's scope is usecases, a call-site, or requests you send (ids or bodies, optional
per-request candidates); one id per audit, also its job's id; results, resume, downloads and the callback live under
`/api/audit/models/{id}`. The UI's Validations pages became the audit's own pages (`/audit/models/:id/requests`,
`.../requests/:request`). "Validation" is now only the internal name of the engine (validation/). The old batches
were migrated into request-scope audits with their ids. Env names still say VALIDATION_* (deployment settings).

## Findings vs choice (2026-10-12)
An audit's findings are rows of `audit_verdicts` (audit x call-site x model), history kept; "what we know" about a
call-site is a query (latest valid verdict per model; cheapest/fastest that pass). The call-site record has one owner
per part: identity (analysis, or an audit's sampling: upsert only), profile (profiler), cache_findings (cache analysis),
choice (router or a person; the only stored decision, pointing at the audit it relied on). No shared decisions JSON,
no race. Any generative usecase can be audited (no "analysed" gate). The usecases list counts generative calls only:
chat-format analysed, other formats (prompt / non-embedding input) counted as not analysed yet; embeddings out.
Strategies pick among served generative models (up to 3), with a preview; drift watch removed until the engine can
compare with a previous audit's answers. catalog.canonical_model fixed: dashed dates and longest match
(gpt-5.4-2026-03-05 was mapped, and priced, as gpt-5).

## One queue for all background work (2026-10-12)
An audit's sampling and a usecase's analysis are rows of `tasks` (core/tasks.py), no longer threads of the web
process: any worker on any instance runs them, with the same claim token, heartbeat and stale takeover as request
groups; a released task is claimable at once; a task whose workers kept dying is given up after 3 attempts and its
audit shows failed. Workers take a task before a request group (tasks are short and unblock the rest). One analysis
per usecase at a time (a task key); "analysing" is read from the queue, never stored. A sampling re-run after a crash
finds its job and only finishes the status (samples are saved before the job is created). The usecases-list reload is
still a thread: cheap, idempotent, nothing to lose.

## Word windows and the scheduler (2026-10-13)
Call-sites now come from five-word windows (numbers masked, 1 in 8 kept by the first word's hash, shares on a sample of
5,000 per usecase, groups merged when their cores overlap 80%); the cache prefix from consecutive calls compared word
by word (30 pairs per call-site, median); fixed vs changing text from windows as written. The log is read once per
analysis (temp table src). Hard bench (tools/grouping_bench.py, sandbox table litellm.grouping_bench): 21/21 call-sites,
prefix within 2 words; lines got 20/21 with one-paragraph prompts merged and their prefix off by 1,270 words. Synthetic
log: 17/17. Whole window, 182k calls: 174 s, 17 GB billed.
Scheduler (core/schedule.py, page /schedules): jobs on our own database, one instance wins each due run by a
conditional UPDATE, the run is a task on the queue. Usecases list every 12 h (on); analyse every usecase every 24 h
(off until production's cost is known). The last background thread (list reload) is a task now.
Open: production's table size per day, clustering, table or view (the dry run); then a nightly pass over one new day.

## Next: rounds + daily incremental (agreed 2026-10-13)
Why: BigQuery has no index; a usecase filter does not cut the bill (only the day partitions do), so the log is read
once per run for all usecases, never per usecase. The 5%-of-sample rule hides call-sites under ~5% of a usecase.
1. Rounds (peeling), per usecase, all usecases in the same SQL step: sample the calls not yet placed; windows seen in
   >= a minimum COUNT of them (not a share) are candidate template text; group, merge; each group's template = the
   windows nearly all its calls share (the per-group histogram); place every call that contains most of a template;
   repeat on the leftovers (3 rounds). Rare call-sites become common among the leftovers. Calls never placed are shown
   as "not placed: N", never hidden. Windows stay; the histogram moves from the whole usecase to each group.
2. Daily incremental: each night BigQuery reads only yesterday (all usecases, one read) and returns day-groups with
   their templates (window hashes) and numbers. Our database links each day-group to a known call-site by template
   overlap (same / new version / new call-site); known call-sites can't live in BigQuery (production is read-only).
   Calls left unplaced in a day keep their window hashes in our DB, and are grouped across days there (rare call-sites
   with a few calls a day). History = one row per call-site per day; a 30-day view is a sum. Backfill: last 30 days,
   one day at a time. Daily vs weekly: same bytes in total; daily is fresher and smaller.
3. Model audits pick sample ids stored per call-site per day and fetch only those days. Output audit (later) reads the
   same nightly pass: answer length, length-limit hits, max_tokens use, format validity, per call-site.
Test first in the sandbox (tools/grouping_bench.py): add call-sites of ~150 and ~15 calls; rounds vs today's method;
then day by day with linking, including across a revision. Build into the app only if both hold.

## Done (2026-10-13): rounds, daily processing, scheduler
core/ledger/build.py is the day script (rounds over word windows; key = a call's most common windows, split by
20..80% windows, merge by core overlap; prefix from consecutive calls); core/ledger/refresh.py processes the missing
days of the window (ANALYSIS_WINDOW_DAYS, default 7) in order as one task, links day-groups to known call-sites per
usecase, keeps unplaced calls (capped) and groups them across days, writes site_days / site_samples, queues a profile
task per new call-site, redoes cache findings. Usecases list reads the usecase and time columns only. Audits sample
from site_samples and fetch bodies by id for those days. Scores: hard bench 24/24, synthetic log exact, 0 unplaced.
Versions are coarse (1-in-8 windows): a revision needs >= 3 windows and 10% of the template to change.

## Placement by fingerprint (2026-10-13, after the scale review)
A call-site's fingerprint = 5 phrases (5-word windows, lower case, numbers masked) nearly all its calls hold and its
siblings don't; stored on the call-site (markers) and passed to the day script (@known). Every call is placed by
substring search of its usecase's fingerprints, one pass, row-local, no row explosion; only the calls nothing matched
are learned from (sampled rounds, windows on the sample only). Slot time grows in a straight line with calls:
7 days of the synthetic log (90k calls, learning from nothing) 125 s wall, 434 slot-s, 3.2 GB. Scores unchanged:
synthetic log exact, hard bench 24/24 (the 15-call site left to the app's cross-day grouping). The same phrases serve
a router: a substring check on an incoming request. Found and fixed: BigQuery evaluates SELECT expressions before
QUALIFY (sample first, then compute), and carrying an array into UNNEST rows copies it per row (quadratic).
Next: hourly split and a per-statement byte cap; the router contract (fingerprints to Redis).

## Skipped usecases (2026-10-15)
SKIP_USECASES: LIKE patterns (e.g. `user-%`, developer traffic such as Claude Code: no stable template). One filter
(bq.SKIP_FILTER, the patterns as an array parameter: empty keeps all, a NULL usecase compares as '' and is kept) in
the day read and the usecases list; the lookup by request id ignores it. The router contract publishes the same list:
a request from a skipped usecase passes through with no matching and no pinning.
