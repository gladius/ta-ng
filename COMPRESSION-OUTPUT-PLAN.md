# Plan — Compression + Output levers (reuse-first)

**Status:** DRAFT for refinement · 2026-08-08
**Frame:** token_auditor is one *out-of-path, transform-agnostic, optimize→verify engine*. Levers are just
transforms plugged into the SAME proof spine, **tiered by how strong a "pass" is**. This plan adds two levers —
**input compression** and **output optimization** — by *reusing* token_auditor's proof spine and *porting*
battle-tested IP from headroom. It is deliberately NOT a rewrite.

---

## 0. The thesis (why this is tractable out-of-path)

In-path tools use weak extractive compression (LLMLingua et al.) because they can't afford latency and **can't
verify**. We are out-of-path, one-time cost: we can (a) use a *frontier-LLM abstractive rewrite* that restructures,
not just drops tokens, and (b) **verify** it three ways — rule-coverage gate, then replay + output-judge against the
model's own noise floor. Verification is what makes the stronger transform safe. That is the whole game, and the
verification machinery already exists.

---

## 1. What we REUSE from token_auditor (do not rebuild)

The proof/verdict/evidence/UI spine is lever-agnostic. Confirmed by survey of `app/services/`.

| Piece | File | Reuse |
|---|---|---|
| N×K proof engine + self-variance baseline | `audit.prove_transform` | **as-is** — pass a compression/output `produce`-fn |
| Output-equivalence judge (A=recorded, B=candidate) | `audit.judge_preserved` | **as-is** — transform-blind |
| Per-input binary + node SAFE/NOT-SAFE/BORDERLINE roll-up | `audit._verdict`, `_one_repeat` | **as-is** |
| Diverse sampling | `audit._distinct` | **as-is** (upgrade in §6) |
| Provider replay boundary | `audit.replay` + `llm_client.run` | **new produce-fn** — swap the system prompt on the trace |
| Trace rebuild pattern | `cache_reorg.apply` | **pattern** → `with_compressed_system(t)` |
| Snapshot / store / proof_key | `snapshot.py`, `store.py`, `prove.proof_key` | **as-is** (shape-agnostic) |
| Report ZIP + evidence rows + tier chips | `report_doc.py`, `report.html` | **new section** — return the downgrade-shaped result dict |
| Funnel row schema | `funnel.build` | **extend** — add `compress*` / `output*` fields |
| Prove orchestration + $ roll-up | `prove.stream`, `_prove_one`, `_step_msg` | **extend** — add dispatch branches |
| Price/tokens helpers | `util.PRICE`, `approx_tokens`, `canonical_model` | **as-is** |

**Net:** the entire spine is reused. Levers differ only in five swappable slots (see §4).

---

## 2. What we PORT from headroom (don't reinvent — it's already built and debugged)

Ranked; paths under `C:\Workspace\headroom\`.

1. **`strategies/prompt_compression/compress.py` → `compress_boilerplate`** — preservation-first compressor
   (model = COMPRESSOR that never RESPONDS; token count a SOFT ceiling; **coverage-gate guard** = tag-wrapped ∧
   strictly-shorter ∧ keep_tail survives ∧ every must-preserve literal present; input-sized cap; one stricter
   retry). *The crown jewel — port near-verbatim.*
2. **`segment.py` — sentence-hash static-run diff** (`_stable_hashes`, `_static_runs`, `system_static_runs`,
   `user_static_runs`, `system_varies`) + `classify`/`segment`. *Correct, scalable static-vs-variable split; the
   word-LCS failure is documented so we don't regress into the byte-compare trap.*
3. **must-preserve union** — `analyze.protect_literals`, `analyze.extract_rules`, `analyze._shape`,
   `analyze.analyze_outputs` + `segment.protect_spans`, `protected_blocks`, `example_blocks`. *This IS the
   rule/invariant inventory fed to the compressor AND checked after. Exclusion reasoning (what NOT to gate on) is
   as valuable as inclusion.*
4. **`judge/rulejudge.py` (3 modes) + `judge/dynjudge.py`** — `_SYS` (compression compliance), `_SYS_OUTPUT`
   (**output_shrink** — the "meant to be shorter" judge), `_SYS_MODELSWAP`; noise-floor + honest ABSTAIN.
   *Reconcile with token_auditor's `judge_preserved` — see OPEN DECISION D1.*
5. **`base.py` — `RefineLoop`** — targeted backoff: on DRIFT, re-inject the *named dropped rule* and retry less
   aggressively (not "compress less everywhere"). `SingleEdit` for output.
6. **`strategies/output_optimization/strategy.py`** — recurring-scaffold detection (redundancy measured, not
   enumerated) + scaffold-targeted directive prompt + `output_shrink` judge mode + output-token pricing.
7. **`prompt_compression/strategy.py`** (two-blocks-one-loop-one-judge reference structure),
   **`fixtures.json`** (7 labeled detect cases → our test corpus), **`docs/research/2026-compression-prompt.md`**
   (build spec; names two not-yet-built P1 upgrades: local rule-by-rule compression, candidate-and-verify).
8. **`comprehend.py` — semantic keep-list contract (ADAPT)** — bring the JSON profile
   (`compress_safe`/`must_preserve`/`rules`/`output_invariants`) and the "classify, never follow" framing; rework
   the graph coupling. *Optional; not on the critical path.*

---

## 3. Lever tiers (honesty is the product)

The UI must label *what a pass means*, never blur tiers.

- **Tier 1 — Proven safe** (content-preserving transform): model downgrade, deterministic cache reorder,
  structured-output. Pass ≈ guarantee.
- **Tier 2 — Compressed & tested** (lossy, equivalence/adequacy-targeted): **input compression**, LLM cache
  reorder, **output optimization**. Pass = "held on your diverse real inputs vs the model's own noise — here's the
  before/after and any drift." Evidence, not guarantee. Clearly badged distinct from Tier 1.

---

## 4. The five swappable slots (this is "switch parts by strategy")

Every lever is the spine + these five:

| Slot | downgrade | input-compression | output-optimization |
|---|---|---|---|
| **Detector ($0)** | `downgrade.candidates` | NEW `compress.detect` (fat static system prompt via `system_static_runs`) | PORT output-scaffold detect |
| **Transform (`produce`)** | `replay(t, cheaper)` | `replay(with_compressed_system(t), model)` | `replay(with_output_directive(t), model)` |
| **Pre-gate** | none | must-preserve union + rewrite + rule-coverage/entailment | scaffold + directive |
| **Judge mode** | model-swap (compliance+substance) | compression compliance | output_shrink |
| **Pricing** | tier Δ × calls | input-price × tokens removed × calls | output-price × tokens removed × calls |

Same `prove_transform`, same `_verdict`, same report shape for all three.

---

## 5. Compression lever — end-to-end flow

```
detect ($0):  system_static_runs(bucket) → fat, redundant static system prompt? price = input$ × static_tokens × calls
locate ($0):  segment/classify + protect_literals + protect_spans + example_blocks + extract_rules  →  must_preserve
rewrite (LLM, once/node):  compress_boilerplate(system, subject=SYSTEM, must_preserve, target)  →  compressed
gate 1 (guard):  tag-wrapped ∧ shorter ∧ every must_preserve literal present         (from compress.py)
gate 2 (rules):  for each extract_rules(original): compressed ENTAILS it?  (atomic rule-coverage, Lynx-style rubric on our judge)  → else RefineLoop targeted backoff
prove (paid):    prove_transform(_distinct(bucket), λt: replay(with_compressed_system(t), model), model, k)
price:           input$ × tokens_removed × calls, booked ONLY on node SAFE
report:          downgrade-shaped result dict → new .strat-sec, before/after prompt + per-input evidence
```

Static-only, once per node. Dynamic payload (query, RAG, images, tool outputs) is **never** touched.

---

## 6. Two upgrades we owe regardless of lever

- **Sampling (§`_distinct`):** today = first-N lexically-distinct in arrival order = dedup, not representativeness.
  Upgrade to **diversity-max + hardest-tail**: embed the bucket, greedy farthest-point to span the space, and
  force-include the longest / most complex inputs. "SAFE on the most diverse *and* hardest inputs" is a far
  stronger claim. Benefits all levers.
- **Cross-node validation (no end-to-end needed):** use the trace graph as the dependency map — derive per node
  *what downstream actually consumes* from its output, and have the judge verify *those* properties survive;
  compress **shared** static blocks *consistently* across nodes (detect shared runs, compress once, reuse). This is
  the out-of-path approximation of end-to-end.

---

## 7. What is genuinely NEW (the small real surface)

1. `compress.detect(bucket)` — $0, find fat static system prompts worth compressing.
2. `with_compressed_system(t)` / `with_output_directive(t)` — trace rebuilders (pattern from `cache_reorg.apply`).
3. `compress.prove` / `output.prove` — orchestrators mirroring `cache_reorg.prove`, returning the downgrade-shaped dict.
4. **Rule-coverage / entailment gate** — atomic `extract_rules` → per-rule entailment on our judge (Lynx 3-part
   rubric: nothing dropped / nothing invented / no contradiction). No equivalent exists in either project.
5. Funnel/prove/report/select **extensions** for the two new levers (mechanical).

---

## 8. Sequencing

- **P0 — one-node proof of value (no UI):** port `compress.py` + `segment.py` + must-preserve + rulejudge; run
  detect→locate→rewrite→gate→`prove_transform` on ONE real node's system prompt via a CLI/script. Output: real
  compression ratio + SAFE/BORDERLINE + before/after on *our* data. **This decides whether dense-prompt reality
  bites or restructuring wins — run before any wiring.**
- **P1 — wire compression into the app:** funnel/prove/report/select extensions; test corpus = `fixtures.json`.
- **P2 — port output optimization** (output_shrink judge + scaffold directive) as a second Tier-2 lever.
- **P3 — the two upgrades (§6):** diverse+hard sampling, cross-node consumed-interface validation.
- **Deferred:** dynamic-payload compression (trap), local rule-by-rule compression + candidate-and-verify (research P1),
  comprehend port.

---

## 9. OPEN DECISIONS (refine together before P0)

- **D1 — one judge or two?** token_auditor's `judge_preserved` is a single output-equivalence judge. headroom's
  `rulejudge` has 3 modes (compression / model-swap / output_shrink) + `dynjudge` noise-floor. Reconcile: extend
  `judge_preserved` with a `mode` param, or port `rulejudge` alongside it? (Recommend: extend, keep one judge with
  modes — output_shrink *needs* the "shorter is ok" rubric, so at least two modes are mandatory.)
- **D2 — entailment gate: our judge vs a ported checker?** Recommend running the atomic rule-coverage on our own
  judge LLM (copy the Lynx/MiniCheck *rubric*, not the models). Confirm.
- **D3 — static-only as a hard product rule?** Recommend YES (leverage + testability + sidesteps multimodal).
- **D4 — adopt headroom's compressor prompt, or rewrite it?** Recommend adopt near-verbatim (its comments are fixed
  bugs), then tune.
- **D5 — scope of first ship:** compression only, or compression + output together? (Recommend compression P0/P1
  first; output P2.)

### Decisions locked in discussion (2026-08-09)
- **Proof = behavioral equivalence, always.** The N×K replay + output-judge is THE proof. Rule extraction /
  entailment is DEMOTED to an optional *convergence heuristic* (helps the RefineLoop, belt-and-suspenders for
  guardrails) — never the safety guarantee. First agent runs **behavioral-only, zero rule extraction.** (supersedes
  the D2 framing; see OP-3.)
- **Output lever OFF for the first proof.** Ported-but-dormant. Input compression only.
- **Static-first is the hard rule** for the delivered artifact; dynamic/context is a separate advisory path (OP-2).
- **First deliverable = a PROFILE/TRIAGE report + one proven input-compression node**, not a broad sweep.

---

## 10. Open problems we're tracking (living list — 2026-08-09)

Each has a *current direction*; none is final. Close them as we arrive at solutions.

- **OP-1 — Prompt version + error filtering (deal with it in the GRAPH layer).**
  *Version:* one node's bucket can mix prompt versions → averaging stale behavior. Direction: derive our OWN
  static-prompt **content signature** (hash the static system run); default-anchor on the **latest** signature and
  prove against that cohort; optionally surface the last 2–3 signatures on the agent page as a selector. Do NOT
  depend on LangSmith's revision id (present only if they use Hub prompts). *Errors:* LangSmith runs carry
  top-level `status`/`error`; filter `status == "error"` OUT at ingestion (an errored call has no valid recorded
  output to preserve, and a possibly-abnormal prompt — pure noise for cost proof). Keep a dropped-count for honesty.
  Reliability is a different product; not ours.
  *(revision 2026-08-09):* PREFER LangSmith's optional whole-agent `revision_id` (commonly the app git SHA, present
  when the team sets it) — pin the audit to one revision, matches how they think about "version." The per-node
  prompt signature is the FALLBACK (no revision_id) AND the finer within-revision drift check (a deploy may not
  change a node's prompt; a RAG-assembled prompt changes without a deploy). Two levels, complementary — not
  "hash instead of version."

- **OP-2 — Dynamic/"context" compression: prove the METHOD, not an artifact.** Static → prove a fixed artifact,
  deliver it. Dynamic (RAG/history/tool outputs) → no fixed artifact; we take each recorded call's real dynamic
  content, compress it with method M at ratio R, replay, judge vs recorded output across N diverse inputs → advise
  "deploy M in-path at R; here's measured preservation + $ on your traffic." **Always evidence-backed, never blind.**
  Confidence is lower than static (proven on a *sample* of past content, not on the artifact). NOT in the first
  proof. This is the honest answer to the director's word "context compression": same word, two deliverables, two
  risk profiles.

- **OP-3 — Is rule-judge fundamentally flawed? (resolved-in-principle.)** The behavioral judge is robust because it
  is **per-input**: candidate B is compared to *that input's own recorded output A* — drastic input-to-input
  variation is handled by construction. Rule-judge needs a FIXED rule inventory, which is not standard across agents
  and is meaningless when rules are RAG-injected per input (OP-4). Resolution: **behavioral equivalence is the
  proof; rules are optional convergence sugar that degrades gracefully to "none."** Residual risk is *coverage of
  edge inputs*, handled by diverse+hardest sampling (§6) + `example_blocks` protection — NOT by rules. The real gap
  this exposed is the missing **profile/analysis layer** (OP-5), not a better rule-judge.

- **OP-4 — Dynamic RAG-assembled system prompts are a real node CLASS (confirmed common in 2026).** For such nodes:
  little truly-static system prompt (only scaffold), rules vary per input, rule extraction is meaningless, and the
  token mass lives in the dynamic injected instructions → an OP-2 dynamic-context opportunity, not a static-artifact
  one. The **profile must detect "varying system prompt" and route accordingly** (scaffold-compress + flag dynamic
  opportunity) instead of treating it as static. The first production agent is exactly this class — a good hard test.

- **OP-5 — The profile/analysis layer (the missing intelligence between grouping and optimization).** Strategy:
  **deterministic spine + one BOUNDED LLM label per node — NOT an agentic loop** (a central enterprise auditor needs
  reproducible, explainable, cheap profiling over thousands of nodes). Deterministic ($0, from traces): LLM spend,
  calls, model(s), mixed/routed, static-vs-dynamic system prompt (segment diff + `system_varies`), static token mass
  + redundancy, output verbosity/scaffold, structured-output, downgrade-candidacy, error rate, prompt-signature
  set, graph position + what it feeds. Bounded LLM (≤1 call/node, optional, headroom `comprehend` idea): role,
  semantic must-preserve keep-list, "safely optimizable?" with reasons. **Output = a per-node, per-lever verdict
  with reasons** (candidate + $ / "no meaningful static" / "no LLM spend — orchestration node" / "dynamic prompt →
  context advisory" / "too little volume"). This profile IS the triage report AND the substrate every strategy reads
  — strategies become thin. Boundary guard: this is *optimization intelligence*, NOT a trace UI — we never rebuild
  LangSmith's trace view.
  *(revision 2026-08-09):* the deterministic/LLM split is by NATURE OF TASK, **not cost** — deterministic only for
  exactly-computable FACTS (token counts, prices, diffs, error rate; an LLM does arithmetic *worse*); LLM judgment
  as DEEP as the node warrants (multiple calls / a small reasoning loop are fine, NOT capped at one) — just not a
  black-box agent for the whole pipeline, for reproducibility. Open **P0 validation**: current static detection is
  **line-level byte-exact** (`cache._recoverable_text`, `cache_reorg._annotated` — 100% set-intersection of lines);
  headroom's is **sentence-hash, ≥90% occurrence, whitespace-normalized** (`segment.py`). Line-level breaks when a
  per-call token sits inside an otherwise-static line; sentence-level is more robust but its ≥90% gate can over-
  include. Which is right is a TEST on the real agent's traces, not an assumption.

- **OP-6 — Graph intel is underused today.** Its two legitimate jobs: (1) OP-5 profiling, (2) cross-node
  consumed-interface validation (know what downstream reads from a node's output; verify THAT survives) + shared-
  block consistent compression. Neither is visualization.

- **OP-8 — Graph logic is split across `connectors/graph.py` and `app/services/graph.py` (layer smell).**
  `build_graph` operates on NEUTRAL run-records (no platform types) → it's DOMAIN logic living in the connector
  layer, whose job should end at fetch → normalize to neutral records. Symptom: the connector SPLITS call-sites by
  shape/frame variant, then `app/services/graph.build` MERGES them back by stable key (re-buckets + re-sums) —
  grouping happens in BOTH, pulling opposite directions (part may be vestigial from before stable keys). Clean
  target: connector = data only; ONE graph builder in app/services. VERIFY whether the connector-split is
  load-bearing before consolidating. NOT now — a spine refactor that doesn't advance the production proof; do it as
  its own slice, ideally BEFORE the profile layer (which sits on the graph and would inherit the mess).

- **OP-7 — Rules are OUT of the critical path; the RefineLoop steers off the behavioral drift reason.** The
  behavioral judge returns `{preserved, reason}` PER INPUT — on drift it names the specific broken behavior for
  THAT input. That reason is what feeds the next compression retry (not a pre-extracted rule inventory). So we never
  need "common rules" across inputs: for a static-prompt node the rule is common but the judge surfaces it from the
  drift anyway; for a dynamic/RAG-prompt node there are no common rules and we don't pretend there are. Rule
  extraction stays an OPTIONAL bonus signal for static-prompt nodes only. This resolves "how do you make common
  rules when inputs vary hugely" — you don't; the per-input behavioral judge does the work.

---

## 11. Profile detail spec (draft — WHAT we generate; no graph visualization)

FACT = deterministic from traces · JUDGE = LLM (as deep as the node warrants).

**Per-AGENT summary**
- name · pinned revision/signature · trace window · #traces (·#error runs excluded) · #nodes  *(FACT)*
- est LLM spend $/period · #calls · #nodes-with-LLM-spend vs orchestration-only  *(FACT)*
- addressable $ per lever (downgrade / compress-static / compress-context) · unaddressable $ + top reasons  *(FACT)*
- one-line posture: "N nodes, M optimizable, headline win = …"  *(JUDGE)*

**Per-NODE row**
- identity: name, graph path, position (feeds →), leaf/branch  *(FACT)*
- volume: calls, % of agent spend, est $/period  *(FACT)*
- model: model(s), mixed/routed?, thinking on?  *(FACT)*
- tokens: avg in / out, static-token mass, dynamic-token mass  *(FACT)*
- prompt shape: STATIC system prompt | DYNAMIC/RAG-assembled (`system_varies`); #signatures seen (drift)  *(FACT)*
- redundancy: measured compressibility of the static part  *(FACT)*
- output shape: structured? recurring verbose scaffold?  *(FACT)*
- role: one line on what the node does  *(JUDGE)*
- must-preserve: semantic keep-list — facts/ids/decisions that must survive  *(JUDGE)*
- safety: guardrail-heavy? compress conservatively? any red flag  *(JUDGE)*
- **LEVER VERDICTS (payload):**
  - downgrade: candidate → model, $ | reason-not
  - compress-static: candidate, static tok, $ | reason-not (no static / dynamic prompt / too small)
  - compress-context: opportunity $, "needs runtime compressor" | n/a
  - **overall:** OPTIMIZABLE / ADVISORY-ONLY / NO-OPPORTUNITY + one-line why

This per-node, per-lever verdict object IS the director's triage view AND the substrate each strategy reads.

---

## 12. Scope discipline (anti-bloat) — the rule that keeps this from becoming headroom-2

headroom had everything but was bloat. Porting the whole §2 PORT LIST recreates that. So:

- **§2 is a MENU of what EXISTS in headroom, not a v1 build list.** Port ON DEMAND, per proven need.
- **A profile field is generated ONLY if a strategy consumes it.** Agent-type / use-case labeling that nothing
  consumes is narrative → one line max, no machinery.
- **v1 target = fully-static system prompts** (`system_varies == False`): compress the WHOLE system prompt, **no
  cross-call segmentation needed** (there's no dynamic in the system message to separate). Segmentation is a LATER
  capability for varying / user-frame cases. This sidesteps the whole line-vs-sentence question for v1.
- **v1 minimal port** (given behavioral-only proof, output off, static-only): `compress_boilerplate` (compressor)
  + a simplified `RefineLoop` (steers off the behavioral drift reason) + cheap literal protectors
  (`protect_literals` / `protect_spans` / `example_blocks`) + a SCOPED profile-judge (load-bearing fields only).
  **NOT ported for v1:** `rulejudge` (behavioral `judge_preserved` already exists), `dynjudge` (defer), the
  segmenter (only when `system_varies` / frame), output strategy (off), full `comprehend`.
- **N×K is empirical, not inherited.** Reusing downgrade's sample/repeat structure is fine, but the exact counts
  (and whether compression needs MORE distinct inputs to catch edge-rule drops) are a P0 tuning result, not a
  given. A high compression FAIL rate is expected and correct — an honest "not compressible → NO-OPPORTUNITY" is a
  valid, trustworthy output; only a problem if EVERY node fails (then that agent's story is downgrade, not compress).
