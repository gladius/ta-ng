# Profiling Plan — token_auditor graph profiler (settled 2026-08-16)

Branch: `feat/agent-profiling`. This is the CURRENT, authoritative plan. The old `AGENT-PROFILING-PLAN.md`
is **deleted** (superseded). Written to survive context compaction — it captures the hard-won
conclusions so we don't re-litigate them.

## Goal
A trustworthy per-node + per-deployment **profile** from LangSmith traces: what each call-site does,
its **measured** cost/token/cache/latency facts, and the graph shape. Consumers (downgrade / cache /
compress levers, and a future error/self-improvement loop) read this profile.

## Scope: the PROFILE is the WHOLE deployment; the LEVERS are `run_type=llm` only
The profile must describe **everything** — llm + tool + retriever + chain nodes + topology + an
agent-level "what this deployment does" — so we understand the agent end to end. We focus on
`run_type=llm` *only for the optimization levers* (downgrade/cache/compress), because that's where
tokens and input-complexity live and where we'd act. **Do not conflate them:** llm-only is the lever
scope, not the profile scope.
> **Current gap (verified):** `connectors/graph.py::build_graph` builds nodes+stats from **llm samples
> only** (line 196); chain nodes appear only in the edge ordering; **tool/retriever runs are invisible
> (neither nodes nor stats).** Whole-deployment profiling requires **building typed non-llm nodes** —
> new work, not reuse.

---

## The hard-won realization (why the plan is shaped this way)

We spent a long research + spike effort trying to GROUP a node's inputs into "input-kinds." Verified
conclusions — do NOT re-open these:

1. **Input-kind grouping is FUZZY and UNVERIFIABLE.** Agent inputs are unbounded natural language;
   there is **no ground truth** for "the correct kinds" in production. (The spike could measure
   recovery only because we *planted* the kinds.) Any grouping is one plausible summary, not a fact.
2. **It is also NOT cheap for large inputs.** Spike results (`spikes/input_grouping/`):
   off-the-shelf **embed+cluster fails** — small local embedder too weak (over-fragments; splits a
   1-kind summarizer into 4-7; smears subtle kinds), large local embedder too slow on CPU (no GPU),
   and clustering can't reliably decide K. **"One LLM call over 100 requests" also fails** when the
   variable content is large (summarizer docs, RAG context, code diffs) — you can't batch big inputs
   into one prompt. The "request is always small" assumption is false.
3. Production (LangSmith Insights) uses an LLM to group *because it is a judgment* — but that's
   advisory, not provable, and expensive for big inputs.

**Therefore: input-kind grouping is DEMOTED to an advisory, best-effort garnish — NEVER load-bearing
for a trustworthy claim.** For large-input nodes it is honestly just "one operation, inputs vary (not
enumerated)." This is the north star reasserting itself: *don't judge what you can't prove.*

---

## The trustworthy architecture (layered by what is actually provable)

1. **Metadata facts (the SPINE) — size-agnostic, $0, MEASURED, verifiable.** Computed from run
   metadata (token counts, cost, cache, latency), **NOT by reading input text** — so a 100-token node
   and a 100k-token node profile identically and cheaply. This is the trustworthy core.
2. **One-sample advisory role description — ONE bounded LLM call** (read a single sample, head+tail
   digest if huge) → "what this node does." Advisory, but reliable for operation/role because the
   operation is stable even when inputs vary.
3. **Downgrade proof coverage — via diverse SAMPLING, not grouping.** Sample N diverse real inputs,
   prove per-input, bound honestly (rule-of-three: drift ≤ ~3/N). Verifiable. Grouping was an
   unverifiable detour toward coverage we can get directly by sampling.
4. **Input-kind grouping — DEFERRED, advisory only.** Best-effort LLM-per-node over a sample where
   feasible (small requests); "not enumerated" for large-input nodes. Never a fact the system depends on.

---

## What EXISTS vs what to BUILD (verified against the code 2026-08-16)

**EXISTS — reuse:**
- **`connectors/graph.py::build_graph` — the deterministic metadata profile is ~90% BUILT.** Per
  call-site it already computes: `model` (mode) + `models_list` distribution, `mixed_model`,
  `avg_in`/`avg_out`, `avg_ms`, `error_rate`, **`cost_ls` (measured LangSmith cost)**, `avg_tools`,
  **`feedback_score`**, **`ttft_ms`**, `out_type` (json/short/free-form), `tool_input_rate`,
  **`out_p50`/`out_p95`**, `traces`, `first`/`last`, `graph_path`; plus `edges` (topology) and
  `revisions`.
- **`app/services/profile/select.py`** — deterministic sample + `bucket_facts` (`distinct_system`
  count = the free static-check) + **head+tail digest for big prompts** (the "read one sample,
  truncate if huge" mechanism). BUILT, $0.
- **`app/services/profile/analyze.py`** — ONE `PROFILE_MODEL` call → role / system_nature / … BUT it
  is **over-built** (huge schema) and **UNWIRED** (dead code — nothing calls it).

Errors are handled correctly (reuse): failed/empty samples are **counted** (`excluded` → `error_rate`)
but kept **out** of the audited bucket (lines 249-251). Revisions are captured (`revs`) but **unused**.
Threads (`thread_id`/`session_id`) are **not modeled** — grouping is by `trace_id` only.

**BUILD / CHANGE:**
- **(a) Surface the dropped measured facts.** `app/services/graph.py` (≈lines 39-49) passes up only
  `avg_in/out`, `model`, `mixed`, `out_type` — it **drops** `cost_ls`, `error_rate`, `feedback_score`,
  `ttft_ms`, `avg_ms`, `out_p50/p95`, `avg_tools`, `tool_input_rate`. Pass them through.
- **(b) Add cache facts** — cache-enabled + cache-read % per node (from `usage` cache_read tokens /
  the caching lever).
- **(c) Build typed NON-LLM nodes** (whole-deployment scope) — represent tool / retriever / chain runs
  as nodes with structural facts (type, name, call count, latency, error rate — no tokens); currently
  invisible. Deep facts stay on llm nodes.
- **(d) Simplify + WIRE `analyze.py`** — trim to the reliable advisory bits (`role`, `purpose`,
  `system_nature` reconciled against `distinct_system`), invoke once per llm node via `select.py`'s
  sample+digest. Label **ADVISORY**. Plus an agent-level "what this deployment does" summary.
- **(e) Assemble a `DeploymentProfile`** — deployment → agents → **all** nodes (typed) + topology +
  measured facts (llm) + advisory role.
- **(f) Read-only UI** (later): render the profile. No computation.

**FOUNDATION DECISIONS (not pure defer — needed for whole-deployment):**
- **Threads** — capture `thread_id`/`session_id` (already fetched, unused) for multi-turn grouping +
  per-conversation cost. Decide now whether v1 models threads or flags them.
- **Revision** — `revision_id` is the deployment-version dimension; key `revision → agent → node`
  (metadata key, NEVER a content fingerprint — content keys drift and reset $ to 0).

**DEFER:** input-kind grouping (advisory), fleet / cross-agent rollup.

---

## STATUS — first slice LANDED (2026-08-16, tested green, 25/25 existing tests pass)
Built + working end-to-end (offline, keyless):
- `connectors/recorded/source.py` — keyless offline DataSource serving `fixtures/*.json` (source name
  `recorded`), so the whole pipeline runs with no LangSmith key.
- `app/services/graph.py` — now **surfaces the measured facts** it used to drop (cost_ls, error_rate,
  feedback, ttft, avg_ms, p50/p95, tools), variant-merge-aware.
- `app/services/profile/assemble.py` — `build_profile(g)` → `{header, nodes, edges, graphs}`; **cost from
  the ONE catalog source** (`auditor.util` PRICE/usd, same as downgrade), unknown/self-hosted models fall
  back to LangSmith cost else flagged "price unknown".
- `app/web/server.py` `/…/profile` route + `app/web/templates/profile.html` — snapshot-pinned, read-only
  **whole-deployment profile page**: header stats + per-call-site measured facts, honest sample-size +
  advisory-role + scope labels. Verified on the meridian-support fixture (6 call-sites, $236.63/10k).
- Cost decision applied: **one source = tokens × `auditor/models.json`** (edit that file to add
  self-hosted prices); LangSmith `cost_ls` = fallback/cross-check only.

**NEXT increments (labeled on the page as "coming"):** typed non-llm nodes (tool/retriever/chain —
connector change, char-tests first) · thread grouping · untagged/deep-agent skeleton-fingerprint · the
advisory role LLM pass · per-node sampling floors (replace the 300-run flat cap) · a nav link to the page.

## FIRST SLICE (buildable now)
The **whole-deployment** deterministic metadata profile end-to-end:
1. (a) surface the already-computed measured facts + (b) cache facts in `app/services/graph.py`;
2. (c) build typed non-llm nodes so the graph is the whole deployment, not just llm call-sites;
3. (d) trim + wire a one-sample **advisory** role per llm node + an agent-level summary;
4. (e) assemble the `DeploymentProfile` (all node types + topology + facts).
Plus a **foundation call** on threads (model vs flag) and revision (version key). Trustworthy (measured
facts), size-robust (metadata-based, never reads big inputs); (a)/(b) reuse existing code, (c) is new.

---

## Spike artifacts (the grouping validation — keep for the record)
`spikes/input_grouping/`:
- `synth_data.py` — 614 planted-ground-truth records across `dynamic_router`, `rag_answer` (question
  buried in ~10k doc), `planner`, `doc_summarizer` (1 kind, varied payloads/shapes), `mixed_bucket`
  (2 kinds), + noise; near-dup retries; length/shape variance.
- `run_spike.py` — frame-strip (whole/user/request/adaptive) → embed → cluster → V-measure/ARI.
- **Result:** off-the-shelf embed+cluster does NOT reliably recover kinds (over-fragments; fails the
  1-kind node; smears subtle kinds; frame-strip effect real but fragile). This is the empirical basis
  for demoting grouping to advisory.
- Deps installed in the global 3.12 env: `datasketch`, `fastembed`, `umap-learn` (+ `hdbscan`,
  `scikit-learn 1.9` already present).
