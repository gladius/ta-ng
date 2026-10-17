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


def _site(site, uc, markers):
    store.save_identity({"site": site, "usecase": uc, "markers": markers, "calls": 1, "first_day": "2026-10-09", "last_day": "2026-10-10",
                         "versions": []}, 7)


def test_the_automatic_checks_flag_weak_placement_bad_sums_fingerprints_model_mixes_and_volume_jumps():
    _clean()
    store.replace_coverage(["2026-10-10"], [
        {"usecase": "billing", "day": "2026-10-10", "calls": 100, "other_format": 0, "repeated": 0, "placed": 80, "unplaced": 20},
        {"usecase": "triage", "day": "2026-10-10", "calls": 100, "other_format": 10, "repeated": 0, "placed": 85, "unplaced": 2}])
    _site("s1", "billing", ["a b c d e"])
    _site("s2", "billing", [])
    _site("s3", "triage", ["x y z w v"])
    store.replace_days(["2026-10-09"], [{"site": "s1", "day": "2026-10-09", "model": "m1", "calls": 5}])
    store.replace_days(["2026-10-10"], [{"site": "s1", "day": "2026-10-10", "model": "m1", "calls": 40},
                                        {"site": "s1", "day": "2026-10-10", "model": "m2", "calls": 40},
                                        {"site": "s2", "day": "2026-10-10", "model": "m1", "calls": 3},
                                        {"site": "s3", "day": "2026-10-10", "model": "m1", "calls": 85}])
    quality.run("2026-10-10")
    r = quality.report("2026-10-10")
    failed = {(f["kind"], f["usecase"], f["site"]) for f in r["failures"]}
    assert ("placement", "billing", None) in failed                    # 80% placed
    assert ("consistency", "triage", None) in failed                   # 100 != 85 + 2 + 10
    assert ("fingerprint", "billing", "s2") in failed and ("fingerprint", "triage", "s3") not in failed
    assert ("volume", "billing", "s1") in failed                       # 80 calls against 5 the day before
    assert r["kinds"]["models"] == {"checked": 1, "failed": 0}          # s1 on two models: listed, not a failure
