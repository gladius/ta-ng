"""Profiled downgrade engine — the per-input verdict math ($0, no network).

Stub profile_downgrade's three paid calls: replay -> model name; profile_input -> a fixed contract with a chosen
CONFIDENCE; judge_within_envelope -> a per-call KEPT/BROKE predicate. Sequential (max_parallel=1) so the phase-C
judges fire in c_tasks order (input0's K, then input1's K). Assert the verdict rules:

  breaks <= FLOOR         -> input SAFE
  breaks  > FLOOR         -> input NOT-SAFE
  LOW-confidence profile  -> input NOT-SAFE (abstain), whatever the breaks   (never a false SAFE)
  node rollup is BINARY   -> SAFE iff EVERY input SAFE (no BORDERLINE)

Run: python -m tests.test_downgrade_profiled   (from repo root)
"""
from contextlib import contextmanager

from auditor.util import canonical_model, next_cheaper
from app.services import profile_downgrade as pd

ORIG = canonical_model("claude-sonnet-5") or "claude-sonnet-5"
CHEAP = next_cheaper(ORIG)


def _trace(user):
    return {"model": ORIG,
            "input_messages": [{"role": "system", "content": "route the ticket"},
                               {"role": "user", "content": user}],
            "tools_defined": [], "output": "ORIGINAL", "usage": {"input_tokens": 700, "output_tokens": 5}}


_BUCKET = [_trace("a"), _trace("b"), _trace("c"), _trace("d"), _trace("e")]


@contextmanager
def _patch(kept_pred, confidence="HIGH", floor=1):
    """Stub the three paid calls + pin sequential order and the break floor."""
    saved = (pd.replay, pd.profile_input, pd.judge_within_envelope, pd.AUDIT_MAX_PARALLEL,
             pd.AUDIT_DOWNGRADE_BREAK_FLOOR)
    pd.replay = lambda t, m, max_tokens=None: m
    pd.profile_input = lambda req, samples: {"commitments": "C", "allowed_variation": "V",
                                             "confidence": confidence, "text": "t"}
    pd.judge_within_envelope = lambda req, rec, c, v, cand: (kept_pred(), "why")
    pd.AUDIT_MAX_PARALLEL = 1
    pd.AUDIT_DOWNGRADE_BREAK_FLOOR = floor
    try:
        yield
    finally:
        (pd.replay, pd.profile_input, pd.judge_within_envelope, pd.AUDIT_MAX_PARALLEL,
         pd.AUDIT_DOWNGRADE_BREAK_FLOOR) = saved


_ALWAYS = lambda: True
_NEVER = lambda: False


def _first_n_kept(n):
    st = {"i": 0}
    def f():
        st["i"] += 1
        return st["i"] <= n
    return f


def _break_every(mod):                # break exactly every mod-th judge -> one break per mod-run input
    st = {"i": 0}
    def f():
        st["i"] += 1
        return (st["i"] % mod) != 0
    return f


def test_all_kept_safe():
    with _patch(_ALWAYS):
        inputs, verdict, safe = pd.prove_transform_profiled(_BUCKET[:2], CHEAP, ORIG, k=5)
    assert verdict == "SAFE" and safe == 2
    assert all(i["breaks"] == 0 and i["verdict"] == "SAFE" for i in inputs)
    print("[ok] cheaper stays in the envelope -> SAFE")


def test_breaks_over_floor_notsafe():
    with _patch(_NEVER, floor=1):
        inputs, verdict, safe = pd.prove_transform_profiled(_BUCKET[:1], CHEAP, ORIG, k=5)
    assert verdict == "NOT-SAFE" and inputs[0]["breaks"] == 5 and inputs[0]["verdict"] == "NOT-SAFE"
    print("[ok] breaks > floor -> NOT-SAFE")


def test_break_at_floor_safe():
    with _patch(_break_every(5), floor=1):          # exactly 1 break in 5 -> == floor -> SAFE
        inputs, verdict, safe = pd.prove_transform_profiled(_BUCKET[:1], CHEAP, ORIG, k=5)
    assert inputs[0]["breaks"] == 1 and verdict == "SAFE"
    print("[ok] breaks == floor -> SAFE (boundary)")


def test_low_confidence_abstains():
    with _patch(_ALWAYS, confidence="LOW - node too noisy"):
        inputs, verdict, safe = pd.prove_transform_profiled(_BUCKET[:1], CHEAP, ORIG, k=5)
    assert inputs[0]["breaks"] == 0 and verdict == "NOT-SAFE" and "low-confidence" in inputs[0]["note"]
    print("[ok] LOW-confidence profile -> NOT-SAFE even with 0 breaks (abstain, never a false SAFE)")


def test_binary_rollup():
    # input0 all kept (SAFE), input1 all broke (NOT-SAFE) -> node NOT-SAFE, no BORDERLINE
    with _patch(_first_n_kept(5), floor=1):
        inputs, verdict, safe = pd.prove_transform_profiled(_BUCKET[:2], CHEAP, ORIG, k=5)
    assert inputs[0]["verdict"] == "SAFE" and inputs[1]["verdict"] == "NOT-SAFE"
    assert verdict == "NOT-SAFE" and safe == 1
    print("[ok] node rollup is binary -> any NOT-SAFE input makes the node NOT-SAFE")


def test_low_evidence_abstains_before_engine():
    # audit_node abstains on too-few distinct inputs BEFORE any paid call; and by default (min_evidence=1) a single
    # real input IS audited (delegates to the profiled engine). Stub the engine so "audited" makes no live call.
    from app.services import audit
    saved = (pd.replay, pd.profile_input, pd.judge_within_envelope, pd.AUDIT_MAX_PARALLEL)
    pd.replay = lambda t, m, max_tokens=None: m
    pd.profile_input = lambda req, samples: {"commitments": "C", "allowed_variation": "V", "confidence": "HIGH", "text": "t"}
    pd.judge_within_envelope = lambda req, rec, c, v, cand: (True, "why")
    pd.AUDIT_MAX_PARALLEL = 1
    try:
        low = audit.audit_node("node", _BUCKET[:1], cheaper=CHEAP, n=5, min_evidence=2)   # 1 distinct < 2 -> abstain
        assert low["verdict"] == "LOW-EVIDENCE" and low["inputs"] == [], low
        one = audit.audit_node("node", _BUCKET[:1], cheaper=CHEAP, n=5)                    # default min_evidence=1
        assert one["verdict"] == "SAFE" and one["n"] == 1, one
    finally:
        (pd.replay, pd.profile_input, pd.judge_within_envelope, pd.AUDIT_MAX_PARALLEL) = saved
    print("[ok] <min_evidence -> LOW-EVIDENCE (no paid call); a single input is audited by default")


if __name__ == "__main__":
    test_all_kept_safe()
    test_breaks_over_floor_notsafe()
    test_break_at_floor_safe()
    test_low_confidence_abstains()
    test_binary_rollup()
    test_low_evidence_abstains_before_engine()
    print("\nall profiled-downgrade tests passed")
