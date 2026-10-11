"""The cache module's arithmetic: the TTL what-if over gap buckets and the diagnosis causes."""
from modules.cache import diagnosis, ttl


def _gaps(**calls):
    return [{"bucket": b, "calls": n, "prefix_tokens": n * 5000, "cached": 0, "written": 0} for b, n in calls.items()]


def test_a_longer_claude_lifetime_pays_when_calls_fall_between_5_minutes_and_an_hour():
    w = ttl.what_if(_gaps(first=1, **{"1m": 10, "15m": 100}), "claude-sonnet-5")
    assert w["options"][300]["reads"] == 10 and w["options"][3600]["reads"] == 110
    assert w["best_ttl"] == 3600 and w["saving"] > 0
    assert w["beyond_default"] == 101


def test_no_longer_lifetime_when_calls_are_within_5_minutes():
    w = ttl.what_if(_gaps(first=1, **{"1m": 200}), "claude-sonnet-5")
    assert w["best_ttl"] == 300 and w["saving"] == 0


def _site(static_chars=24000, after=0.0, tool_sets=1, versions=1):
    return {"static_chars": static_chars, "dynamic_chars": 2000, "static_after_dynamic": after, "tool_sets": tool_sets,
            "versions": [{}] * versions}


def _ledger(model, calls=1000, prompt=7000, cached_share=0.0):
    return {"model": model, "calls": calls, "prompt_tokens": calls * prompt, "cached_tokens": int(calls * prompt * cached_share),
            "calls_cached": calls if cached_share else 0, "cost": 10.0}


def test_causes():
    assert diagnosis.diagnose(_site(), _ledger("claude-sonnet-5"), None)["cause"] == "no_mark"
    assert diagnosis.diagnose(_site(static_chars=1000), _ledger("gpt-5.4-mini"), None)["cause"] == "below_min"
    assert diagnosis.diagnose(_site(after=0.9), _ledger("gpt-5.4-mini"), None)["cause"] == "dynamic_first"
    assert diagnosis.diagnose(_site(), _ledger("gpt-5.4-mini", cached_share=0.8), None)["cause"] == "ok"
    w = ttl.what_if(_gaps(first=1, **{"15m": 1000}), "claude-sonnet-5")
    assert diagnosis.diagnose(_site(), _ledger("claude-sonnet-5", cached_share=0.3), w)["cause"] == "gaps"
    w = ttl.what_if(_gaps(first=1, **{"6h": 1000}), "gemini-3.5-flash")
    assert diagnosis.diagnose(_site(), _ledger("gemini-3.5-flash", cached_share=0.1), w)["cause"] == "sparse"


def test_a_mark_is_worth_the_fixed_part_on_every_unread_call():
    d = diagnosis.diagnose(_site(static_chars=40000), _ledger("claude-sonnet-5", calls=100), None)
    assert d["cause"] == "no_mark" and d["proof"] == "safe"
    assert d["saving"] > 0 and d["evidence"]["fixed_tokens"] == 10000
