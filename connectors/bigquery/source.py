"""BigQuery DataSource — LLM calls logged one row per request/response in a single table or view, e.g. LiteLLM's
logs (trust_router's `litellm.v_requests`). Three .env settings; everything else is read out of the two JSON columns:

    BQ_TABLE            project.dataset.table_or_view
    BQ_REQUEST_COLUMN   the request: an OpenAI messages array, or a request body holding "messages" (+ "tools",
                        "tool_choice", "max_tokens")
    BQ_RESPONSE_COLUMN  the response: an OpenAI chat completion (id, model, choices, usage) — LiteLLM's logged shape
    BQ_ID_COLUMN        (optional, default request_id) the call's id, for validations submitted by request_id
    BQ_PARAMS_COLUMN    (optional) a JSON column with max_tokens / tools / tool_choice, when the request column holds
                        only the messages (as trust_router's view does: model_parameters)
    BQ_MODEL_COLUMN     (optional) the model that answered; set it when a router is in front, whose responses name
                        the router (e.g. jev-router) instead (trust_router's view: model). Else the response's model
    BQ_AGENT_COLUMN     (optional) the agent / service name: one agent per value instead of one for the whole table
    BQ_NODE_COLUMN      (optional) the call-site name; rows where it is NULL fall back to the system-prompt hash
    BQ_TIME_COLUMN      (optional) a DATE or TIMESTAMP partition column: pulls read only the last BQ_DAYS days
                        (default 7) instead of scanning the whole table

Without BQ_AGENT_COLUMN the table is ONE agent, and without BQ_NODE_COLUMN a call-site is its system prompt (numbers
masked, so a date in the prompt doesn't split it). Auth is Google Application Default Credentials, else the gcloud
login. Machine-specific auth or network setup goes in connectors/bigquery/_local.py, which git ignores and which is
imported first when present.
"""
import hashlib
import json
import math
import re
import subprocess

import credentials
from connectors import schema
from connectors.datasource import DataSource

try:                                       # machine-specific overrides (corporate auth, proxies), never in git
    import connectors.bigquery._local  # noqa: F401
except ModuleNotFoundError as e:
    if e.name != "connectors.bigquery._local":
        raise                              # a real error inside _local.py must show, not vanish

# ponytail: no time column configured -> an arbitrary _ROWS rows and a full scan of both columns per pull; add a
# partition-column filter + ORDER BY when the table outgrows that.
_ROWS = 2000
_COPIES = 5             # identical requests kept: health checks / probes repeat one prompt thousands of times


def _cfg():
    return tuple(credentials.get_config(k) for k in ("BQ_TABLE", "BQ_REQUEST_COLUMN", "BQ_RESPONSE_COLUMN"))


def _days(since_hours=None):
    """The lookback in whole days: since_hours rounded up, else BQ_DAYS (default 7)."""
    return math.ceil(since_hours / 24) if since_hours else int(credentials.get_config("BQ_DAYS", "7"))


def _window(params, days):
    """SQL keeping only the last `days` days of BQ_TIME_COLUMN (BigQuery then skips older partitions; DATE() works
    for a DATE or a TIMESTAMP column, and still prunes), or TRUE without one. Adds its query parameter to `params`."""
    from google.cloud import bigquery
    col = credentials.get_config("BQ_TIME_COLUMN")
    if not col:
        return "TRUE"
    params.append(bigquery.ScalarQueryParameter("days", "INT64", days))
    return "DATE(`%s`) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)" % col


def _agent():
    return (_cfg()[0] or "bigquery").split(".")[-1]


def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _callsite(messages):
    """'<first words of the system prompt>-<hash>' — the same call-site even when a date or id in the prompt changes."""
    system = "\n".join(m["content"] for m in messages if m["role"] == "system").strip()
    if not system:
        return "no-system-prompt"
    words = re.findall(r"[a-z0-9]+", system.lower())[:4]
    return "-".join(words + [hashlib.sha1(re.sub(r"\d+", "#", system).encode("utf-8")).hexdigest()[:6]])


def _client(project):
    """Google Application Default Credentials when set (always, on Cloud Run); otherwise the `gcloud` CLI login a
    dev machine already has — the same one `bq` uses."""
    from google.cloud import bigquery                      # lazy: the source lists before the library is installed
    from google.auth.exceptions import DefaultCredentialsError
    try:
        return bigquery.Client(project=project)
    except DefaultCredentialsError:
        from google.oauth2.credentials import Credentials
        token = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True,
                               check=True).stdout.strip()
        return bigquery.Client(project=project, credentials=Credentials(token))


# the fields of a logged OpenAI request a re-run sends as they were (model and max_tokens are set per run; stream,
# n, metadata, user etc. are not part of the behaviour)
SENT = ("messages", "tools", "tool_choice", "parallel_tool_calls", "response_format", "temperature", "top_p", "seed",
        "stop", "reasoning_effort", "frequency_penalty", "presence_penalty")
_ROLES = {"system", "developer", "user", "assistant", "tool", "function"}


def _as_sent(body):
    """The request's SENT fields, or None when its messages aren't OpenAI chat messages (the re-run then rebuilds the
    request from the trace)."""
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
    from app.services.llm_client import usage as read_usage    # THE usage reader: OpenAI- or Anthropic-shaped
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


def lookup(request_ids):
    """{request_id: (request body, response or None)} for the logged calls among `request_ids`, in one query. The body
    is OpenAI-format: messages, max_tokens and the other SENT fields when logged, and as `model` the model that
    answered (BQ_MODEL_COLUMN, else the response's). BQ_ID_COLUMN (default request_id) holds the id; BQ_PARAMS_COLUMN,
    when set, a JSON column holding max_tokens, tools, temperature etc. (the request column then being only the
    messages). Unknown ids
    are simply absent.
    ponytail: scans the whole table per call; filter on its partition column once it gets large."""
    from google.cloud import bigquery
    table, req_col, resp_col = _cfg()
    id_col = credentials.get_config("BQ_ID_COLUMN", "request_id")
    params_col, model_col = credentials.get_config("BQ_PARAMS_COLUMN"), credentials.get_config("BQ_MODEL_COLUMN")
    extra = "".join(", `%s` AS %s" % (c, a) for c, a in ((params_col, "params"), (model_col, "served")) if c)
    sql = "SELECT `{i}` AS id, `{q}` AS request, `{r}` AS response{x} FROM `{t}` WHERE `{i}` IN UNNEST(@ids)".format(
        i=id_col, q=req_col, r=resp_col, t=table, x=extra)
    rows = _client(table.split(".")[0]).query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("ids", "STRING", list(request_ids))])).result(timeout=120)
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


class BigQuerySource(DataSource):
    name = "bigquery"

    def configured(self):
        return all(_cfg())

    def workspaces(self):
        return [{"id": "bq", "name": _cfg()[0]}]

    def agents(self, ws_id):
        col = credentials.get_config("BQ_AGENT_COLUMN")
        if not col:
            return [{"id": _agent(), "name": _agent()}]
        from google.cloud import bigquery
        table, params = _cfg()[0], []
        sql = "SELECT DISTINCT `{a}` AS name FROM `{t}` WHERE `{a}` IS NOT NULL AND {w} ORDER BY name LIMIT 200".format(
            a=col, t=table, w=_window(params, _days()))
        rows = _client(table.split(".")[0]).query(sql, job_config=bigquery.QueryJobConfig(
            query_parameters=params)).result(timeout=120)
        return [{"id": r["name"], "name": r["name"]} for r in rows]

    def pull(self, ws_id, project, limit=None, since_hours=None):
        """(buckets, skipped): contract traces from the table, bucketed by call-site. `project` is the agent when
        BQ_AGENT_COLUMN is set. ponytail: `limit` (whole traces, for LangSmith) is not used; _ROWS calls are read."""
        from google.cloud import bigquery
        table, req_col, resp_col = _cfg()
        agent_col, node_col = credentials.get_config("BQ_AGENT_COLUMN"), credentials.get_config("BQ_NODE_COLUMN")
        params, where = [], ["`%s` IS NOT NULL" % resp_col]
        if agent_col:                                   # a parameter: `project` comes from the page's URL
            where.append("`%s` = @agent" % agent_col)
            params.append(bigquery.ScalarQueryParameter("agent", "STRING", project))
        where.append(_window(params, _days(since_hours)))
        part = "`%s`" % node_col if node_col else "TO_JSON_STRING(`%s`)" % req_col   # a named call-site is cheaper
        sql = ("SELECT `{q}` AS request, `{r}` AS response{x} FROM `{t}` WHERE {w} "
               "QUALIFY ROW_NUMBER() OVER (PARTITION BY {p}) <= {c} LIMIT {n}"
               ).format(q=req_col, r=resp_col, t=table, w=" AND ".join(where), p=part, c=_COPIES, n=_ROWS,
                        x=", `%s` AS node" % node_col if node_col else "")
        buckets, skipped = {}, []
        rows = _client(table.split(".")[0]).query(sql, job_config=bigquery.QueryJobConfig(
            query_parameters=params)).result(timeout=300)
        for row in rows:
            t = to_trace(row["request"], row["response"], project if agent_col else _agent())
            if node_col and row["node"]:
                t["node_id"] = row["node"]
            if [p for p in schema.validate_trace(t) if not p.startswith("unknown model")]:
                skipped.append(t["trace_id"])
                continue
            buckets.setdefault(t["node_id"], []).append(t)
        return buckets, skipped


SOURCE = BigQuerySource()
