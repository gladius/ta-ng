"""Thin CLI over the SAME nav service the web pages use — verify the flow with zero server plumbing.

    python cli.py sources                    # data sources + configured status
    python cli.py workspaces <source>        # workspaces in a source
    python cli.py agents <source> <ws_id>    # agents/projects in a workspace
"""
import sys

from app.services import nav


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "sources":
        for d in nav.datasources():
            print("  %-12s %s" % (d["id"], "configured" if d["configured"] else "no credentials"))
    elif cmd == "workspaces" and len(argv) >= 2:
        rows = nav.workspaces(argv[1])
        print("  %d workspace(s):" % len(rows))
        for w in rows:
            print("    %-40s %s" % (w["id"], w["name"]))
    elif cmd == "agents" and len(argv) >= 3:
        rows = nav.agents(argv[1], argv[2])
        print("  %d agent(s):" % len(rows))
        for a in rows:
            print("    %-28s runs=%-6s %s" % (a["name"], a.get("runs"), a["id"]))
    elif cmd == "graph" and len(argv) >= 4:
        from app.services import graph
        g = graph.build(argv[1], argv[2], argv[3])          # graph <source> <ws_id> <agent>
        print("agent %s" % g["agent"])
        print("\nNODES (call-sites) — %d:" % len(g["nodes"]))
        print("  %-20s %6s %6s %8s %8s   %s" % ("node", "calls", "traces", "avg_in", "avg_out", "model(s)"))
        for n in g["nodes"]:
            ms = ", ".join("%s x%d" % (m["name"], m["calls"]) for m in n["models"])
            print("  %-20s %6d %6d %8d %8d   %s%s"
                  % (n["node"][:20], n["calls"], n["traces"], n["avg_in"], n["avg_out"], ms,
                     "  [MIXED]" if n["mixed"] else ""))
        print("\nEDGES (flow, by start-time order) — %d:" % len(g["edges"]))
        for e in g["edges"]:
            print("  %-20s -> %-20s  x%d" % (e["src"][:20], e["dst"][:20], e["count"]))
    elif cmd == "downgrade" and len(argv) >= 4:
        from app.services import graph, downgrade
        g = graph.build(argv[1], argv[2], argv[3])
        cands = downgrade.candidates(g["nodes"])
        print("agent %s — downgrade candidates (detected, NOT yet proven; $ per 1000 calls):\n" % g["agent"])
        if not cands:
            print("  none (every single-model node is already cheapest, or too cheap to matter).")
        for c in cands:
            print("  %-18s %-22s -> %-18s  avg_out=%-4d  ~$%.2f / 1k" %
                  (c["node"][:18], c["model"], c["cheaper"], c["avg_out"], c["usd"]))
        mixed = [n["node"] for n in g["nodes"] if n.get("mixed")]
        if mixed:
            print("\n  skipped (mixed/routed — routing audit, later): %s" % ", ".join(mixed))
        print("\n  next: re-run the cheaper tier on diverse inputs + judge drift -> PRESERVED/DRIFT (the proof).")
    elif cmd == "downgrade-audit" and len(argv) >= 5:
        from connectors.datasource import get_source
        from app.services import audit
        buckets, _ = get_source(argv[1]).pull(argv[2], argv[3], limit=200)      # <source> <ws> <agent> <node>
        node, bucket = None, None
        for key, b in buckets.items():
            if argv[4] in key:
                node, bucket = key.split("/")[-1], b
                break
        if not bucket:
            print("node %r not found. available: %s" % (argv[4], [k.split("/")[-1] for k in buckets]))
        else:
            print("auditing %s (%d recorded traces) — temp=0 head-to-head on the cheaper tier + judge ...\n" % (node, len(bucket)))
            r = audit.audit_node(node, bucket, n=5)
            print("  %s : %s -> %s   %s   (%d/%d preserved)" %
                  (r["node"], r.get("model"), r.get("cheaper", "-"), r["verdict"], r.get("preserved", 0), r.get("n", 0)))
            if r.get("reason"):
                print("    reason: %s" % r["reason"])
            for s in r.get("samples", []):
                print("    [%-5s via %-13s] %s | A=%r  B=%r" %
                      ("keep" if s["preserved"] else "DRIFT", s["method"], s["reason"], s["orig"], s["cand"]))
    else:
        print("usage: python cli.py sources | workspaces <source> | agents <source> <ws_id> | graph <ws_id> <agent>")


if __name__ == "__main__":
    main(sys.argv[1:])
