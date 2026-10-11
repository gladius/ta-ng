"""Model checks: the sample picks representative requests; presets resolve candidates from the catalog."""
from modules.models import checks


def _rows(n):
    return [{"id": "r%d" % i, "prompt_tokens": 100 * (i + 1), "completion_tokens": (i * 37) % 500} for i in range(n)]


def test_pick_takes_the_extremes_quartiles_heaviest_answers_then_random():
    out = checks.pick(_rows(40), n=8)
    assert len(out) == 8 and len({s["id"] for s in out}) == 8
    whys = [s["why"] for s in out]
    assert whys[:2] == ["smallest prompt", "largest prompt"]
    assert "median prompt" in whys and whys.count("quartile prompt") == 2 and whys.count("heaviest answer") == 2
    assert out[0]["id"] == "r0" and out[1]["id"] == "r39"
    heaviest = max(_rows(40), key=lambda r: r["completion_tokens"])["id"]
    assert any(s["id"] == heaviest for s in out)


def test_pick_with_fewer_rows_than_asked_returns_them_all_once():
    out = checks.pick(_rows(3), n=8)
    assert sorted(s["id"] for s in out) == ["r0", "r1", "r2"]
    assert checks.pick([], n=8) == []


def test_presets_resolve_from_the_catalog_and_custom_skips_the_original():
    assert checks.candidates_for("gpt-5.4", "custom", ["gpt-5.4", "gpt-5.4-mini"]) == ["gpt-5.4-mini"]
    assert checks.candidates_for("gpt-5.4", "release", ["gpt-5.5"]) == ["gpt-5.5"]
    cheaper = checks.candidates_for("gpt-5.4", "cheaper", [])
    assert cheaper == [] or (len(cheaper) == 1 and cheaper[0] != "gpt-5.4")
