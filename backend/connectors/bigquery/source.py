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

LOOKUP_DAYS = int(credentials.get_config("LOOKUP_DAYS") or 30)    # how far back a plain request_id is searched for




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
    """Application Default Credentials when set, else the dev machine's `gcloud` login."""
    from google.cloud import bigquery                      # lazy: the source lists before the library is installed
    from google.auth.exceptions import DefaultCredentialsError
    try:
        return bigquery.Client(project=project)
    except DefaultCredentialsError:
        from google.oauth2.credentials import Credentials
        token = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True,
                               check=True).stdout.strip()
        return bigquery.Client(project=project, credentials=Credentials(token))


# A re-run sends the logged request as it was: every field, known to us or not (`thinking`, a provider's own options),
# except these. They are set per run (model, the output cap), belong to the caller's transport or identity, or are the
# gateway's own controls, which a re-run sets itself (no cache, no log).
NOT_SENT = frozenset((
    "model", "max_tokens", "max_completion_tokens",                         # set per run
    "stream", "stream_options", "n",                                        # how the answer came back; we read one whole
    "user", "metadata", "safety_identifier", "prompt_cache_key", "store",   # who called, and what they keep
    "service_tier",                                                         # billing and speed, not behaviour
    "cache", "no-log", "tags", "fallbacks", "guardrails", "mock_response", "api_key", "api_base", "base_url",
    "timeout", "extra_headers", "extra_body", "proxy_server_request"))      # the gateway's and the client's own


def sendable(field):
    return field not in NOT_SENT and not field.startswith("litellm_")
_ROLES = {"system", "developer", "user", "assistant", "tool", "function"}


_BLOCKS = {"tool_use", "tool_result", "thinking", "redacted_thinking"}       # content blocks only Anthropic's format has


def request_format(body):
    """'anthropic' for a request logged in Anthropic's own message format (the gateway's /v1/messages), else 'openai'.
    A re-run goes back through the same endpoint: the other one refuses these fields."""
    if isinstance(body.get("system"), (str, list)) or "output_config" in body or "anthropic_version" in body:
        return "anthropic"
    for m in body.get("messages") or []:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list) and any(isinstance(b, dict) and (b.get("type") in _BLOCKS or (
                b.get("type") == "image" and "source" in b)) for b in content):
            return "anthropic"
    return "openai"


def _answer(resp):
    """The logged answer as the message the schema readers take, whichever format it was logged in."""
    if isinstance(resp.get("content"), list) and not resp.get("choices"):          # Anthropic's: content blocks
        blocks = [b for b in resp["content"] if isinstance(b, dict)]
        return {"content": "\n".join(b.get("text") or "" for b in blocks if b.get("type") == "text"),
                "tool_calls": [{"function": {"name": b.get("name"), "arguments": b.get("input") or {}}}
                               for b in blocks if b.get("type") == "tool_use"]}
    return ((resp.get("choices") or [{}])[0] or {}).get("message") or {}


def _as_sent(body):
    """The request as a re-run sends it (all but NOT_SENT), or None when its messages aren't OpenAI chat messages
    (then it is rebuilt from the trace)."""
    msgs = body.get("messages")
    if not (isinstance(msgs, list) and msgs and all(isinstance(m, dict) and m.get("role") in _ROLES for m in msgs)):
        return None
    return {k: v for k, v in body.items() if sendable(k) and v is not None}


def to_trace(request, response, agent):
    """One logged (request, response) pair -> a contract trace."""
    req, resp = _json(request), _json(response) or {}
    body = req if isinstance(req, dict) else {"messages": req}
    fmt = request_format(body)
    lead = [{"role": "system", "content": body["system"]}] if fmt == "anthropic" and body.get("system") else []
    msgs = schema.norm_messages(lead + body["messages"] if lead and isinstance(body.get("messages"), list)
                                else body.get("messages"))
    message = _answer(resp)
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
    t["logged_format"] = fmt
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
        # Two steps: the log has no index on the id, so a filter by id still bills every byte of the days it scans.
        # First the id and time columns only (gigabytes over LOOKUP_DAYS), then the bodies from the days found.
        sql = ("SELECT DISTINCT DATE(`{time}`) AS d FROM `{table}` "
               "WHERE DATE(`{time}`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY) AND `{id}` IN UNNEST(@ids)").format(**c)
        dates = sorted(r["d"] for r in bq.query(sql, {"ids": plain, "days": days}, timeout=120))
        if dates:
            sql = ("SELECT CAST(`{id}` AS STRING) AS k, `{req}` AS request, `{resp}` AS response{x} FROM `{table}` "
                   "WHERE DATE(`{time}`) IN UNNEST(@dates) AND `{id}` IN UNNEST(@ids)").format(x=extra, **c)
            rows += bq.query(sql, {"ids": plain, "dates": dates}, timeout=600)
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
            body = {**{k: v for k, v in params.items() if v is not None and k != "messages"
                       and (sendable(k) or k in ("max_tokens", "max_completion_tokens"))}, **body}
        resp = resp if isinstance(resp, dict) else None
        served = (row["served"] if model_col else None) or (resp or {}).get("model")
        if served:
            body["model"] = served
        out[row["k"]] = (body, resp)
    return out
