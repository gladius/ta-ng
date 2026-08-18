"""Assemble a DEPLOYMENT PROFILE from the pinned graph — deterministic, $0, measured facts only.

Cost is computed from measured tokens x the ONE catalog price map (auditor.util / auditor/models.json),
the SAME source the downgrade lever uses — so profile and levers reconcile by construction. LangSmith's
own `cost_ls` is used only as a fallback for a model the catalog doesn't price (self-hosted / custom);
if neither has a price we say so ("price unknown — add it to models.json") rather than invent a number.

Scope note (honest): today the graph carries LLM call-sites only (tool/retriever/chain nodes + threads +
untagged-agent handling are the next increments), so `type` is always "llm" here. The advisory `role`
(one LLM call per node) is a separate, optional pass — left None here so the deterministic profile stands
alone with no model spend.
"""
from app.config import CALLS_BASIS
from auditor.util import PRICE, usd, canonical_model, tier, cache_mode
from app.services.profile.facts import fact_pack     # the deterministic $0 fact-pack (Phase 1)


def _node_cost(n):
    """$ for this call-site from tokens x catalog price. Falls back to LangSmith cost, else flags unknown."""
    canon = canonical_model(n.get("model"))
    p = PRICE.get(canon) if canon else None
    if p:
        per_call = usd(n["avg_in"], p["input"]) + usd(n["avg_out"], p["output"])
        return {"per_call": per_call, "per_basis": round(per_call * CALLS_BASIS, 2),
                "total_measured": round(per_call * n["calls"], 4), "source": "catalog", "known": True}
    if (n.get("cost_ls") or 0) > 0:                                  # self-hosted/custom: LangSmith's estimate
        per_call = n["cost_ls"] / max(1, n["calls"])
        return {"per_call": per_call, "per_basis": round(per_call * CALLS_BASIS, 2),
                "total_measured": round(n["cost_ls"], 4), "source": "langsmith", "known": True}
    return {"per_call": None, "per_basis": None, "total_measured": None,   # no price anywhere -> say so
            "source": "unknown", "known": False}


def _shape(n):
    """A COARSE operation hint from MEASURED signals (tool usage + output type/size + input size) — a
    fact-derived shape, NOT the semantic role (that's the advisory LLM read, still None). Conservative;
    always yields at least the output descriptor. Kept to the two most operation-relevant signals."""
    ot = n.get("out_type")
    out = n.get("avg_out") or 0
    tin = n.get("tool_input_rate") or 0
    tools = n.get("avg_tools") or 0
    parts = []
    if tin >= 0.5:
        parts.append("tool-result handler")          # its inputs carry prior tool outputs
    elif tools >= 0.5:
        parts.append("tool-caller")
    if ot == "json":
        parts.append("JSON out")
    elif ot == "short" or out < 12:
        parts.append("short-answer")
    else:
        parts.append("free-form out")
    if (n.get("avg_in") or 0) >= 4000:
        parts.append("long-context")
    return " · ".join(parts[:2])


def _struct_summary(s):
    """A deterministic one-liner for a non-llm node (there's no prompt to read) — just its measured behavior."""
    bits = ["%d call%s" % (s["calls"], "" if s["calls"] == 1 else "s")]
    if s.get("error_rate"):
        bits.append("%.0f%% error" % (s["error_rate"] * 100))
    if s.get("avg_ms"):
        bits.append("%dms avg" % s["avg_ms"])
    return "%s node · %s" % (s["type"], ", ".join(bits))


def _facts_line(f):
    """Compact human-facing deterministic facts (supporting context, NOT the headline) from the fact-pack."""
    if not f:
        return []
    bits = []
    ds = f.get("distinct_system")
    if ds is not None:
        bits.append("frame: fixed" if ds == 1 else "frame: varies (%d)" % ds)
    if f.get("distinct_output") is not None and f.get("n"):
        bits.append("distinct out: %d/%d" % (f["distinct_output"], f["n"]))
    if f.get("tools"):
        bits.append("calls: " + ", ".join(f["tools"]))
    if f.get("multi_turn"):
        bits.append("multi-turn")
    if f.get("rag_present"):
        bits.append("RAG")
    return bits


def _flow_str(f):
    """'upstream → this → downstream' from the fact-pack flow (empty on flat sources)."""
    fl = (f or {}).get("flow") or {}
    up, down = fl.get("upstream") or [], fl.get("downstream") or []
    return "%s → this → %s" % (", ".join(up) or "·", ", ".join(down) or "·") if (up or down) else ""


def _opp_of(r):
    """Per-node optimization opportunity from the funnel row (candidacy + $), or None if nothing to act on."""
    if not r:
        return None
    o = {"usd": round(r.get("opportunity") or 0, 2), "downgrade_to": r.get("downgrade_to"),
         "downgrade_usd": round(r.get("downgrade_usd") or 0, 2), "cache_verdict": r.get("cache_verdict"),
         "cache_usd": round(r.get("cache_usd") or 0, 2), "compress": bool(r.get("compress_candidate"))}
    return o if (o["downgrade_to"] or o["cache_usd"] or o["compress"]) else None


def build_profile(g, comp=None):
    """graph dict (pinned snapshot) [+ optional per-node comprehension] -> a one-page deployment report:
    {header, sections (grouped by subgraph), hotspots, edges}. Facts are deterministic/measured; `comp` adds
    the advisory op (one word) + summary per llm node once the LLM pass has run (else those fields are empty)."""
    comp = comp or {}
    fp = fact_pack(g)                                    # deterministic $0 facts per node (Phase 1)
    try:                                                 # the $0 OPTIMIZATION MAP — reuse the funnel's lever detectors
        from app.services import funnel
        _opp = {r["key"]: r for r in funnel.build("", "", g.get("agent", "agent"),
                                                   levers=["downgrade", "cache", "compress"], g=g)["rows"]}
    except Exception:
        _opp = {}
    nodes = []
    total_basis, total_in, total_out, any_unknown = 0.0, 0, 0, False
    for n in g.get("nodes", []):
        cost = _node_cost(n)
        canon = canonical_model(n.get("model"))
        c = comp.get(n["key"]) or {}
        nodes.append({
            "kind": "llm",
            "node": n["node"], "key": n["key"], "graph_path": n.get("graph_path", ""),
            "untagged": n.get("untagged", False), "mixed": n.get("mixed", False),
            "model": n["model"], "tier": tier(canon) if canon else None,
            "calls": n["calls"], "traces": n.get("traces", 0),
            "avg_in": n["avg_in"], "avg_out": n["avg_out"],
            "out_p95": n.get("out_p95"), "out_type": n.get("out_type"),
            "avg_ms": n.get("avg_ms"), "ttft_ms": n.get("ttft_ms"),
            "error_rate": n.get("error_rate", 0.0), "errors_excluded": n.get("errors_excluded", 0),
            "feedback_score": n.get("feedback_score"),
            "cache_mode": cache_mode(canon) if canon else None,
            "cost": cost, "shape": _shape(n),
            # advisory comprehension (present only after the LLM pass): op = ONE word, summary = what it does
            "op": c.get("op", ""), "summary": c.get("summary", ""),
            "coherence": c.get("coherence") or {}, "confidence": c.get("confidence", ""),
            "unavailable": c.get("unavailable", ""),
            "facts": _facts_line(fp.get(n["key"])), "flow": _flow_str(fp.get(n["key"])),
            "opp": _opp_of(_opp.get(n["key"])),          # optimization candidacy + $ (the "where's the money")
        })
        total_in += n["avg_in"] * n["calls"]
        total_out += n["avg_out"] * n["calls"]
        if cost["per_basis"] is not None:
            total_basis += cost["per_basis"]
        any_unknown = any_unknown or not cost["known"]
    nodes.sort(key=lambda x: (x["cost"]["per_basis"] is None, -(x["cost"]["per_basis"] or 0)))

    # typed non-llm nodes (tool / retriever) — deterministic summary (no prompt to comprehend)
    structural = []
    for s in g.get("structural_nodes", []):
        structural.append({
            "kind": s["type"], "node": s["node"], "key": s["key"], "graph_path": s.get("graph_path", ""),
            "calls": s["calls"], "errors": s.get("errors", 0), "error_rate": s.get("error_rate", 0.0),
            "avg_ms": s.get("avg_ms"), "traces": s.get("traces", 0),
            "op": s["type"], "summary": _struct_summary(s),
            "facts": [], "flow": _flow_str(fp.get(s["key"])),
        })

    total_calls = sum(n["calls"] for n in nodes)
    total_err = sum(n["errors_excluded"] for n in nodes)
    header = {
        "agent": g.get("agent", "agent"), "traces": g.get("traces", 0), "call_sites": len(nodes),
        "tool_count": sum(1 for s in structural if s["kind"] == "tool"),
        "retriever_count": sum(1 for s in structural if s["kind"] == "retriever"),
        "subgraphs": g.get("graphs", []), "revisions": g.get("revisions", []),
        "errors_excluded": g.get("errors_excluded", 0),
        "total_calls": total_calls, "total_in": total_in, "total_out": total_out,
        "cost_basis": CALLS_BASIS, "total_cost_basis": round(total_basis, 2),
        "error_rate": round(total_err / max(1, total_calls + total_err), 3),
        "price_incomplete": any_unknown, "has_comprehension": bool(comp),
        # the OPTIMIZATION MAP totals ($0, deterministic) — the "where's the money" headline
        "opportunity_total": round(sum((r.get("opportunity") or 0) for r in _opp.values()), 2),
        "downgrade_total": round(sum((r.get("downgrade_usd") or 0) for r in _opp.values()), 2),
        "cache_total": round(sum((r.get("cache_usd") or 0) for r in _opp.values()), 2),
        "opp_wins": sum(1 for r in _opp.values() if (r.get("opportunity") or 0) > 0 or r.get("compress_candidate")),
    }

    hotspots = None
    if nodes:                                                        # measured superlatives — no judgment
        busiest = max(nodes, key=lambda x: x["calls"])
        costliest = next((n for n in nodes if n["cost"]["per_basis"] is not None), None)  # sorted costliest-first
        slowest = max(structural, key=lambda s: s.get("avg_ms") or 0) if structural else None
        hotspots = {
            "busiest": {"node": busiest["node"], "calls": busiest["calls"]},
            "costliest": ({"node": costliest["node"], "per_basis": costliest["cost"]["per_basis"]}
                          if costliest else None),
            "slowest": ({"node": slowest["node"], "avg_ms": slowest["avg_ms"]}
                        if slowest and slowest.get("avg_ms") else None),
            "errors_excluded": header["errors_excluded"],
        }

    # SECTIONS — group every node (llm + non-llm) by subgraph; top level first, then subgraphs alpha, llm first.
    by_gp = {}
    for x in nodes + structural:
        by_gp.setdefault(x.get("graph_path") or "", []).append(x)
    order = [""] + sorted(k for k in by_gp if k)
    sections = [{"name": (gp or "top level"), "graph_path": gp,
                 "nodes": sorted(by_gp[gp], key=lambda x: (x["kind"] != "llm",
                                                           -(x.get("cost", {}).get("per_basis") or 0)))}
                for gp in order if gp in by_gp]

    return {"header": header, "deployment": comp.get("_deployment") or {},
            "sections": sections, "hotspots": hotspots,
            "nodes": nodes, "structural": structural, "edges": g.get("edges", [])}
