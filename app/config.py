"""Demo-wide knobs in one place; each is env-overridable."""
import os

import credentials

# Every $ figure is quoted "per CALLS_BASIS calls": a normalizing basis, not a monthly volume.
CALLS_BASIS = int(os.environ.get("AUDIT_CALLS_BASIS", "10000"))

JUDGE_MODEL = credentials.get_config("AUDIT_JUDGE_MODEL", "claude-sonnet-5")
PROFILE_MODEL = credentials.get_config("AUDIT_PROFILE_MODEL", "claude-sonnet-5")
# Output ceilings are billed on tokens generated. They are generous so a thinking model (or a gateway that injects
# thinking) still emits its reply; an empty reply drops the node description or reads as "differs from the set".
PROFILE_MAX_TOKENS = int(os.environ.get("AUDIT_PROFILE_MAX_TOKENS", "5000"))


AUDIT_SAMPLES = int(os.environ.get("AUDIT_SAMPLES", "5"))
AUDIT_MIN_EVIDENCE = int(os.environ.get("AUDIT_MIN_EVIDENCE", "1"))
AUDIT_MAX_PARALLEL = int(os.environ.get("AUDIT_MAX_PARALLEL", "8"))
# Chars of each output the judge reads; a longer output is unverifiable, never judged safe from a partial view.

# Reference set: the recorded output plus PROFILE_RERUNS fresh original re-runs; DOWNGRADE_K cheaper re-runs are
# judged against it. REF_CHARS caps each output packed into a judge prompt; a longer one marks the input unverified.
AUDIT_PROFILE_RERUNS = int(os.environ.get("AUDIT_PROFILE_RERUNS", "4"))
AUDIT_DOWNGRADE_K = int(os.environ.get("AUDIT_DOWNGRADE_K", "5"))
AUDIT_JUDGE_MAX_TOKENS = int(os.environ.get("AUDIT_JUDGE_MAX_TOKENS", "5000"))

# Verdict math (validation/engine.py), all fractions. Each cheaper output is judged JUDGE_VOTES times, majority wins;
# it is safe if its fit rate >= original_consistency - MARGIN. Below COHERENCE_FLOOR the original is too inconsistent
# to certify against. A node is safe when NODE_SAFE_RATIO of its inputs are (0.8 = one of five may fail).
AUDIT_JUDGE_VOTES = int(os.environ.get("AUDIT_JUDGE_VOTES", "3"))
AUDIT_COHERENCE_FLOOR = float(os.environ.get("AUDIT_COHERENCE_FLOOR", "0.6"))
AUDIT_DOWNGRADE_MARGIN = float(os.environ.get("AUDIT_DOWNGRADE_MARGIN", "0.2"))
AUDIT_NODE_SAFE_RATIO = float(os.environ.get("AUDIT_NODE_SAFE_RATIO", "0.8"))
