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


def build(source_id, ws_id, project, limit=300):
    """Fold an agent's traces into {agent, nodes, edges, graphs, buckets} via the one rich builder. Read-only, no LLM.

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
                          "calls": s, "traces": n["traces"], "avg_in": ai, "avg_out": ao, "model": n["model"],
                          "models": n.get("models_list") or [{"name": n["model"], "calls": s}],
                          "mixed": n.get("mixed_model", False), "graph_path": n.get("graph_path", ""),
                          "out_type": n.get("out_type"), "untagged": n.get("untagged", False),
                          "_in": ai * s, "_out": ao * s}
        else:                                               # merge a content-variant into the stable call-site
            m = merged[sk]
            m["calls"] += s; m["traces"] += n["traces"]; m["_in"] += ai * s; m["_out"] += ao * s
            m["avg_in"] = round(m["_in"] / max(1, m["calls"])); m["avg_out"] = round(m["_out"] / max(1, m["calls"]))
            m["mixed"] = m["mixed"] or n.get("mixed_model", False)
    nodes = list(merged.values())
    for m in nodes:
        m.pop("_in", None); m.pop("_out", None)
    nodes.sort(key=lambda x: -x["avg_out"])
    graphs = sorted({n["graph_path"] for n in nodes if n["graph_path"]})
    return {"agent": g.agent, "nodes": nodes, "edges": g.edges, "graphs": graphs,
            "buckets": buckets, "skipped": len(skipped)}
