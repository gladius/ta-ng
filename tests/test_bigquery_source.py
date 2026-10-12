"""BigQuery source: one logged OpenAI-shaped (request, response) row -> a contract trace, grouped by call-site.
Offline — the BigQuery client is replaced by a fake."""
import json

from connectors.bigquery import source as bq

_RESP = {"id": "chatcmpl-1", "model": "gpt-4o",
         "choices": [{"message": {"role": "assistant", "content": "Refund issued.",
                                  "tool_calls": [{"id": "c1", "type": "function",
                                                  "function": {"name": "refund", "arguments": "{\"amount\": 5}"}}]}}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 40}}}


def _msgs(system, user="refund my order"):
    return ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]


def test_row_to_trace():
    t = bq.to_trace(json.dumps(_msgs("You are a billing assistant.")), json.dumps(_RESP), "agent")
    assert (t["trace_id"], t["model_raw"], t["agent_id"]) == ("chatcmpl-1", "gpt-4o", "agent")
    assert t["usage"] == {"input_tokens": 100, "output_tokens": 10, "cached_tokens": 40}
    assert t["output"] == 'Refund issued.\n[calls refund({"amount": 5})]'
    assert t["tools_called"] == ["refund"]
    assert [m["role"] for m in t["input_messages"]] == ["system", "user"]
    anthropic_shaped = dict(_RESP, usage={"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 40})
    t = bq.to_trace(json.dumps(_msgs("You are a billing assistant.")), json.dumps(anthropic_shaped), "agent")
    assert t["usage"] == {"input_tokens": 140, "output_tokens": 10, "cached_tokens": 40}   # Anthropic's excludes the cache
    from validation.engine import _recorded_run                 # and the original's cost charges the cache once
    item = type("Item", (), {"request": t, "recorded": "x", "model": "gpt-4o"})()
    assert _recorded_run(item)["usage"] == {"input": 100, "output": 10, "cache_read": 40, "cache_write": 0}


def test_request_body_form():
    body = {"messages": _msgs("You are a billing assistant."), "max_tokens": 256, "tool_choice": "auto",
            "tools": [{"type": "function", "function": {"name": "refund", "parameters": {"type": "object"}}}]}
    t = bq.to_trace(body, _RESP, "agent")
    assert t["max_output_tokens"] == 256 and t["tool_choice"] == "auto"
    assert [name for name, _ in t["tools_defined"]] == ["refund"]


def test_callsite_grouping():
    key = lambda system: bq.to_trace(_msgs(system), _RESP, "a")["node_id"]
    assert key("Billing agent. Today is 2026-10-05.") == key("Billing agent. Today is 2026-11-30.")
    assert key("Billing agent. Today is 2026-10-05.") != key("Triage agent. Today is 2026-10-05.")
    assert key("") == "no-system-prompt"
    assert "," not in key("You are, in short, a helper.")         # keys travel comma-joined in the audit URL


def test_no_adc_falls_back_to_gcloud_login(monkeypatch):
    from google.cloud import bigquery
    from google.auth.exceptions import DefaultCredentialsError
    used = {}

    class FakeClient:
        def __init__(self, project, credentials=None):
            if credentials is None:
                raise DefaultCredentialsError("Your default credentials were not found.")
            used["project"], used["token"] = project, credentials.token

    monkeypatch.setattr(bigquery, "Client", FakeClient)
    monkeypatch.setattr(bq.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "tok-123\n"})())
    bq._client("proj")
    assert used == {"project": "proj", "token": "tok-123"}


_SETTINGS = {"BQ_TABLE": "proj.ds.v_requests", "BQ_REQUEST_COLUMN": "messages", "BQ_RESPONSE_COLUMN": "response",
             "BQ_ID_COLUMN": "request_id", "BQ_USECASE_COLUMN": "vegas_usecase_name",
             "BQ_TIME_COLUMN": "created_timestamp_utc", "BQ_LATENCY_COLUMN": "duration_ms"}


def test_every_column_setting_is_required(monkeypatch):
    from core.ledger import bq as core_bq
    settings = dict(_SETTINGS, BQ_USECASE_COLUMN=None, BQ_LATENCY_COLUMN=None)
    monkeypatch.setattr(bq.credentials, "get_config", lambda k, d=None, **kw: settings.get(k))
    assert not core_bq.configured()
    try:
        core_bq.cfg()
        raise AssertionError("cfg() guessed the missing columns")
    except RuntimeError as e:
        assert str(e) == "BigQuery settings missing: BQ_USECASE_COLUMN, BQ_LATENCY_COLUMN"


def test_lookup_by_request_id(monkeypatch):
    from core.ledger import bq as core_bq
    settings = dict(_SETTINGS, BQ_PARAMS_COLUMN="model_parameters", BQ_MODEL_COLUMN="model")
    monkeypatch.setattr(bq.credentials, "get_config", lambda k, d=None, **kw: settings.get(k))
    routed = dict(_RESP, model="jev-router")                    # a router in front names itself in the response
    rows = [{"id": "chatcmpl-1", "request": json.dumps(_msgs("Billing agent.")), "response": json.dumps(routed),
             "params": json.dumps({"max_tokens": 32768, "tools": [{"type": "function", "function": {"name": "refund"}}],
                                   "temperature": 0.2}), "served": "openai/gpt-5.4-mini"}]
    seen = {}
    monkeypatch.setattr(core_bq, "query", lambda sql, params, timeout=None: seen.update(sql=sql, params=params) or rows)
    got = bq.lookup(["chatcmpl-1", "chatcmpl-2"])
    assert "WHERE DATE(`created_timestamp_utc`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY) AND `request_id` IN " \
           "UNNEST(@ids)" in seen["sql"]                         # only the window's partitions, never the whole table
    assert seen["params"] == {"ids": ["chatcmpl-1", "chatcmpl-2"], "days": 30}
    body, resp = got["chatcmpl-1"]
    assert body["model"] == "openai/gpt-5.4-mini" and body["max_tokens"] == 32768 and body["temperature"] == 0.2
    assert [t["function"]["name"] for t in body["tools"]] == ["refund"] and resp["id"] == "chatcmpl-1"
    assert list(got) == ["chatcmpl-1"]                         # an unknown id is just absent
