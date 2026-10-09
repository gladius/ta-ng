"""The validation engine, its judge-reply readers, the downgrade adapter and the measured run record. Offline."""
import types

import anthropic
import pytest

from validation import judges, engine, Spec, Item, Candidate, validate, validate_many
from app.services import downgrade_refset, llm_client


# ── judge replies are read strictly ──────────────────────────────────────────────────────────────────────────────
def test_fit_reply_parsing():
    assert judges.parse_fit("KEPT - same decision") == (True, "same decision")
    assert judges.parse_fit("**BROKE** - changed the amount")[0] is False
    assert judges.parse_fit("Verdict: KEPT - fine")[0] is True
    # prose that merely contains the words is not a verdict; the verdict line decides
    assert judges.parse_fit("The candidate kept the tool but changed the amount.\nBROKE - amount 50 -> 100") == \
        (False, "amount 50 -> 100")
    assert judges.parse_fit("It broke nothing material.")[0] is None           # no verdict: the vote abstains
    assert judges.parse_fit("") == (None, "no verdict in the judge's reply")
    assert judges.parse_fit("EQUIVALENT - same decision and identifier") == (True, "same decision and identifier")
    assert judges.parse_fit("VARIATION - another valid option") == (True, "variation: another valid option")
    assert judges.parse_fit("**DRIFT** - claims an unstated policy") == (False, "claims an unstated policy")
    assert judges.parse_fit("UNSURE - output is truncated") == (None, "unsure: output is truncated")


def test_coherence_reply_parsing():
    assert judges.parse_coherence("CONSISTENT 4/5\nDIVERGENT 2\n2: changed the amount", 5) == (4, {2}, {2: "changed the amount"})
    assert judges.parse_coherence("CONSISTENT 5/5\nDIVERGENT none", 5) == (5, set(), {})
    assert judges.parse_coherence("CONSISTENT 3/5", 5) == (3, set(), {})
    assert judges.parse_coherence("DIVERGENT 2\n3: a reason", 5)[:2] == (4, {2})   # the list stays on its own line
    assert judges.parse_coherence("", 5) is None                                     # unreadable -> unknown, never 5/5
    assert judges.parse_coherence("I cannot assess this.", 5) is None


# ── the engine ───────────────────────────────────────────────────────────────────────────────────────────────────
def _trace(user="refund order 1"):
    return {"input_messages": [{"role": "system", "content": "Billing agent."}, {"role": "user", "content": user}],
            "usage": {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 0}, "runtime_ms": 900}


def _run(trace, model):
    if model == "broken-model":
        raise RuntimeError("404 model not found")
    return {"output": "%s says ok" % model, "usage": {"input": 100, "output": 20, "cache_read": 0, "cache_write": 0},
            "latency_ms": 10, "model": model, "stop_reason": "end_turn", "degraded": False}


def _judges(monkeypatch, kept_if=lambda cand: True, agree=5):
    monkeypatch.setattr(engine.judges, "fit", lambda ref, cand, req, spec: (kept_if(cand), "why", 0.001))
    monkeypatch.setattr(engine.judges, "coherence",
                        lambda req, outs, spec: {"agree": agree, "of": len(outs), "divergent": set(), "reasons": {},
                                                 "cost_usd": 0.002})


def _item(candidates=("claude-haiku-4-5",), recorded="recorded answer"):
    return Item(id="r1", request=_trace(), model="claude-sonnet-5", recorded=recorded,
                candidates=[Candidate("c%d" % i, m) for i, m in enumerate(candidates)])


def test_within_with_measured_runs(monkeypatch):
    _judges(monkeypatch)
    r = validate(_item(), Spec(), _run)
    c = r["candidates"][0]
    assert (c["verdict"], c["kept"], c["of"], c["fit_rate"]) == ("within", 5, 5, 1.0)
    assert r["reference"]["of"] == 5 and r["reference"]["consistency"] == 1.0 and r["reference"]["runs"][0]["recorded"] is True
    assert c["runs"][0]["latency_ms"] == 10 and c["runs"][0]["cost_usd"] > 0 and c["runs"][0]["votes"] == "3/3"
    assert c["latency_ms"] == 10 and r["spend_usd"] > 0 and c["judge_reason"] == "why"


def test_rates_not_counts(monkeypatch):
    _judges(monkeypatch)                                               # 3/3 candidate runs vs a 5/5 reference set
    c = validate(_item(), Spec(candidate_runs=3), _run)["candidates"][0]
    assert (c["verdict"], c["kept"], c["of"]) == ("within", 3, 3)      # the old count arithmetic would have failed this


def test_unverified_cases(monkeypatch):
    _judges(monkeypatch, agree=None)
    assert validate(_item(), Spec(), _run)["candidates"][0]["verdict"] == "unverified"       # unreadable coherence
    _judges(monkeypatch, agree=2)
    c = validate(_item(), Spec(), _run)["candidates"][0]
    assert c["verdict"] == "unverified" and "2/5 consistent" in c["reason"]                  # below the floor
    _judges(monkeypatch)
    c = validate(_item(), Spec(cap_chars=5, ref_chars=5), _run)["candidates"][0]
    assert c["verdict"] == "unverified" and "budget" in c["reason"]                          # too long to judge whole
    c = validate(_item(candidates=("broken-model",)), Spec(), _run)["candidates"][0]
    assert c["verdict"] == "unverified" and "5 of 5 candidate calls failed" in c["reason"]
    assert c["runs"][0]["error"].startswith("404")


def test_outside_and_no_recorded(monkeypatch):
    _judges(monkeypatch, kept_if=lambda cand: "haiku" in cand, agree=4)
    r = validate(_item(candidates=("claude-haiku-4-5", "gpt-5"), recorded=None), Spec(), _run)
    assert len(r["reference"]["runs"]) == 5 and not any(x.get("recorded") for x in r["reference"]["runs"])
    by = {c["label"]: c for c in r["candidates"]}
    assert by["c0"]["verdict"] == "within" and by["c1"]["verdict"] == "outside"
    assert by["c0"]["model"] == "claude-haiku-4-5" and by["c1"]["model"] == "gpt-5"


def test_many_items_keep_order(monkeypatch):
    _judges(monkeypatch)
    items = [Item(id="a", request=_trace("a"), model="claude-sonnet-5", recorded="x", candidates=[Candidate("k", "claude-haiku-4-5")]),
             Item(id="b", request=_trace("b"), model="claude-sonnet-5", recorded="y", candidates=[])]
    out = validate_many(items, Spec(candidate_runs=2, votes=1), _run)
    assert [o["id"] for o in out] == ["a", "b"] and out[1]["candidates"] == []


# ── the downgrade adapter: report rows + call-site roll-up ───────────────────────────────────────────────────────
def test_downgrade_rollup(monkeypatch):
    def fake(items, spec, run=None):
        return [{"id": it.id, "model": it.model, "spend_usd": 0.01,
                 "reference": {"agree": 5, "of": 5, "runs": [{"output": "o", "agrees": True, "reason": ""}]},
                 "candidates": [{"label": "cheaper", "model": "claude-haiku-4-5", "kept": 4, "of": 5,
                                 "verdict": "within" if int(it.id) < 4 else "outside", "reason": "r",
                                 "latency_ms": 10, "cost_usd": 0.001,
                                 "runs": [{"output": "c", "kept": True, "votes": "3/3", "judges": [], "reason": "r",
                                           "latency_ms": 10, "cost_usd": 0.001}]}]} for it in items]
    monkeypatch.setattr(downgrade_refset, "validate_many", fake)
    sample = [_trace("t%d" % i) for i in range(5)]
    rows, node, safe = downgrade_refset.prove_transform_refset(sample, "claude-haiku-4-5", "claude-sonnet-5")
    assert (node, safe) == ("SAFE", 4) and rows[4]["verdict"] == "NOT-SAFE"        # 4 of 5 inputs -> SAFE
    assert rows[0]["self_rate"] == "5/5" and rows[0]["samples"][0]["votes"] == "3/3" and rows[0]["cost_usd"] == 0.001
    rows, node, safe = downgrade_refset.prove_transform_refset(sample[:3], "claude-haiku-4-5", "claude-sonnet-5")
    assert (node, safe) == ("SAFE", 3)
    monkeypatch.setattr(downgrade_refset, "validate_many",
                        lambda items, spec, run=None: [dict(r, candidates=[dict(r["candidates"][0], verdict="unverified")])
                                                       for r in fake(items, spec)])
    assert downgrade_refset.prove_transform_refset(sample[:2], "claude-haiku-4-5", "claude-sonnet-5")[1] == "NOT-SAFE"


# ── the model call returns a measured record, and only a 400 degrades the request ────────────────────────────────
def _resp(text="hi"):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], model="claude-haiku-4-5-20251001",
                                 stop_reason="end_turn",
                                 usage=types.SimpleNamespace(input_tokens=50, output_tokens=5, cache_read_input_tokens=0,
                                                             cache_creation_input_tokens=0))


def test_run_records_usage_and_degrades_only_on_400(monkeypatch):
    seen = []
    monkeypatch.setattr(llm_client, "_complete_raw", lambda **kw: (seen.append(kw), _resp())[1])
    r = llm_client.run(model="claude-haiku-4-5", messages=[{"role": "user", "content": "x"}], max_tokens=10)
    assert r["text"] == "hi" and r["usage"]["input"] == 50 and r["model"] == "claude-haiku-4-5-20251001"
    assert r["latency_ms"] >= 0 and r["stop_reason"] == "end_turn" and r["degraded"] is False

    bad = anthropic.BadRequestError.__new__(anthropic.BadRequestError)            # the target rejects the request
    calls = []
    def flaky(**kw):
        calls.append(kw)
        if "thinking" in kw:
            raise bad
        return _resp()
    monkeypatch.setattr(llm_client, "_complete_raw", flaky)
    r = llm_client.run(model="m", messages=[], max_tokens=10, reasoning=True)
    assert r["degraded"] is True and "thinking" not in calls[-1] and len(calls) == 2

    monkeypatch.setattr(llm_client, "_complete_raw", lambda **kw: (_ for _ in ()).throw(RuntimeError("network")))
    try:
        llm_client.run(model="m", messages=[], max_tokens=10, reasoning=True)
        raise AssertionError("a non-400 failure must propagate, not silently change the request")
    except RuntimeError:
        pass


def test_gateway_is_one_setting_and_skips_its_cache_and_log(monkeypatch):
    from app import registry
    monkeypatch.setattr(llm_client, "_client", None)
    monkeypatch.setattr(llm_client, "_gateway", False)
    monkeypatch.setattr(registry, "_gateway", lambda: ("http://gw.test:4000", "sk-gw"))
    c = llm_client.client()                                   # the registry's (url, key) is what we send through
    assert str(c.base_url).startswith("http://gw.test:4000") and c.api_key == "sk-gw" and llm_client._gateway

    sent = []
    fake = types.SimpleNamespace(messages=types.SimpleNamespace(create=lambda **kw: (sent.append(kw), _resp())[1]))
    monkeypatch.setattr(llm_client, "client", lambda: fake)
    monkeypatch.setattr(registry, "serving_name", lambda m: m)
    llm_client._complete_raw(model="gpt-5.4-mini", messages=[], max_tokens=5)
    monkeypatch.setattr(llm_client, "_gateway", False)
    llm_client._complete_raw(model="claude-haiku-4-5", messages=[], max_tokens=5)
    assert sent[0]["extra_body"] == {"cache": {"no-cache": True}, "no-log": True}   # fresh answer, not logged as traffic
    assert "extra_body" not in sent[1]                        # the direct Anthropic API gets the plain request


def test_unreachable_endpoint_is_raised_not_recorded(monkeypatch):
    import httpx
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)
    def refused(**kw):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "http://gw.test"))
    monkeypatch.setattr(llm_client, "client", lambda: types.SimpleNamespace(messages=types.SimpleNamespace(create=refused)))
    for call in (lambda: llm_client._complete_raw(model="m", messages=[], max_tokens=5),            # after its retries
                 lambda: validate(_item(), Spec(), lambda trace, model: llm_client._complete_raw(model=model))):
        try:
            call()
            raise AssertionError("an unreachable endpoint must not become a failed run")
        except llm_client.Unreachable:
            pass


def test_replays_stay_under_the_sdks_non_streaming_limit(monkeypatch):
    from app.services import audit, llm_client
    seen = {}

    def run(**kw):
        seen.update(kw)
        return {"text": "ok", "tool_calls": [], "usage": {}, "latency_ms": 1, "model": kw["model"],
                "stop_reason": "end_turn", "degraded": False}
    monkeypatch.setattr(llm_client, "run", run)
    audit.replay({"input_messages": [{"role": "user", "content": "hi"}], "max_output_tokens": 32768}, "gpt-5.4-mini")
    assert seen["max_tokens"] == audit.NONSTREAMING_MAX_TOKENS        # logged 32k would be refused by the SDK


def test_out_of_credit_is_raised_at_once_not_recorded(monkeypatch):
    import httpx
    calls = []
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)
    for status, body in ((429, "Budget has been exceeded! Key=token-auditor Current cost: 25.0"),     # LiteLLM budget
                         (429, "{'error': {'message': 'You exceeded your current quota, please check your plan', "
                               "'type': 'insufficient_quota', 'code': 'insufficient_quota'}}"),       # OpenAI
                         (400, "Your credit balance is too low to access the Anthropic API"),         # Anthropic
                         (401, "invalid x-api-key")):
        def refused(**kw):
            calls.append(1)
            raise anthropic.APIStatusError(body, response=httpx.Response(status, request=httpx.Request("POST", "http://gw")),
                                           body=None)
        monkeypatch.setattr(llm_client, "client", lambda: types.SimpleNamespace(messages=types.SimpleNamespace(create=refused)))
        calls.clear()
        with pytest.raises(llm_client.Refused):
            llm_client._complete_raw(model="m", messages=[], max_tokens=5)
        assert len(calls) == 1                                     # not retried
        with pytest.raises(llm_client.Refused):                    # a model call raises it, not an unverified run
            validate(_item(), Spec(), lambda trace, model: llm_client._complete_raw(model=model))
    for status, body in ((401, "key not allowed to access model. This key can only access models=['gpt-5.4']"),
                         (403, "Request blocked by content policy"),
                         (429, "Quota exceeded for metric: generate_content requests per minute. "
                               "You exceeded your current quota")):                  # one model / request / a rate limit
        e = anthropic.APIStatusError(body, response=httpx.Response(status, request=httpx.Request("POST", "http://gw")),
                                     body=None)
        assert not llm_client._refused(e), body


def test_a_timeout_is_one_slow_call_not_an_unreachable_endpoint(monkeypatch):
    import httpx
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: None)
    def slow(**kw):
        raise anthropic.APITimeoutError(request=httpx.Request("POST", "http://gw.test"))
    monkeypatch.setattr(llm_client, "client", lambda: types.SimpleNamespace(messages=types.SimpleNamespace(create=slow)))
    with pytest.raises(anthropic.APITimeoutError):
        llm_client._complete_raw(model="m", messages=[], max_tokens=5)
    _judges(monkeypatch)
    c = validate(_item(), Spec(), lambda trace, model: llm_client._complete_raw(model=model))["candidates"][0]
    assert c["verdict"] == "unverified"                          # that request, not the whole job


def test_a_failed_judge_call_leaves_the_request_unverified(monkeypatch):
    _judges(monkeypatch)
    def blocked(*a):
        raise RuntimeError("400 output blocked by content filter")
    monkeypatch.setattr(engine.judges, "fit", blocked)
    c = validate(_item(), Spec(), _run)["candidates"][0]
    assert (c["verdict"], c["reason"]) == ("unverified", "a judge call failed: 400 output blocked by content filter")
    _judges(monkeypatch)
    monkeypatch.setattr(engine.judges, "coherence", blocked)
    c = validate(_item(), Spec(), _run)["candidates"][0]
    assert c["verdict"] == "unverified" and c["reason"].startswith("a judge call failed")



def test_unsure_votes_abstain(monkeypatch):
    _judges(monkeypatch)
    votes = iter([(True, "same", 0.0), (None, "unsure: truncated", 0.0), (True, "same", 0.0)] * 5)
    monkeypatch.setattr(engine.judges, "fit", lambda *a: next(votes))
    one_at_a_time = Spec(max_parallel=1)                         # votes reach runs in order
    c = validate(_item(), one_at_a_time, _run)["candidates"][0]
    assert (c["verdict"], c["kept"]) == ("within", 5)            # 2 of 3 decided, both kept: one unsure vote is fine
    votes = iter([(None, "unsure: truncated", 0.0), (None, "unsure: truncated", 0.0), (True, "same", 0.0)] * 5)
    c = validate(_item(), one_at_a_time, _run)["candidates"][0]
    assert (c["verdict"], c["reason"]) == ("unverified", "unsure: truncated")   # most abstained: not judged
