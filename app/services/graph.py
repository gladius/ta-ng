"""Graph view: the connector's graph (`source.pull_graph`) reshaped into the neutral dict the app services use, so no
connector type leaks upward. Nodes are LLM call-sites with stats, edges the observed flow, graphs the subgraph paths."""
from collections import OrderedDict

from connectors.datasource import get_source


def build(source_id, ws_id, project, limit=150):
    """Fold an agent's last `limit` complete traces (not spans) into {agent, nodes, edges, graphs, buckets}. No LLM.
    The call-site key is `agent/subgraph-path/node`: the connector's content-variant key drifts with the sample, so
    selected keys would go stale before the proof. Variants sharing a key are merged, sample-weighted."""
    g, skipped = get_source(source_id).pull_graph(ws_id, project, limit=limit)

    def _skey(n):
        gp = (n.get("graph_path") or "").strip("/")
        return "%s/%s%s" % (g.agent, gp + "/" if gp else "", n["node"])

    merged, buckets = OrderedDict(), OrderedDict()
    for n in g.nodes:
        sk = _skey(n)
        buckets.setdefault(sk, []).extend(g.buckets.get(n["key"], []))
        s, ai, ao = n["samples"], n["avg_in"], n["avg_out"]
        if sk not in merged:
            merged[sk] = {"node": n["node"], "key": sk, "variant": n.get("variant", ""),
                          "calls": s, "avg_in": ai, "avg_out": ao, "model": n["model"],
                          "models": n.get("models_list") or [{"name": n["model"], "calls": s}],
                          "mixed": n.get("mixed_model", False), "graph_path": n.get("graph_path", ""),
                          "out_type": n.get("out_type"), "untagged": n.get("untagged", False),
                          # measured facts for the profile; its $ comes from tokens x price, cost_ls is a cross-check
                          "cost_ls": n.get("cost_ls", 0) or 0, "errors_excluded": n.get("errors_excluded", 0) or 0,
                          "out_p50": n.get("out_p50", 0), "out_p95": n.get("out_p95", 0),
                          "_in": ai * s, "_out": ao * s, "_tsum": n["traces"], "_maxvar": s,
                          "_ms": (n.get("avg_ms", 0) or 0) * s, "_tools": (n.get("avg_tools", 0) or 0) * s,
                          "_toolin": (n.get("tool_input_rate", 0) or 0) * s,
                          "_ttft_s": (n["ttft_ms"] * s) if n.get("ttft_ms") is not None else 0,
                          "_ttft_n": s if n.get("ttft_ms") is not None else 0,
                          "_fb_s": (n["feedback_score"] * s) if n.get("feedback_score") is not None else 0,
                          "_fb_n": s if n.get("feedback_score") is not None else 0}
        else:
            m = merged[sk]
            m["calls"] += s; m["_in"] += ai * s; m["_out"] += ao * s; m["_tsum"] += n["traces"]
            m["avg_in"] = round(m["_in"] / max(1, m["calls"])); m["avg_out"] = round(m["_out"] / max(1, m["calls"]))
            m["mixed"] = m["mixed"] or n.get("mixed_model", False)
            m["cost_ls"] += n.get("cost_ls", 0) or 0; m["errors_excluded"] += n.get("errors_excluded", 0) or 0
            m["_ms"] += (n.get("avg_ms", 0) or 0) * s; m["_tools"] += (n.get("avg_tools", 0) or 0) * s
            m["_toolin"] += (n.get("tool_input_rate", 0) or 0) * s
            if n.get("ttft_ms") is not None: m["_ttft_s"] += n["ttft_ms"] * s; m["_ttft_n"] += s
            if n.get("feedback_score") is not None: m["_fb_s"] += n["feedback_score"] * s; m["_fb_n"] += s
            if s > m["_maxvar"]:                            # percentiles/out_type from the dominant variant
                m["_maxvar"] = s; m["out_p50"] = n.get("out_p50", 0)
                m["out_p95"] = n.get("out_p95", 0); m["out_type"] = n.get("out_type")
    nodes = list(merged.values())
    for m in nodes:
        # Distinct trace ids: variants of one call-site recur in the same traces, so summed counts double-count.
        distinct = len({t.get("trace_id") for t in buckets.get(m["key"], []) if t.get("trace_id")})
        m["traces"] = distinct if distinct else m["_tsum"]
        c = max(1, m["calls"])
        m["avg_ms"] = round(m["_ms"] / c)
        m["avg_tools"] = round(m["_tools"] / c, 1)
        m["tool_input_rate"] = round(m["_toolin"] / c, 2)
        m["ttft_ms"] = round(m["_ttft_s"] / m["_ttft_n"]) if m["_ttft_n"] else None
        m["feedback_score"] = round(m["_fb_s"] / m["_fb_n"], 3) if m["_fb_n"] else None
        m["error_rate"] = round(m["errors_excluded"] / max(1, m["calls"] + m["errors_excluded"]), 3)
        m["cost_ls"] = round(m["cost_ls"], 5)
        for k in ("_tsum", "_in", "_out", "_maxvar", "_ms", "_tools", "_toolin",
                  "_ttft_s", "_ttft_n", "_fb_s", "_fb_n"):
            m.pop(k, None)
    nodes.sort(key=lambda x: -x["avg_out"])
    structural = list(getattr(g, "structural_nodes", []))                      # typed non-llm nodes (tool/retriever)
    graphs = sorted({n["graph_path"] for n in nodes if n["graph_path"]}
                    | {s["graph_path"] for s in structural if s.get("graph_path")})
    return {"agent": g.agent, "nodes": nodes, "structural_nodes": structural,
            "edges": g.edges, "graphs": graphs, "buckets": buckets,
            "traces": getattr(g, "trace_count", 0), "skipped": len(skipped),
            "errors_excluded": getattr(g, "errors_excluded", 0),               # failed runs dropped from the audit
            "revisions": getattr(g, "revisions", []),
            "run_totals": getattr(g, "run_totals", {}),
            "node_run_types": getattr(g, "node_run_types", {})}
