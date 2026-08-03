# Plan — Per-node Input Characterization (the shared substrate for all strategies)

_Draft to refine, NOT to implement yet. This is the keystone: characterize each node's input distribution
ONCE, store it in the graph profiler, and every strategy (downgrade, cache, compress, output) reads it.
Open questions are called out explicitly — several are unresolved and must be settled before we're confident._

## The idea

The question under all four strategies is the same: **what is the distribution of inputs at this node, and
does my optimization hold across it?** So we characterize each node once and share it.

- **cohort** = a *kind* of request to a node (a cluster of similar ones), e.g. `classify` → {billing, technical, security}.
- Instead of testing one request (misleading) or every request (infinite), we group into a handful of cohorts and reason/prove **per cohort**.
- **Confidence = coverage** (fraction of the node's real traffic-cohorts the proof actually touched) — the MSC idea from 2026 eval research. Never over-claim.

## Layers (and where the LLM is / isn't)

| step | LLM? | notes |
|---|---|---|
| graph structure, edges, per-node stats | no | deterministic |
| static/dynamic split | **byte-diff only for fixed-frame nodes** | fragile for complex shapes — see Risk 1 |
| **cohort discovery** (the kinds) | embeddings+clustering (proven) OR 1 cheap LLM call/node | shape-agnostic — the robust primitive |
| cohort assignment | deterministic / cheap | match to cohort definition |
| downgrade candidate detection ($) | no | model + tokens |
| **the proof** (stratified sample per cohort → judge) | **yes** | only for candidate nodes |

**Out-of-path is our advantage:** unlike in-path routers (RouteLLM), we are NOT latency-bound, so we can use
an LLM to *characterize* — better than a cheap offline classifier. The limit is **where** it runs: discover
cohorts **per node** (O(nodes)) and judge **per candidate** — NEVER an LLM call per request per node.

## This lives in the graph profiler

The profiler evolves: `structure + token stats` → `structure + per-node {static/dynamic, cohorts, sizes}`.
Computed once (bounded LLM), stored per (agent, version), read by all four strategies. This IS "graph as backbone."

## Downgrade flow (concrete)

1. per node: cohort the dynamic inputs.
2. proof: stratified sample across cohorts → re-run cheaper per cohort → judge vs recorded output.
3. verdict PER COHORT + coverage.
4. output: *"cheaper model safe for cohorts = X% of traffic; cohort Y needs the strong model"* — a coverage-bounded, cohort-level recommendation (matches RouteLLM: ~86% of traffic never needed the frontier model).

---

## OPEN QUESTIONS / RISKS — unresolved, settle before implementing

**Risk 1 — static/dynamic byte-diff is NOT general.** Reliable for fixed-frame nodes (Q&A, classifiers,
graders). Degrades for: **chat/recursive** (input is a *growing* conversation, not a fixed frame + slot),
**code assistants** (varied structure, tiny static frame), **supervisor/recursive** (nested/variable state),
and dynamic inserts (timestamp/session) even in simple agents. → Decision needed: make byte-diff a *cheap
shortcut for fixed-frame nodes only*, and use LLM/embedding-based structure for complex ones? How do we
detect which regime a node is in?

**Risk 2 — agent shapes we have NOT designed for.** Every example so far was the easy shape. Unhandled:
- **Chat / multi-turn:** cohort = the *current turn's intent* (on the latest user message), not the whole
  history? Static = system prompt; dynamic = history (unbounded). How do we cohort a growing context?
- **Supervisor / recursive:** a node whose input is other agents' state; recursion/loops.
- **Code assistants:** kinds = {refactor, debug, generate, explain} — semantic, large/varied payloads.
- This is a CENTRAL system → it will see all of these. Cohorting (embeddings/LLM, shape-agnostic) is the
  path, but each shape needs an explicit sanity check on real traces.

**Risk 3 — cohorting method choice.** Embeddings + sklearn (K-means/HDBSCAN) — proven, needs an embedding
model (cost/dep). vs LLM cohort discovery — semantic, out-of-path-friendly, less formally benchmarked. vs
hybrid (cheap embeddings cluster → LLM labels). Decide, and pick the number-of-clusters strategy (fixed k?
HDBSCAN auto? LLM-decided?).

**Risk 4 — rare cohorts.** Discovery from a *sample* can miss a rare kind → the coverage metric must reflect
that honestly (report lower coverage, never a false 100%). How do we estimate cohorts we didn't sample?

**Risk 5 — cost at scale.** LLM cohort-discovery = O(nodes). Fine for 8 nodes; what about a 100-node agent?
Do we cohort only candidate nodes, or all? Embeddings are cheaper than LLM here — maybe embeddings for all,
LLM only to label candidates.

**Risk 6 — is byte-diff even needed if cohorting is embedding/LLM-based?** Maybe static/dynamic becomes a
*consequence* of cohorting (what's constant across a cohort = static) rather than a separate byte-diff step.
Open.

## Grounded in (research)

- Coverage-based stratified sampling + MSC: [Coverage, Not Averages (arXiv 2604.20763)](https://arxiv.org/html/2604.20763v1)
- Evaluate with a small chosen subset: [tinyBenchmarks](https://openreview.net/pdf/25ac9459dd5b345badfd3d9a2c5e56b32fd89b07.pdf)
- Routing / difficulty / K-means query clusters, ~86% traffic cheap: [Dynamic Model Routing survey (arXiv 2603.04445)](https://arxiv.org/html/2603.04445v2), RouteLLM
- Old project: cohorts by `task_type` LABELS + distinct-count gate — label-dependent, insufficient.
