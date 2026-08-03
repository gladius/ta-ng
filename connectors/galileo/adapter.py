"""Galileo adapter — recorded LLM spans -> Token Auditor traces.

Live:    pulls LLM spans via Galileo's span-search REST API. Standing config is just KEY + HOST (from
         the process env or a gitignored auditor/.env — see .env.example; env wins over file). WHICH
         project to pull is a per-call SELECTION (project_id=<uuid> / project=<name> argument), not config.
Offline: pass records=<list of dicts> or a path to exported span JSON — no key, no install.
         Handy for tests and for replaying an export.

Confirmed against Galileo's API reference (docs.galileo.ai):
  - list spans : POST {base}/v2/projects/{project_id}/spans/search
                 body {log_stream_id?, limit, starting_token} ; response
                 {records:[...], num_records, next_starting_token, paginated}
  - LLM span   : {type:"llm", model, input:[Message], output:Message,
                  metrics:{num_input_tokens, num_output_tokens, num_total_tokens, duration_ns},
                  tools:[...], events:[...], name, user_metadata, trace_id, id, project_id}

Live-path facts to confirm against a real instance (the OFFLINE path + the whole field mapping in
to_trace() are fully exercised by test_adapter.py without any of this):

  - AUTH: Galileo's OpenAPI exposes an API-KEY HEADER scheme (not necessarily `Authorization: Bearer`).
    The exact header name isn't public, so it is CONFIGURABLE — no code change needed to correct it:
        GALILEO_AUTH_HEADER   header that carries the key   (default 'Galileo-API-Key')
        GALILEO_AUTH_PREFIX   value prefix                  (default ''; set 'Bearer ' for bearer auth)
    A 401/403 raises an actionable message naming these knobs.
  - PROJECT: pass `project_id` (a UUID from the dashboard URL) to be exact; `project=<name>` triggers a
    best-effort `GET /v2/projects` lookup.
  - INLINE vs N+1: if `spans/search` returns summary records WITHOUT inline input/output, those records
    simply fail the trace contract and are reported in `skipped` (never crash) — confirm your instance
    returns content inline, or add a get-trace enrichment step.

Call-site (agent_id) grouping mirrors the LangSmith adapter. Default: "<agent>/<node>" where
    agent = user_metadata.agent_id | user_metadata.agent | project
    node  = user_metadata.langgraph_node | user_metadata.node | span.name
Override with group_by: "span_name" | "metadata:<key>".
"""

import json
import time
import urllib.request
import urllib.error

import credentials
from connectors.base import Adapter
from connectors import schema

DEFAULT_BASE = "https://api.galileo.ai"
_PAGE = 100                    # spans/search default page size


def _first(*vals):
    """First non-None value (0 is kept; None is skipped)."""
    for v in vals:
        if v is not None:
            return v
    return None


def _output_text(output):
    """Assistant text from a Galileo output Message (or list / plain string)."""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        if "content" in output:                     # a Message: {role, content}
            return schema.norm_content(output.get("content"))
        return schema.extract_output(output)        # fall back to the generic extractor
    if isinstance(output, list) and output:
        return _output_text(output[-1])             # last message of a list
    return str(output)


def _tools_called(rec):
    """Tool names the model actually invoked — from the output message's tool_calls and/or span events."""
    names = set()
    out = rec.get("output")
    if isinstance(out, dict):
        for tc in (out.get("tool_calls") or []):
            if isinstance(tc, dict):
                n = tc.get("name") or (tc.get("function") or {}).get("name")
                if n:
                    names.add(n)
    for ev in (rec.get("events") or []):            # Galileo span events: tool calls, web search, MCP, ...
        if not isinstance(ev, dict):
            continue
        et = str(ev.get("type") or ev.get("event_type") or "").lower()
        if "tool" in et:
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            n = (ev.get("name") or ev.get("tool_name")
                 or (ev.get("function") or {}).get("name") or payload.get("name"))
            if n:
                names.add(n)
    return sorted(names)


def _runtime_ms(rec, metrics):
    ns = _first(metrics.get("duration_ns"), rec.get("duration_ns"), 0) or 0
    try:
        return int(ns) // 1_000_000
    except Exception:
        return 0


def _callsite(rec, meta, project, group_by):
    """Return (agent_id, node_id): agent = the agent identity, node = the call-site within it."""
    agent = meta.get("agent_id") or meta.get("agent") or project or "agent"
    if group_by == "span_name":
        node = rec.get("name") or "span"
    elif group_by and group_by.startswith("metadata:"):
        node = meta.get(group_by.split(":", 1)[1], "unknown")
    else:
        node = meta.get("langgraph_node") or meta.get("node") or rec.get("name") or "span"
    return str(agent), str(node)


class GalileoAdapter(Adapter):
    name = "galileo"

    # ------------------------------------------------------------------ fetch
    def fetch(self, *, project=None, project_id=None, log_stream_id=None, limit=None,
              records=None, api_key=None, base_url=None, **_):
        if records is not None:                              # -- offline --
            if isinstance(records, str):
                records = json.load(open(records, encoding="utf-8"))
            if isinstance(records, dict):
                records = records.get("records", [records])
            for r in records:
                yield r
            return

        base = (base_url or credentials.get_config("GALILEO_API_URL", DEFAULT_BASE)).rstrip("/")
        key = api_key or credentials.get_secret("GALILEO_API_KEY", files=(".galileo_key", ".env"))
        if not key:
            raise RuntimeError("GALILEO_API_KEY not set (env or a gitignored .env / .galileo_key; "
                               "or pass records=<dicts/path> for offline mode)")
        pid = project_id or self._resolve_project_id(base, key, project)   # which project: a per-call selection

        n, starting_token = 0, 0                             # -- live: paginated spans/search --
        url = "%s/v2/projects/%s/spans/search" % (base, pid)
        while True:
            body = {"limit": _PAGE, "starting_token": starting_token}
            if log_stream_id:
                body["log_stream_id"] = log_stream_id
            resp = self._post(url, body, key)
            recs = resp.get("records") or []
            for r in recs:
                yield r
                n += 1
                if limit and n >= limit:
                    return
            nxt = resp.get("next_starting_token")
            if not recs or nxt is None:
                break
            starting_token = nxt

    # ------------------------------------------------------------------ to_trace
    def to_trace(self, rec, *, project=None, group_by=None, **_):
        if not isinstance(rec, dict):
            return None
        if (rec.get("type") or "llm") != "llm":              # only LLM spans carry prompts + token usage
            return None
        meta = rec.get("user_metadata") or rec.get("metadata") or {}
        metrics = rec.get("metrics") or {}
        model = rec.get("model") or meta.get("model")
        usage = {"input_tokens": _first(metrics.get("num_input_tokens"), rec.get("num_input_tokens"), 0),
                 "output_tokens": _first(metrics.get("num_output_tokens"), rec.get("num_output_tokens"), 0)}
        agent_id, node_id = _callsite(rec, meta, project, group_by)
        status = rec.get("status_code")
        error = "" if status in (None, "") or str(status).startswith("2") else "status %s" % status
        return schema.build_trace(
            trace_id=rec.get("trace_id") or rec.get("id"),
            agent_id=agent_id, node_id=node_id,
            model=model,
            task_type=meta.get("task_type") or meta.get("task") or "unknown",
            owner=meta.get("owner") or meta.get("team") or "n/a",
            input_messages=schema.norm_messages(rec.get("input")),
            output=_output_text(rec.get("output")),
            tools_defined=schema.norm_tools(rec.get("tools")),
            tools_called=_tools_called(rec),
            usage=usage,
            error=error,
            runtime_ms=_runtime_ms(rec, metrics),
            source="galileo:%s" % (project or rec.get("project_id") or ""),
        )

    # ------------------------------------------------------------------ live helpers
    def _headers(self, key):
        """Auth headers — CONFIGURABLE, because the OpenAPI shows an api-key header scheme but not its
        exact name. Override without a code change: GALILEO_AUTH_HEADER (default 'Galileo-API-Key') and
        GALILEO_AUTH_PREFIX (default ''; use 'Bearer ' for bearer-style auth)."""
        header = credentials.get_config("GALILEO_AUTH_HEADER", "Galileo-API-Key")
        prefix = credentials.get_config("GALILEO_AUTH_PREFIX", "")
        return {header: prefix + key, "Content-Type": "application/json", "Accept": "application/json"}

    def _request(self, method, url, key, body=None):
        """One HTTP path with retries + actionable, key-REDACTED errors (401/403 -> auth knobs, 404 ->
        base/project, 429/5xx -> backoff)."""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        last = None
        for attempt in range(5):
            req = urllib.request.Request(url, data=data, headers=self._headers(key), method=method)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                txt = credentials.redact(e.read().decode("utf-8", "ignore"))
                if e.code in (401, 403):
                    raise RuntimeError(
                        "Galileo auth failed (%s). Check GALILEO_API_KEY, or set the correct header via "
                        "GALILEO_AUTH_HEADER (the API uses an api-key header; for bearer auth set "
                        "GALILEO_AUTH_HEADER=Authorization and GALILEO_AUTH_PREFIX='Bearer ')." % e.code)
                if e.code == 404:
                    raise RuntimeError("Galileo 404 at %s — check GALILEO_API_URL and project_id." % url)
                if e.code in (429, 500, 502, 503, 529):
                    last = "Galileo API %s: %s" % (e.code, txt)
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError("Galileo API %s: %s" % (e.code, txt))
            except (urllib.error.URLError, TimeoutError) as e:
                last = "network error: %s" % credentials.redact(str(e))
                time.sleep(2 ** attempt)
        raise RuntimeError(last or "Galileo request failed")

    def _post(self, url, body, key):
        return self._request("POST", url, key, body=body)

    def _resolve_project_id(self, base, key, project):
        """Map a project NAME to its id (best-effort GET /v2/projects). Pass project_id=<uuid> to skip."""
        if not project:
            raise RuntimeError("pass project_id=<uuid> (or project=<name>) to pull Galileo spans")
        data = self._request("GET", "%s/v2/projects" % base, key)
        items = data if isinstance(data, list) else (data.get("projects") or data.get("records") or [])
        for p in items:
            if isinstance(p, dict) and (p.get("name") == project or p.get("id") == project):
                return p.get("id") or p.get("project_id")
        raise RuntimeError("project %r not found — pass project_id=<uuid> from the Galileo dashboard URL" % project)


ADAPTER = GalileoAdapter()      # discovered by connectors.registry.load_adapters()
