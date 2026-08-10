"""Isolated probe for the profile layer — runs select -> analyze -> report on EVERY node of one agent and prints the
per-node profiles. No app wiring, no UI. Real PROFILE_MODEL calls (set AUDIT_PROFILE_MODEL=claude-opus-5 for a demo).

    python profile_probe.py [source] [workspace] [project]
    default: recorded  recorded  meridian-support

This validates the profile layer's comprehension on real nodes BEFORE anything touches the app.
"""
import sys

try:                                    # Windows console is cp1252 — recorded traces contain em-dashes etc.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.services import graph
from app.services.profile import select, analyze_node, render


def main():
    source = sys.argv[1] if len(sys.argv) > 1 else "recorded"
    ws = sys.argv[2] if len(sys.argv) > 2 else "recorded"
    project = sys.argv[3] if len(sys.argv) > 3 else "meridian-support"

    g = graph.build(source, ws, project)
    nodes, buckets = g["nodes"], g["buckets"]
    print("AGENT: %s   nodes: %d   traces: %s   errors_excluded: %s\n"
          % (g["agent"], len(nodes), g.get("traces"), g.get("errors_excluded")))

    for node in nodes:
        bucket = buckets.get(node["key"], [])
        if not bucket:
            print("NODE %s — no traces, skipped" % node["node"])
            continue
        selection = select(bucket, n=5)
        profile = analyze_node(node, selection)
        print(render(profile))


if __name__ == "__main__":
    main()
