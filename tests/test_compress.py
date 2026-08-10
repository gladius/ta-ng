"""Input-compression static/dynamic split — the compressible target is the system content byte-identical ACROSS a
call-site's calls; each call's per-call system bits (a date, a ticket id) are preserved verbatim and NEVER replaced
by trace 0's. This is the fix for "compress bucket[0]'s whole system and stamp it on every call". $0, no network.
Run: python -m tests.test_compress  (from repo root)."""
from app.services import compress, funnel, audit

# A mostly-static system: a big shared policy core (>=400 tok) + ONE per-call dynamic line (a ticket id). So the
# systems DIFFER across calls (old gate: not a candidate) but the STATIC portion is large (new gate: a candidate).
STATIC = "\n".join("RULE %02d: a standing policy line that is identical on every single call to this node." % i
                   for i in range(60))


def _trace(user, ticket):
    sys = STATIC + "\nTICKET_ID: %s" % ticket
    return {"model": "claude-sonnet-5",
            "input_messages": [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            "tools_defined": [], "output": "OUT", "usage": {"input_tokens": 500, "output_tokens": 10}}


BUCKET = [_trace("refund a double charge", "AAA"),
          _trace("cannot log in to portal", "BBB"),
          _trace("cancel my subscription", "CCC")]


def test_static_system_extracts_shared_core():
    static, common = compress.static_system(BUCKET)
    assert "RULE 00:" in static and "RULE 59:" in static, "shared policy core is kept"
    assert "TICKET_ID" not in static, "the per-call id is NOT part of the static core"
    for t, tid in zip(BUCKET, ["AAA", "BBB", "CCC"]):
        assert compress._dynamic_system(t, common).strip() == "TICKET_ID: %s" % tid
    print("[ok] static core = shared lines; each call's dynamic = its OWN per-call line")


def test_compressible_gates_on_static_portion():
    # systems DIFFER (3 distinct) but the static portion is >=400 tok -> now a candidate (old gate said no).
    assert funnel._compressible(BUCKET) is True, "mostly-static node must be a compress candidate"
    tiny = [{"model": "m", "input_messages": [{"role": "system", "content": "hello %d" % i},
             {"role": "user", "content": "q"}], "tools_defined": [], "output": "o", "usage": {}} for i in range(3)]
    assert funnel._compressible(tiny) is False, "tiny shared core -> not a candidate"
    print("[ok] candidacy gates on the STATIC portion, not whole-system byte-identity")


def test_prove_reattaches_each_calls_own_dynamic():
    # THE FLAW FIX: each call must be sent compressed_core + ITS OWN ticket id, never trace 0's.
    saved = (compress.compress_system, audit.replay, audit.judge_preserved,
             audit.AUDIT_MAX_PARALLEL, audit.AUDIT_SELF_BASELINE)
    seen = {}
    compress.compress_system = lambda text, must_preserve=(), target_tokens=None, guidance="", abstractive=True: \
        ("SHORT_CORE", True)
    def _replay(t, m, max_tokens=None):
        sysmsg = "\n".join(x["content"] for x in t["input_messages"] if x["role"] == "system")
        seen[t["input_messages"][-1]["content"]] = sysmsg
        return "OUT"
    audit.replay = _replay
    audit.judge_preserved = lambda req, a, b, model=None: (True, "ok")
    audit.AUDIT_MAX_PARALLEL, audit.AUDIT_SELF_BASELINE = 1, False
    try:
        r = compress.prove("node", BUCKET, n=5, k=1, min_evidence=1)
    finally:
        (compress.compress_system, audit.replay, audit.judge_preserved,
         audit.AUDIT_MAX_PARALLEL, audit.AUDIT_SELF_BASELINE) = saved
    assert r["verdict"] == "SAFE", r["verdict"]
    for user, tid in [("refund a double charge", "AAA"), ("cannot log in to portal", "BBB"),
                      ("cancel my subscription", "CCC")]:
        sysmsg = seen.get(user, "")
        assert sysmsg.startswith("SHORT_CORE"), "compressed core applied, got: %r" % sysmsg[:40]
        assert "TICKET_ID: %s" % tid in sysmsg, "call %r must keep its OWN id, got: %r" % (user, sysmsg)
    print("[ok] prove sends compressed static + each call's OWN dynamic (never trace 0's)")


if __name__ == "__main__":
    test_static_system_extracts_shared_core()
    test_compressible_gates_on_static_portion()
    test_prove_reattaches_each_calls_own_dynamic()
    print("\nALL COMPRESSION SPLIT TESTS PASSED ($0, no network)")
