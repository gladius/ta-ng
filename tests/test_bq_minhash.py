"""The pre-pass's pure parts; its SQL runs only against BigQuery (tools/identity_score.py --prepass)."""
from core.ledger import bq_minhash as m


def test_phase1_phrases_are_word_rich_spread_over_the_head_and_skip_number_runs():
    tail = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa quebec romeo sierra tango uniform victor whiskey xray yankee zulu"
    norm = " today is # # # you are a billing agent for acme answer in # sentences " + tail + " "
    ph = m.phrases(norm)
    assert len(ph) == 5 and len(set(ph)) == 5 and all(sum(1 for w in g.split() if w != "#") >= 3 for g in ph)
    assert "is # # #" not in ph
    assert ph[0].startswith("you are") or ph[0].startswith("are a") or ph[0].startswith("a billing") or ph[0].startswith("billing agent")
    assert ph[-1].split()[0] in tail.split()[-8:]                       # the last phrase comes from the end of the head


def test_candidates_need_free_calls_and_no_collision_and_one_per_template():
    cands = [{"cand": "a"}, {"cand": "b"}, {"cand": "c"}, {"cand": "d"}]
    counts = {"a": {"free": 50, "collisions": []}, "b": {"free": 3, "collisions": []}, "c": {"free": 50, "collisions": [{"site": "x", "n": 9}]},
              "d": {"free": 40, "collisions": []}}
    assert [c["cand"] for c in m.pick(cands, counts)] == ["a", "d"]
    assert [c["cand"] for c in m.pick(cands, counts, [("a", "d", 30)])] == ["a"]      # d's calls are mostly a's: one template
    assert [c["cand"] for c in m.pick(cands, counts, [("a", "d", 5)])] == ["a", "d"]


def test_sample_aims_at_half_a_million_calls_between_1_and_5_percent():
    assert m.sample_pct(100_000_000) == 1 and m.sample_pct(20_000_000) == 2 and m.sample_pct(1_000) == 5
    assert m.need(["a", "b", "c"]) == 3 and m.need(["a", "b", "c", "d", "e"]) == 4
