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
   BQ_ID_COLUMN=vegas_common_services_txn_id BQ_AGENT_COLUMN=vegas_usecase_name BQ_TIME_COLUMN=created_timestamp_utc
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
