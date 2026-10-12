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

LOOKUP_DAYS = 30        # a lookup by id reads only these days of the log (its partitions), not the whole table


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
    """{request_id: (OpenAI-format request body, response or None)} for the ids logged in the last `days` days, in
    one query; unknown ids are absent. The body's `model` is the one that answered (BQ_MODEL_COLUMN, else the
    response's)."""
    from core.ledger import bq
    c = bq.cfg()
    params_col, model_col = credentials.get_config("BQ_PARAMS_COLUMN"), credentials.get_config("BQ_MODEL_COLUMN")
    extra = "".join(", `%s` AS %s" % (col, a) for col, a in ((params_col, "params"), (model_col, "served")) if col)
    sql = ("SELECT `{id}` AS id, `{req}` AS request, `{resp}` AS response{x} FROM `{table}` "
           "WHERE DATE(`{time}`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY) AND `{id}` IN UNNEST(@ids)"
           ).format(x=extra, **c)
    rows = bq.query(sql, {"ids": sorted(request_ids), "days": days}, timeout=120)
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
        out[row["id"]] = (body, resp)
    return out
