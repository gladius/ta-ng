# Agent / Deployment Profiling — Plan (LEAN)

v4 (2026-08-18). Lean core; researched methods **cut** (§7); levers validated against the code (§7).
See **Build status** below for what actually exists vs. the design target (§1–§11 describe the *target*).

---

## Build status — HONEST (2026-08-18) — read this first

**Built + tested** ($0 unless noted):
- **Fact-pack** (`profile/facts.py`) — LLM 6-fact pack + non-LLM structural pack; frame-stripped distinct-output;
  flow-context; a deterministic **mixed-shape** fact (catches structural mixes over ALL N, incl. rare minorities).
  Shown on the profile page as compact per-node context. **41/41 tests.**
- **One-shot LLM read** (`profile/comprehend.py`) — a **SINGLE** call per node → op + summary + coherence.
  **NOTE: this is the one-step read, NOT the tool-driven agent** (§2/§5).
- **Guards** (`profile/guards.py`) — deterministic reconciliation: citation existence-check · veto · mixed-shape
  flag · thin-evidence.
- **Opportunity map** — the funnel's $0 lever candidacy ($ per node: downgrade / cache / compress) merged into the
  profile (`assemble.py`); **UI half-wired**.
- **Validation** (`spikes/validation/`) — controlled (**6/7**; the miss diagnosed to a **sampler blind spot**,
  addressed by the deterministic mixed-shape fact) + stress (~100 calls/node · rare 5% modes · large content — handled).

**NOT built — the gap:**
- **The tool-driven AGENT** (§2/§5's core) — the analyst that *uses tools* (sample/group/diff/flow) to go the extra
  mile on hard/uncertain nodes. `comprehend` is a one-shot; **this is not it.**
- **`profile/tools.py`** (Phase 3 toolset). `walk(trace)` deferred to the error-loop.

**Clarifications (things that drifted in conversation):**
- **"semantic-mixed"** = a **validation test case** (one node doing two prose jobs, same shape), **NOT** a plan
  concept. The residual it exposed — a *rare same-shape semantic* mix — needs output-clustering over all N (cut).
- **Understanding, not rating** (§8): dropped per-node HIGH/MEDIUM/LOW scores; output is understanding + honest
  caveats + the agent's trail.

**THE OPEN FORK:** (a) **build the tool-driven agent** — the differentiator that captures structure/type/meaning
where deterministic + one-shot can't; or (b) **ship what exists** (facts + one-shot read + opportunity map) as
good-enough and move on.

---

## 1. What it is / isn't
A **profiler**: understand each node + the whole deployment from its traces — what it does, how it varies, its
measured facts, its place in the flow — as **honest understanding** (not a rating; §8). It is **not** a lever prover:
downgrade / cache / compress are **consumers** that read the profile's candidate flags and run their **own** proofs.

## 2. The whole profiler (hold this in your head)
An **analyst agent** works each node. It's handed the **~6 provable facts** + a **small toolset**, and it
**decides how deep to look** — reasoning over the facts, reading samples, and (as the node needs) grouping /
diffing / tracing the flow — until it can characterize the node with grounded, honest confidence.
- **Easy LLM node** (a clear router) → it converges in essentially **one read**.
- **Hard LLM node** (mixed / free-form / deep-agent) → it uses more tools, or **abstains honestly**.
- **Non-LLM node** (tool / retriever) → characterized **deterministically** (its *name* is its op) — **no analyst,
  no lever**; it's for whole-deployment understanding + flow.
Then it emits the per-node profile + deployment summary + lever-candidacy flags — every claim grounded;
the output is **understanding, not a rating** (§8).

**The agent is the core.** The facts are its ground truth; the tools are how it inspects. "One read is often
enough" is an *emergent property on easy nodes* — **not** a demotion of the agent. Everything below is detail.

## 3. The fact-pack — TWO variants (LLM vs non-LLM), each $0 · deterministic · provable

The pack a node gets depends on its type — the LLM facts read the contract trace (system/output/messages), which
a tool/retriever run simply doesn't have.

**LLM nodes — the 6-fact pack** (reads the contract trace; feeds the analyst AND the levers):
| Fact | Answers |
|---|---|
| `distinct_system` | is the instruction frame fixed? (also fixes the `compress` gap) |
| output type (label / json / prose) | decision or free-form? |
| distinct-output count (**frame-stripped**) | decision cardinality — strip the per-call variable FIRST, else a templated node (`…order {id}`) reads as free-form and we mis-flag its cache/compress candidacy |
| tool-call rate + tool set | tool-caller? which tools? |
| role-shape / multi-turn / `thread_id` | conversational? (also the cache trigger) |
| RAG-present | grounded answerer? |
| flow-context (upstream → this → downstream) | where it sits — a node fed by a router is a *worker*; **$0 from edges** |

**Non-LLM nodes (tool / retriever) — the structural pack** (no prompt/output to read → **deterministic only, NO
analyst, NOT consumed by any lever**; for whole-deployment understanding + flow):
| Fact | Answers |
|---|---|
| type (tool / retriever) | what kind of op |
| **name** | **IS the op** — self-describing, no LLM needed |
| calls · error-rate · latency | volume · reliability · time-cost |

Source: `graph.py` + `select.bucket_facts`. (**Dropped:** non-LLM distinct-arg/result — not useful for a tool
called 1000s of times, and it would need the adapter to capture tool i/o.)

## Modularity — where it lives (nothing scattered)

Everything profiler-specific lives in **`app/services/profile/`** and *consumes* the shared graph; it never
duplicates graph logic or leaks into the levers:

```
app/services/profile/
  select.py      — sampler + bucket_facts (distinct_system, digests)          [exists]
  facts.py       — assembles the per-node FACT-PACK (both variants);          [NEW · Phase 1]
                   computes the profile-only facts (distinct-output/arg/result)
  comprehend.py  — the analyst (LLM read over the fact-pack + samples)         [exists → extend]
  assemble.py    — the report shape (sections, header, candidacy flags)        [exists]
  tools.py       — the analyst's inspection tools (sample/group/diff/flow)     [NEW · Phase 3]
```
- **Consumes (does not own):** `connectors/graph.py` + `app/services/graph.py` (shared graph facts), `auditor.util` (pricing).
- **Exposes:** the profile (per-node fact-pack + characterization + candidacy flags). Levers read the *exposed*
  profile (e.g. `distinct_system` for the compress fix) — they never reach into profiler internals.
- **Rule:** a new profiler fact / tool / step = a change **inside `app/services/profile/`**, not scattered across
  the graph engine or the levers.

## 4. The one read — understand from the facts + fill what the facts CAN'T find
`comprehend.py`: given the fact-pack + 3–5 samples, the agent (1) absorbs what the facts determine (fixed-frame?
tool-set? decision-cardinality?), (2) **fills the semantic gap determinism can't reach** — what the varying
outputs *mean*, *why* a frame varies — from the samples, (3) **names any gap it still can't close**. Every claim
is **reconciled against the facts** (a fact-contradicting claim loses) and **cited** (trace_id + quote, existence-
checked). Output is **understanding, not a rating** (§8). Demonstrated working (router / tool_caller / summaries).

## 5. The analyst's toolset (this IS the agent — it drives)
The agent decides what to look at — little on easy nodes, more on hard ones. All tools are **read-only, bounded**:
- `sample(strategy, n)` — read examples (incl. fetch-more for a rare node)
- `group(key)` — partition by a $0 key (output-label / tool / system)
- `diff(a, b)` — compare two calls
- `flow(node)` — aggregate neighbours: what generally feeds this node / what it feeds
- `walk(trace)` — **DEFERRED.** The actual sequence of ONE request. For *profiling*, the aggregate `flow` is enough
  context; the full walk's real value is the **future error-loop** (walk a failed trace) + one illustrative
  trajectory — so it is **not** in the profiling toolset now. (I over-sold it earlier.)

**Budget:** ≤ ~5 tool-calls per node; **stop as soon as confident** (the primary control) → easy node ≈ one read,
nothing runs away. **The agent's trail** (e.g. "one read — clear" vs "grouped by output · walked t-42") **is shown
in the UI**, so you see *how* each characterization was reached.

## 6. Sampling — the AGENT picks inputs; profiling covers ALL nodes
- **Profiling analyzes every node** — deterministic facts for all ($0), and the analyst reads each one. (Node
  *selection* — "which nodes to AUDIT" — is the separate **paid audit** flow, where the user picks call-sites for
  the expensive proof. Don't conflate: **profiling = all nodes · audit = user-selected nodes.**)
- **Per node, the AGENT picks the inputs it needs**, guided by the facts: ~5 samples by default, NEVER the
  100s/1000s (the facts already cover all N for $0). A hard node fetches more (bounded ≤ ~15); **stop-when-confident**.
- "Did I see every behaviour" is a *lever/coverage* concern, not the profiler's — the profiler *describes*.

## 7. CUT — and why (the subtraction that restores confidence)
**Removed from the profiler core:**
- **Vendi / diversity scalars** → a refinement (templated↔generative); a *lever* signal if ever needed, not core.
- **RAGAS faithfulness/relevancy** → quality eval; a *lever/eval* concern, not profiling.
- **facility-location / k-center / rule-of-three / stratified coverage** → all *coverage-for-a-proof* = **lever-owned**.
- **fixed node-kind method-routing** → replaced by the **analyst agent** (it decides how to inspect, not a hard-coded branch).

**Kept:** the analyst **agent** + the 6 facts (its ground truth) + a small toolset (how it inspects). Nothing else.

## 8. Understanding, not rating (the reframe)
The goal is **maximal understanding** of each node, cheaply and honestly — **not a per-node score**.
- Output per node is a **rich characterization** (op · what it does · how it varies · structure · flow ·
  lever-candidacy). Where the agent **can't** determine something, it says so **as an observation** — *"the system
  prompt varies every call (8 distinct) → one operation with a dynamic frame"* — which is itself understanding.
- **No HIGH/MEDIUM/LOW headline score.** Solidity shows through the richness + the honest caveats (and the agent's
  trail, §5).
- **The grounding guards stay** — they make the understanding TRUE, not a rating: a claim a fact contradicts is
  **vetoed by the fact**; every claim is **cited + existence-checked**; a one-sample read is **never a fact**;
  analysis is **pinned** (reproducible).
- If a lever wants a "solid enough to act" gate, it **derives** one — it is not the profiler's headline.

## Scale & guardrails (enterprise — a central system over many agents)
- **Token cap:** each sample truncated to **~5k tokens in / ~5k out** (head+tail, `[…omitted…]`) — a 50k-token
  node never floods the analyst. Configurable.
- **Samples:** ~5 per node (never all N); ≤ ~15 on a hard node via fetch-more.
- **Tool-calls:** ≤ ~5 per node; **stop-when-confident**; a **per-run ceiling** on total calls/$.
- **Profiling reads all nodes** (cost is bounded by the per-node caps, not by dropping nodes). *Node selection is
  the separate paid **audit** flow* — do not conflate.
- **Read-only over the pinned snapshot** — worst case is wasted $, never damage.
Per-agent cost is bounded + predictable: `selected-nodes × 1 read × ~5 samples × ≤5k tokens` + a few tool-calls on
hard nodes. The knobs (token-cap · sample-count · per-run ceiling) cap total spend across many agents.

## Trust model & caveats — can we ACT on the agent's output?
Trust is **layered** — you never rely on the agent being perfect:
1. **Provable facts (the floor)** — `distinct_system`, mixed-shape, distinct-output, flow, cost, the **$ opportunity
   map**. These are FACTS — act on them directly. This is the **optimization backbone**.
2. **Grounded agent (adds meaning)** — every claim reconciled against the facts (a fact can **veto** it), cited +
   existence-checked, honest-when-unsure. A **good analyst, not an oracle** — it can be wrong on hard cases, but it
   **flags doubt** rather than faking.
3. **Downstream proof (the net)** — optimization is **proven** by the lever (re-run + verify) before acting; a
   future error-fix is **tested before surfacing**. So a wrong agent read is **caught, never shipped**.

**Caveats (honest):**
- The agent's *semantic* output is **interpretation, not proof** — use as grounded advisory, not fact.
- Whether the tool-driven agent **materially beats the one-shot read on the hard tail** is *validated by building
  it + testing the same-shape semantic case* — **not assumed**.
- A **rare same-shape semantic mix** still needs output-clustering over all N (cut) — even the agent can miss it if
  sampling never hits the mode.

**Net:** usable for **optimization now** AND the **eventual error-loop** — because trust is *architectural (layers
+ proof)*, not a confidence score.

## 9. Levers consume it (validated against code, 2026-08-18)
- **downgrade** already reads node facts (clean).
- **cache** computes its own contiguous-byte prefix (genuinely cache-specific — keep).
- **compress** takes `bucket[0].system` and **doesn't check `distinct_system`** → a latent gap the profiler's
  fact **fixes**.
- **output-opt** not built (the profiler would *enable* it).
The profiler adds `distinct_system` + candidate flags + sizing; each lever keeps its own projection + runs its own proof.

## 10. Limits
Recorded window only · free-form kinds not enumerable (describe, don't fake) · mixed/untagged flagged not solved ·
per-node, not end-to-end/trajectory · the LLM can mis-describe → grounded + evidence-gated + abstains.

## 11. Build
- **Phase 1 — the fact-pack in `profile/facts.py` — ✅ DONE** ($0, no LLM): LLM 6-fact pack + non-LLM structural
  pack, consuming the shared graph; frame-stripped distinct-output; flow-context; wired to the profile page as
  compact per-node context; **41/41 tests**. (Dropped: tool arg/result-variability.)
- **Phase 2 — the analyst agent over the fact-pack** (`comprehend` → understand from facts + fill the gaps §4;
  the grounding guards; **understanding-first** output §8; the token/sample/tool-call caps §Scale; the agent trail
  in the UI §5).
- **Phase 3 — remaining tool refinements** + per-node sampling floors. (`walk(trace)` + trajectory deferred to
  the error-loop — not needed for profiling.)

## Later (not now)
Error analysis / self-improving loop — the same machinery, with error signals as more facts. Not a fork.
