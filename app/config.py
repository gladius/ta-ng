"""Demo-wide knobs. One place, so every page reconciles by construction."""
import os

import credentials

# The fixed CALL-COUNT BASIS every $ figure is normalized to — NOT a monthly volume (a sample of traces can't
# reveal real production volume). We quote "$X per CALLS_BASIS calls"; the per-call saving is exact, so a viewer
# multiplies by their own real volume. Change it here (or via AUDIT_CALLS_BASIS) and every page — select, report,
# download — moves together. Larger basis = larger headline number; keep it honestly labelled "per N calls".
CALLS_BASIS = int(os.environ.get("AUDIT_CALLS_BASIS", "10000"))

# The ONE place levers turn on/off — a single source of truth, so no scattered per-lever booleans. Every layer
# (funnel, prove, report) reads this. We are focused on MODEL-TIER DOWNGRADE, so caching is simply omitted; add
# "cache" back to the list to re-enable it. Env override: LEVERS="downgrade,cache".
LEVERS = [s.strip() for s in os.environ.get("LEVERS", "downgrade").split(",") if s.strip()]


def lever_on(name):
    return name in LEVERS


# ── Model roles — ALL model choices live HERE, one place, each .env-overridable so a model can be swapped per role
# without touching code. Distinct roles: JUDGE (the equivalence verdict — quality is the crux), OPTIMIZER (rewrite /
# reorg / compress — judgement work), FILTER (cheap relevance pruning, not a verdict), PROFILE (the per-node
# comprehension analyst — a trust dial, one call per node, out of path; bump to Opus for a demo). Override in .env
# with AUDIT_JUDGE_MODEL / AUDIT_OPTIMIZER_MODEL / AUDIT_FILTER_MODEL / AUDIT_PROFILE_MODEL.
JUDGE_MODEL = credentials.get_config("AUDIT_JUDGE_MODEL", "claude-sonnet-5")
OPTIMIZER_MODEL = credentials.get_config("AUDIT_OPTIMIZER_MODEL", "claude-sonnet-5")
FILTER_MODEL = credentials.get_config("AUDIT_FILTER_MODEL", "claude-haiku-4-5")
PROFILE_MODEL = credentials.get_config("AUDIT_PROFILE_MODEL", "claude-sonnet-5")


# Downgrade-audit rigor — the ONE place these knobs live.
#   SAMPLES  = distinct real inputs per node (the DIVERSITY axis — different tickets)
#   REPEATS  = re-runs of the cheaper model per input (the STABILITY axis — kills run-to-run flipping)
#   MIN_EVIDENCE = fewer distinct inputs than this -> ABSTAIN. Default 1: audit whatever real inputs exist (even a
#   single one) and report the count honestly, rather than refusing to look; only a truly EMPTY bucket abstains.
# An INPUT is SAFE if at least AUDIT_SAFE_RATIO of its re-runs preserved behavior; a NODE is SAFE only if EVERY
# input is SAFE. NOT-SAFE if it never preserved; anything in between -> BORDERLINE.
AUDIT_SAMPLES = int(os.environ.get("AUDIT_SAMPLES", "5"))
AUDIT_REPEATS = int(os.environ.get("AUDIT_REPEATS", "3"))
AUDIT_MIN_EVIDENCE = int(os.environ.get("AUDIT_MIN_EVIDENCE", "1"))
# Per-node concurrency cap for the paid replay+judge calls. The N x K checks are independent so they parallelize
# safely; this bounds how many run at once so we never hammer the provider (llm_client also backs off on 429).
AUDIT_MAX_PARALLEL = int(os.environ.get("AUDIT_MAX_PARALLEL", "8"))
# How many chars of EACH output the judge reads. Set generously — real agent outputs fit ENTIRELY, so the judge
# never compares only the beginning (a truncated view can hide tail drift and cause a FALSE SAFE). This is a large
# fraction of the judge model's context (~200k tokens); an output that STILL exceeds it is treated as unverifiable
# -> DRIFT (never SAFE from a partial view). Tune here, in one place.
AUDIT_JUDGE_MAX_CHARS = int(os.environ.get("AUDIT_JUDGE_MAX_CHARS", "120000"))
# An input is SAFE if at least this FRACTION of its re-runs preserved behavior. Used ONLY as the fallback bar when
# the self-variance baseline is off (below). 2/3 tolerates one drift in three. Raise toward 1.0 for a stricter bar.
AUDIT_SAFE_RATIO = float(os.environ.get("AUDIT_SAFE_RATIO", "0.66"))

# Self-variance baseline. When the cheaper model isn't PERFECT on an input (it drifted at least once), a flat bar
# can't tell "the downgrade broke it" from "this node is just noisy". So we measure the ORIGINAL model's OWN
# run-to-run consistency on that same input and judge the cheaper RELATIVE to it. It runs ONLY on doubtful inputs
# (cheaper < K/K), so deterministic nodes cost nothing extra. The recorded output is the original's free sample #1
# (it IS the anchor, trivially preserved), so we add only K-1 fresh original re-runs and count recorded as the +1.
# An input whose ORIGINAL self-rate is below AUDIT_SELF_FLOOR is an unreliable anchor -> BORDERLINE (human review),
# never a confident SAFE/NOT-SAFE. Set AUDIT_SELF_BASELINE=0 to disable and fall back to AUDIT_SAFE_RATIO.
AUDIT_SELF_BASELINE = os.environ.get("AUDIT_SELF_BASELINE", "1") not in ("0", "false", "False", "")
AUDIT_SELF_FLOOR = float(os.environ.get("AUDIT_SELF_FLOOR", "0.5"))

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
AUDIT_JUDGE_MAX_TOKENS = int(os.environ.get("AUDIT_JUDGE_MAX_TOKENS", "512"))
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
