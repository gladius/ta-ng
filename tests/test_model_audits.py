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


def test_strategies_resolve_candidates_and_never_the_original_itself():
    assert audits.candidates_for("gpt-5.4", "manual", ["gpt-5.4", "gpt-5.4-mini"]) == ["gpt-5.4-mini"]
    assert audits.candidates_for("gpt-5.4", "release", ["gpt-5.5"]) == ["gpt-5.5"]
    assert "gpt-5.4" not in audits.candidates_for("gpt-5.4", "cheaper", [])
    assert "gpt-5.4" not in audits.candidates_for("gpt-5.4", "better_value", [])
    assert audits.candidates_for("gpt-5.4", "drift_watch", []) == ["gpt-5.4"]        # itself, labelled "now"
    rec = {"decisions": {"models": {"candidates": {"gpt-5.4-mini": {}, "gpt-5.4": {}}}}, "versions": []}
    assert audits.candidates_for("gpt-5.4", "revalidate", [], rec=rec) == ["gpt-5.4-mini"]


def test_verdicts_expire_and_are_tied_to_the_prompt_version():
    rec = {"versions": [{"version": "v2"}]}
    cands = {"a": {"valid_until": "2999-01-01T00:00:00", "site_version": "v2", "verdict": "passes", "cost_vs_original_pct": -50, "audit": "c1"},
             "b": {"valid_until": "2000-01-01T00:00:00", "site_version": "v2", "verdict": "passes", "cost_vs_original_pct": -90, "audit": "c0"},
             "c": {"valid_until": "2999-01-01T00:00:00", "site_version": "v1", "verdict": "passes", "cost_vs_original_pct": -95, "audit": "c0"}}
    out = audits.with_validity({"candidates": cands}, rec)
    assert [m for m, v in out["candidates"].items() if v["valid"]] == ["a"]
    assert out["cheapest"]["model"] == "a"                     # the expired and the old-version verdicts don't count
