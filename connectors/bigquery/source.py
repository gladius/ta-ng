"""BigQuery DataSource — LLM calls logged one row per request/response in a single table or view, e.g. LiteLLM's
logs (trust_router's `litellm.v_requests`). Three .env settings; everything else is read out of the two JSON columns:

    BQ_TABLE            project.dataset.table_or_view
    BQ_REQUEST_COLUMN   the request: an OpenAI messages array, or a request body holding "messages" (+ "tools",
                        "tool_choice", "max_tokens")
    BQ_RESPONSE_COLUMN  the response: an OpenAI chat completion (id, model, choices, usage) — LiteLLM's logged shape

A request log names no agent and no graph node, so the table is ONE agent and a call-site is its system prompt
(numbers masked, so a date in the prompt doesn't split it). Auth is Google Application Default Credentials.
"""
import hashlib
import json
import re
import subprocess

import credentials
from connectors import schema
from connectors.datasource import DataSource

# ponytail: no time column configured -> an arbitrary _ROWS rows and a full scan of both columns per pull; add a
# partition-column filter + ORDER BY when the table outgrows that.
_ROWS = 2000
_COPIES = 5             # identical requests kept: health checks / probes repeat one prompt thousands of times


def _cfg():
    return tuple(credentials.get_config(k) for k in ("BQ_TABLE", "BQ_REQUEST_COLUMN", "BQ_RESPONSE_COLUMN"))


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


def to_trace(request, response, agent):
    """One logged (request, response) pair -> a contract trace."""
    req, resp = _json(request), _json(response) or {}
    body = req if isinstance(req, dict) else {"messages": req}
    msgs = schema.norm_messages(body.get("messages"))
    message = ((resp.get("choices") or [{}])[0] or {}).get("message") or {}
    out = {"generations": [[{"message": message}]]}     # the shape schema's output readers already take
    usage = resp.get("usage") or {}
    t = schema.build_trace(
        trace_id=resp.get("id"), agent_id=agent, node_id=_callsite(msgs),
        model=resp.get("model") or body.get("model"), input_messages=msgs,
        output=schema.render_behavior(out), tools_called=schema.tool_calls_from_output(out),
        usage={"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
               "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens")},
        tools_defined=schema.norm_tools(body.get("tools")), tool_choice=body.get("tool_choice"), source="bigquery")
    t["max_output_tokens"] = body.get("max_tokens") or body.get("max_completion_tokens")
    return t


class BigQuerySource(DataSource):
    name = "bigquery"

    def configured(self):
        return all(_cfg())

    def workspaces(self):
        return [{"id": "bq", "name": _cfg()[0]}]

    def agents(self, ws_id):
        return [{"id": _agent(), "name": _agent()}]

    def pull(self, ws_id, project, limit=None, since_hours=None):
        """(buckets, skipped): contract traces from the table, bucketed by call-site."""
        table, req_col, resp_col = _cfg()
        client = _client(table.split(".")[0])
        sql = ("SELECT `{q}` AS request, `{r}` AS response FROM `{t}` WHERE `{r}` IS NOT NULL "
               "QUALIFY ROW_NUMBER() OVER (PARTITION BY TO_JSON_STRING(`{q}`)) <= {c} LIMIT {n}"
               ).format(q=req_col, r=resp_col, t=table, c=_COPIES, n=_ROWS)
        buckets, skipped = {}, []
        for row in client.query(sql).result():
            t = to_trace(row["request"], row["response"], _agent())
            if [p for p in schema.validate_trace(t) if not p.startswith("unknown model")]:
                skipped.append(t["trace_id"])
                continue
            buckets.setdefault(t["node_id"], []).append(t)
        return buckets, skipped


SOURCE = BigQuerySource()
