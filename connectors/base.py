"""Adapter base and the generic pull()/grouping shared by every connector. An Adapter needs fetch(**opts) -> raw
records and to_trace(record, **opts) -> a contract trace, or None to skip."""

from contract import validate_trace
from connectors.graph import build_graph


class Adapter:
    name = "base"

    def fetch(self, **opts):
        raise NotImplementedError

    def to_trace(self, record, **opts):
        raise NotImplementedError

    def to_record(self, rec, **opts):
        """One raw record -> a run-record for the graph builder. The default wraps to_trace() as a flat llm record
        (no topology); override to emit the run tree."""
        t = self.to_trace(rec, **opts)
        if t is None:
            return None
        return {"id": t.get("trace_id"), "trace_id": t.get("trace_id"),
                "parent_run_id": None, "dotted_order": "", "run_type": "llm",
                "name": t.get("node_id") or "llm", "start_time": t.get("start_time", ""), "end_time": "",
                "agent_hint": t.get("agent_id"), "node_hint": t.get("node_id") or None, "trace": t}


def _build(adapter, skip_invalid, opts):
    """fetch -> to_record -> execution graph; returns (graph, skipped). An invalid sample leaves the buckets but
    keeps its record, so one bad call never breaks topology. An unknown model is only a warning."""
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
                r["skip"] = problems
        records.append(r)
    return build_graph(records, agent_default=opts.get("project") or "agent"), skipped


def pull(adapter, *, skip_invalid=True, **opts):
    """Run an adapter end to end; returns (buckets, skipped), buckets = OrderedDict[call-site key -> list[trace]]."""
    graph, skipped = _build(adapter, skip_invalid, opts)
    return graph.buckets, skipped


def pull_graph(adapter, *, skip_invalid=True, **opts):
    """Same pull, returning (Graph, skipped) for the graph view."""
    return _build(adapter, skip_invalid, opts)
