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


def test_pull_buckets_and_skips(monkeypatch):
    monkeypatch.setenv("BQ_TABLE", "proj.ds.v_requests")
    monkeypatch.setenv("BQ_REQUEST_COLUMN", "messages")
    monkeypatch.setenv("BQ_RESPONSE_COLUMN", "response")
    rows = [{"request": json.dumps(_msgs("Billing agent.")), "response": json.dumps(_RESP)},
            {"request": json.dumps(_msgs("Billing agent.", "where is my refund")), "response": json.dumps(_RESP)},
            {"request": json.dumps(_msgs("Triage agent.")), "response": json.dumps(_RESP)},
            {"request": json.dumps([{"role": "system", "content": "no user turn"}]), "response": json.dumps(_RESP)}]
    seen = {}

    class FakeClient:
        def __init__(self, project):
            seen["project"] = project

        def query(self, sql, job_config=None):
            seen["sql"] = sql
            return type("Job", (), {"result": lambda self, timeout=None: rows})()

    from google.cloud import bigquery
    monkeypatch.setattr(bigquery, "Client", FakeClient)
    buckets, skipped = bq.SOURCE.pull("bq", "v_requests")
    assert seen["project"] == "proj"
    assert "FROM `proj.ds.v_requests`" in seen["sql"] and "TO_JSON_STRING(`messages`)" in seen["sql"]
    assert sorted(len(b) for b in buckets.values()) == [1, 2]     # two call-sites: billing (2 calls), triage (1)
    assert len(skipped) == 1                                       # the row with no user turn
    assert bq.SOURCE.agents("bq") == [{"id": "v_requests", "name": "v_requests"}]


def test_no_adc_falls_back_to_gcloud_login(monkeypatch):
    monkeypatch.setenv("BQ_TABLE", "proj.ds.v_requests")
    monkeypatch.setenv("BQ_REQUEST_COLUMN", "messages")
    monkeypatch.setenv("BQ_RESPONSE_COLUMN", "response")
    from google.cloud import bigquery
    from google.auth.exceptions import DefaultCredentialsError
    used = {}

    class FakeClient:
        def __init__(self, project, credentials=None):
            if credentials is None:
                raise DefaultCredentialsError("Your default credentials were not found.")
            used["token"] = credentials.token

        def query(self, sql, job_config=None):
            return type("Job", (), {"result": lambda self, timeout=None: []})()

    monkeypatch.setattr(bigquery, "Client", FakeClient)
    monkeypatch.setattr(bq.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "tok-123\n"})())
    assert bq.SOURCE.pull("bq", "v_requests") == ({}, [])
    assert used["token"] == "tok-123"


def test_lookup_by_request_id(monkeypatch):
    monkeypatch.setenv("BQ_TABLE", "proj.ds.v_requests")
    monkeypatch.setenv("BQ_REQUEST_COLUMN", "messages")
    monkeypatch.setenv("BQ_RESPONSE_COLUMN", "response")
    monkeypatch.setenv("BQ_PARAMS_COLUMN", "model_parameters")
    monkeypatch.setenv("BQ_MODEL_COLUMN", "model")
    routed = dict(_RESP, model="jev-router")                    # a router in front names itself in the response
    rows = [{"id": "chatcmpl-1", "request": json.dumps(_msgs("Billing agent.")), "response": json.dumps(routed),
             "params": json.dumps({"max_tokens": 32768, "tools": [{"type": "function", "function": {"name": "refund"}}],
                                   "temperature": 0.2}), "served": "openai/gpt-5.4-mini"}]
    seen = {}

    class FakeClient:
        def __init__(self, project):
            pass

        def query(self, sql, job_config=None):
            seen["sql"], seen["ids"] = sql, job_config.query_parameters[0].values
            return type("Job", (), {"result": lambda self, timeout=None: rows})()

    from google.cloud import bigquery
    monkeypatch.setattr(bigquery, "Client", FakeClient)
    got = bq.lookup(["chatcmpl-1", "chatcmpl-2"])
    assert "WHERE `request_id` IN UNNEST(@ids)" in seen["sql"] and sorted(seen["ids"]) == ["chatcmpl-1", "chatcmpl-2"]
    body, resp = got["chatcmpl-1"]
    assert body["model"] == "openai/gpt-5.4-mini" and body["max_tokens"] == 32768 and body["temperature"] == 0.2
    assert [t["function"]["name"] for t in body["tools"]] == ["refund"] and resp["id"] == "chatcmpl-1"
    assert list(got) == ["chatcmpl-1"]                         # an unknown id is just absent


def test_agent_callsite_and_time_columns(monkeypatch):
    for k, v in (("BQ_TABLE", "proj.ds.vegas"), ("BQ_REQUEST_COLUMN", "messages"), ("BQ_RESPONSE_COLUMN", "response"),
                 ("BQ_AGENT_COLUMN", "common_service_name"), ("BQ_NODE_COLUMN", "vegas_usecase_name"),
                 ("BQ_TIME_COLUMN", "published_dt"), ("BQ_DAYS", "3")):
        monkeypatch.setenv(k, v)
    rows = [{"request": json.dumps(_msgs("Billing agent.")), "response": json.dumps(_RESP), "node": "refunds"},
            {"request": json.dumps(_msgs("Triage agent.")), "response": json.dumps(_RESP), "node": None}]
    seen = []

    class FakeClient:
        def __init__(self, project):
            pass

        def query(self, sql, job_config=None):
            seen.append((sql, {p.name: p.value for p in job_config.query_parameters}))
            return type("Job", (), {"result": lambda self, timeout=None: rows if "AS request" in sql
                                    else [{"name": "billing-svc"}, {"name": "claims-svc"}]})()

    from google.cloud import bigquery
    monkeypatch.setattr(bigquery, "Client", FakeClient)
    assert bq.SOURCE.agents("bq") == [{"id": "billing-svc", "name": "billing-svc"},
                                      {"id": "claims-svc", "name": "claims-svc"}]
    assert "DISTINCT `common_service_name`" in seen[-1][0] and seen[-1][1] == {"days": 3}
    buckets, _ = bq.SOURCE.pull("bq", "billing-svc' OR 1=1 --", since_hours=6)
    sql, params = seen[-1]
    assert "`common_service_name` = @agent" in sql and params == {"agent": "billing-svc' OR 1=1 --", "days": 1}
    assert "DATE(`published_dt`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)" in sql
    assert "PARTITION BY `vegas_usecase_name`" in sql
    nodes = sorted(buckets)
    assert "refunds" in nodes and any(n.startswith("triage-agent") for n in nodes)    # NULL -> system-prompt hash
    assert all(t["agent_id"] == "billing-svc' OR 1=1 --" for b in buckets.values() for t in b)
