"""Ingestion/graph hygiene — a FAILED sample (errored / empty output) is kept OUT of the audited bucket but
COUNTED, a RECOVERED error (error + real output) is kept, and the agent version (revision_id) is captured.
Deterministic, $0, no network. Run: python -m tests.test_graph_ingest  (from repo root)."""
from connectors.graph import build_graph


def _rec(tid, out, err="", rev="", node="triage"):
    """A neutral llm run-record carrying a contract trace — the shape build_graph folds."""
    return {"id": tid, "trace_id": tid, "parent_run_id": None, "dotted_order": "",
            "run_type": "llm", "name": "n", "start_time": "", "end_time": "",
            "agent_hint": "a", "node_hint": node,
            "trace": {"trace_id": tid, "agent_id": "a", "node_id": node,
                      "input_messages": [{"role": "user", "content": "hi"}],
                      "output": out, "error": err, "revision": rev,
                      "usage": {"input_tokens": 10, "output_tokens": 5}, "model": "m", "graph_path": ""}}


def test_failed_samples_excluded_but_counted():
    # trace 3 errored with NO output -> excluded from the audited bucket, but counted in errors_excluded so the
    # $-figure/verdict never rests on a non-decision, and the honesty count survives.
    recs = [_rec("1", "ok1", rev="sha1"), _rec("2", "ok2", rev="sha1"),
            _rec("3", "", err="boom", rev="sha2"), _rec("4", "ok4", rev="sha2")]
    g = build_graph(recs, agent_default="a")
    node = g.nodes[0]
    assert node["samples"] == 3, "only the 3 successful samples define the bucket"
    assert sum(len(v) for v in g.buckets.values()) == 3, "the errored sample is NOT in the audited bucket"
    assert node["errors_excluded"] == 1 and g.errors_excluded == 1
    assert node["error_rate"] == 0.25, "1 failed of 4 TOTAL samples"
    print("[ok] failed sample excluded from the audited bucket, counted (error_rate over ALL samples)")


def test_recovered_error_with_output_is_kept():
    # an 'error' that STILL produced a real output (the agent recovered) is valid audit signal -> KEPT.
    recs = [_rec("1", "ok", rev="sha1"), _rec("2", "recovered", err="transient", rev="sha1")]
    g = build_graph(recs, agent_default="a")
    assert g.nodes[0]["samples"] == 2 and g.errors_excluded == 0
    print("[ok] recovered error (error + real output) is kept, not dropped")


def test_revision_captured_distinct_sorted():
    # agent version (revision_id) is carried onto the trace and surfaced distinct + sorted; a blank one is ignored.
    recs = [_rec("1", "ok", rev="sha2"), _rec("2", "ok", rev="sha1"), _rec("3", "ok", rev="")]
    g = build_graph(recs, agent_default="a")
    assert g.revisions == ["sha1", "sha2"], g.revisions
    print("[ok] agent versions captured — distinct, sorted, blank ignored")


def test_same_name_nodes_in_different_subgraphs_split():
    # Two subgraphs each with a node literally named 'worker' (SAME prompt shape) are DIFFERENT call-sites, not one
    # blended bucket. The structural graph_path disambiguates them; without it the audit proves one verdict across
    # two positions and books all $ under a mislabeled node.
    recs = [_rec("1", "ok", node="worker"), _rec("2", "ok", node="worker"),
            _rec("3", "ok", node="worker"), _rec("4", "ok", node="worker")]
    recs[0]["trace"]["graph_path"] = "billing"; recs[1]["trace"]["graph_path"] = "billing"
    recs[2]["trace"]["graph_path"] = "refund";  recs[3]["trace"]["graph_path"] = "refund"
    g = build_graph(recs, agent_default="a")
    paths_per_bucket = {k: sorted({t.get("graph_path") for t in v}) for k, v in g.buckets.items()}
    assert len(g.buckets) == 2, "same-name nodes in 2 subgraphs must be 2 buckets, got %d: %s" % (
        len(g.buckets), paths_per_bucket)
    assert all(len(p) == 1 for p in paths_per_bucket.values()), "no bucket may span >1 graph_path: %s" % paths_per_bucket
    # and a FLAT node (no graph_path) still keys exactly as before — the fix is a no-op without path info
    flat = build_graph([_rec("1", "ok"), _rec("2", "ok")], agent_default="a")
    assert list(flat.buckets) == ["a/triage"], flat.buckets
    print("[ok] same-name nodes in different subgraphs split by graph_path; flat nodes unchanged")


if __name__ == "__main__":
    test_failed_samples_excluded_but_counted()
    test_recovered_error_with_output_is_kept()
    test_revision_captured_distinct_sorted()
    test_same_name_nodes_in_different_subgraphs_split()
    print("\nALL INGEST-HYGIENE TESTS PASSED ($0, no network)")
