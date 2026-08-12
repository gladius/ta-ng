"""Dev-only traceability dump. When AUDIT_DEBUG is set, write each pipeline stage per SNAPSHOT to disk so you can
inspect exactly what was fetched, how it was grouped into call-sites, and what was proven:

    .audit_debug/<snap>/
      traces/<call-site>.json   the raw recorded runs in each call-site bucket (what was downloaded)
      graph/nodes.json          the call-sites with their derived stats
      graph/edges.json          the graph edges
      graph/summary.json        agent, trace count, subgraphs, bucket keys
      results/proof.json        the frozen proof (verdicts, per-input evidence)

STRICTLY off unless AUDIT_DEBUG is truthy, so it never runs in production. Best-effort: a dump failure is logged and
swallowed — diagnostics must never break the audit. `.audit_debug/` is gitignored.
"""
import json
import os
import re

_ROOT = ".audit_debug"


def enabled():
    return os.environ.get("AUDIT_DEBUG") not in (None, "", "0", "false", "False")


def _slug(s):
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", str(s)).strip("-")[:80] or "node"


def _write(snap, sub, name, obj):
    d = os.path.join(_ROOT, str(snap), sub)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str, ensure_ascii=False)


def dump_graph(snap, g):
    """After the graph is built: raw per-call-site traces + the derived graph."""
    if not (enabled() and snap and g):
        return
    try:
        buckets = g.get("buckets", {}) or {}
        for key, traces in buckets.items():
            _write(snap, "traces", _slug(key) + ".json", traces)
        _write(snap, "graph", "nodes.json", g.get("nodes", []))
        _write(snap, "graph", "edges.json", g.get("edges", []))
        _write(snap, "graph", "summary.json", {
            "agent": g.get("agent"), "traces": g.get("traces"), "graphs": g.get("graphs", []),
            "node_count": len(g.get("nodes", [])), "bucket_keys": list(buckets.keys()),
            "bucket_sizes": {k: len(v) for k, v in buckets.items()}})
    except Exception as e:                                    # diagnostics must never break the audit
        print("[snapdump] graph dump failed for %s: %s" % (snap, e))


def dump_results(snap, proof):
    """After proving: the frozen proof."""
    if not (enabled() and snap and proof):
        return
    try:
        _write(snap, "results", "proof.json", proof)
    except Exception as e:
        print("[snapdump] results dump failed for %s: %s" % (snap, e))
