"""READ-ONLY LangSmith QUERY tool — reuses the app's own connector + adapter (same creds, endpoint, tenant).
For production diagnosis: how many traces & runs exist per agent, the run_type mix, distinct revisions (versions)
and nodes, and the time span. No writes, no re-runs, no model calls, no cost.

    python -m tools.diag.ls_query                              # OVERVIEW: every workspace -> project -> trace count
    python -m tools.diag.ls_query --ws <WS> --project "<P>"    # DEEP: counts for one agent
    python -m tools.diag.ls_query --ws <WS> --project "<P>" --runs 4000   # also tally runs (capped; slower)

Counts are exact up to a cap; past the cap they read ">=cap" (a huge project is never fully walked here).
"""
import os
import sys
import argparse
from collections import Counter

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
try:
    from connectors.langsmith.adapter import _revision
except Exception:
    def _revision(meta):
        for k in ("revision_id", "LANGSMITH_HOST_REVISION_ID", "LANGSMITH_LANGGRAPH_API_REVISION"):
            if meta.get(k):
                return str(meta[k])
        return ""


def _client():
    return _common.client()                                  # key + endpoint from .env, same as the connector


def _count(it, cap):
    n = 0
    for _ in it:
        n += 1
        if n >= cap:
            return n, True
    return n, False


def overview(source, cap):
    src = get_source(source)
    print("source=%s  configured=%s\n" % (source, src.configured()))
    for w in src.workspaces():
        os.environ["LANGSMITH_WORKSPACE_ID"] = w["id"]
        print("workspace: %s  (%s)" % (w["name"], w["id"]))
        try:
            agents = src.agents(w["id"])
        except Exception as e:
            print("   agents() failed:", repr(e)[:160]); continue
        c = _client()
        for a in agents:
            try:
                n, capped = _count(c.list_runs(project_name=a["name"], is_root=True, select=["id"]), cap)
                print("   - %-38s traces=%s%s" % (a["name"], n, "+" if capped else ""))
            except Exception as e:
                print("   - %-38s traces=? (%s)" % (a["name"], repr(e)[:80]))
    print("\nRe-run with --ws <id> --project <name> for a deep count of one agent.")


def deep(source, ws, project, cap, run_cap):
    os.environ["LANGSMITH_WORKSPACE_ID"] = ws
    ad = get_adapter(source)
    c = _client()
    print("=" * 78)
    print("AGENT: %s   (ws=%s)" % (project, ws))
    print("=" * 78)

    traces, tcap = (ad.available_traces(project, cap=cap) if hasattr(ad, "available_traces")
                    else _count(c.list_runs(project_name=project, is_root=True, select=["id"]), cap))
    print("Traces (root runs): %s%s" % (traces, "  (capped)" if tcap else ""))

    if not run_cap:
        print("(pass --runs N to also tally runs / run_type / revisions / nodes)")
        return

    rt, revs, nodes, first, last, n = Counter(), Counter(), Counter(), "", "", 0
    capped = False
    for r in c.list_runs(project_name=project, select=["id", "run_type", "start_time", "extra"]):
        n += 1
        rt[r.run_type] += 1
        meta = (getattr(r, "extra", None) or {}).get("metadata") or {}
        rv = _revision(meta)
        if rv:
            revs[rv] += 1
        nd = meta.get("langgraph_node")
        if nd:
            nodes[nd] += 1
        st = str(getattr(r, "start_time", "") or "")
        if st:
            first = min(first or st, st); last = max(last, st)
        if n >= run_cap:
            capped = True
            break
    ratio = ("   (~%.1f runs/trace)" % (n / traces)) if (traces and not capped) else ""
    print("Runs tallied: %s%s%s" % (n, "  (capped — ratio N/A)" if capped else "", ratio))
    print("run_type mix: %s" % dict(rt))
    print("time span: %s  ->  %s" % (first or "?", last or "?"))
    print("distinct revisions (versions): %d" % len(revs))
    for rv, cnt in revs.most_common(10):
        print("   - %s   x%d" % (rv, cnt))
    print("distinct langgraph_node: %d" % len(nodes))
    for nd, cnt in nodes.most_common(20):
        print("   - %-28s x%d" % (nd, cnt))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="langsmith")
    ap.add_argument("--ws")
    ap.add_argument("--project")
    ap.add_argument("--cap", type=int, default=5000, help="max root runs to count for a trace total")
    ap.add_argument("--runs", type=int, nargs="?", const=4000, default=0, help="also tally up to N runs")
    a = ap.parse_args()
    if a.project:
        deep(a.source, _common.ws_for_project(a.project, a.ws), a.project, a.cap, a.runs)   # ws that holds the agent
    else:
        overview(a.source, a.cap)


if __name__ == "__main__":
    main()
