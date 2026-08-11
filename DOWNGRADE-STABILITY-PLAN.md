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

Grounded in: dynamic invariant detection (Daikon), metamorphic testing for LLMs, symmetric metamorphic relations for model stability (see References).

## 4. The flow (per call-site)

For **each input independently** (no cross-input mixing):

| # | Step | What it does | Why it helps |
|---|---|---|---|
| 1 | **Sample N diverse inputs** (`_distinct`) | pick N genuinely-different real inputs | diversity bounds the contract quality — the single biggest lever |
| 2 | **Run the ORIGINAL K×** | K outputs + the recorded output = the old model's behaviour envelope on this input | this is the reference; per-input only, no pollution |
| 3 | **Extract the per-input profile** — one LLM call, on the ORIGINAL's outputs only | → **Commitments** (identical across all: decision/label, codes, required facts, shape) + **Allowed variation** (what differs: wording, order, minor details, values that legitimately vary) | the intelligence; separated from the cheaper so it is not a mega-call |
| 4 | **Run the CHEAPER K×** | K candidate outputs | same as today |
| 5 | **Judge each cheaper re-run** — one call per re-run, given the profile | "keep these commitments, ignore this variation — did this output keep every commitment? y/n + which broke" | concrete y/n → low judge variance → stable; a broken commitment (mislabel) is caught regardless of surface similarity |
| 6 | **Per-input verdict** | input PASSES if the cheaper kept all commitments across its re-runs (strict; allow at most 1 as a cushion for residual judge noise) | one bad re-run of a real commitment = not safe |
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
| **Strict commitments + K=5, WITH the profile** | the profile kills the judge's fake breaks, so strict is viable *and* stable; K=5 tightens the real-break estimate. Loosening the pass rule to buy consistency costs safety (false SAFE) — avoided. |
| **Freeze the profile per (node + prompt-version)** *(optional, later)* | removes the profiler's own run-to-run variance from the reference; invalidate on prompt/agent-version change. |

## 6. The two prompts (sketch — to be refined)

**Profiler** (old model's K+1 outputs → profile):
> You are shown several outputs the SAME model produced for the SAME request. List (a) COMMITMENTS — everything identical across all of them that carries meaning (decisions, classifications, labels, codes, required facts, output structure); (b) ALLOWED VARIATION — everything that differs (wording, order, optional details, values that legitimately vary). Be conservative: if unsure whether something is a commitment, list it as a commitment.

**Commitment-judge** (one cheaper output + profile → kept?):
> Reference commitments for this request: {commitments}. It may vary in: {allowed variation}. Candidate output: {output}. Did the candidate preserve EVERY commitment (ignoring allowed variation)? Answer KEPT or BROKE, and name any commitment it broke.

## 7. Honest trade-offs / residuals

- **A genuinely flaky cheaper still needs samples.** If the cheaper breaks a commitment ~1/3 of the time, K=3 catches it ~70% of audits, K=5 ~87%; it fails toward NOT-SAFE. Only more re-runs close this — it's the cheaper's own randomness, not the judge.
- **More re-runs help ONLY with the profile.** Under strict 0-tolerance *without* the profile, K↑ makes clean nodes fail *more* (more chances to hit a judge fluke). The profile (reliable judge) is what makes K↑ a net win. Illustration (clean node, 5% fluke rate): K=3 strict passes 86%, K=5 strict passes 77% (worse), K=5 strict *with* the profile (fluke ~1%) passes 95%.
- **The trace-change source is untouched here.** A fresh audit pulling different inputs is a separate instability; pinning/caching the sampled inputs addresses it (a later phase).
- **The profiler can over-declare commitments** from few runs (Daikon's known weakness) → over-strict → a few false NOT-SAFEs. Fails safe; more old-model runs relax it.
- **Cost:** runs the original K× on *every* input (not just doubtful today) + one profiler call per input. More than today — the price of a richer, stable reference.

## 8. Knobs to decide (calibrate on real nodes, not guess)

- **K (re-runs):** 5 proposed. Cost vs consistency.
- **Tolerance (commitment breaks allowed per input):** 0 (strict) or ≤1 (cushion for residual judge noise). Never more — that tolerates real break rates.
- **Freeze the profile?** Yes per prompt-version (stable), or re-profile live each audit.
- **Original on all inputs vs doubtful-only:** all inputs, since the profile is per-input.

## 9. Implementation phases (only if we proceed — refine first)

- **Phase 0 — measure first (cheap, no verdict change):** write the two prompts; on 2–3 real nodes, run the profiler on the old model's runs and eyeball whether COMMITMENTS / ALLOWED-VARIATION come out sensible; measure the clean-node fake-break rate. **Decide K and tolerance from real numbers, not estimates.**
- **Phase 1:** add the profiler step + the commitment-judge behind a flag; keep the current `_verdict` as the fallback.
- **Phase 2:** wire per-input pass + binary node roll-up into `audit_node` / `prove_transform`.
- **Phase 3:** freeze the profile per prompt-version; surface the commitments in the report as the interpretable artifact.
- **Phase 4 (later):** pin/cache the sampled inputs to remove the trace-change instability.

Maps onto existing code: `app/services/audit.py` (`prove_transform`, `_verdict`, `judge_preserved`), `app/config.py` (K, tolerance, feature flag). No change to cache/compress.

## 10. Out of scope (deliberately)

- Cache-prefix expansion and input compression (not touched).
- Embedding / semantic-similarity scoring — masks critical small changes (a mislabel is 98% text-similar); rejected.
- A curated golden dataset — impossible at this scale; rejected.
- INCONCLUSIVE as a third verdict — folded into NOT-SAFE per the governance "when unsure, don't downgrade" stance.

## 11. Open questions to resolve before implementing

1. Does the profiler produce sensible commitments on *messy real* prompts (structured, routing, classification, summary)? — **Phase 0 answers this.**
2. What is the real clean-node fake-break rate, and therefore the right K and tolerance?
3. Freeze vs re-profile — measure the stability difference.
4. Cost budget per node at K=5 across a large fleet — acceptable?

## 12. References

- Daikon — dynamic detection of likely invariants: https://homes.cs.washington.edu/~mernst/pubs/daikon-tool-scp2007.pdf
- Metamorphic Testing of LLMs: https://arxiv.org/abs/2511.02108
- Symmetric metamorphic relations for LLM output stability: https://www.sciencedirect.com/science/article/abs/pii/S0164121224003741
- LLM-as-judge self-inconsistency (Rating Roulette): https://arxiv.org/abs/2510.27106
