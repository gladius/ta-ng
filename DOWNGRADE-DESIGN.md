# Model-Tier Downgrade — Design

**Status:** agreed, ready to implement. Describes the target `downgrade_refset.py`. Supersedes
`DOWNGRADE-STABILITY-LOGIC.md`.

## Principle

The original model's own 5 outputs **are** the acceptable-behaviour range. Judge the cheaper against that range;
recommend the downgrade only if the cheaper lands inside the range **at least as often as the original itself is
consistent.** The original is never "graded" against a rubric — its own outputs are the ground truth.

## The flow — per call-site

For each of **N distinct real inputs** sampled from the recorded traces:

| # | Step | What | Why | LLM calls |
|---|---|---|---|---|
| 1 | Sample | Pick N distinct real inputs (deterministic) | cover real cases; stable sample across audits | 0 |
| 2 | Reference set | recorded output + run the **ORIGINAL 4×** = **5 original outputs** | the real behaviour **and its natural variance** — what the cheaper must reproduce | 4 replays |
| 3 | **Coherence** | **ONE call**: "of these 5, how many agree materially?" → `original n/5` (+ which diverge) | one holistic look is enough — judging its own outputs 15× gives the same number for 15× the cost | **1** |
| 4 | Cheaper runs | Run the **CHEAPER 5×** | sample the cheaper on the same input | 5 replays |
| 5 | **Cheaper fit** | Judge **each cheaper output vs the 5 originals**, **3× majority vote** → `cheaper n/5` | the reference set carries the variance, so the judge forgives the cheaper varying the *same way*; voting stabilises each per-output decision (a single judge flips ~2/5 on borderline outputs — measured) | 5 × 3 = 15 |
| 6 | Verdict | see rule below | relative to the original's own consistency, so a noisy original isn't held to perfection | 0 |
| — | Node | SAFE if ≥ `NODE_SAFE_RATIO` of inputs are SAFE | one noisy input shouldn't tank a node; all-must-pass amplifies noise | 0 |

**The original is used exactly twice, both legitimate:** as the **reference set** that weights how the cheaper is
judged (step 5), and as **one coherence number** (step 3). It is not judged 15×.

## Verdict rule (per input)

```
if   original_n < COHERENCE_FLOOR:        verdict = NOT-SAFE   # can't verify — original too inconsistent for a stable bar
elif cheaper_n >= original_n - MARGIN:    verdict = SAFE       # cheaper no worse than the original's own rate
else:                                     verdict = NOT-SAFE
```

- **The comparison is RELATIVE (`cheaper ≥ original − MARGIN`), not a fixed bar.** This is what keeps borderline
  nodes stable: if the original itself only lands 4/5, the cheaper is judged against 4/5, not against perfection. A
  fixed bar flips when the cheaper's rate sits on it; the relative bar absorbs that wobble.
- **The coherence gate is a GUARD, not a diagnosis.** If the original is too inconsistent, the reference set is a
  grab-bag and the relative bar collapses toward 0 — which would falsely stamp everything SAFE. So a low-coherence
  input is a plain **NOT-SAFE with a reason** ("couldn't verify — your model gave different answers here"). We do NOT
  add a special verdict for it; we just don't certify what we can't verify.

Node: `SAFE if #SAFE inputs ≥ ceil(NODE_SAFE_RATIO × N)`, else `NOT-SAFE`.

## Judge context

Both the **coherence call** (step 3) and the **cheaper fit judge** (step 5) see the **system prompt + user prompt +
the outputs**.
- **Include the system prompt** — the rules let the judge see *why* a difference is material (grounds it on complex
  nodes). It's identical across a node's judge calls → **prompt-cacheable**, so ~free after the first.
- **Exclude tool definitions** — any tool call is already in the output; the schema would only add tokens.
- Implementation: the judge's `request` = `_system_text(t) + _user_text(t)` (capped), not `_user_text(t)`.

## What counts as a difference — SELF-CALIBRATED by the reference set

The judge does NOT apply a global rule about what's cosmetic. Materiality is read from the original's own behaviour:

> The cheaper may differ from the set **only in ways the set itself already shows.**

- **Wording / ordering** — always fine.
- **Format / structure** (e.g. JSON vs plain text) — fine **only if the reference set itself varies it.** If every
  original holds one form, a candidate that breaks it does NOT belong. So format is material-or-not *per node*, read
  from what the original actually does — never a blanket "ignore formatting."
- **Decision / required fact / value / tool arg** — a change, drop, add, or contradiction **never** belongs.
- Unsure → BROKE.

This is why we don't need a rubric or type detection: whatever the original *varies* is forgiven; whatever it *holds*
is enforced.

## Four production cases (walkthrough)

| Case | Original's 5 outputs | Cheaper | Verdict — why |
|---|---|---|---|
| Stable | all same decision, same form | same | **SAFE** — cheaper matches the range |
| **Format drift** (original loose) | same decision, *some text some JSON* | correct decision, clean JSON | **SAFE** — the set already varies format, so the cheaper's form is in range; it's a strict improvement (note: cheaper more consistent) |
| Strict form (original tight) | same decision, *always JSON* | correct decision, but plain text | **NOT-SAFE** — the set never varies format, so form is a commitment the cheaper broke |
| Content drift (original flaky) | *different decisions* run-to-run | anything | **NOT-SAFE** — no stable behaviour to verify against; we don't certify what we can't verify (reason shown) |
| Worse cheaper | stable | wrong decision / dropped value | **NOT-SAFE** — cheaper broke a material commitment |

## Cost

Per input: 4 + 5 replays + **1** coherence + (5 × 3) fit = **9 replays + 16 judge calls = 25**. Per node (N=5) =
**≈125 calls** (down from the committed 195 — the original leave-one-out, 15 calls/input, becomes one coherence call).

## What changes from the currently-committed engine

1. **Original: leave-one-out (15 voted judges) → ONE coherence call.** Same number, 1/15th the cost.
2. **Judge context: user-only → system + user.** Both judges.
3. **Coherence gate outcome: flat NOT-SAFE → `ORIGINAL-UNSTABLE`** (distinct, informative; shows cheaper consistency).
4. **Keep unchanged:** reference set, cheaper 5×3 voted fit, the **relative comparison**, the node roll-up.

## Knobs (config)

`AUDIT_PROFILE_RERUNS`=4 · `AUDIT_DOWNGRADE_K`=5 · `AUDIT_JUDGE_VOTES`=3 (cheaper only) ·
`AUDIT_COHERENCE_FLOOR`=3 · `AUDIT_DOWNGRADE_MARGIN`=1 · `AUDIT_NODE_SAFE_RATIO`=0.8.

## Validate before trusting (developer test — not the product flow)

This is a one-time check *we* run, not part of the per-call-site flow. Before wiring it in:
1. **Stability:** run the full flow **twice** on a clean node, a borderline node (big sonnet→haiku gap), and an
   inconsistent-original node; confirm the node verdict is **identical across the two runs**.
2. **Sanity:** clean → SAFE, inconsistent original → ORIGINAL-UNSTABLE, deliberately-bad cheaper → NOT-SAFE.
3. **Calibrate** `COHERENCE_FLOOR` / `MARGIN` from the observed numbers.

Only if 1–3 pass do we replace the engine.

## Honest open risks

- **Borderline cheapers still wobble** a little (true rate near the bar). The relative margin absorbs most of it; it
  doesn't erase it. A genuine fix (sample more only near the bar) is **out of scope for now** — noted, not planned.
- **The coherence call is one LLM call** — on a very noisy node it could itself vary; the margin absorbs small moves.
- **Cross-model style bias:** the reference set is in the original's "voice"; a different-voice cheaper could read as
  "not the same kind." The "ignore cosmetic" clause is the defense — verify on real outputs.
