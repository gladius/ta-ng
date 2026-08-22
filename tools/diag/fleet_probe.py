"""READ-ONLY probe: where does deep-agent / Fleet sub-agent identity live? (it is NOT in langgraph_node)

Fleet (LangSmith no-code Agent Builder) runs on `deepagents` / the LangGraph Platform. In its traces EVERY llm
run is tagged `langgraph_node="model"` and ~91% of runs are `*Middleware.*` chain plumbing, so our langgraph_node
grouping collapses to 0 call-sites. This tool proves the identity IS recoverable one layer up: the sub-agent name
is the chain run directly under the nearest `task` tool ancestor (mirrored in `langgraph_checkpoint_ns` nesting).
Reuses the app's connector + .env creds, exactly like the other diag tools. No writes, no model calls, no cost.

Full findings + the connector-resume kit: FLEET-DEEPAGENT-TRACES.md at the repo root.

    python -m tools.diag.fleet_probe                                  # project 'fleet' (the shared Fleet project)
    python -m tools.diag.fleet_probe --project fleet --n 5
    python -m tools.diag.fleet_probe --ws <WS_ID> --project <P> --n 5
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
from connectors import get_adapter
from tools.diag import _common                               # key + endpoint + workspace all come from .env


def _meta(r):
    return (r.get("extra") or {}).get("metadata") or {}


def _id(r):
    return r.get("id") or r.get("run_id")


def _subagent_of(chain):
    """Derive the deep-agent call-site from a run's parent-name chain (closest-first). The sub-agent name is the
    chain run immediately BELOW the nearest `task` tool ancestor; an llm with no `task` ancestor is the orchestrator.
    Heuristic (validated on the `fleet` project) — the real connector should key on this + checkpoint_ns nesting."""
    for i, c in enumerate(chain):
        if c.startswith("task") and c.endswith("[tool]"):
            return chain[i - 1].split("[")[0] if i > 0 else "(task, unnamed)"
    return "(orchestrator)"


def probe(source, ws, project, n):
    os.environ["LANGSMITH_WORKSPACE_ID"] = ws
    ad = get_adapter(source)
    runs = list(ad.fetch(project=project, traces=n))
    if not runs:
        print("0 runs fetched — empty project or wrong workspace/name.")
        return
    byid = {_id(r): r for r in runs if _id(r)}

    def parents(r):
        chain, seen = [], set()
        p = r.get("parent_run_id")
        while p and p in byid and p not in seen:
            seen.add(p); pr = byid[p]
            chain.append("%s[%s]" % (pr.get("name"), pr.get("run_type")))
            p = pr.get("parent_run_id")
        return chain

    llm = [r for r in runs if r.get("run_type") == "llm"]
    traces = len({r.get("trace_id") for r in runs})
    chains = sum(1 for r in runs if r.get("run_type") == "chain")
    mids = sum(1 for r in runs if r.get("run_type") == "chain" and "Middleware" in (r.get("name") or ""))
    print("project=%s  traces=%d  runs=%d  (llm=%d, chain=%d, middleware=%d => %.0f%% noise)"
          % (project, traces, len(runs), len(llm), chains, mids, 100 * mids / max(1, len(runs))))

    derived = Counter(_subagent_of(parents(r)) for r in llm)
    print("\nDERIVED call-sites (sub-agent from nearest task-ancestor) — what the connector SHOULD group on:")
    for name, c in derived.most_common():
        print("   - %-30s llm_calls=%d" % (name, c))

    print("\nper-llm detail (first 24):")
    for i, r in enumerate(llm[:24], 1):
        m = _meta(r)
        print("   llm %2d  callsite=%-28s node=%-7s ns=%s"
              % (i, _subagent_of(parents(r)), m.get("langgraph_node"),
                 (m.get("langgraph_checkpoint_ns") or "")[:70]))

    tools = [r for r in runs if r.get("run_type") == "tool"]
    print("\nTOOL names (clean, by run.name):", dict(Counter(r.get("name") for r in tools)))
    print("Note: our langgraph_node grouping yields 0 usable call-sites here — identity is in the tree, not the label.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="langsmith")
    ap.add_argument("--ws")
    ap.add_argument("--project", default="fleet")
    ap.add_argument("--n", type=int, default=5)
    a = ap.parse_args()
    probe(a.source, _common.ws_for_project(a.project, a.ws), a.project, a.n)   # ws that holds the project


if __name__ == "__main__":
    main()
