"""Revision scoping — the audit fetch is LATEST-ONLY: it hard-filters roots to exactly ONE revision (the newest
root's, i.e. what's serving now) so the reference set can never mix code versions (a mixed set makes the downgrade
judge unsound). An explicit revision pins a specific one instead. Deterministic, $0, no network.
Run: python -m tests.test_revision_scope  (from repo root)."""
from types import SimpleNamespace as NS

from connectors.langsmith.adapter import _scope_to_latest

LATEST = "64b4ff50-dd11-4629-b870-bd4763304a54"
MID = "ffc4552c-9173-4c27-a4d2-905e499d825f"
OLD = "dea73876-4cdc-4560-87a5-6cc7349a5add"


def _root(rev):
    """A root run object as list_runs returns it — revision lives in extra.metadata.LANGSMITH_HOST_REVISION_ID."""
    return NS(trace_id="t", extra={"metadata": {"LANGSMITH_HOST_REVISION_ID": rev}})


def _revs(roots):
    return {r.extra["metadata"]["LANGSMITH_HOST_REVISION_ID"] for r in roots}


def test_auto_latest_keeps_only_newest_revision():
    # newest-first, with OLDER revisions INTERLEAVED among the latest -> the filter must drop every non-latest one.
    roots = ([_root(LATEST)] * 6) + [_root(MID)] + ([_root(OLD)] * 5) + [_root(MID)]
    scoped, target = _scope_to_latest(roots, 150)
    assert target == LATEST, "the newest root's revision is the target"
    assert _revs(scoped) == {LATEST}, "ONLY the latest revision survives — no mixing"
    assert len(scoped) == 6, "all six latest-revision roots kept, the interleaved older ones dropped"
    print("[ok] auto-latest keeps exactly one revision, drops interleaved older traces")


def test_explicit_revision_pins_that_one():
    roots = ([_root(LATEST)] * 6) + ([_root(OLD)] * 5)
    scoped, target = _scope_to_latest(roots, 150, revision=OLD)
    assert target == OLD and _revs(scoped) == {OLD} and len(scoped) == 5
    print("[ok] explicit revision pins exactly that revision")


def test_n_is_a_ceiling_not_a_demand():
    roots = [_root(LATEST)] * 6
    scoped, _ = _scope_to_latest(roots, 3)
    assert len(scoped) == 3, "n caps the count; a thin revision is never padded from elsewhere"
    print("[ok] n is a ceiling — thin stays thin, never padded")


def test_empty_project():
    scoped, target = _scope_to_latest([], 150)
    assert scoped == [] and target is None
    print("[ok] empty project -> no traces, no revision (caller shows the no-traces call-out)")


if __name__ == "__main__":
    test_auto_latest_keeps_only_newest_revision()
    test_explicit_revision_pins_that_one()
    test_n_is_a_ceiling_not_a_demand()
    test_empty_project()
    print("\nall revision-scope tests passed")
