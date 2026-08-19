"""Graph VIEW — the app-facing call-site + flow shape, from the ONE grouping brain (connectors/graph.py).

There is a single graph builder: the connector's tree-folder (`source.pull_graph`), which resolves node
identity from the run tree, splits a node by prompt shape (ReAct pre/post-tool are different call-sites),
and computes real node->node edges. This module does NOT re-group; it just reshapes that builder's output
into the neutral dict the app services (funnel, downgrade, audit, cli) render — so no connector type leaks
upward and every consumer keeps one stable shape.

  nodes = LLM call-sites with core stats (model, calls, avg tokens, graph_path, out_type)
  edges = observed node->node flow (most-travelled path)
  graphs = distinct subgraph paths (select / rollup by graph for multi-agent)
"""
from collections import OrderedDict

from connectors.datasource import get_source


def build(source_id, ws_id, project, limit=150):
    """Fold an agent's traces into {agent, nodes, edges, graphs, buckets} via the one rich builder. Read-only, no LLM.
    `limit` = the number of COMPLETE traces to pull — the connector fetches whole trace trees (roots, then all their
    runs), NOT a run-window, so N here means N traces, not N spans (a fat agent is ~10-40 runs per trace).

    Call-site identity is STABLE and sample-independent: `agent / subgraph-path / node-label` (all metadata-derived).
    NOT the connector's content-variant key (`...~<frame>`), which is derived by clustering the fetched traces and
    therefore DRIFTS when the live sample changes between a page load and the prove call — the selected keys would go
    stale, nothing would prove, and every $ would reset to 0. A stable key also disambiguates same-named nodes across
    subgraphs (two 'supervisor's in different graphs) without depending on content. Connector variants that map to the
    same stable key (e.g. a ReAct node's pre/post-tool split) are merged, sample-weighted."""
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
                          # MEASURED facts (were computed in connectors/graph but dropped here) — surfaced for
                          # the profile. Summed/weighted across content-variants below; the profile computes $
                          # from tokens x price (auditor.util), not from cost_ls (kept only as a cross-check).
                          "cost_ls": n.get("cost_ls", 0) or 0, "errors_excluded": n.get("errors_excluded", 0) or 0,
                          "out_p50": n.get("out_p50", 0), "out_p95": n.get("out_p95", 0),
                          "_in": ai * s, "_out": ao * s, "_tsum": n["traces"], "_maxvar": s,
                          "_ms": (n.get("avg_ms", 0) or 0) * s, "_tools": (n.get("avg_tools", 0) or 0) * s,
                          "_toolin": (n.get("tool_input_rate", 0) or 0) * s,
                          "_ttft_s": (n["ttft_ms"] * s) if n.get("ttft_ms") is not None else 0,
                          "_ttft_n": s if n.get("ttft_ms") is not None else 0,
                          "_fb_s": (n["feedback_score"] * s) if n.get("feedback_score") is not None else 0,
                          "_fb_n": s if n.get("feedback_score") is not None else 0}
        else:                                               # merge a content-variant into the stable call-site
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
        # DISTINCT traces from the merged bucket. Variants of ONE call-site (a ReAct node's pre/post-tool calls)
        # recur in the SAME traces, so summing per-variant counts double-counts (handler read 22 across only 12
        # traces). Count distinct trace_ids in the bucket; fall back to the summed count only if traces carry no id.
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
                    | {s["graph_path"] for s in structural if s.get("graph_path")})   # subgraphs across ALL node types
    return {"agent": g.agent, "nodes": nodes, "structural_nodes": structural,
            "edges": g.edges, "graphs": graphs, "buckets": buckets,
            "traces": getattr(g, "trace_count", 0), "skipped": len(skipped),    # sample size = # of traces
            "errors_excluded": getattr(g, "errors_excluded", 0),               # failed runs dropped from the audit
            "revisions": getattr(g, "revisions", []),                          # agent versions seen (pin/select later)
            "run_totals": getattr(g, "run_totals", {}),                        # run_type -> count over ALL runs (full)
            "node_run_types": getattr(g, "node_run_types", {})}                # per-node run_type census over ALL runs
