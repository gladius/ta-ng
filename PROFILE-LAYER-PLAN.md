# Profile Layer — Plan (FINAL)

**Status:** locked for build · 2026-08-09 · branch feat/compression

## Purpose

**Per-node comprehension.** One profile per node that (1) **informs** — a presentable report about any agent (role,
structure, what's optimizable and why) *before* optimization runs; and (2) **routes + samples** — decides which
lever applies per node and shares the exact sample the proof uses. Runs once per node, out of path.

**Hard boundary:** the profile is **descriptive + routing only**. Every optimization *claim* (a saving) is proven
downstream by real re-runs (`prove_transform`). Profile proposes; the proof disposes. This keeps "don't judge what
you can prove" intact, one layer earlier.

**Trust model:** not "provable" — **trustworthy like a competent analyst**: grounded (every claim cites a
trace/span), coverage-honest, cross-checked against full-bucket facts, and every $ claim proven downstream. A
frontier LLM doing 1–2 grounded calls per node, the way we trust a senior engineer's read of a handful of cases.

---

## 1. What one profile is built from

- **The node's bucket** = all N recorded calls to that call-site.
- **Cheap full-bucket facts** (over *all* N, $0, pure counting — the **trust cross-check** on any sample):
  `n_total`, `distinct_system_count`, `distinct_user_frame_count`, model set, output-type distribution,
  tool-use rate, `thinking_enabled`, error rate.
- **Diversity measure (over all N, $0):** group the N inputs by a coarse key (length band × message-shape ×
  normalized system fingerprint × output type); the **number of groups** is the node's diversity. This is what tells
  us whether a small sample is enough — **we don't assume it.**
- **The sample = cover the groups, bounded.** Take distinct instances spanning the measured groups, capped (~8–12).
  For a **low-diversity node** this is ≤5 and the first-5 overlap `_distinct(bucket, 5)`
  ([audit.py:179](app/services/audit.py#L179)) — consistent with the proof (its 5 are a subset). For a
  **high-diversity node** it's more, and **coverage is reported honestly** ("sampled 8 of 40 groups → LOW"). 5 is a
  **floor for simple nodes, not an assumption.**

### Instance size — never send raw big prompts

An enterprise instance can be 50k–200k tokens (long system prompt, big RAG context, long tool results). We **do not
send raw instances** — 5 × 200k blows the window. Each instance is reduced to a **structure-aware digest**
(deterministic, ~3–5k tokens): the message skeleton with role labels, each block's **type-guess + size**, small
blocks verbatim, big blocks head+tail with `[N tokens omitted]`, and a cheap per-block **"identical / differs across
the sample"** mark. A 200k instance → a ~3k digest that still carries everything the profiler reasons over. The full
content of one block is **deep-read only when its nature is genuinely ambiguous** (§2 drill).

---

## 2. The LLM consolidation — sized to the node

The digests are small, so the **default is ONE consolidation call over the sample's digests** — best for the
cross-comparison static-vs-dynamic needs (the model sees them side by side). This works even at 200k/instance
*because it reads digests, not raw prompts.*

**When the node is big or very diverse** (many groups → digests overflow one call): **map-reduce** — analyze
per-instance (or per-batch) → a final **consolidation call** merges the per-instance reads and reconciles against the
full-N facts. (This is the per-instance-then-consolidate approach; it's the right fallback at scale. One call is the
small-node special case.)

Consolidation is **per-field** either way:

| Field | How the sample consolidates |
|---|---|
| `role` | **consensus** — all should agree (disagreement → coherence check) |
| system static-vs-dynamic | **cross-compare** the digests' system blocks, **reconciled with `distinct_system_count` over all N** |
| `components` | **union** — present-in-all = stable; present-in-some = dynamic |
| `behavior` | **map** input-type → action |
| `optimizability` | derived per lever |

Every claim **cites a trace#/block** (can't quote it → can't claim it). `confidence` + `coverage` stated.

### The two consolidation checks

1. **Reconcile the sample against the full-N facts.** A "static" read from the sample is a guess; checked against
   `distinct_system_count` over all N it's **confirmed at 100% coverage** (count = 1) or **flagged** (count > 1 →
   sample missed variation → drill).
2. **Coherence / mixed-bucket detection.** If the sample looks like *different jobs*, distinguish **multi-modal
   node** (legit — behavior varies by input type) from **possible mixed bucket** (the known ReAct-mix /
   untagged-collapse grouping bugs → **flag it, don't fake one profile**).

### Drill — read deeper only where unresolved (the only "adaptive" part)

After the first pass, spend **at most 1–2 targeted extra calls**, only on the *specific* thing that's unresolved:
**deep-read one ambiguous block's full content**, or **analyze an uncovered group**. It never re-sends everything —
it reads *more of one region*. Triggers: low confidence, static/dynamic conflicts with `distinct_system_count`,
coherence ambiguous, or coverage low with budget to spare.

---

## 3. NodeProfile schema

- `role` — plain-language purpose.
- `components[]` — {`type`: guardrail | instruction | fewshot | tool_def | tool_result | rag | boilerplate;
  `static|dynamic`; `compressibility`: leave | candidate | extractive; `evidence`: trace#/span}.
- `user_structure` — {frame; slots: query | retrieved_doc | tool_result}.
- `input_types` — kinds + rough frequency.
- `behavior` — input-type → action.
- `optimizability` — per lever {candidate, reason}:
  - `cache` (+ static-prefix size / break point), `compress_static`, `compress_context`,
  - `downgrade` (+ `difficulty`: simple | moderate | hard — from task type, output structure, constraint density,
    current-tier headroom, `thinking_enabled` / tool-use).
- `coherence` — coherent | multimodal | possible_mixed_bucket.
- `confidence`, `coverage` ("read 5 of N, all K observed types").

---

## 4. How it feeds the levers

- **Downgrade:** `candidate` + `difficulty` + `current_model` → *where* to run the proof + *what to expect* (skip
  already-cheapest nodes; run on expensive ones; simple-categorical → likely SAFE, nuanced → likely NOT-SAFE).
- **Compression:** the **component map** routes the right region and sets the honest expectation —
  guardrails / instructions / tool_defs = **leave**; fewshot / boilerplate = **candidate**; tool_results / rag =
  **extractive**. Prevents compressing the wrong thing (the #1 failure mode) and gives the "minimal recovery here,
  useful elsewhere" story.
- **Cache:** static-prefix size + the exact break point (e.g. a mid-prompt RAG injection).

Divergence between a profile suggestion and the proof (e.g. `kb_resolve` → BORDERLINE) is **shown** — that's the
system working (propose with LLM, prove with real re-runs, let the proof veto).

---

## 5. Model config (one model, no bloat)

- `PROFILE_MODEL` = `credentials.get_config("AUDIT_PROFILE_MODEL", "claude-sonnet-5")` in `config.py`, following the
  existing `JUDGE_MODEL`/`OPTIMIZER_MODEL` pattern. Trust dial — set to Opus in `.env` for the demo. One call per
  node, out of path.
- Full-bucket facts + selection = **deterministic, no model**.

---

## 6. Consolidation of files

Everything profile-related in **one `profile/` folder**: `select` (sampler + facts), `analyze` (the LLM call +
schema + the two checks), `report` (rendering). No profile logic scattered across the app.

---

## 7. Phasing

- **MVP (build now):** an **isolated probe** — `select` → one `analyze` call → `report` — run on **meridian's 6
  nodes** with real LLM calls, printed to console. **No app changes, no UI.** We read the 6 profiles and judge them
  against "as good as a competent analyst" before anything touches the app.
- **P1:** port to `app/services/profile/`; the **profile report becomes the post-graph landing** (replacing the bare
  select page — lever selection folds in, each node showing its candidates + a "prove" action); wire the shared 5
  into the downgrade/compress proofs; adaptive drill.
- **P2:** semantic (embedding) clustering for input types; versioned re-audit; output-optimization routing.

**Boundary guard:** this is *optimization intelligence*, not a trace viewer — we never rebuild LangSmith's UI.

---

## 8. Would I build this? — non-negotiables

1. **LLM is the analyst; facts cross-check it.** Full-bucket counts reconcile the 5-instance read; nothing is
   classified by a diff.
2. **Descriptive + routing only** — `prove_transform` stays the sole verdict.
3. **Every claim cites its evidence span** + confidence + coverage.
4. **Consolidation handles disagreement** (multimodal vs mixed-bucket) rather than forcing one false profile.
5. **Proven on the 6 meridian nodes in isolation** before the app sees it.
