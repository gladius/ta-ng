"""READ-ONLY agent/graph DIAGNOSTIC — dumps the full fetch -> graph pipeline for ONE agent so you can SEE
exactly why it shows the traces / call-sites / non-llm nodes / edges it does. No writes, no re-runs, no model
calls, no cost. Safe to run against production.

    python -m tools.diag.inspect_agent                              # list workspaces + projects (with run counts)
    python -m tools.diag.inspect_agent --ws <WS_ID> --project <P>   # deep-dive one agent
    python -m tools.diag.inspect_agent --ws <WS_ID> --project <P> --n 150

It reports, from the LIVE data:
  1. RAW FETCH   — traces vs runs, run_type mix, how many llm runs carry langgraph_node metadata (tagged vs
                   untagged), roots present, revisions, and a sample of runs (name / type / node / root?).
  2. GRAPH BUILD — llm call-sites (with the `untagged` flag), structural (tool/retriever) nodes, edges, revisions.
  3. DIAGNOSIS   — plain-language flags for the usual culprits (untagged collapse, no edges, no structural nodes,
                   run-windowed fetch, mixed revisions).
"""
import os
import sys
import argparse
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import credentials
credentials.load()
from connectors.datasource import get_source
from connectors import get_adapter
from tools.diag import _common                               # key + endpoint + workspace all come from .env
try:                                                          # SAME revision keys the graph build uses (incl.
    from connectors.langsmith.adapter import _revision       # LANGSMITH_HOST_REVISION_ID) — not just revision_id
except Exception:
    def _revision(meta):
        for k in ("revision_id", "LANGSMITH_HOST_REVISION_ID", "LANGSMITH_LANGGRAPH_API_REVISION"):
            if meta.get(k):
                return str(meta[k])
        return ""


def _meta(rec):
    return (rec.get("extra") or {}).get("metadata") or {}


def list_all(source):
    src = get_source(source)
    print("source=%s  configured=%s" % (source, src.configured()))
    for w in src.workspaces():
        print("\nworkspace: %s  (%s)" % (w["name"], w["id"]))
        try:
            for a in src.agents(w["id"]):
                print("   - %-36s runs=%s" % (a["name"], a.get("runs")))
        except Exception as e:
            print("   agents() failed:", repr(e)[:160])
    print("\nRe-run with --ws <id> --project <name> to deep-dive one agent.")


def inspect(source, ws, project, n):
    os.environ["LANGSMITH_WORKSPACE_ID"] = ws
    ad = get_adapter(source)

    print("=" * 82)
    print("RAW FETCH   source=%s  project=%s  (target=%d traces)" % (source, project, n))
    print("=" * 82)
    try:
        runs = list(ad.fetch(project=project, traces=n))      # trace-complete (new connector)
    except TypeError:
        runs = list(ad.fetch(project=project, limit=n))       # old connector (no `traces` kw) -> run-window
    if not runs:
        print("  0 runs fetched — project empty, or wrong workspace/name.")
        return
    by_tr = defaultdict(list)
    for r in runs:
        by_tr[r.get("trace_id")].append(r)
    rt = Counter(r.get("run_type") for r in runs)
    llm = [r for r in runs if r.get("run_type") in (None, "llm")]
    tagged = sum(1 for r in llm if _meta(r).get("langgraph_node"))
    roots = [r for r in runs if not r.get("parent_run_id")]
    revs = sorted({_revision(_meta(r)) for r in runs if _revision(_meta(r))})   # LANGSMITH_HOST_REVISION_ID et al.
    print("  traces=%d   runs=%d   (~%.1f runs/trace)" % (len(by_tr), len(runs), len(runs) / max(1, len(by_tr))))
    print("  run_type mix: %s" % dict(rt))
    print("  llm runs=%d   with langgraph_node=%d   => %s"
          % (len(llm), tagged, "TAGGED" if tagged else "UNTAGGED (collapses to 1 call-site)"))
    print("  roots present=%d   revisions=%s" % (len(roots), revs or "(none tagged)"))
    print("\n  sample runs (name · run_type · langgraph_node · is_root):")
    for r in runs[:10]:
        m = _meta(r)
        print("    %-24s %-9s node=%-18s root=%s"
              % ((r.get("name") or "")[:24], r.get("run_type"), m.get("langgraph_node") or "-",
                 not r.get("parent_run_id")))
    print("  root metadata keys:", sorted(_meta(roots[0]).keys()) if roots else "(no root captured)")

    print("\n" + "=" * 82)
    print("GRAPH BUILD   (from the SAME runs — no second fetch)")
    print("=" * 82)
    from connectors.graph import build_graph
    try:
        records = [rec for rec in (ad.to_record(r, project=project) for r in runs) if rec]
        g = build_graph(records, agent_default=project)
    except Exception as e:
        import traceback
        print("  build_graph FAILED:", repr(e)[:200]); traceback.print_exc(); return
    gn, sn, ge = g.nodes, g.structural_nodes, g.edges
    print("  traces=%s   llm call-sites=%d   structural(non-llm) nodes=%d   edges=%d   revisions=%s"
          % (g.trace_count, len(gn), len(sn), len(ge), g.revisions))
    print("  (call-sites shown per content-variant; the app view merges variants of one node)")
    print("\n  LLM call-sites:")
    for nn in gn:
        print("    - %-24s calls=%-5s traces=%-5s untagged=%-5s model=%s"
              % (nn["node"], nn.get("samples"), nn.get("traces"), nn.get("untagged"), nn.get("model")))
    print("\n  structural nodes (tool/retriever):")
    for s in sn:
        print("    - %-24s type=%-9s calls=%s traces=%s" % (s["node"], s["type"], s["calls"], s["traces"]))
    if not sn:
        print("    (none)")
    print("\n  edges (flow, top 20):")
    for e in ge[:20]:
        print("    %s -> %s   x%d" % (e["src"].split("/")[-1], e["dst"].split("/")[-1], e["count"]))
    if not ge:
        print("    (none)")

    print("\n" + "=" * 82)
    print("DIAGNOSIS")
    print("=" * 82)
    probs = []
    if len(by_tr) <= 12:
        probs.append("Only %d traces fetched. If the project has more, this build is RUN-WINDOWED (lacks the "
                     "trace-complete fetch — pull the latest connector) OR the project genuinely has few traces."
                     % len(by_tr))
    if llm and not tagged:
        probs.append("LLM runs carry NO langgraph_node metadata => every llm call collapses into ONE untagged "
                     "call-site. THIS is the '1 call-site'. Fix: tag graph nodes (langgraph_node), or add "
                     "untagged-splitting in the connector.")
    if len(gn) <= 1 and len(llm) > 1:
        probs.append("1 call-site despite %d llm runs -> collapse (see tagging above)." % len(llm))
    if not sn:
        probs.append("No structural (tool/retriever) nodes. Either the agent uses none, or the tool/retriever runs "
                     "have generic/blank names and were filtered. Check the run_type mix + sample names above: is "
                     "there a 'tool'/'retriever' run_type with a real name?")
    if not ge:
        probs.append("No edges. Topology couldn't be built — runs lack dotted_order/parent links, or every node "
                     "resolved to a generic name. Check the 'is_root'/parent info and names above.")
    if len(revs) > 1:
        probs.append("Multiple revisions in the window (%s). The audit's reference set would MIX versions -> pin "
                     "one revision for a coherent audit." % revs)
    if not probs:
        print("  Healthy: multiple tagged call-sites, structural nodes and edges present, single revision.")
    for i, p in enumerate(probs, 1):
        print("  %d. %s" % (i, p))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="langsmith")
    ap.add_argument("--ws")
    ap.add_argument("--project")
    ap.add_argument("--n", type=int, default=150)
    a = ap.parse_args()
    if a.project:
        inspect(a.source, _common.ws_for_project(a.project, a.ws), a.project, a.n)   # ws that holds the agent
    else:
        list_all(a.source)


if __name__ == "__main__":
    main()
