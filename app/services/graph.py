"""Graph builder — folds an agent's call-sites (nodes) + flow (edges) from CONTRACT traces.

Framework-agnostic on purpose: it uses ONLY the normalized fields every trace carries — `node_id` (the
call-site), `trace_id` (one execution), `start_time` (ordering), `model`, `usage`. No platform-specific
metadata. So it works for any source (LangGraph, a Gemini supervisor agent, an offline export) identically.

  nodes = the LLM call-sites, grouped by node_id, with core stats (model, calls, avg tokens)
  edges = the observed flow: within each trace, order the call-sites by start_time; consecutive → an edge

Structure + core stats only. No pricing (that arrives with the downgrade slice).
"""
import re
from collections import defaultdict, Counter

from connectors.datasource import get_source

# a model on a premium tier — the backend decides this (not the template), so the UI just colours by the flag
_BIG_MODEL = re.compile(r"sonnet|opus|gpt-4|gpt-5|gemini[- ]?(2|3).*pro|pro-preview", re.I)


def _tier(model_names):
    return "big" if any(_BIG_MODEL.search(m or "") for m in model_names) else "small"


def _st(t):
    s = t.get("start_time")
    return s if isinstance(s, str) else (str(s) if s else "")


def build(source_id, ws_id, project, limit=300):
    """Pull an agent's contract traces and fold them into {agent, nodes, edges}. Read-only, no LLM, no pricing."""
    buckets, skipped = get_source(source_id).pull(ws_id, project, limit=limit)
    traces = [t for b in buckets.values() for t in b]

    # NODES — group the LLM call-sites by node_id
    agg = {}
    for t in traces:
        n = t.get("node_id") or t.get("agent_id") or "node"
        u = t.get("usage") or {}
        d = agg.setdefault(n, {"calls": 0, "in": 0, "out": 0, "models": Counter(), "traces": set()})
        d["calls"] += 1
        d["in"] += u.get("input_tokens", 0) or 0
        d["out"] += u.get("output_tokens", 0) or 0
        d["models"][t.get("model_raw") or t.get("model") or "?"] += 1
        d["traces"].add(t.get("trace_id"))
    nodes = []
    for n, d in agg.items():
        c = max(1, d["calls"])
        mm = d["models"].most_common()                       # [(model, count), ...] most-used first
        avg_in, avg_out = round(d["in"] / c), round(d["out"] / c)
        nodes.append({"node": n, "calls": d["calls"], "traces": len(d["traces"]),
                      "avg_in": avg_in, "avg_out": avg_out,
                      "model": mm[0][0] if mm else "?",       # dominant
                      "models": [{"name": m, "calls": k} for m, k in mm],   # distribution (routing / A-B / drift)
                      "mixed": len(mm) > 1,
                      "weight": (avg_in + avg_out) * max(1, d["calls"]),     # sizing basis — computed here, not in the UI
                      "tier": _tier([m for m, _ in mm])})                    # "big"/"small" — the UI only colours by it

    # EDGES — within each trace, order call-sites by start_time; consecutive distinct nodes are the flow
    by_trace = defaultdict(list)
    for t in traces:
        by_trace[t.get("trace_id")].append((_st(t), t.get("node_id") or "node"))
    edge_ct = defaultdict(int)
    for seq in by_trace.values():
        seq.sort(key=lambda x: x[0])
        run = []                                              # collapse consecutive duplicates (a node firing N times)
        for _, n in seq:
            if not run or run[-1] != n:
                run.append(n)
        for a, b in zip(run, run[1:]):
            edge_ct[(a, b)] += 1
    edges = [{"src": a, "dst": b, "count": c} for (a, b), c in sorted(edge_ct.items(), key=lambda kv: -kv[1])]

    return {"agent": project, "nodes": sorted(nodes, key=lambda x: -x["avg_out"]),
            "edges": edges, "skipped": len(skipped)}
