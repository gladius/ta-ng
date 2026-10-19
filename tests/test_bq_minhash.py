"""The pre-pass's pure parts; its SQL runs only against BigQuery (tools/identity_score.py --prepass)."""
from core.ledger import bq_minhash as m


def test_phase1_phrases_are_word_rich_and_skip_number_runs():
    norm = " today is # # # you are a billing agent for acme answer in # sentences "
    ph = m.phrases(norm)
    assert len(ph) == 5 and all(sum(1 for w in g.split() if w != "#") >= 3 for g in ph)
    assert "is # # #" not in ph and "you are a billing" in ph


def test_candidates_need_free_calls_and_no_collision():
    cands = [{"cand": "a"}, {"cand": "b"}, {"cand": "c"}]
    counts = {"a": {"free": 50, "collisions": []}, "b": {"free": 3, "collisions": []}, "c": {"free": 50, "collisions": [{"site": "x", "n": 9}]}}
    assert [c["cand"] for c in m.pick(cands, counts)] == ["a"]


def test_sample_aims_at_half_a_million_calls_between_1_and_5_percent():
    assert m.sample_pct(100_000_000) == 1 and m.sample_pct(20_000_000) == 2 and m.sample_pct(1_000) == 5
    assert m.need(["a", "b", "c"]) == 3 and m.need(["a", "b", "c", "d", "e"]) == 4
