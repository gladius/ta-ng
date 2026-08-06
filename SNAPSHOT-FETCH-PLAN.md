# Plan — snapshot + complete-trace fetch + worker-safe store

**Project:** token_auditor (C:\Workspace\token_auditor) · remote `git@github.com:gladius/ta-ng.git`
**Branch:** `feat/cache-prefix-expansion` · **Date:** 2026-08-06
**Test agents (recorded source):** `meridian-support` = downgrade(`route`) + cache-BREAKER(`triage`); `acme-support` = downgrade only.
**Server:** `uvicorn app.web.server:app --port 8100 --no-reload` (SINGLE worker for now — see #2).

## DONE (committed + pushed on feat)
- **Cache-prefix reorg lever** `app/services/cache_reorg.py` (`445df77`): byte-compare (static/dynamic across a call-site's instances) → expert LLM reorg (hoist static to a fixed prefix, dynamic moved VERBATIM) → DUAL proof: behaviour (reuse `audit.prove_transform` — re-run original model on the reorg + judge) AND caching (`cache_proof.prove_prefix` write→read, before vs after). Recommend only if SAFE + cache-proven. Reorg-only (CACHEABLE = informational, gateway handles it). No LLM early-exit.
- **Stable call-site keys** `app/services/graph.build` (`571a94f`): key = `agent/graph_path/node` (metadata-derived), NOT the connector content-variant `~<frame>` (which drifted with the live sample). Merges connector variants sample-weighted. buckets re-keyed to match.
- **Immutable per-report SNAPSHOT** `app/services/snapshot.py` + wiring (`8f573e6`): fetch+build graph ONCE, pin under `?snap=<id>` in the report URL. `report_view` mints+redirects on first load; view/prove/reprove/download all read THAT snapshot by id. No TTL (immutable → LRU-bounded, in-process). `funnel.build(g=...)` derives rows+cache from the pinned graph's own buckets (keyed by unique key). `prove.stream(snap)` reads rows+buckets from the pinned graph. `proof_key(...,snap)`. Empty-order fail-safe (never store all-zero). **This FIXED the $0-reset** (key drift between view and prove). Live-verified: first load 303→`?snap=`, follow→200.

## Clarification (the "fixed only partially")
- Snapshot fixed the **STABILITY** problem (reset) — DONE.
- Separate REMAINING problem = **DATA COMPLETENESS**: our fetch grabs a shifting WINDOW of the most-recent `limit` RUNS (`list_runs`, no `is_root`) → can slice trace trees in half → incomplete trees → node identity/graph_path mis-resolved, call-sites missing/`untagged`. A snapshot won't drift, but it may be built from a partial slice. Snapshot = one capture per report/audit-run (pinned by id; "Re-audit fresh" mints a new one).

## HIGH — do next (in order)
### #1 Complete-trace fetch via ROOT RUNS  (VALIDATED against docs · IMPLEMENTED in adapter.fetch live path · PENDING live prod verification — offline/tests unaffected)
- **Now:** `connectors/langsmith/adapter.fetch` → `client.list_runs(project, select=_SELECT)` no `is_root`, paginate to `limit` (source.py pull/pull_graph `limit=500` RUNS). Grabs most-recent 500 runs (mixed types, partial trees).
- **VALIDATED approach:** (a) `client.list_runs(project_name, is_root=True, limit=N)` -> N most-recent complete TRACES (returns one root run per trace; select id+trace_id, cheap). (b) for each trace, `client.list_runs(trace_id=<id>)` -> ALL runs of that trace (documented: fetches every run in the trace) -> a COMPLETE tree. Parallelize (b) with a bounded ThreadPoolExecutor. (c) feed whole-tree records to `connectors/graph.build_graph`.
- **Why this and not "fetch llm runs + trust metadata":** docs do NOT confirm `langgraph_node`/`checkpoint_ns` metadata is inherited by the child LLM run — and our prod `untagged`/drift proves it isn't reliable. So we NEED the tree for node/graph_path resolution. `is_root` + `trace_id=` are both documented; the batch `in(trace_id,[...])` filter syntax is unconfirmed -> avoid, use per-trace.
- **Count:** configurable env `AUDIT_TRACES` (default ~500 TRACES, up to 1000). NOTE this is TRACES now, not runs (500 traces x ~20 runs = big jump vs old 500 runs) — parallelize + cache to keep it fast.
- **Files:** `connectors/langsmith/adapter.py` (fetch: add is_root + per-trace hydration), `connectors/langsmith/source.py` (pull/pull_graph -> traces). Keep `pull_graph` feeding whole-tree records to `connectors/graph.build_graph`.
- **Efficiency:** ~N+1 calls, parallelized; snapshot built ONCE per report + cached to SQLite (#2), so cost is paid once.

### #2 Worker-safe snapshot/proof store (user OK'd adding a dependency)
- **Now:** `snapshot.py` + `store.py` are in-process module dicts → break with >1 uvicorn/gunicorn worker (page-load vs prove hit different workers → `snapshot.get` None). Fail-safe prevents a $0 reset but nothing proves.
- **DECIDED: simple IN-MEMORY store + Cloud Run `--max-instances=1`** (optionally `--min-instances=1` for no cold starts). Rationale: user deploys on Cloud Run (ephemeral + per-instance FS, autoscales) — so ANY per-instance store (memory OR SQLite) fails cross-instance, and GCS/Redis is over-engineering for a low-traffic advisory tool. The simple fix is to NOT need cross-instance sharing: one instance serves a user's whole flow (view->prove->re-fetch); in-memory works; a container restart just means the user re-audits (mints a new snapshot). SQLite was reverted.
- **`store.py` = in-memory `put/peek/clear` (LRU, no TTL). `snapshot.py` delegates.** Store stays a swappable put/peek/clear, so IF horizontal scale is ever truly needed, swap ONLY this module's backend for GCS/Redis — a one-file change, not now.
- **Ops note to give the user:** deploy with `gcloud run deploy --max-instances=1` (and `--min-instances=1` to avoid cold-start snapshot loss).
- **Immediate mitigation:** confirm prod launch command; run SINGLE worker until the shared store lands.

## DEFERRED — medium/low, revisit AFTER high
3. **Hero UX:** after prove, show "$X proven · N of M safe" + keep estimate visible (so $0 reads as verdict, not reset). `report.html:40`.
4. **Replay Anthropic-only** (`llm_client`): OpenAI/Gemini nodes detect but can't be proven (404→$0). Label "detect-only" until litellm neutralization (deferred handoff).
5. **`limit` truncation** silent: surface "sample of N over T".
6. **Cache lever:** multi-turn turn-aware `apply()`; LLM refusal handling on sensitive content.
7. **avg_in cache undercount** (input_tokens excludes cache-reads) → savings understated.
8. **Flat CALLS_BASIS** volume: use per-node observed volume (samples ÷ timespan) we already have.
9. **`untagged` nodes** (parent outside window) not flagged in UI (ties to #1).
10. **Dead code:** `store.get_or_build`/TTL now unused on the report path — remove.

## Guardrails
- Don't commit `PROJECT-OVERVIEW.md` (not ours; got swept into 8f573e6 by `git add -A`).
- Downgrade suites must stay green (`tests/test_downgrade_proof`, `test_downgrade_spine`, `test_cache_reorg`). NEVER read `.env` values.
