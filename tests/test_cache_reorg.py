"""Cache-prefix reorg — deterministic verdict wiring + apply, with the paid steps (LLM reorg, replay, judge,
round-trip) stubbed. Asserts the DUAL-PROOF gate: $ only when behaviour is SAFE AND caching is proven.

Run: python -m tests.test_cache_reorg   (from repo root)
"""
from app.services import cache_reorg, audit


def _trace(system, user):
    return {"model": "claude-sonnet-5",
            "input_messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "tools_defined": [], "output": "DECISION", "usage": {"input_tokens": 500, "output_tokens": 5}}


def test_apply_hoists_static_moves_dynamic_verbatim():
    # static prefix -> system (byte-identical), everything not hoisted -> user turn VERBATIM (never regenerated)
    t = _trace("RULE A\nDATE: today\nRULE B", "please refund order 12")
    reordered = cache_reorg.apply(t, "RULE A\nRULE B", {"RULE A", "RULE B"})
    msgs = {m["role"]: m["content"] for m in reordered["input_messages"]}
    assert msgs["system"] == "RULE A\nRULE B", msgs["system"]                     # fixed prefix hoisted to system
    assert "DATE: today" in msgs["user"] and "please refund order 12" in msgs["user"]   # dynamic + query verbatim
    assert "RULE A" not in msgs["user"], "hoisted static must be removed from the tail"
    print("[ok] apply: static -> system prefix, dynamic remainder verbatim -> user")


_STAT = "\n".join("RULE %d: a standing policy line that is identical on every call." % i for i in range(30))
_BUCKET = [_trace(_STAT + "\nDATE: 2026-08-0%d" % i, u)
           for i, u in enumerate(["refund a double charge on order 12",
                                   "cannot login sso okta redirect loop",
                                   "package arrived damaged cracked screen"], 1)]


def _patch(judge_ok, after_proven, after_read=1680):
    saved = (cache_reorg.plan, audit.replay, audit.judge_preserved,
             cache_reorg.cache_proof.prove_prefix, cache_reorg.cache_min, audit.AUDIT_MAX_PARALLEL)
    big = _STAT                                                    # the reorged prefix (already ~static, >cmin stub)
    cache_reorg.plan = lambda bucket: (big, set(l for l in _STAT.split("\n")))
    audit.replay = lambda t, m, max_tokens=None: "OUT"
    audit.judge_preserved = lambda req, a, b, model=None: (judge_ok(), "why")
    # BEFORE prefix (contiguous common) caches nothing; AFTER (the reorged big prefix) caches per the args
    cache_reorg.cache_proof.prove_prefix = lambda model, prefix, cmin=None: (
        {"read": after_read, "proven": after_proven, "prefix_tok": 2000} if prefix == big
        else {"read": 0, "proven": False, "prefix_tok": 4})
    cache_reorg.cache_min = lambda m: 10                          # let the size gate pass so we test the verdict gate
    audit.AUDIT_MAX_PARALLEL = 1
    return saved


def _restore(saved):
    (cache_reorg.plan, audit.replay, audit.judge_preserved,
     cache_reorg.cache_proof.prove_prefix, cache_reorg.cache_min, audit.AUDIT_MAX_PARALLEL) = saved


def test_recommend_requires_safe_and_cached():
    saved = _patch(judge_ok=lambda: True, after_proven=True)
    try:
        r = cache_reorg.prove("node", _BUCKET, n=5, k=3)
    finally:
        _restore(saved)
    assert r["verdict"] == "SAFE" and r["recommend"] is True, r["verdict"]
    assert r["before"]["read"] == 0 and r["after"]["read"] == 1680          # 0 -> 1680 cached (before/after)
    assert r["recovered_tok"] == 1680 and r["save_per_1k"] > 0
    print("[ok] behaviour SAFE + cache proven -> recommend, $ > 0, before 0 -> after 1680")


def test_cache_not_proven_blocks_recommend():
    saved = _patch(judge_ok=lambda: True, after_proven=False)     # behaviour fine, but the prefix didn't cache
    try:
        r = cache_reorg.prove("node", _BUCKET, n=5, k=3)
    finally:
        _restore(saved)
    assert r["verdict"] == "SAFE" and r["recommend"] is False, r
    assert r["save_per_1k"] == 0.0, "no cache proof -> no claimed saving"
    print("[ok] behaviour SAFE but caching NOT proven -> no recommend, $0")


def test_behaviour_drift_blocks_recommend():
    saved = _patch(judge_ok=lambda: False, after_proven=True)     # caching works, but the reorg changed behaviour
    try:
        r = cache_reorg.prove("node", _BUCKET, n=5, k=3)
    finally:
        _restore(saved)
    assert r["verdict"] != "SAFE" and r["recommend"] is False, r
    assert r["save_per_1k"] == 0.0, "behaviour change -> no claimed saving even though it caches"
    print("[ok] caching proven but behaviour drifts -> no recommend, $0 (the safety gate)")


def test_too_small_and_low_evidence_abstain():
    # < min distinct inputs -> LOW-EVIDENCE, no LLM/proof spent
    saved = _patch(judge_ok=lambda: True, after_proven=True)
    called = {"plan": 0}
    cache_reorg.plan = lambda bucket: (called.__setitem__("plan", called["plan"] + 1), (_STAT, set()))[1]
    try:
        r = cache_reorg.prove("node", _BUCKET[:1], n=5, k=3)
    finally:
        _restore(saved)
    assert r["verdict"] == "LOW-EVIDENCE" and called["plan"] == 0, r
    print("[ok] < min distinct inputs -> LOW-EVIDENCE, reorg model never called")


if __name__ == "__main__":
    test_apply_hoists_static_moves_dynamic_verbatim()
    test_recommend_requires_safe_and_cached()
    test_cache_not_proven_blocks_recommend()
    test_behaviour_drift_blocks_recommend()
    test_too_small_and_low_evidence_abstain()
    print("\nALL CACHE-REORG TESTS PASSED (paid steps stubbed)")
