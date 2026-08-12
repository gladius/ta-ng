# Downgrade Stability — Logic v2 (reference-set, relative-rate)

**Status:** DESIGN — not yet implemented. Supersedes the profile/contract engine described in
`DOWNGRADE-STABILITY-PLAN.md` (§4 flow, §6 prompts, §10 integration). That plan's *motivation* (§1–§2) and
Phase-0 findings still stand; only the verdict mechanism changes here.

---

## 1. Why v2 — what v1 (profile/contract) got wrong

v1 profiled the original's 5 outputs into a **prose contract** (COMMITMENTS / ALLOWED-VARIATION), fed that prose to
the judge, and gated on a subjective **CONFIDENCE** field. Production (`API Healing Agent`, gemini-2.5-pro →
gemini-2.5-flash) showed it **stacked three non-deterministic LLM steps and added new failure modes without removing
the flip**:

- **Profiler wrote self-contradicting contracts** — "tool call with empty `{}` — HARD" *and* "whether a tool call is
  emitted — ALLOWED VARIATION" for the same aspect.
- **Judge softened the contract** — `{"confirmation":"run"}` judged "empty-ish → KEPT" on one re-run and
  "non-empty → BROKE" on another. Same output, opposite verdicts.
- **CONFIDENCE decoupled the verdict from the evidence** — an input showing `preserved 5/5 · original 5/5` was
  ruled NOT-SAFE purely because CONFIDENCE came back LOW (which itself contradicted the 5/5 self-consistency).

**Root cause:** we converted *directly comparable data* (5 original outputs + 5 cheaper outputs) into an *ambiguous
prose intermediary* and asked a second model to interpret it back. Every conversion loses information and adds a
new coin-flip.

## 2. Core principle

**Don't summarize the original into prose. Use its 5 real outputs AS the reference set (data). One judge.**

- The 5 originals **are** the acceptable range — the judge *sees* them, so it forgives exactly the variation the
  original itself shows. No contract to write or misread; five real outputs **cannot contradict themselves**.
- The original's **coherence** (are the 5 mutually consistent?) is the certifiability gate — a *measured* number,
  not a hunch. Judging original-vs-original per output is near-tautological, so this is **one holistic call**, not
  leave-one-out.
- The verdict is the cheaper's **fit rate** against that set — a number that **is** the decision, so the screen
  always reconciles (`cheaper n/5 · original n/5`).

## 2b. MEASURED (probe, `scratchpad/v2_probe.py`) — the reference set is necessary but NOT sufficient

Fixing the outputs and judging each one repeatedly (so any change is *pure judge non-determinism*):

- **Synthetic** `{}`-set vs `{"confirmation":"run"}`, judged 6× → `BBBBBK`: **still flips 1/6** (production was ~50/50, so the reference set *helped* — it just didn't eliminate it).
- **supervisor** (sonnet-5→haiku), each of 5 outputs judged 3× → **2/5 outputs flipped** (`KBK`, `BKB`).
- **route** (clean) → all `KKK`, and **coherence 5/5 twice** on every node.

**Conclusion:** the judge coin-flips on genuinely-borderline outputs even with the reference set. Swapping the
judge alone will NOT stabilise hard nodes. Stability needs THREE things together: **voting** (kill per-output judge
flip), a **margin** decision (not a hard threshold), and a **softer node roll-up** (all-must-pass amplifies noise:
0.74⁵ ≈ 22%). Coherence is single-call (it's stable).

## 3. The loop (per sampled input) — step by step

Data per input: `recorded (1)` + `AUDIT_PROFILE_RERUNS (4)` original re-runs = **5 original outputs** (the reference
set); `AUDIT_DOWNGRADE_K (5)` cheaper re-runs.

1. **Build the reference set.** Take the recorded output + run the ORIGINAL model 4× → 5 original outputs. These
   are the acceptable-behaviour range (raw data, no prose).
2. **Coherence (1 call).** Show the judge the 5 originals: *"how many are materially consistent with one another?"*
   → **original n/5**. This is understanding the original, not comparing to the cheaper.
3. **Run the cheaper 5×** → 5 candidate outputs.
4. **Fit-judge each candidate, with VOTING.** For each of the 5 candidates, judge it against the 5-original set
   `VOTES` times (e.g. 3) and take the **majority** → a stable per-candidate KEPT/BROKE. Count majority-KEPT →
   **cheaper n/5**.
5. **Per-input verdict (margin, not threshold):**
   - `original n/5 < COHERENCE_FLOOR (3/5)` → **NOT-SAFE** ("can't certify — the original is inconsistent with itself").
   - else `cheaper n/5 ≥ original n/5 − MARGIN (1)` → **SAFE** (cheaper is no worse than the original's own rate,
     within one).
   - else → **NOT-SAFE** (cheaper clearly worse). The ambiguous middle lands here — conservative and *stable*.
6. **Node verdict (softer roll-up).** SAFE if the SAFE inputs are a clear majority (e.g. `safe ≥ ceil(0.6·N)`), not
   "every input must pass". *(All-must-pass is a noise amplifier; the exact rule is a knob to calibrate.)*

**Display:** `cheaper n/5 · original n/5` per input + the majority-judge's one-line reason per candidate (optional in
the drill-down). Both numbers drive the verdict, so the screen always reconciles.

## 4. The two prompts (sketch — to refine in the probe)

**COHERENCE**
> Here are N outputs the SAME model produced for the SAME request. How many are **materially consistent** with one
> another — same decision / classification / required facts / tool call / structure? **Ignore** wording, order, and
> cosmetic formatting. Answer `n/N`, then ONE line on what they share.

**FIT JUDGE**
> Here are N outputs the model considers **acceptable** for this request (the reference set). Here is ONE candidate.
> Is the candidate the **same kind of answer** — same decision, required facts, structure — staying within the
> variation the set already shows? **Ignore** wording, order, cosmetic formatting; a changed decision / dropped fact
> / contradicted or invented material claim is NOT within range. Think ONE short line, then output KEPT or BROKE.

Note: both prompts carry the **"ignore cosmetic"** clause explicitly — that single lever decides substance-vs-format
sensitivity (the "# DECISION vs **Decision:**" over-strictness we hit). It is the main thing to calibrate.

## 5. Worked against the exact production failures

- **Input 2 (was `5/5 · 5/5 → NOT SAFE` from CONFIDENCE):** coherence 5/5 ≥ 3, cheaper fit 5/5 ≥ 4 → **SAFE.** The
  subjective gate is gone; the numbers decide.
- **Tool-call node (`{}` vs `{"confirmation":"run"}`):** the fit judge *sees* all 5 originals used empty `{}` → a
  cheaper output with a non-empty arg is out of range → **BROKE**, for the *same concrete reason each time* (it's
  comparing to real anchors, not a fuzzy "empty-ish" contract). If the cheaper adds the arg every run → fit 0/5 →
  **NOT-SAFE**, correctly. If the original itself sometimes emits nothing (the set shows 1/5 empty), a cheaper that
  emits nothing is **forgiven** — because the set contains that behavior.
- **Chaotic original (coherence 1/5):** `1/5 < 3` → **NOT-SAFE (can't certify)** — from the measured number; the
  report can say "the original reproduced its own behavior only 1/5, so a downgrade can't be certified."

## 6. What's removed vs v1

- Prose contract (COMMITMENTS / ALLOWED-VARIATION extraction) — **gone from the decision path.**
- CONFIDENCE field — **gone** (replaced by measured coherence).
- Leave-one-out judging of the original — **not used** (near-tautological; one coherence call suffices).
- `breaks ≤ fixed 1` gate — replaced by `cheaper n/5 ≥ FIT_BAR`, gated by coherence.
- The stacked profiler→contract→judge→confidence chain (3 subjective steps) → **one judge run on the cheaper,
  calibrated by the original's own set.**

Optional: keep a *short* human-readable "what the 5 originals share" blurb in the report as explanation only — never
gating. The report's interpretable artifact becomes **the 5 original outputs themselves** (the real range).

## 7. Knobs (calibrate on real nodes, not guess)

- `AUDIT_PROFILE_RERUNS` = 4 (→ 5 original samples), `AUDIT_DOWNGRADE_K` = 5.
- `VOTES` = 3 (judge each candidate this many times, take majority) — **the probe proved this is required**, not optional.
- `COHERENCE_FLOOR` = 3/5 — below this, abstain (can't certify).
- `MARGIN` = 1 — cheaper is SAFE if `cheaper n/5 ≥ original n/5 − MARGIN` (proportional to the original's own rate).
- Node roll-up threshold — `safe ≥ ceil(0.6·N)` vs strict all-must-pass. Calibrate; all-must-pass amplifies noise.
- The "ignore cosmetic" clause wording — the substance-vs-format dial.

## 8. Cost

Per input: **9 re-runs + 1 coherence + (5 fit × VOTES) fit calls**. At VOTES=3 that's 9 + 1 + 15 = **25 calls/input**
(vs v1's 15). The extra ~10/input buys the voting that the probe showed is *required* for stability — it's the price
of not flipping. Adaptive option: judge once, and only spend the extra votes on candidates near the margin.

## 9. Validation

**Done (`scratchpad/v2_probe.py`):** measured judge-flip on fixed outputs — see §2b. Verdict: reference set helps but
the judge still flips ~2/5 on borderline outputs → **voting is required.** Coherence is stable.

**Next, before engine code:** a second probe that adds `VOTES=3` majority and runs the **full loop twice** on a
borderline node (supervisor sonnet→haiku) + a clean node, and confirms:
1. the per-candidate majority verdict is **stable across the two runs** (voting killed the flip),
2. the node verdict is stable under the margin rule + softer roll-up,
3. and from the observed `original n/5` / `cheaper n/5`, lock `MARGIN` and the roll-up threshold.

Success criterion: same node verdict both runs on both nodes. If voting-3 isn't enough on the borderline node, raise
VOTES (or go adaptive) BEFORE writing engine code — the whole point is to not rewrite the engine on an unproven bet
again.

## 10. Implementation phases (only after the probe passes)

- **Phase 0 — probe + calibrate** (above). No engine change.
- **Phase 1 — new engine:** a `prove_transform_refset(sample, cheaper, original)` replacing the step-3/5/6 of
  `prove_transform_profiled`. Reuse `replay` / `_recorded` / `_system_text`. Emit the SAME report dict shape
  (`kept`, `k`, `verdict`, `note`, `self_rate`=coherence, `samples`) so the report keeps working.
- **Phase 2 — delete** the profiler / contract / confidence code from `profile_downgrade.py`.
- **Phase 3 — report:** the contract panel becomes "the original's 5 outputs (the reference set)" + coherence line;
  keep the cheaper re-run tabs.

## 11. Open questions

- Does the fit judge actually flip *less* than the contract judge? → the probe answers this; it's the whole bet.
- Reference set of 5 outputs in one prompt — size/cost on large outputs; cap each output (reuse
  `AUDIT_PROFILER_SAMPLE_CHARS`).
- Fixed `FIT_BAR` vs proportional (`cheaper ≥ original − 1`) — which is steadier across node types?
- Does "ignore cosmetic" over-forgive on structured/JSON nodes (where format IS material)? May need the coherence
  call to flag "output type = JSON/tool-call" so the fit judge tightens on structure.
