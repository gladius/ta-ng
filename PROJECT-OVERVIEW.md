# Token Auditor — Project Overview

*A briefing document for product / management. Written to be readable without opening the code.*

**Status:** working prototype, running end-to-end on real recorded traces.
**Repo:** `token_auditor` · **Branch:** `feat/cache-prefix-expansion` · **Language:** Python 3.12
**Size:** ~4,400 lines of Python across 44 modules + a small FastAPI web UI.

---

## 1. Executive summary

**Token Auditor finds money being wasted inside AI agents, and then *proves* the fix is safe before recommending it.**

Companies now run LLM agents in production — customer support bots, triage routers, document processors. Every
one of those agents makes many model calls per request, and most of them are over-provisioned: they use an
expensive frontier model where a cheaper one would give the same answer, or they structure their prompts in a way
that silently defeats the provider's caching discount. The bill is real, but nobody can safely act on it, because
the only question that matters — *"if I make this cheaper, does the agent still behave the same?"* — has no
trustworthy answer. Teams either overpay forever or downgrade blindly and break production.

Token Auditor answers that question with evidence. It reads an agent's **already-recorded** traces from an
observability platform (LangSmith, Galileo), finds the expensive call-sites, and for each candidate fix it
**re-runs the real recorded inputs through the cheaper configuration and compares the actual behaviour** against
what production really did. It outputs a per-call-site verdict — *safe / borderline / not safe* — with a dollar
figure attached only to the ones that proved safe, and a downloadable evidence pack showing every before/after
output so a human can check the work.

Two properties define the product:

| Property | What it means | Why it matters commercially |
|---|---|---|
| **Out-of-path** | It never sits in the live request path. It reads recorded history and runs its experiments offline. | Zero latency risk, zero blast radius. It cannot break a customer's production agent. It's an *advisory* tool, so it's easy to adopt. |
| **Proven, not estimated** | Every dollar claimed is backed by real model calls on real recorded inputs, judged for behavioural equivalence. | Competing "cost optimizer" tools produce estimates that nobody trusts enough to act on. A proof is actionable. |

---

## 2. The problem, concretely

An LLM agent is not one model call — it's a **graph** of call-sites. A support agent might look like:

```
   ticket ──▶ triage ──▶ billing_handler ─┐
                     └──▶ technical_handler ├──▶ responder ──▶ reply
                     └──▶ shipping_handler ─┘
```

Each box is a separate call-site with its own prompt, its own model, and its own cost profile. Waste hides at the
*call-site* level, not the agent level:

1. **Model-tier over-provisioning.** `triage` just picks one of four categories — that almost certainly doesn't
   need a frontier model. But it was written with whatever model the developer had open, and nobody has ever
   dared change it.
2. **Broken prompt caching.** Providers give a ~90% discount on the repeated, byte-identical *leading prefix* of a
   prompt. A single per-call value — a timestamp, a session id, a retrieved document — placed near the top of the
   prompt destroys that discount for the entire prompt. Teams pay full price on thousands of identical tokens
   without knowing it.

Both are fixable in minutes. Neither gets fixed, because the risk of a silent behavioural regression outweighs
the saving — until someone can *show* it's safe.

---

## 3. What the product does — the user journey

The web app is four screens:

```
  Data source  ──▶  Workspace  ──▶  Agent  ──▶  Savings report
  (LangSmith /      (their org)     (the app     (the deliverable)
   Galileo /                         to audit)
   recorded)
```

The **savings report** is the product. It has two distinct halves, and the split is deliberate:

**Half 1 — Detection (free, instant, deterministic, no AI involved).**
Every call-site in the agent is listed, ranked costliest-first, with its current model, its cost, and which
levers apply. This is pure arithmetic over recorded token counts and a price catalog — same input always gives
the same output, and it costs nothing to compute. On the shipped demo agent this produces a headline of
**$190 per 10,000 calls of detected opportunity across 6 call-sites**.

**Half 2 — Proof (paid, live, on-demand).**
The user selects call-sites (default: the 5 costliest) and clicks **Prove selected**. The system then makes real
model calls in the background, streaming progress to the browser, and returns a verdict per call-site. **The
headline number is then recomputed to include only what actually proved safe** — an unproven or drifting
candidate contributes $0. The report can be downloaded as a ZIP evidence pack.

> **This split is the core product insight.** Detection is free so the user always sees value immediately;
> proof is the expensive, differentiating step and stays opt-in and explicit.

### Dollar figures are quoted honestly

Every `$` in the product is stated as **"per 10,000 calls"**, never as "per month". A sample of traces cannot
reveal a customer's real production volume, so inventing a monthly number would be a fabrication. The per-call
saving is exact; the viewer multiplies by their own real volume. This is a small detail that carries a lot of
credibility in an enterprise sale.

---

## 4. The two optimization levers

### Lever 1 — Model-tier downgrade *(primary, most mature)*

> *"This call-site runs on Sonnet. Haiku is 67% cheaper. Does the agent still behave identically?"*

**Detection ($0):** For each call-site, look up its model in a catalog of **34 models across Anthropic, OpenAI and
Google**, find the most capable model one capability tier down *from the same provider*, and price the difference
against the recorded token volumes.

A subtlety worth mentioning to a technical audience: the ladder requires the target be cheaper on **both** input
and output pricing. Model pricing isn't monotonic with capability — a newer mid-tier model can cost *more* per
input token than an older top-tier one — so a naive "one tier down" rule produces recommendations that don't
actually save anything. The catalog also flags retired-but-still-priced models so we never recommend downgrading
to something you can't call.

**Proof (paid):** covered in §5 — this is the heart of the system.

### Lever 2 — Cacheable-prefix reorganization *(newer, opt-in via a checkbox)*

> *"A timestamp near the top of your prompt is costing you the cache discount on 1,600 tokens, every call."*

This lever has an unusual division of labour between deterministic code and AI, which is worth putting on a slide:

| Step | Who does it | What happens |
|---|---|---|
| 1. **Ground** | Deterministic byte-compare | Compare every line of the prompt across all recorded samples. Lines identical in all of them are `static`; lines that vary are `dynamic`. This is *observed ground truth*, not a guess. |
| 2. **Reorg** | An LLM (Sonnet) | Given the annotated lines and the caching rules, it decides which static lines can be safely hoisted into a fixed leading prefix, rewording only for coherence. It is explicitly forbidden from touching dynamic lines. |
| 3. **Apply** | Deterministic code | The AI's prefix becomes the system turn; **every line it did not hoist is moved to the user turn verbatim.** The AI never regenerates per-call content, so nothing can be silently dropped or altered. |
| 4. **Prove** | Both, doubly | **(a) Behaviour:** re-run the agent on the reorganized prompt and judge it against recorded output. **(b) Caching:** send the new prefix to the provider twice and read back the provider's own `cache_read_input_tokens` counter. |

The **dual proof** is the safety gate: the saving is only claimed if behaviour is SAFE **and** the provider's own
counters confirm the prefix actually caches. A semantic break vetoes it; a cache miss vetoes it. There's also a
free pre-flight check — if even the best possible static content is below the provider's minimum cacheable size,
the system abstains *before* spending anything on the AI reorg step.

---

## 5. How the proof works — the differentiator

This is the part that took the most engineering, and it's what a competitor cannot trivially copy. It's worth a
slide of its own.

### The naive approach, and why it fails

The obvious method — "run the cheap model once, ask an AI judge if the answer matches" — does not work, and its
failure mode is dangerous. LLMs are stochastic: the same model given the same input twice will often produce
differently-worded, occasionally differently-*decided*, answers. So a single run tells you nothing. You can't
distinguish "the cheaper model broke it" from "this call-site is just naturally noisy," and your verdicts flip
between audits. An earlier iteration of this project failed for exactly this reason, and the current design is a
direct response to it.

### The actual method: three axes plus a noise floor

**Axis 1 — Diversity (N).** Test **5 genuinely different recorded inputs** per call-site, selected
deterministically (near-duplicates are filtered out by word-shingle overlap). Deterministic, not random — random
sampling would reintroduce exactly the instability we're trying to eliminate. Fewer than 3 distinct inputs
available → the system **abstains** (`LOW-EVIDENCE`) rather than judge from thin data.

**Axis 2 — Stability (K).** Re-run each input **3 times** on the cheaper model. We report a preservation *rate*
(e.g. "2 of 3"), not a coin flip.

**Axis 3 — The self-variance baseline (the clever part).**
If the cheaper model is perfect (3/3), it's SAFE — it cannot do better, so how noisy the original is doesn't
matter. But if it drifted even once, we don't yet know whether the downgrade caused it. So the system then
**measures the original model's own run-to-run consistency on that same input** and judges the cheaper model
*relative to that noise floor*:

| Observation | Verdict | Reasoning |
|---|---|---|
| Cheaper is perfect (3/3) | **SAFE** | Can't do better. |
| Cheaper is as steady as the original | **SAFE** | The drift was the call-site's own noise, not the downgrade. |
| Cheaper drifts one more time than the original | **BORDERLINE** | Just under the noise floor — send to a human. |
| Cheaper drifts two or more times worse | **NOT-SAFE** | Clearly worse than the model's own variance. |
| The **original** can't reproduce itself | **BORDERLINE** | The reference is unreliable, so no confident verdict is possible. |

Note the last row: the system is capable of reporting *"we cannot verify this, and here's why."* That honesty is
a feature, not a gap.

The baseline runs **only on inputs where the cheaper model wasn't perfect**, so clean call-sites cost nothing
extra. The recorded production output counts as the original's free first sample.

**The judge.** One general-purpose LLM judge handles every payload type — prose, JSON, or tool calls are all
rendered into a single behaviour string, so there's no fragile branching per output shape. It's prompted to be
**drift-biased**: it asks only whether the candidate kept the reference's *decision and every material fact*
(style, wording and length are explicitly excluded), and any uncertainty resolves to DRIFT. If the judge's reply
can't be parsed, that's DRIFT too. There's a separate labelled test corpus (`tests/judge_corpus.py`) spanning
billing, classification, tool calls, medical, code and safety-refusal cases, used to measure the judge's
false-PRESERVED and false-DRIFT rates.

### The one rule everything is built around

> **Never a false SAFE.**
> Every tie, every ambiguity, every unparseable result, every unverifiable case breaks toward *not* recommending
> the change. A call-site verdict is SAFE only if **every** tested input was SAFE.

That asymmetry is the whole trust proposition. A missed saving costs the customer a little money. A false SAFE
breaks their production agent and destroys the product's credibility permanently.

---

## 6. Architecture

Layered so that each layer only knows about the one below it. Two boundaries do the heavy lifting.

```
┌──────────────────────────────────────────────────────────────────┐
│  WEB / CLI          FastAPI + Jinja templates · CLI              │
│                     4 pages · SSE progress stream · ZIP export   │
├──────────────────────────────────────────────────────────────────┤
│  SERVICES           nav · graph · funnel                         │
│    (the product)    downgrade (detect)  ·  cache (detect)   $0   │
│                     audit (prove)  ·  cache_reorg (prove)   paid │
│                     prove (orchestrate) · store · report_doc     │
├──────────────────────────────────────────────────────────────────┤
│  llm_client         ◀── PROVIDER BOUNDARY. One file. Everything  │
│                         above it is provider-neutral.            │
├──────────────────────────────────────────────────────────────────┤
│  CONNECTORS         langsmith · galileo · recorded (fixtures)    │
│                     graph builder · datasource registry          │
├──────────────────────────────────────────────────────────────────┤
│  CONTRACT           the canonical trace shape — one definition   │
│  CATALOG            models.json — 34 models, 3 providers, prices │
└──────────────────────────────────────────────────────────────────┘
```

**Boundary 1 — the connector interface.** Adding an observability platform means dropping in one folder; the
registry auto-discovers it. Nothing above the connector layer knows LangSmith exists. Three connectors ship
today: **LangSmith** (full, including run-tree topology), **Galileo**, and **recorded** (frozen local JSON
fixtures, so demos are byte-stable and the tool works fully offline).

**Boundary 2 — the provider boundary (`llm_client`).** All the audit *logic* is provider-neutral; only the
request-building and response-parsing is provider-shaped. Callers pass neutral *intent* ("bind these tools",
"use reasoning") and receive a neutral result. Swapping to a multi-provider gateway (LiteLLM) so the tool can
audit OpenAI and Gemini agents is a **single-file change**, and the spec for it is already written up in
`LITELLM-HANDOFF.md`.

### Understanding the agent's shape

Getting call-site identity right is subtler than it looks, and two real problems drove the design:

- A **ReAct-style node** emits more than one model call under the same label (the initial reasoning call, and the
  call after a tool returns). These are different prompts and different optimization targets, so they must be
  treated as different call-sites — the system splits them by prompt *shape*.
- An **untagged call** has no node label at all; grouping by model class name would collapse an entire agent into
  one meaningless bucket. Instead the node is resolved by walking up the recorded **run tree** to the nearest
  named ancestor, and flagged if we had to fall back.

The graph builder also derives each call-site's **subgraph path**, so multi-agent systems can be filtered and
rolled up per subgraph in the UI. Call-site identity is deliberately built from *stable metadata* (agent /
subgraph / node), never from prompt content — a content-derived key drifts as the sampled traces change, which
would silently invalidate the user's selection between viewing a report and proving it.

---

## 7. Economics of running the tool

Costs are bounded by design and disclosed in the UI.

| Operation | Cost | Notes |
|---|---|---|
| Navigation + detection + ranking | **$0** | Pure arithmetic. Recomputed on every page view. |
| Downgrade proof, per call-site | ~5 inputs × 3 repeats × 2 calls (replay + judge) | Plus up to 2 extra pairs *per doubtful input only* for the noise-floor baseline. |
| Cache round-trip proof | Exactly 2 calls, `max_tokens=1` | Cents. |
| Cache reorg planning | 1 call | Skipped entirely if the free pre-flight check shows it can't possibly cache. |

Proof runs are parallelized (bounded pools at both the call-site and per-input level), so wall-clock time is the
slowest single call-site rather than the sum of everything. Results are frozen in a per-agent store so the report
shows identical numbers on every reload — a paid audit never silently re-runs on a page refresh. One call-site
failing doesn't kill the run.

---

## 8. Current status — what's real and what isn't

**Working end-to-end, today:**

- Full navigation across three connectors; LangSmith is wired to the live REST API.
- Deterministic graph/call-site extraction, cost ranking, and both detectors.
- The complete downgrade proof pipeline with the self-variance baseline.
- The complete cache-reorg pipeline with dual proof.
- Web report with live streaming progress, subgraph filtering, ZIP evidence export, print-to-PDF.
- CLI equivalents of every flow.
- **All three deterministic test suites pass** (`test_downgrade_spine`, `test_downgrade_proof`,
  `test_cache_reorg`) — these run at $0 with no network, stubbing the paid calls so the verdict *logic* is
  tested independently of model behaviour.
- A synthetic agent generator (`sample_agents/support_agent.py`) that makes real model calls across four domains
  with deliberately varied inputs — so the audit is tested against authentic outputs, not hand-written examples.
  The shipped demo fixture is 144 real traces across 6 call-sites.

**Honest limitations to be aware of before promising anything:**

- **Provider coverage.** The live call layer is Anthropic-only today. Detection and pricing already cover OpenAI
  and Google; *proving* an OpenAI or Gemini agent needs the LiteLLM swap (spec written, not yet implemented).
- **Real-data validation.** Everything has been validated on the synthetic-but-real-API demo agent and on
  parsing fixtures. Full validation of subgraph handling needs a genuine multi-agent production export.
- **Token counting** uses a ~4-chars-per-token approximation for cache-size estimates, not a real tokenizer.
- **Mixed/routed call-sites are skipped** on purpose — a node that already routes across tiers is a *routing*
  audit, which is a different (deferred) piece of work.
- The result store is in-memory with a TTL — fine for a demo, needs persistence for a real deployment.
- One known gap is documented in the code: cache detection keys by display label, so two call-sites sharing a
  label could collide there (the downgrade path already uses unique keys).

---

## 9. Roadmap

**Immediate, well-specified:**
1. **LiteLLM swap** — one file, unlocks auditing OpenAI and Gemini agents. Acceptance tests already written.
2. **Persistent storage** for proof results.
3. **Validation on a real multi-agent production export.**

**Designed but explicitly deferred** (the discipline of not building these yet is itself worth noting — the
project has a written anti-scope-creep anchor in `DOWNGRADE-PLAN.md`):

- **Per-node input characterization / cohorts** (`INPUT-CHARACTERIZATION-PLAN.md`). The idea: instead of proving
  a fix against a handful of inputs, cluster each call-site's real traffic into *cohorts* (e.g. billing vs.
  technical vs. security requests) and produce a **coverage-bounded** verdict: *"the cheaper model is safe for
  cohorts covering 78% of your traffic; this cohort still needs the strong model."* This is the shared substrate
  every future strategy would read, and it's grounded in published evaluation research. Six specific open risks
  are documented and unresolved — deliberately not built until they're settled.
- **Routing audits** for already-tiered nodes.
- **Prompt compression** and **output-length** levers.
- **Hierarchy / per-subgraph rollup UI** (the underlying data already exists).

---

## 10. Suggested presentation structure

If the deck needs a spine, this ordering works:

1. **The hook** — "Your AI agent is over-provisioned. You already know that. You can't act on it because nobody
   can prove the cheap version still works."
2. **The problem** — agents are graphs of call-sites; waste hides per call-site; two specific waste patterns
   (tier over-provisioning, broken cache prefix).
3. **The product in one line** — reads recorded traces, proves fixes, never touches production.
4. **Demo / screenshot** — the report screen: free ranked detection, then one click to prove.
5. **The differentiator (spend the most time here)** — why single-run comparison fails, and the three-axis +
   noise-floor method. The "never a false SAFE" rule.
6. **Trust artifacts** — abstention verdicts, the downloadable evidence pack, provider counters as ground truth.
7. **Architecture in one diagram** — two swappable boundaries: connectors and provider.
8. **Status & roadmap** — what's proven today, the honest limitations, what's next.

**Three messages to make sure survive into the deck:**

- **"We prove, we don't estimate."** Every dollar is backed by real re-runs on real recorded inputs.
- **"Out-of-path means zero risk to adopt."** It reads history and runs experiments offline. It cannot break
  anything.
- **"It abstains."** The system reports *"we can't verify this"* rather than guessing — and that's precisely why
  its SAFE verdicts are worth acting on.

---

## Appendix — glossary

| Term | Meaning |
|---|---|
| **Call-site** | One specific model-calling step inside an agent (e.g. `triage`). The unit everything is measured and proved at. |
| **Trace** | One recorded model call: its prompt, tools, output, and token counts. |
| **Lever** | An optimization type. Two exist: model-tier downgrade, cacheable-prefix reorg. |
| **Detection** | The free, deterministic step that finds and prices candidates. |
| **Proof** | The paid step that re-runs real inputs and judges whether behaviour held. |
| **Drift** | The candidate changed the decision or a material fact. The thing we refuse to allow. |
| **Self-variance / noise floor** | How inconsistently the *original* model answers the same input — the bar the cheaper model is measured against. |
| **SAFE / BORDERLINE / NOT-SAFE / LOW-EVIDENCE** | Recommend · flips, don't · don't · not enough data to judge. |
| **Out-of-path** | Never in the live request flow; reads recorded history only. |
| **Prompt cache prefix** | The leading, byte-identical section of a prompt that providers will serve at ~90% discount. |

### Files worth opening, if the PM wants a source

| Question | File |
|---|---|
| What is the proof method? | `app/services/audit.py` (module docstring is the spec) |
| How does the cache lever work? | `app/services/cache.py`, `app/services/cache_reorg.py` |
| What are the tunable rigor knobs? | `app/config.py` |
| What's in scope / deliberately out? | `DOWNGRADE-PLAN.md` |
| What's the next big idea? | `INPUT-CHARACTERIZATION-PLAN.md` |
| How do we go multi-provider? | `LITELLM-HANDOFF.md` |
