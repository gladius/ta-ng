"""Demo-wide knobs. One place, so every page reconciles by construction."""
import os

# The monthly call volume every $ figure is quoted against. A sample of traces can't reveal real production
# volume, so we quote a transparent assumed basis ("per N calls / mo") and let the viewer plug in their own.
# Change it here (or via env) and every page — select, report, download — moves together.
DISPLAY_CALLS = int(os.environ.get("AUDIT_CALLS_PER_MONTH", "10000"))

# The ONE place levers turn on/off — a single source of truth, so no scattered per-lever booleans. Every layer
# (funnel, prove, report) reads this. We are focused on MODEL-TIER DOWNGRADE, so caching is simply omitted; add
# "cache" back to the list to re-enable it. Env override: LEVERS="downgrade,cache".
LEVERS = [s.strip() for s in os.environ.get("LEVERS", "downgrade").split(",") if s.strip()]


def lever_on(name):
    return name in LEVERS


# Downgrade-audit rigor — the ONE place these knobs live.
#   SAMPLES  = distinct real inputs per node (the DIVERSITY axis — different tickets)
#   REPEATS  = re-runs of the cheaper model per input (the STABILITY axis — kills run-to-run flipping)
#   MIN_EVIDENCE = fewer distinct inputs than this -> ABSTAIN (don't judge from thin data)
# An INPUT is SAFE if at least AUDIT_SAFE_RATIO of its re-runs preserved behavior; a NODE is SAFE only if EVERY
# input is SAFE. NOT-SAFE if it never preserved; anything in between -> BORDERLINE.
AUDIT_SAMPLES = int(os.environ.get("AUDIT_SAMPLES", "5"))
AUDIT_REPEATS = int(os.environ.get("AUDIT_REPEATS", "3"))
AUDIT_MIN_EVIDENCE = int(os.environ.get("AUDIT_MIN_EVIDENCE", "3"))
# Per-node concurrency cap for the paid replay+judge calls. The N x K checks are independent so they parallelize
# safely; this bounds how many run at once so we never hammer the provider (llm_client also backs off on 429).
AUDIT_MAX_PARALLEL = int(os.environ.get("AUDIT_MAX_PARALLEL", "8"))
# How many chars of EACH output the judge reads. Set generously — real agent outputs fit ENTIRELY, so the judge
# never compares only the beginning (a truncated view can hide tail drift and cause a FALSE SAFE). This is a large
# fraction of the judge model's context (~200k tokens); an output that STILL exceeds it is treated as unverifiable
# -> DRIFT (never SAFE from a partial view). Tune here, in one place.
AUDIT_JUDGE_MAX_CHARS = int(os.environ.get("AUDIT_JUDGE_MAX_CHARS", "120000"))
# An input is SAFE if at least this FRACTION of its re-runs preserved behavior. 2/3 tolerates one drift in three
# (noise, or occasional model drift). Raise toward 1.0 for a stricter "never tolerate any drift" bar.
AUDIT_SAFE_RATIO = float(os.environ.get("AUDIT_SAFE_RATIO", "0.66"))
