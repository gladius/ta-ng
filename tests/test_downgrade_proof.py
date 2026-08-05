"""Proof-path logic — the DETERMINISTIC parts of the repeat-and-vote downgrade audit, no network ($0).

We stub the two paid calls (`replay`, `judge_preserved`) and assert the verdict math:
  - cheaper always preserves  -> every input K/K  -> node SAFE
  - cheaper always drifts      -> every input 0/K -> node NOT-SAFE
  - one input flips run-to-run -> that input 1..K-1 -> node BORDERLINE (the anti-flip guarantee)
  - too few distinct inputs    -> LOW-EVIDENCE, and nothing is replayed
Plus the behavior renderer (tool call folded into the output the ONE judge sees).

Run: python -m tests.test_downgrade_proof   (from repo root)
"""
from app.services import audit


def _trace(user, model="claude-sonnet-5"):
    return {"model": model,
            "input_messages": [{"role": "system", "content": "route the ticket"},
                               {"role": "user", "content": user}],
            "tools_defined": [], "output": "ORIGINAL", "usage": {"input_tokens": 700, "output_tokens": 5}}


_BUCKET = [_trace("charge on order 123 double billed"), _trace("cannot login sso okta redirect loop"),
           _trace("package damaged monitor cracked screen"), _trace("downgrade my team plan to pro please"),
           _trace("refund for subscription renewal i cancelled")]


def test_render():
    r = audit._render("Looking it up.", [{"name": "set_category", "args": {"category": "billing"}}])
    assert "Looking it up." in r and "set_category" in r and '"category": "billing"' in r
    assert audit._render("", []) == "(empty output)"
    print("[ok] behavior renderer folds tool call into output")


def _run(judge_fn):
    orig = (audit.replay, audit.judge_preserved, audit.AUDIT_MAX_PARALLEL)
    audit.replay = lambda t, m, max_tokens=None: "CHEAPER"
    audit.judge_preserved = judge_fn
    audit.AUDIT_MAX_PARALLEL = 1                 # sequential -> deterministic order for the alternating-judge case
    try:
        return audit.audit_node("node", _BUCKET, n=5, k=5)
    finally:
        audit.replay, audit.judge_preserved, audit.AUDIT_MAX_PARALLEL = orig


def test_safe():
    r = _run(lambda req, a, b, model=None: (True, "same decision"))
    assert r["verdict"] == "SAFE", r["verdict"]
    assert r["safe_inputs"] == 5 and all(i["kept"] == 5 for i in r["inputs"])
    print("[ok] all preserved -> SAFE (5 inputs x 5/5)")


def test_not_safe():
    r = _run(lambda req, a, b, model=None: (False, "dropped policy"))
    assert r["verdict"] == "NOT-SAFE", r["verdict"]
    assert all(i["kept"] == 0 for i in r["inputs"])
    print("[ok] all drift -> NOT-SAFE")


def test_borderline_flip():
    # one input flips (alternating verdicts) -> lands between 0 and K -> BORDERLINE node (this is the anti-flip fix)
    state = {"n": 0}
    def judge(req, a, b, model=None):
        state["n"] += 1
        return (state["n"] % 2 == 0, "flip")
    r = _run(judge)
    assert r["verdict"] == "BORDERLINE", r["verdict"]
    assert any(0 < i["kept"] < i["k"] for i in r["inputs"]), "at least one input flipped mid-range"
    print("[ok] a flipping input -> BORDERLINE (not a coin-flip SAFE/NOT-SAFE)")


def test_safe_tolerates_one_drift():
    # 2/3 rule: an input where the cheaper model preserved in >=2/3 of re-runs is SAFE (one drift tolerated).
    state = {"n": 0}
    def judge(req, a, b, model=None):
        state["n"] += 1
        return (state["n"] % 3 != 0, "one-in-three drift")   # per input's 3 re-runs (n=1,2,3): keep, keep, DRIFT -> 2/3
    orig = (audit.replay, audit.judge_preserved, audit.AUDIT_MAX_PARALLEL)
    audit.replay = lambda t, m, max_tokens=None: "CHEAPER"; audit.judge_preserved = judge; audit.AUDIT_MAX_PARALLEL = 1
    try:
        r = audit.audit_node("node", _BUCKET, n=5, k=3)      # k=3 so 2/3 = one drift tolerated
    finally:
        audit.replay, audit.judge_preserved, audit.AUDIT_MAX_PARALLEL = orig
    assert r["verdict"] == "SAFE", r["verdict"]
    print("[ok] 2/3 rule: one drift in three -> input still SAFE")


def test_low_evidence():
    calls = {"n": 0}
    orig_r = audit.replay
    audit.replay = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), "x")[1]
    try:
        r = audit.audit_node("node", [_trace("only one ticket")], n=5, k=5)   # 1 distinct < MIN_EVIDENCE
    finally:
        audit.replay = orig_r
    assert r["verdict"] == "LOW-EVIDENCE", r["verdict"]
    assert calls["n"] == 0, "abstain must NOT spend any replay"
    print("[ok] < min evidence -> LOW-EVIDENCE, zero spend")


if __name__ == "__main__":
    test_render()
    test_safe()
    test_not_safe()
    test_borderline_flip()
    test_safe_tolerates_one_drift()
    test_low_evidence()
    print("\nALL PROOF-PATH TESTS PASSED ($0, no network)")
