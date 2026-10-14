"""Model audits: the sample picks representative requests; presets resolve candidates from the catalog."""
from modules.models import audits


def _rows(n):
    return [{"id": "r%d" % i, "prompt_tokens": 100 * (i + 1), "completion_tokens": (i * 37) % 500} for i in range(n)]


def test_pick_takes_the_extremes_quartiles_heaviest_answers_then_random():
    out = audits.pick(_rows(40), n=8)
    assert len(out) == 8 and len({s["id"] for s in out}) == 8
    whys = [s["why"] for s in out]
    assert whys[:2] == ["smallest prompt", "largest prompt"]
    assert "median prompt" in whys and whys.count("quartile prompt") == 2 and whys.count("heaviest answer") == 2
    assert out[0]["id"] == "r0" and out[1]["id"] == "r39"
    heaviest = max(_rows(40), key=lambda r: r["completion_tokens"])["id"]
    assert any(s["id"] == heaviest for s in out)


def test_pick_with_fewer_rows_than_asked_returns_them_all_once():
    out = audits.pick(_rows(3), n=8)
    assert sorted(s["id"] for s in out) == ["r0", "r1", "r2"]
    assert audits.pick([], n=8) == []


def _verdict(audit, site, model, verdict, cost, version, days_left, age_days=0):
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(days=age_days)
    return {"audit_id": audit, "site": site, "model": model, "usecase": "uc", "trigger": "user", "verdict": verdict,
            "requests": 4, "within": 4, "outside": 0, "unverified": 0, "pass_rate": 1.0, "fit_rate": 1.0,
            "cost_vs_original_pct": cost, "latency_vs_original_pct": -10, "site_version": version, "at": now,
            "valid_until": now + datetime.timedelta(days=days_left)}


def test_strategies_pick_served_generative_models_and_never_the_original_itself():
    assert audits.candidates_for("gpt-5.4", "manual", ["gpt-5.4", "gpt-5.4-mini"]) == ["gpt-5.4-mini"]
    assert audits.candidates_for("gpt-5.4", "release", ["gpt-5.5"]) == ["gpt-5.5"]
    cheaper = audits.candidates_for("gpt-5.4", "cheaper", [])
    assert cheaper and "gpt-5.4" not in cheaper and len(cheaper) <= audits.MAX_PICKS
    assert cheaper[0].startswith("gpt-")                       # same provider first
    better = audits.candidates_for("gpt-5.4-mini", "better_value", [])
    assert better and "gpt-5.4-mini" not in better and len(better) <= audits.MAX_PICKS


def test_knowledge_is_the_latest_valid_verdict_per_model_and_audits_never_touch_the_record():
    from app.services import db
    eng = db.ready()
    with eng.begin() as c:
        c.execute(audits.verdict_rows.delete())
        c.execute(audits.verdict_rows.insert(), [
            _verdict("a1", "s1", "a", "passes", -50, "v2", 30),
            _verdict("a0", "s1", "b", "passes", -90, "v2", -1),      # expired
            _verdict("a0", "s1", "c", "passes", -95, "v1", 30),      # measured on an older prompt version
            _verdict("a0", "s1", "a", "drifts", -40, "v2", 30, age_days=3)])   # older verdict of a: the newer wins
    rec = {"site": "s1", "versions": [{"version": "v2"}]}
    k = audits.knowledge("s1", rec)
    assert {m: v["valid"] for m, v in k["candidates"].items()} == {"a": True, "b": False, "c": False}
    assert k["cheapest"]["model"] == "a" and k["candidates"]["a"]["audit"] == "a1"
