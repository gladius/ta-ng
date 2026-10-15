"""The audit of the audit (modules/quality): the automatic checks catch what they should, the model verdict is read
as meant, the spot-check pick is stable. Offline: the model call is replaced."""
import json

from sqlalchemy import delete

from app.services import db
from core.identity import store
from modules import quality


def _clean():
    with db.ready().begin() as c:
        for t in (store.usecase_days, store.site_days, store.callsites, quality.checks):
            c.execute(delete(t))


def _site(site, uc, markers, ambiguous=False):
    store.save_identity({"site": site, "usecase": uc, "template": markers, "markers": markers, "ambiguous": ambiguous,
                         "calls": 1, "first_day": "2026-10-09", "last_day": "2026-10-10", "versions": []}, 7)


def test_the_automatic_checks_flag_weak_placement_bad_sums_fingerprints_model_mixes_and_volume_jumps():
    _clean()
    store.replace_coverage(["2026-10-10"], [
        {"usecase": "billing", "day": "2026-10-10", "calls": 100, "other_format": 0, "repeated": 0, "placed": 80, "unplaced": 20},
        {"usecase": "triage", "day": "2026-10-10", "calls": 100, "other_format": 10, "repeated": 0, "placed": 85, "unplaced": 2}])
    _site("s1", "billing", ["a b c d e"])
    _site("s2", "billing", [])
    _site("s3", "triage", ["x y z w v"], ambiguous=True)
    store.replace_days(["2026-10-09"], [{"site": "s1", "day": "2026-10-09", "model": "m1", "calls": 5}])
    store.replace_days(["2026-10-10"], [{"site": "s1", "day": "2026-10-10", "model": "m1", "calls": 40},
                                        {"site": "s1", "day": "2026-10-10", "model": "m2", "calls": 40},
                                        {"site": "s2", "day": "2026-10-10", "model": "m1", "calls": 3},
                                        {"site": "s3", "day": "2026-10-10", "model": "m1", "calls": 85}])
    quality.run("2026-10-10", {})
    r = quality.report("2026-10-10")
    failed = {(f["kind"], f["usecase"], f["site"]) for f in r["failures"]}
    assert ("placement", "billing", None) in failed                    # 80% placed
    assert ("consistency", "triage", None) in failed                   # 100 != 85 + 2 + 10
    assert ("fingerprint", "billing", "s2") in failed and ("fingerprint", "triage", "s3") in failed
    assert ("volume", "billing", "s1") in failed                       # 80 calls against 5 the day before
    assert r["kinds"]["models"] == {"checked": 1, "failed": 0}          # s1 on two models: listed, not a failure


def test_a_model_verdict_of_two_templates_fails_the_spot_check(monkeypatch):
    _clean()
    _site("s1", "billing", ["a b c d e"])
    verdict = {"same": False, "same_why": "two different prompts", "different": True, "different_why": "",
               "phrases": {"a b c d e": "fixed"}}

    class B:                                                           # the day's session, as the day script left it
        resumed, s = True, None
        def __init__(self, *a, **k): pass
        def site_calls(self, sites): return [{"site": "d1", "usecase": "billing", "id": "1", "model": "m", "sys": "x", "rn": 1}]
        def close(self): pass
    from core.ledger import build
    monkeypatch.setattr(build, "Build", B)
    monkeypatch.setattr(quality, "ask", lambda same, sib, phrases: verdict)
    quality.run("2026-10-10", {"s1": {"usecase": "billing", "day_sites": ["d1"], "sibling_day_site": None}}, session_id="sess")
    spot = quality.report("2026-10-10")["spot_checks"]
    assert len(spot) == 1 and spot[0]["ok"] is False and spot[0]["detail"]["same_why"] == "two different prompts"


def test_the_pick_takes_the_busiest_half_then_a_stable_random_rest():
    calls = {"s%d" % i: 100 - i for i in range(30)}
    recs = {s: {} for s in calls}
    a, b = quality.pick(recs, calls, 10), quality.pick(recs, calls, 10)
    assert a == b and a[:5] == ["s0", "s1", "s2", "s3", "s4"] and len(set(a)) == 10
