"""Demo-wide knobs. One place, so every page reconciles by construction."""
import os

# The monthly call volume every $ figure is quoted against. A sample of traces can't reveal real production
# volume, so we quote a transparent assumed basis ("per N calls / mo") and let the viewer plug in their own.
# Change it here (or via env) and every page — select, report, download — moves together.
DISPLAY_CALLS = int(os.environ.get("AUDIT_CALLS_PER_MONTH", "10000"))
