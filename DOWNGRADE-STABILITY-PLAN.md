# Model-Tier Downgrade — Stability Redesign Plan

**Status:** DRAFT — for refinement, not yet implemented
**Branch:** `feat/downgrade-stability`
**Scope:** model-tier downgrade ONLY. Cache-prefix expansion and input-compression are out of scope / disabled for this work.

---

## 1. The problem we are solving

On **re-audit in production, downgrade verdicts are not stable** — the same call-site flips SAFE ↔ NOT-SAFE between runs. For a central governance layer auditing many agents of every shape, a verdict that changes on re-run is not trustworthy. We need verdicts that are **stable across re-audits** and **fail toward NOT-SAFE** (never a false SAFE), without a golden dataset and without being able to pin model temperature.

## 2. Why it flips today (root causes)

A downgrade verdict is a **noisy statistical estimate sitting on a hard threshold**; each re-audit re-draws the noise. Sources, in order of impact:

1. **LLM non-determinism you cannot pin.** The newest models reject `temperature=0`, so every re-run of the cheaper model *and* every judge call is a fresh non-deterministic sample. Same input → different output → different verdict.
2. **Tiny sample + knife-edge rule.** `K=3` re-runs → the pass-count `ck ∈ {0,1,2,3}` is coarse; the per-input rule `ck ≥ self_kept` compares two small noisy integers; the node is SAFE only if **all** inputs pass, so **one** input flipping flips the whole node.
3. **The binary judge flips on ambiguous pairs.** "Is this materially preserved?" is a vibe call; on genuinely-borderline changes it is inconsistent run-to-run.
4. **The trace set changes.** A fresh audit pulls a different window of recorded traces → a different sample → effectively a *different question* (independent of LLM noise).

## 3. The design principle

There is **no golden dataset** (unlimited agent variety) and we **cannot pin temperature**. So we do what dynamic invariant detection + metamorphic/differential testing do: **the OLD model's own behaviour is the reference.**

For each input, learn what the old model **holds constant** (its *commitments*) versus what it **naturally varies** (*allowed variation*), then require the new (cheaper) model to **preserve the commitments while allowing it exactly the same variation** — "if the old model errs this way, so may the new."

Why this works:
- **Per-input** — a router's decision is input-specific, so a node-level contract would be wrong.
- **Self-calibrating & type-agnostic** — the commitment for a classifier is the label; for a summary it's the key facts; discovered automatically per node.
- **No golden set** — the reference is the old model's own runs.
- **Stable** — it turns the judge's vibe call into a concrete checklist, which is the thing that actually removes the flips.

**The crux is the ALLOWED-VARIATION side, not the commitments.** Catching a broken commitment (a mislabel) is the *easy* half. The *hard* half — and where most false NOT-SAFEs and most instability come from — is correctly recognising that a reworded / reordered / differently-detailed output is **still fine because the original itself varies that way**. So the profile's real job is to characterise, per aspect, **what the original varies and by how much**, and the cheaper's **tolerance on each aspect is the original's own variance on that aspect** — *proportional, not a flat number*:
- Aspect the original **never** varies (a true commitment, 0 variance) → cheaper must match it **exactly** (no cushion).
- Aspect the original **does** vary (wording, order, an optional detail, a value, even an occasional slip) → cheaper is allowed to vary it **up to the same degree** the original does (cushion = the original's measured variance there).

This is the self-variance principle applied *per aspect*: "if the old model does it (or wobbles on it), the new model may too; if the old model is rock-solid on it, so must the new be."

Grounded in: dynamic invariant detection (Daikon), metamorphic testing for LLMs, symmetric metamorphic relations for model stability (see References).

## 4. The flow (per call-site)

For **each input independently** (no cross-input mixing):

| # | Step | What it does | Why it helps |
|---|---|---|---|
| 1 | **Sample N diverse inputs** (`_distinct`) | pick N genuinely-different real inputs | diversity bounds the contract quality — the single biggest lever |
| 2 | **Run the ORIGINAL 4×** | 4 re-runs + the recorded output = **5 samples** of the old model on this input (the recorded one is real production behaviour — the most authoritative sample, and free) | the reference envelope; per-input only, no pollution |
| 3 | **Extract the per-input profile** — ONE LLM call that **reasons then extracts**; sees the request (prompt+input) + the original's 5 samples, **NEVER the cheaper** | reasons about the task and *why* each thing is stable vs varies, then outputs: **Commitments** (each HARD/SOFT), **Allowed variation** (+ degree/kind), the **output type**, and its **confidence** (were 5 enough, or unsure?). Optional second verify/critique pass — decide in Phase 0. | reasoning cuts false-positive commitments (Daikon's weakness); the extra fields let the judge weight and let us **abstain honestly** when the profile is uncertain; separated from the cheaper so it is not a mega-call |
| 4 | **Run the CHEAPER 5×** | 5 candidate outputs (matched to the original's 5 samples for a fair comparison) | same as today, just K=5 |
| 5 | **Judge each cheaper re-run** — one call per re-run, given the profile | "keep these commitments, ignore this variation — did this output keep every commitment? y/n + which broke" | concrete y/n → low judge variance → stable; a broken commitment (mislabel) is caught regardless of surface similarity |
| 6 | **Per-input verdict** | input PASSES if the cheaper stays inside the original's envelope: holds every hard commitment (0-variance aspects) AND varies the rest **no more than the original did**. The cushion per aspect = the original's own variance there — NOT a flat number. Where the original itself wobbled (a soft commitment), the cheaper gets the same wobble as cushion. | the goal is the variation side: allow exactly the original's own spread, flag only variation *beyond* it → few false NOT-SAFEs, stable |
| 7 | **Node verdict (binary)** | SAFE if every input passes; else NOT-SAFE | conservative; borderline folds into NOT-SAFE ("when unsure, don't downgrade") |

**Key architectural point:** extraction (Step 3) reads only the ORIGINAL's outputs; judging (Step 5) is **one call per cheaper re-run** given a short profile summary. No single call ever reads all outputs together.

## 5. Key design decisions + rationale

| Decision | Why |
|---|---|
| **Per-input profile, not a node-level contract** | the decision is input-specific; mixing inputs corrupts it (a router does A→billing, B→auth). |
| **Concrete commitments, not a "materiality" score** | concrete y/n has far lower judge variance → stable; a mislabel is caught regardless of surface similarity. |
| **Profile from the OLD model's own runs** | allows the new model exactly the old's variation; no golden truth needed. |
| **One judge call per re-run (no mega-call)** | a single call over all outputs is harder, noisier, and hides which output failed; extraction and judging are separated. |
| **Binary SAFE / NOT-SAFE, no INCONCLUSIVE** | governance wants a decision; "not confidently safe" = don't downgrade; borderline fails safe. |
| **Cushion is proportional to the original's own variance, not a flat "allow N"** | the real work is defining allowed variation; tying the cushion per aspect to how much/often the original itself varies means "if the old wobbles, the new may too; if the old is solid, the new must be" — that's what makes it both fair and stable. |
| **Strict on 0-variance commitments + K=5, WITH the profile** | the profile kills the judge's fake breaks, so strict-where-the-original-is-strict is viable *and* stable; K=5 tightens the estimate. Loosening the standard *globally* to buy consistency costs safety (false SAFE) — instead the cushion is *earned* per aspect from the original. |
| **Freeze the profile per (node + prompt-version)** *(optional, later)* | removes the profiler's own run-to-run variance from the reference; invalidate on prompt/agent-version change. |

## 6. The two prompts (sketch — to be refined)

**Profiler** (request + the original's 5 samples → profile). ONE call, reason-then-extract:
> Here is the request (system prompt + input) and 5 outputs the SAME (original) model produced for it. First, briefly REASON about what this node is doing and why each thing stays the same or changes. Then output:
> - TASK / OUTPUT TYPE (label · JSON · summary · tool-call · free text)
> - COMMITMENTS — what is identical across ALL 5 and carries meaning (decisions, labels, codes, required facts, structure); mark each HARD (held every time) or SOFT (held most times — note how often)
> - ALLOWED VARIATION — what differs, and **how much / in what way** (wording? order? which optional details? a value in a range?). The cheaper model may vary exactly this much, no more.
> - CONFIDENCE — were 5 samples enough to be sure, or is the node too noisy to profile? (if unsure, say so)
> Be conservative in BOTH directions: if unsure whether something is a commitment, call it HARD; if unsure whether a difference is acceptable, do NOT list it under ALLOWED VARIATION. (The judge forgives only what you list here, so an over-broad allow-list is the only way to wave through a real regression.) Never invent a commitment the outputs don't support.

**Commitment-judge** (one cheaper output + profile → kept?):
> Reference commitments for this request: {commitments}. It may vary in: {allowed variation}. Candidate output: {output}. Did the candidate preserve EVERY commitment (ignoring allowed variation)? Answer KEPT or BROKE, and name any commitment it broke.

## 7. Honest trade-offs / residuals

- **A genuinely flaky cheaper still needs samples.** If the cheaper breaks a commitment ~1/3 of the time, K=3 catches it ~70% of audits, K=5 ~87%; it fails toward NOT-SAFE. Only more re-runs close this — it's the cheaper's own randomness, not the judge.
- **More re-runs help ONLY with the profile.** Under strict 0-tolerance *without* the profile, K↑ makes clean nodes fail *more* (more chances to hit a judge fluke). The profile (reliable judge) is what makes K↑ a net win. Illustration (clean node, 5% fluke rate): K=3 strict passes 86%, K=5 strict passes 77% (worse), K=5 strict *with* the profile (fluke ~1%) passes 95%.
- **The trace-change source is untouched here.** A fresh audit pulling different inputs is a separate instability; pinning/caching the sampled inputs addresses it (a later phase).
- **The profiler can over-declare commitments** from few runs (Daikon's known weakness) → over-strict → a few false NOT-SAFEs. Fails safe; more old-model runs relax it.
- **Cost:** runs the original K× on *every* input (not just doubtful today) + one profiler call per input. More than today — the price of a richer, stable reference.

## 8. Knobs to decide (calibrate on real nodes, not guess)

- **Sample counts:** original = **4 re-runs + recorded = 5 samples**; cheaper = **5 re-runs** (equal sizes). Cost vs consistency.
- **Profiler: one call or two?** Baseline = ONE reason-then-extract call. Add a second verify/critique pass ONLY if Phase-0 shows over/under-declared commitments. Measure first.
- **Profiler confidence → abstain:** if the profiler says the node is too noisy to profile, treat the node as NOT-SAFE (don't certify a downgrade on an un-pinnable node) rather than guessing.
- **Tolerance:** DERIVED per aspect from the original's own variance (proportional), not a flat global "allow N". A small global floor may still be useful to absorb residual judge noise on hard commitments — but the primary cushion is earned from the original's spread. Decide: is a global floor needed on top, and how big.
- **Freeze the profile?** Yes per prompt-version (stable), or re-profile live each audit.
- **Original on all inputs vs doubtful-only:** all inputs, since the profile is per-input.

## 9. Implementation phases (only if we proceed — refine first)

- **Phase 0 — measure first (cheap, no verdict change):** write the two prompts; on 2–3 real nodes, run the profiler on the old model's runs and eyeball whether COMMITMENTS / ALLOWED-VARIATION come out sensible; measure the clean-node fake-break rate. **Decide K and tolerance from real numbers, not estimates.**
- **Phase 1:** add the profiler step + the commitment-judge behind a flag; keep the current `_verdict` as the fallback.
- **Phase 2:** wire per-input pass + binary node roll-up into `audit_node` / `prove_transform`.
- **Phase 3:** freeze the profile per prompt-version; surface the commitments in the report as the interpretable artifact.
- **Phase 4 (later):** pin/cache the sampled inputs to remove the trace-change instability.

Maps onto existing code: `app/services/audit.py` (`prove_transform`, `_verdict`, `judge_preserved`), `app/config.py` (K, tolerance, feature flag). No change to cache/compress.

## 10. Integration with the existing engine (decisions)

Today `prove_transform` is ONE shared, transform-agnostic engine reused by all three levers. The profile flow **inverts three of its assumptions** (single-recorded reference; doubtful-only baseline; integer `ck ≥ self_kept`), so it doesn't slot in — it forks. The decisions:

1. **New judge, don't touch the shared one.** `judge_preserved(request, recorded, candidate)` can't carry the profile. Add `judge_within_envelope(request, recorded, profile, candidate)` for downgrade only; leave `judge_preserved` for cache/compress.
2. **Judge framing = "candidate vs the RECORDED, *ignoring* differences that fall inside allowed-variation."** NOT "check a commitments checklist." Rationale = failure direction: if the profiler **misses** something, the checklist framing never checks it → **false SAFE** (bad downgrade ships); the vs-recorded framing flags it as an un-forgiven diff → **false NOT-SAFE** (we lose a saving, never ship a regression). The recorded stays the concrete anchor; the profile is the *forgive-list*. This is also what makes allowed-variation the crux.
3. **Profiler conservatism is two-sided, one principle:** over-list commitments AND under-list allowed-variation → both = "when unsure, stay strict." (Add the second half to the §6 prompt.)
4. **The self-variance runs are repurposed; the doubtful-only optimization dies.** The profiled path always runs the original 4× (they ARE the profiler's input, not a tiebreaker). Straight line: original 4× → profile → cheaper 5× → judge each vs envelope. ~10 calls + 1 profiler per node — more than today's conditional baseline; acceptable per cost stance, flagged.
5. **`_verdict` is replaced; cushion lives in TWO places.** Per-aspect proportional cushion is applied *inside the judge* (via allowed-variation). A *small aggregate tolerance* (e.g. allow ≤1 break of 5) sits in `_verdict_profiled(kept, k)` to absorb judge/LLM flicker. The integer `ck ≥ self_kept` compare is gone. **Aggregate threshold = a Phase-0 measurement, not a guess.**
6. **Fork, don't mutate.** New `prove_transform_profiled`; `audit_node` branches to it behind a flag; old engine stays as fallback + for cache/compress.
7. **Report fields shift (Phase 3, downstream).** Downgrade has no prompt transform, so `system_before/after` are equal; new evidence = the profile + which commitment each break hit; `self_kept` leaves the UI. Touches `report.html` + `report_doc.py` — not in Phase 1.
8. **Two stability tiers — don't over-promise tier 1.** The profile *reduces* judge flips immediately (concrete envelope). *Eliminating* cross-audit drift also needs pinned inputs + a frozen profile (Phase 4). Tier 1 = steadier; tier 2 = deterministic.

## 11. Out of scope (deliberately)

- Cache-prefix expansion and input compression (not touched).
- Embedding / semantic-similarity scoring — masks critical small changes (a mislabel is 98% text-similar); rejected.
- A curated golden dataset — impossible at this scale; rejected.
- INCONCLUSIVE as a third verdict — folded into NOT-SAFE per the governance "when unsure, don't downgrade" stance.

## 12. Open questions to resolve before implementing

1. Does the profiler produce sensible commitments on *messy real* prompts (structured, routing, classification, summary)? — **Phase 0 answers this.**
2. What is the real clean-node fake-break rate, and therefore the right K and tolerance?
3. Freeze vs re-profile — measure the stability difference.
4. Cost budget per node at K=5 across a large fleet — acceptable?

## 13. References

- Daikon — dynamic detection of likely invariants: https://homes.cs.washington.edu/~mernst/pubs/daikon-tool-scp2007.pdf
- Metamorphic Testing of LLMs: https://arxiv.org/abs/2511.02108
- Symmetric metamorphic relations for LLM output stability: https://www.sciencedirect.com/science/article/abs/pii/S0164121224003741
- LLM-as-judge self-inconsistency (Rating Roulette): https://arxiv.org/abs/2510.27106
