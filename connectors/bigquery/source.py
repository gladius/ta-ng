"""The BigQuery log (LiteLLM's, one row per request/response): a logged call -> a contract trace, and the lookup of
logged calls by id. The table and its columns are core/ledger/bq.py's settings, plus two optional ones:

    BQ_PARAMS_COLUMN    JSON max_tokens / tools / tool_choice, when the request column holds only messages
    BQ_MODEL_COLUMN     the model that answered, when the response names a router (e.g. jev-router)
"""
import hashlib
import json
import re
import subprocess

import credentials
from connectors import schema

try:                                       # machine-specific auth or proxies, git-ignored
    import connectors.bigquery._local  # noqa: F401
except ModuleNotFoundError as e:
    if e.name != "connectors.bigquery._local":
        raise                              # a real error inside _local.py must show, not vanish

LOOKUP_DAYS = 2         # a lookup by plain request_id reads these last days (BigQuery has no index: every day is a full read)


def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _callsite(messages):
    """'<first words of the system prompt>-<hash>', numbers masked so a date or id in the prompt doesn't split it."""
    system = "\n".join(m["content"] for m in messages if m["role"] == "system").strip()
    if not system:
        return "no-system-prompt"
    words = re.findall(r"[a-z0-9]+", system.lower())[:4]
    return "-".join(words + [hashlib.sha1(re.sub(r"\d+", "#", system).encode("utf-8")).hexdigest()[:6]])


def _client(project):
    """Application Default Credentials when set (always on Cloud Run), else the dev machine's `gcloud` login."""
    from google.cloud import bigquery                      # lazy: the source lists before the library is installed
    from google.auth.exceptions import DefaultCredentialsError
    try:
        return bigquery.Client(project=project)
    except DefaultCredentialsError:
        from google.oauth2.credentials import Credentials
        token = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True,
                               check=True).stdout.strip()
        return bigquery.Client(project=project, credentials=Credentials(token))


# The logged request fields a re-run sends as they were (model and max_tokens are set per run).
SENT = ("messages", "tools", "tool_choice", "parallel_tool_calls", "response_format", "temperature", "top_p", "seed",
        "stop", "reasoning_effort", "frequency_penalty", "presence_penalty")
_ROLES = {"system", "developer", "user", "assistant", "tool", "function"}


def _as_sent(body):
    """The request's SENT fields, or None when its messages aren't OpenAI chat messages (rebuilt from the trace)."""
    msgs = body.get("messages")
    if not (isinstance(msgs, list) and msgs and all(isinstance(m, dict) and m.get("role") in _ROLES for m in msgs)):
        return None
    return {k: body[k] for k in SENT if body.get(k) is not None}


def to_trace(request, response, agent):
    """One logged (request, response) pair -> a contract trace."""
    req, resp = _json(request), _json(response) or {}
    body = req if isinstance(req, dict) else {"messages": req}
    msgs = schema.norm_messages(body.get("messages"))
    message = ((resp.get("choices") or [{}])[0] or {}).get("message") or {}
    out = {"generations": [[{"message": message}]]}     # the shape schema's output readers already take
    from app.services.llm_client import usage as read_usage
    u = read_usage(resp.get("usage") or {})
    t = schema.build_trace(
        trace_id=resp.get("id"), agent_id=agent, node_id=_callsite(msgs),
        model=resp.get("model") or body.get("model"), input_messages=msgs,
        output=schema.render_behavior(out), tools_called=schema.tool_calls_from_output(out),
        usage={"input_tokens": u["input"] + u["cache_read"] + u["cache_write"], "output_tokens": u["output"],
               "cached_tokens": u["cache_read"]},                  # the trace's input_tokens: the whole prompt
        tools_defined=schema.norm_tools(body.get("tools")), tool_choice=body.get("tool_choice"), source="bigquery")
    t["max_output_tokens"] = body.get("max_tokens") or body.get("max_completion_tokens")
    t["logged_request"] = _as_sent(body)
    return t


def lookup(request_ids, days=LOOKUP_DAYS):
    """{request_id: (OpenAI-format request body, response or None)}; unknown ids are absent. Two kinds of id:
    a log id (a caller's request_id: the last `days` days are read) or a row key from the day processing,
    `<log id>@<unix micros>[#n]` (exactly that call, read from its own day only: the log id alone may be shared by
    several calls). The body's `model` is the one that answered (BQ_MODEL_COLUMN, else the response's)."""
    import datetime
    from core.ledger import bq
    c = bq.cfg()
    params_col, model_col = credentials.get_config("BQ_PARAMS_COLUMN"), credentials.get_config("BQ_MODEL_COLUMN")
    extra = "".join(", `%s` AS %s" % (col, a) for col, a in ((params_col, "params"), (model_col, "served")) if col)
    plain = sorted(i for i in request_ids if "@" not in i)
    keys = {i.split("#")[0]: i for i in request_ids if "@" in i}          # one row per id@time; #n repeats share it
    rows = []
    if plain:
        sql = ("SELECT CAST(`{id}` AS STRING) AS k, `{req}` AS request, `{resp}` AS response{x} FROM `{table}` "
               "WHERE DATE(`{time}`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY) AND `{id}` IN UNNEST(@ids)"
               ).format(x=extra, **c)
        rows += bq.query(sql, {"ids": plain, "days": days}, timeout=120)
    if keys:
        dates = sorted({datetime.datetime.fromtimestamp(int(k.split("@")[1]) / 1e6, datetime.timezone.utc).date() for k in keys})
        sql = ("SELECT k, request, response{xs} FROM (SELECT CONCAT(CAST(`{id}` AS STRING), '@', "
               "CAST(UNIX_MICROS(CAST(`{time}` AS TIMESTAMP)) AS STRING)) AS k, `{req}` AS request, `{resp}` AS response{x} "
               "FROM `{table}` WHERE DATE(`{time}`) IN UNNEST(@dates)) WHERE k IN UNNEST(@keys) "
               "QUALIFY ROW_NUMBER() OVER (PARTITION BY k) = 1"
               ).format(x=extra, xs="".join(", " + a for col, a in ((params_col, "params"), (model_col, "served")) if col), **c)
        rows += [dict(r, k=keys[r["k"]]) for r in bq.query(sql, {"keys": sorted(keys), "dates": dates}, timeout=600)]
    out = {}
    for row in rows:
        req, resp = _json(row["request"]), _json(row["response"])
        body = dict(req) if isinstance(req, dict) else {"messages": req}
        params = _json(row["params"]) if params_col else None
        if isinstance(params, dict):
            body = {**{k: params[k] for k in ("max_tokens", "max_completion_tokens", *SENT[1:])
                       if params.get(k) is not None}, **body}
        resp = resp if isinstance(resp, dict) else None
        served = (row["served"] if model_col else None) or (resp or {}).get("model")
        if served:
            body["model"] = served
        out[row["k"]] = (body, resp)
    return out
