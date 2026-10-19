"""The audit of the audit (modules/quality): the automatic checks catch what they should."""
import json

from sqlalchemy import delete

from app.services import db
from core.identity import store
from modules import quality


def _clean():
    with db.ready().begin() as c:
        for t in (store.usecase_days, store.site_days, store.callsites, quality.checks):
            c.execute(delete(t))


def _site(site, uc, markers, name="Bill explainer", mixed=False):
    store.save_identity({"site": site, "usecase": uc, "markers": markers, "calls": 1, "first_day": "2026-10-09", "last_day": "2026-10-10",
                         "versions": [{"version": 1, "phrases": markers, "mixed": mixed, "mixed_why": "two letters" if mixed else ""}]}, 7)
    store.set_profile(site, {"name": name, "does": "", "task": "other", "output": "text", "model": "test"})


def test_the_automatic_checks_flag_weak_placement_bad_sums_fingerprints_model_mixes_and_volume_jumps():
    _clean()
    store.replace_coverage(["2026-10-10"], [
        {"usecase": "billing", "day": "2026-10-10", "calls": 100, "other_format": 0, "repeated": 0, "placed": 80, "unplaced": 20},
        {"usecase": "triage", "day": "2026-10-10", "calls": 100, "other_format": 10, "repeated": 0, "placed": 85, "unplaced": 2}])
    _site("s1", "billing", ["a b c d e"])
    _site("s2", "billing", [], name="Refund note", mixed=True)
    _site("s3", "triage", ["x y z w v"], name="")
    _site("s4", "triage", ["q r s t u"], name="Ticket router")
    _site("s5", "triage", ["k l m n o"], name="ticket router ")
    store.replace_days(["2026-10-09"], [{"site": "s1", "day": "2026-10-09", "model": "m1", "calls": 5}])
    store.replace_days(["2026-10-10"], [{"site": "s1", "day": "2026-10-10", "model": "m1", "calls": 40},
                                        {"site": "s1", "day": "2026-10-10", "model": "m2", "calls": 40},
                                        {"site": "s2", "day": "2026-10-10", "model": "m1", "calls": 3},
                                        {"site": "s3", "day": "2026-10-10", "model": "m1", "calls": 85},
                                        {"site": "s4", "day": "2026-10-10", "model": "m1", "calls": 95},
                                        {"site": "s4", "day": "2026-10-10", "model": "m2", "calls": 5},
                                        {"site": "s5", "day": "2026-10-10", "model": "m1", "calls": 10}])
    quality.run("2026-10-10")
    r = quality.report("2026-10-10")
    failed = {(f["kind"], f["usecase"], f["site"]) for f in r["failures"]}
    assert ("placement", "billing", None) in failed                    # 80% placed
    assert ("consistency", "triage", None) in failed                   # 100 != 85 + 2 + 10
    assert ("fingerprint", "billing", "s2") in failed and ("fingerprint", "triage", "s3") not in failed
    assert ("volume", "billing", "s1") in failed                       # 80 calls against 5 the day before
    assert r["kinds"]["models"] == {"checked": 1, "failed": 1}          # s1 split 40/40; s4's 5% is under the 10% floor
    assert ("named", "triage", "s3") in failed and ("named", "billing", "s1") not in failed
    assert ("mixed", "billing", "s2") in failed and ("mixed", "billing", "s1") not in failed
    assert {("same_name", "triage", "s4"), ("same_name", "triage", "s5")} <= failed   # case and spaces ignored
    assert ("same_name", "billing", "s1") not in failed
    assert r["trust"]["level"] == "not yet" and any("without a name" in x and "sharing a name" in x for x in r["trust"]["reasons"])


def test_a_clean_day_is_trustworthy_and_a_model_mix_alone_does_not_lower_it():
    _clean()
    store.replace_coverage(["2026-10-10"], [{"usecase": "billing", "day": "2026-10-10", "calls": 100, "other_format": 0, "repeated": 0,
                                             "placed": 99, "unplaced": 1}])
    _site("s1", "billing", ["a b c d e"])
    _site("s2", "billing", ["f g h i j"], name="Refund note")
    store.replace_days(["2026-10-10"], [{"site": "s1", "day": "2026-10-10", "model": "m1", "calls": 50},
                                        {"site": "s1", "day": "2026-10-10", "model": "m2", "calls": 30},
                                        {"site": "s2", "day": "2026-10-10", "model": "m1", "calls": 19}])
    quality.run("2026-10-10")
    r = quality.report("2026-10-10")
    assert [(f["kind"], f["site"]) for f in r["failures"]] == [("models", "s1")]
    assert r["trust"]["level"] == "trustworthy"
