"""The opt-in OPEN-WEIGHT downgrade lane (catalog.open_weight_target + downgrade.candidates mode='open-weight').

Pure selection logic — open_weight_target is called with avail=None (no gating, no network). The one test that goes
through downgrade.candidates (which consults registry.is_available) pins the gateway to None so availability is
UNKNOWN -> True and nothing hits the network. Commercial mode is asserted UNCHANGED alongside, so the lane is
provably additive."""
from app.catalog import open_weight_target, meta
from app.services import downgrade, cache


def test_same_tier_swap_picks_strongest_in_tier():
    # SAME-TIER swap: a balanced commercial node -> the strongest BALANCED open-weight = gpt-oss-120b
    t = open_weight_target("claude-sonnet-5", avg_in=2000, avg_out=500, avail=None)
    assert t == "gpt-oss-120b", t
    assert meta(t)["tier"] == "balanced" and meta(t)["open_weight"] is True
    # a small commercial node -> the strongest SMALL open-weight = gemma-3-27b (never a balanced model)
    s = open_weight_target("claude-haiku-4-5", avg_in=2000, avg_out=500, avail=None)
    assert s == "gemma-3-27b", s
    assert meta(s)["tier"] == "small"


def test_frontier_has_no_open_weight_peer():
    # there is NO open-weight frontier tier -> a frontier node gets no target (honest empty state)
    for m in ["claude-opus-4-8", "gemini-2.5-pro", "gpt-5"]:
        assert meta(m)["tier"] == "frontier"
        assert open_weight_target(m, avg_in=3000, avg_out=800, avail=None) is None, m


def test_unverified_and_moderation_are_never_targets():
    # gemma-4-e2b (confidence=unverified) and gpt-oss-safeguard-20b (purpose=moderation) must NEVER be picked,
    # even though both are cheap and otherwise callable — the guard has teeth.
    assert meta("gemma-4-e2b")["confidence"] == "unverified"
    assert meta("gpt-oss-safeguard-20b")["purpose"] == "moderation"
    picks = set()
    for m in ["claude-sonnet-5", "gemini-3.7-flash", "claude-haiku-4-5", "gpt-5-mini", "gpt-5-nano"]:
        p = open_weight_target(m, avg_in=3000, avg_out=800, avail=None)
        if p:
            picks.add(p)
    assert "gemma-4-e2b" not in picks
    assert "gpt-oss-safeguard-20b" not in picks


def test_strict_no_target_when_nothing_saves():
    # already on the cheapest catalogued open-weight (nano) -> no same-tier open-weight net-saves -> None (strict)
    assert open_weight_target("gemma-3-4b", avg_in=1000, avg_out=1000, avail=None) is None


def test_candidates_open_weight_mode_and_commercial_unchanged(monkeypatch):
    from app import registry
    monkeypatch.setattr(registry, "_gateway", lambda: (None, None))     # no gateway -> availability unknown -> True
    registry.refresh()
    try:
        nodes = [{"key": "k1", "node": "triage", "model": "claude-sonnet-5",
                  "avg_in": 4000, "avg_out": 600, "calls": 100}]
        ow = downgrade.candidates(nodes, per_calls=1000, mode="open-weight")
        assert ow and ow[0]["reason"] == "open-weight"
        assert meta(ow[0]["cheaper"])["open_weight"] is True

        com = downgrade.candidates(nodes, per_calls=1000)               # default = commercial, UNCHANGED
        assert com and com[0]["reason"] in ("migration", "cost-downgrade")
        assert not meta(com[0]["cheaper"]).get("open_weight")           # never an open-weight target in commercial mode
    finally:
        registry.refresh()


def test_cache_none_verdict_for_open_weight_host():
    # cache_mode 'none' (open-weight on a host) -> verdict NONE, not a false AUTO/CACHEABLE
    bucket = [{"model": "gemma-3-27b",
               "input_messages": [{"role": "system", "content": "x" * 8000}, {"role": "user", "content": "hi"}],
               "usage": {"cached_tokens": 0}}]
    d = cache.detect(bucket, model="gemma-3-27b")
    assert d["verdict"] == "NONE", d["verdict"]
    assert d["save_per_1k"] == 0
