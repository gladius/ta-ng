"""Demo-wide knobs. One place, so every page reconciles by construction."""
import os

import credentials

# The fixed CALL-COUNT BASIS every $ figure is normalized to — NOT a monthly volume (a sample of traces can't
# reveal real production volume). We quote "$X per CALLS_BASIS calls"; the per-call saving is exact, so a viewer
# multiplies by their own real volume. Change it here (or via AUDIT_CALLS_BASIS) and every page — select, report,
# download — moves together. Larger basis = larger headline number; keep it honestly labelled "per N calls".
CALLS_BASIS = int(os.environ.get("AUDIT_CALLS_BASIS", "10000"))

# ── Model roles — ALL model choices live HERE, one place, each .env-overridable so a model can be swapped per role
# without touching code. Distinct roles: JUDGE (the equivalence verdict — quality is the crux), PROFILE (the per-node
# comprehension analyst — a trust dial, one call per node, out of path; bump to Opus for a demo). Override in .env
# with AUDIT_JUDGE_MODEL / AUDIT_PROFILE_MODEL.
JUDGE_MODEL = credentials.get_config("AUDIT_JUDGE_MODEL", "claude-sonnet-5")
PROFILE_MODEL = credentials.get_config("AUDIT_PROFILE_MODEL", "claude-sonnet-5")
# output ceiling for the per-node + deployment comprehension — same reasoning as AUDIT_JUDGE_MAX_TOKENS below: a
# thinking model (or a gateway that injects thinking) must have room to think AND still emit the JSON, or the reply
# comes back empty and the node description silently vanishes. Ceiling only — billed on tokens generated.
PROFILE_MAX_TOKENS = int(os.environ.get("AUDIT_PROFILE_MAX_TOKENS", "5000"))


# Downgrade-audit rigor — the ONE place these knobs live.
#   SAMPLES  = distinct real inputs per node (the DIVERSITY axis — different tickets)
#   MIN_EVIDENCE = fewer distinct inputs than this -> ABSTAIN. Default 1: audit whatever real inputs exist (even a
#   single one) and report the count honestly, rather than refusing to look; only a truly EMPTY bucket abstains.
AUDIT_SAMPLES = int(os.environ.get("AUDIT_SAMPLES", "5"))
AUDIT_MIN_EVIDENCE = int(os.environ.get("AUDIT_MIN_EVIDENCE", "1"))
# Per-node concurrency cap for the paid replay+judge calls. The N x K checks are independent so they parallelize
# safely; this bounds how many run at once so we never hammer the provider (llm_client also backs off on 429).
AUDIT_MAX_PARALLEL = int(os.environ.get("AUDIT_MAX_PARALLEL", "8"))
# How many chars of EACH output the judge reads. Set generously — real agent outputs fit ENTIRELY, so the judge
# never compares only the beginning (a truncated view can hide tail drift and cause a FALSE SAFE). This is a large
# fraction of the judge model's context (~200k tokens); an output that STILL exceeds it is treated as unverifiable
# -> DRIFT (never SAFE from a partial view). Tune here, in one place.
AUDIT_JUDGE_MAX_CHARS = int(os.environ.get("AUDIT_JUDGE_MAX_CHARS", "120000"))

# ── Reference-set downgrade engine — re-run counts & judge budgets (verdict math is the next block) ────────────────
# The ONE live downgrade validator (app/services/downgrade_refset.py). Per input the ORIGINAL model's 5 own outputs
# ARE the acceptable-behaviour set (recorded output + ORIG_RERUNS fresh re-runs); each cheaper re-run is judged to
# belong to that set. Re-runs go through audit.replay, whose generation budget scales to the recorded output length.
#   ORIG_RERUNS      = fresh ORIGINAL re-runs building the set (+ the recorded output = ORIG_RERUNS+1 = 5).
#   DOWNGRADE_K      = cheaper re-runs judged against the set.
#   JUDGE_MAX_TOKENS = OUTPUT ceiling for a judge reply (verdict + short reason). A ceiling, not a target.
#   JUDGE_REF_CHARS  = per-output cap when packing reference outputs into a judge prompt. Set generously so real
#                      outputs fit WHOLE; anything beyond it is marked truncated in-prompt AND flags its input
#                      "unverified" — never a silent compare-on-a-clipped-view (which could read as a false SAFE).
AUDIT_PROFILE_RERUNS = int(os.environ.get("AUDIT_PROFILE_RERUNS", "4"))   # env name kept; = ORIG_RERUNS
AUDIT_DOWNGRADE_K = int(os.environ.get("AUDIT_DOWNGRADE_K", "5"))
AUDIT_JUDGE_MAX_TOKENS = int(os.environ.get("AUDIT_JUDGE_MAX_TOKENS", "5000"))   # ceiling, billed on GENERATED tokens;
# generous so a reasoning/thinking model (or a gateway that injects thinking) can't consume the budget before it
# emits the verdict — an empty reply was the "differs from the set" symptom. The judge's real output is one line.
AUDIT_JUDGE_REF_CHARS = int(os.environ.get("AUDIT_JUDGE_REF_CHARS", "16000"))

# ── Reference-set downgrade engine — verdict math ──────────────────────────────────────────────────────────────────
# No prose contract. The original's 5 own outputs ARE the acceptable range; ONE fit-judge decides "does this candidate
# belong to that set?", and every judgement is a MAJORITY VOTE (the probe proved a single judge flips on borderline
# outputs). Per input we get two rates by the IDENTICAL mechanism: original self-consistency (each original vs the
# other 4, leave-one-out) and cheaper fit (each cheaper vs all 5 originals). The verdict is arithmetic on the two.
#   JUDGE_VOTES     = judge each candidate this many times, majority wins (kills per-output judge flip).
#   COHERENCE_FLOOR = original must reproduce itself at least this often (of 5) or we can't certify -> NOT-SAFE.
#   DOWNGRADE_MARGIN= cheaper is SAFE if `cheaper_rate >= original_rate - MARGIN` (proportional to the original).
#   NODE_SAFE_RATIO = node is SAFE if at least this FRACTION of inputs are SAFE (softer than all-must-pass, which
#                     amplifies per-input noise). 0.8 = allow one input of five to fail.
AUDIT_JUDGE_VOTES = int(os.environ.get("AUDIT_JUDGE_VOTES", "3"))
AUDIT_COHERENCE_FLOOR = int(os.environ.get("AUDIT_COHERENCE_FLOOR", "3"))
AUDIT_DOWNGRADE_MARGIN = int(os.environ.get("AUDIT_DOWNGRADE_MARGIN", "1"))
AUDIT_NODE_SAFE_RATIO = float(os.environ.get("AUDIT_NODE_SAFE_RATIO", "0.8"))
