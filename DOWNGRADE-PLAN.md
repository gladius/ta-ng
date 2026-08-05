# Provable Model-Tier Downgrade — the plan (anti-drift anchor)

Branch: `feat/provable-downgrade`. One lever, done to a bulletproof standard, on real traces.
If a change isn't on this page, it's out of scope. Re-read this before touching code.

## Why this and only this
The project failed on **unreliable verification demoed on rigged data**. The fix is *narrow + honest*:
prove ONE thing well — that a cheaper model preserves a call-site's behavior — and **abstain** on everything
we can't back. Trust comes from **never a false PRESERVED**, not from a perfect judge.

## The pipeline (the whole product)
1. **Ingest real LangSmith traces** (connector already parses the real `RunBase` shape).
   - Identify LLM calls by `run_type == "llm"` (authoritative).
   - Call-site identity = **framework-provided** `langgraph_node → node → run.name`. NO content hash, NO byte-compare.
   - **NEW: `graph_path`** — derived from the run tree (`parent_run_id`/`dotted_order`, ancestor node names;
     fallback `metadata.langgraph_checkpoint_ns`). Carried on each trace so we can **select/roll-up by graph/subgraph**.
     Fully validated only on a real subgraph export; single-level agents get a trivial path.
2. **Rank call-sites by cost** — deterministic `tokens x calls x price`. No LLM.
3. **Sample up to 5 DISTINCT same-shape inputs** per candidate (shingle-dedup, deterministic — never random,
   because random reintroduces the instability that killed the demo). Split by output type only if a node mixes
   tool-call and prose; abstain on hopelessly mixed shapes.
4. **Temp=0 head-to-head** — re-run BOTH the original and the cheaper model at temperature 0 on the SAME input.
   Compare those two (NOT the recorded output — that carries sampling noise). Isolates the model.
5. **Judge preservation:**
   - **Primary (all nodes):** Sonnet, **decomposed + drift-biased** — "did B keep A's decision + every material
     fact, with no contradiction? style/wording don't count; unsure → DRIFT." Free-text is the workhorse and must
     be reliable on its own (enterprise outputs are NOT always simple).
   - **Bonus (structured nodes):** deterministic — same tool name+args, or normalized-equal label. Free reliability
     where it applies; never the thing we lean on. Mixed output → both must pass.
6. **Aggregate + abstain:**
   - `< 3 distinct inputs` -> **LOW-EVIDENCE** (don't recommend)
   - all sampled PRESERVED -> **PRESERVED** -> recommend downgrade + $/month
   - any DRIFT -> **DRIFT** -> don't recommend
   Every tie breaks toward NOT recommending.

## Catalog (verified working with current models.json)
Tier-driven ladder: sonnet-5 -> haiku-4-5 (~67%), opus-4-8 -> sonnet-5 (~40%), gpt-5.6-sol -> terra (~60%);
haiku-4-5 -> none (already small); unknown models -> no downgrade. Canonicalization handles dated ids + aliases.

## Deferred — DO NOT BUILD without a new decision
- LLM prompt **reorg** (cache-region expansion) — the harder 2nd lever; needs construct + coverage-gate + proof.
- Caching **enable** — dead (litellm auto-injects the breakpoint).
- **thread_id / session grouping** — only serves conversation-caching, which we're not building.
- **Hierarchy UI / per-subgraph rollup view** — graph_path enables it; the *view* is later.

## Standing constraints
Never a false PRESERVED. Deterministic where possible; LLM only where it must be. No namesake additions.
Real validation needs a real export — `sample_runs.json` is a parsing fixture only, not real data.
