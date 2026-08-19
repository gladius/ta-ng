"""Identify the TRUE 'agent version' to pin for a LangGraph Platform (hosted / LangSmith Enterprise) agent, so a
downgrade audit compares one coherent version instead of a smear of them. READ-ONLY.

Two phases:
  1. VERSION-FIELD MATRIX — for each version-candidate metadata field, the distinct values across the agent's traces
     and how they spread over TIME. This shows which field is the stable identity (graph / assistant) and which one
     actually changes on deploy (the version). If a field changes every ~2 traces it's probably infra, not code.
  2. REVISION -> COMMIT (best effort) — resolves each deployment revision (LANGSMITH_HOST_REVISION_ID) to its
     repo_commit_sha via the LangGraph Control Plane API. THE GROUND TRUTH: if N revisions map to N distinct commits
     they are real versions (pin by them); if they map to the SAME commit it's rebuilds/restarts (do NOT fragment).

    python -m tools.diag.revisions --ws <WS_ID> --project "<AGENT>"
    python -m tools.diag.revisions --ws <WS_ID> --project "<AGENT>" --control-plane https://<control-plane-host>
"""
import os
import sys
import json
import argparse
import urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from tools.diag import _common                               # key + endpoint + workspace all come from .env

# Ordered by how likely each is to be the CODE version. HOST_REVISION_ID = deployment revision (-> a commit);
# assistant_id/graph_id = the deployed agent identity; langgraph_*version = the LIBRARY version (too coarse).
CANDIDATES = ["LANGSMITH_HOST_REVISION_ID", "LANGSMITH_LANGGRAPH_API_REVISION", "revision_id",
              "assistant_id", "graph_id", "LANGSMITH_HOST_PROJECT_ID",
              "langgraph_version", "langgraph_api_version"]


def _meta(r):
    return (getattr(r, "extra", None) or {}).get("metadata") or {}


def _get_json(url, ws):
    req = urllib.request.Request(url, headers={"X-Api-Key": _common.key(), "X-Tenant-Id": ws})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def matrix(roots):
    print("\n=== VERSION-FIELD MATRIX  (distinct values + time spread over %d traces) ===" % len(roots))
    for f in CANDIDATES:
        vals = defaultdict(list)
        for r in roots:
            v = _meta(r).get(f)
            if v is not None:
                vals[str(v)].append(str(getattr(r, "start_time", "") or ""))
        if not vals:
            print("\n%-32s (absent)" % f)
            continue
        tag = "STABLE (one value)" if len(vals) == 1 else "VARIES → %d values" % len(vals)
        print("\n%-32s %s" % (f, tag))
        for v, ts in sorted(vals.items(), key=lambda kv: -len(kv[1]))[:8]:
            print("    %-40s x%-3d   %s .. %s" % (v[:40], len(ts), (min(ts) or "?")[:19], (max(ts) or "?")[:19]))


def resolve(roots, ws, control_plane):
    dep = next((_meta(r).get("LANGSMITH_HOST_PROJECT_ID") for r in roots if _meta(r).get("LANGSMITH_HOST_PROJECT_ID")), None)
    revs = sorted({_meta(r).get("LANGSMITH_HOST_REVISION_ID") for r in roots if _meta(r).get("LANGSMITH_HOST_REVISION_ID")})
    base = (control_plane or _common.endpoint()).rstrip("/")
    print("\n=== REVISION → COMMIT  (LangGraph Control Plane) ===")
    print("control-plane base: %s   deployment_id (LANGSMITH_HOST_PROJECT_ID): %s   revisions: %d" % (base, dep, len(revs)))
    if not dep or not revs:
        print("(no deployment_id or revisions in metadata — can't resolve; the matrix above still identifies the field)")
        return
    try:                                                          # one LIST call is cheaper than N GETs
        data = _get_json("%s/v2/deployments/%s/revisions" % (base, dep), ws)
        items = data.get("resources", data if isinstance(data, list) else [])
        by_id = {str(x.get("id")): x for x in items}
        print("listed %d revisions from the control plane\n" % len(items))
        commits = set()
        for rev in revs:
            x = by_id.get(str(rev)) or {}
            src = x.get("source_revision_config") or {}
            commit = src.get("repo_commit_sha") or x.get("repo_commit_sha") or "(unknown)"
            commits.add(commit)
            print("  revision %s  →  commit %s   created %s   status %s"
                  % (str(rev)[:12], str(commit)[:12], str(x.get("created_at"))[:19], x.get("status")))
        print("\n>>> distinct COMMITS behind the %d revisions: **%d**" % (len(revs), len(commits)))
        if len(commits) <= 1:
            print(">>> Same commit → the CODE never changed; those revisions are rebuilds/restarts. Do NOT fragment — "
                  "audit all traces together, or pin by commit (one group).")
        else:
            print(">>> Distinct commits → real code versions. Pin the audit to ONE (e.g. the latest revision's commit).")
    except Exception as e:
        print("control-plane resolve failed: %s" % (repr(e)[:200]))
        print("(pass --control-plane <host> if the API lives on a different host than LANGSMITH_ENDPOINT; the matrix "
              "above still tells you which field to pin)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default=None, help="workspace id (default: LANGSMITH_WORKSPACE_ID from .env)")
    ap.add_argument("--project", required=True)
    ap.add_argument("--control-plane", default=None, help="Control Plane base URL if it differs from LANGSMITH_ENDPOINT")
    ap.add_argument("--n", type=int, default=300, help="max root runs (traces) to scan — bounds the one query")
    a = ap.parse_args()
    ws = _common.ws_for_project(a.project, a.ws)                  # --ws, else the ws that actually holds the agent
    roots = []                                                    # roots ONLY (ids+metadata) — light; capped at --n
    for r in _common.client().list_runs(project_name=a.project, is_root=True,
                                        select=["id", "trace_id", "start_time", "extra"]):
        roots.append(r)
        if len(roots) >= a.n:
            break
    if not roots:
        print("no traces (root runs) found — check ws/project.")
        return
    matrix(roots)
    resolve(roots, ws, a.control_plane)


if __name__ == "__main__":
    main()
