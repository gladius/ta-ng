"""Adapter base + the generic pull()/grouping shared by every connector.

An Adapter turns one observability platform into Token Auditor traces:

    fetch(**opts)            -> iterable of raw records (dicts or SDK objects)
    to_trace(record, **opts) -> a contract trace dict (via schema.build_trace), or None to skip

Adding a platform = one SUBPACKAGE (connectors/<name>/) exporting ADAPTER; connectors.registry
auto-discovers it. Grouping into buckets (one per call-site) is generic and lives here.
"""

import os
import json
import inspect

from contract import validate_trace
from connectors.graph import build_graph


class Adapter:
    name = "base"

    def fetch(self, **opts):
        raise NotImplementedError

    def to_trace(self, record, **opts):
        raise NotImplementedError

    def to_record(self, rec, **opts):
        """Map ONE raw platform record to a neutral run-record for the graph builder. Default: wrap
        to_trace() as a flat, tree-less llm record — so a connector that only knows llm spans (no run
        tree) still works; it just gets no topology. Override to emit the full tree (see LangSmith)."""
        t = self.to_trace(rec, **opts)
        if t is None:
            return None
        return {"id": t.get("trace_id"), "trace_id": t.get("trace_id"),
                "parent_run_id": None, "dotted_order": "", "run_type": "llm",
                "name": t.get("node_id") or "llm", "start_time": t.get("start_time", ""), "end_time": "",
                "agent_hint": t.get("agent_id"), "node_hint": t.get("node_id") or None, "trace": t}

    # -- co-located resources: a connector carries its own sample data / fixtures in its folder --
    def asset(self, name):
        """Absolute path to a file next to this adapter's module (portable, self-contained)."""
        return os.path.join(os.path.dirname(inspect.getfile(type(self))), name)

    def sample(self, name="sample_runs.json"):
        """Load this connector's own offline sample runs (for demos/tests); None if absent."""
        path = self.asset(name)
        return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else None


def _build(adapter, skip_invalid, opts):
    """fetch -> to_record -> build the agent execution graph. Returns (graph, skipped).

    An invalid llm sample (e.g. empty output) is dropped from the profile buckets but its record is KEPT
    for structure (tree walks + node ordering) so one unusable call never breaks topology. An 'unknown
    model' is a warning (kept; models.json handles it), not a drop.
    """
    records, skipped = [], []
    for rec in adapter.fetch(**opts):
        r = adapter.to_record(rec, **opts)
        if not r:
            continue
        t = r.get("trace")
        if t is not None:
            problems = [p for p in validate_trace(t) if not p.startswith("unknown model")]
            if problems and skip_invalid:
                skipped.append((t.get("trace_id"), problems))
                r["skip"] = problems                     # excluded from buckets, still used for structure
        records.append(r)
    return build_graph(records, agent_default=opts.get("project") or "agent"), skipped


def pull(adapter, *, skip_invalid=True, **opts):
    """Run an adapter end to end and return (buckets, skipped) — buckets are the graph's call-sites,
    OrderedDict[call-site key -> list[trace]]. Same shape the profiler/audit already consume."""
    graph, skipped = _build(adapter, skip_invalid, opts)
    return graph.buckets, skipped


def pull_graph(adapter, *, skip_invalid=True, **opts):
    """Same pull, but return the full Graph (call-sites + edges + timing/volume) for the graph view."""
    return _build(adapter, skip_invalid, opts)          # (graph, skipped)
