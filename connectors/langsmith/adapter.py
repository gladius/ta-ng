"""LangSmith adapter: recorded runs -> Token Auditor traces.

Live reads runs with langsmith.Client.list_runs (needs LANGSMITH_API_KEY); offline takes runs=<dicts or a JSON path>.
Each call-site is its own bucket, "<agent>/<node>": agent = metadata.agent_id | metadata.agent | project, node =
metadata.langgraph_node | metadata.node | run.name. group_by overrides the node: run_name | langgraph_node |
metadata:<key>.
"""

import os
import json
from datetime import datetime, timedelta, timezone

from connectors.base import Adapter
from connectors import schema

# Only the fields we need, to keep payloads small; id/parent_run_id/dotted_order let the graph rebuild the tree.
_SELECT = ["id", "name", "run_type", "inputs", "outputs", "extra", "error", "tags",
           "start_time", "end_time", "trace_id", "session_id", "parent_run_id", "dotted_order",
           "prompt_tokens", "completion_tokens", "total_tokens",
           "feedback_stats", "total_cost", "first_token_time"]


def _run_to_dict(r):
    """Accept either a raw dict (offline) or a langsmith Run object (live)."""
    if isinstance(r, dict):
        return r
    g = lambda k: getattr(r, k, None)
    return {"id": str(g("id") or ""), "name": g("name"), "run_type": g("run_type"),
            "inputs": g("inputs") or {}, "outputs": g("outputs") or {},
            "extra": g("extra") or {}, "error": g("error"), "tags": g("tags") or [],
            "trace_id": str(g("trace_id") or ""), "session_id": str(g("session_id") or ""),
            "parent_run_id": (str(g("parent_run_id")) if g("parent_run_id") else None),
            "dotted_order": g("dotted_order") or "",
            "start_time": g("start_time"), "end_time": g("end_time"),
            "prompt_tokens": g("prompt_tokens"), "completion_tokens": g("completion_tokens"),
            "total_tokens": g("total_tokens"),
            "feedback_stats": g("feedback_stats"), "total_cost": g("total_cost"),
            "first_token_time": g("first_token_time")}


def _iso(x):
    return x.isoformat() if hasattr(x, "isoformat") else (str(x) if x else "")


def _feedback(fs):
    """(mean quality score from feedback_stats {key: {avg, n}}, feedback count), or (None, 0) without feedback."""
    if not isinstance(fs, dict) or not fs:
        return None, 0
    avgs, n = [], 0
    for v in fs.values():
        if isinstance(v, dict) and isinstance(v.get("avg"), (int, float)):
            avgs.append(v["avg"])
            n += int(v.get("n") or 0)
    return (round(sum(avgs) / len(avgs), 3) if avgs else None), n


def _ttft_ms(rec):
    """Time-to-first-token in ms (first_token_time - start_time), or None if not streamed / unavailable."""
    def _dt(x):
        if x is None:
            return None
        if isinstance(x, str):
            try:
                return datetime.fromisoformat(x.replace("Z", "+00:00"))
            except Exception:
                return None
        return x
    a, b = _dt(rec.get("start_time")), _dt(rec.get("first_token_time"))
    try:
        return max(0, int((b - a).total_seconds() * 1000)) if (a and b) else None
    except Exception:
        return None


def _runtime_ms(rec):
    """Wall-clock ms from start/end time (datetime or ISO string); 0 if unavailable."""
    def _dt(x):
        if x is None:
            return None
        if isinstance(x, str):
            try:
                return datetime.fromisoformat(x.replace("Z", "+00:00"))
            except Exception:
                return None
        return x
    a, b = _dt(rec.get("start_time")), _dt(rec.get("end_time"))
    try:
        return int((b - a).total_seconds() * 1000) if a and b else 0
    except Exception:
        return 0


def _graph_path(meta):
    """The call-site's subgraph nesting as 'outer/inner', or '' for a single-level agent.
    From langgraph_checkpoint_ns ('billing:uuid|refund:uuid' -> 'billing/refund'), else a graph/subgraph hint."""
    ns = meta.get("langgraph_checkpoint_ns") or meta.get("langgraph_path") or ""
    if isinstance(ns, (list, tuple)):
        ns = "|".join(str(x) for x in ns)
    parts = [seg.split(":", 1)[0].strip() for seg in str(ns).split("|") if seg.strip()]
    parts = [p for p in parts if p and not _is_generic_seg(p)]
    if parts:
        return "/".join(parts)
    hint = meta.get("subgraph") or meta.get("graph")
    return str(hint) if hint else ""


def _is_generic_seg(p):
    return p.lower() in ("", "__start__", "__end__", "langgraph")


_REV_KEYS = ("revision_id", "revision", "ls_revision_id",
             "LANGSMITH_HOST_REVISION_ID", "LANGSMITH_LANGGRAPH_API_REVISION")


def _revision(meta):
    """The agent version: `revision_id` on a local LangGraph run, LANGSMITH_HOST_REVISION_ID or
    LANGSMITH_LANGGRAPH_API_REVISION on a hosted deployment."""
    for k in _REV_KEYS:
        if meta.get(k):
            return str(meta[k])
    return ""


def _root_rev(r):
    return _revision((getattr(r, "extra", None) or {}).get("metadata") or {})


def _scope_to_latest(roots, n, revision=None):
    """-> (the newest `n` roots on one revision, that revision). Roots arrive newest first, so by default that is the
    revision serving now, even after a rollback; `revision` pins another. Mixed versions make the judge unsound."""
    if not roots:
        return [], None
    target = revision or _root_rev(roots[0])
    return [r for r in roots if _root_rev(r) == target][:n], target


def _callsite(rec, meta, project, group_by):
    """-> (agent_id, node_id); group_by only changes how the node is chosen."""
    agent = meta.get("agent_id") or meta.get("agent") or project or "agent"
    if group_by == "run_name":
        node = rec.get("name") or "unknown"
    elif group_by and group_by.startswith("metadata:"):
        node = meta.get(group_by.split(":", 1)[1], "unknown")
    elif group_by == "langgraph_node":
        node = meta.get("langgraph_node") or rec.get("name") or "unknown"
    else:
        node = meta.get("langgraph_node") or meta.get("node") or rec.get("name") or "node"
    return str(agent), str(node)


def _out_preview(outputs, cap=240):
    """A one-line preview of a run's outputs, to describe what a non-llm node produces."""
    if not outputs:
        return ""
    try:
        s = json.dumps(outputs, default=str, ensure_ascii=False)
    except Exception:
        s = str(outputs)
    s = s.replace("\n", " ")
    return s if len(s) <= cap else s[:cap] + "…"


class LangSmithAdapter(Adapter):
    name = "langsmith"

    def fetch(self, *, project=None, run_type=None, since_hours=None, limit=None,
              traces=None, revision=None, runs=None, api_key=None, api_url=None, **_):
        # run_type=None fetches the whole tree (chain + llm + tool), which the graph layer needs.
        if runs is not None:                                   # -- offline --
            if isinstance(runs, str):
                runs = json.load(open(runs, encoding="utf-8"))
            if isinstance(runs, dict):
                runs = runs.get("runs", [runs])
            for r in runs:
                yield _run_to_dict(r)
            return
        try:                                                   # -- live --
            from langsmith import Client
        except ImportError as e:
            raise RuntimeError("langsmith not installed: `pip install langsmith` "
                               "(or pass runs=<dicts/path> for offline mode)") from e
        import credentials
        credentials.load()
        # Client reads LANGSMITH_WORKSPACE_ID from env and sends it as X-Tenant-Id (org-scoped keys need it).
        client = Client(api_key=api_key or credentials.get_secret("LANGSMITH_API_KEY",
                                                                  aliases=("LANGCHAIN_API_KEY", "LANGSMITH_KEY")),
                        api_url=api_url or credentials.get_config("LANGSMITH_ENDPOINT", None,
                                                                  aliases=("LANGCHAIN_ENDPOINT",)))
        # A whole-tree pull takes the last n whole traces, never a run window that slices a tree at its edge.
        # `traces` (or `limit`) counts traces.
        n = int(traces) if traces else (int(limit) if limit else None)
        if n and not run_type:
            yield from self._fetch_traces(client, project, n, revision, since_hours)
            return
        # A run window, only when scoped by run_type (e.g. llm only): stop after `limit` runs.
        kw = {"project_name": project, "select": _SELECT, "run_type": run_type}
        if since_hours:
            kw["start_time"] = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        m = 0
        for r in client.list_runs(**kw):
            yield _run_to_dict(r)
            m += 1
            if limit and m >= limit:
                break

    def _fetch_traces(self, client, project, n, revision, since_hours):
        """The last `n` whole traces of one agent version, in bulk: the roots first, then every run of those traces."""
        rkw = {"project_name": project, "is_root": True, "select": ["id", "trace_id", "start_time", "extra"]}
        if since_hours:
            rkw["start_time"] = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        # Over-fetch roots so the revision filter still reaches n; a pinned older revision sits deeper.
        cap = max(n * 4, n + 100) if revision else max(n * 2, n + 100)
        roots = []
        for r in client.list_runs(**rkw):
            roots.append(r)
            if len(roots) >= cap:
                break
        roots, _target = _scope_to_latest(roots, n, revision)
        tids = [str(r.trace_id) for r in roots if getattr(r, "trace_id", None)]
        if not tids:
            return
        B = 50                                                 # batch trace ids so each filter string stays bounded
        for i in range(0, len(tids), B):
            flt = "in(trace_id, [%s])" % ", ".join('"%s"' % t for t in tids[i:i + B])
            for r in client.list_runs(project_name=project, select=_SELECT, filter=flt):
                yield _run_to_dict(r)

    def available_traces(self, project, cap=500, api_key=None, api_url=None):
        """-> (how many traces the project has, capped at `cap`, whether capped), for the debug capture."""
        try:
            from langsmith import Client
        except ImportError:
            return None, False
        import credentials
        credentials.load()
        client = Client(api_key=api_key or credentials.get_secret("LANGSMITH_API_KEY",
                                                                  aliases=("LANGCHAIN_API_KEY", "LANGSMITH_KEY")),
                        api_url=api_url or credentials.get_config("LANGSMITH_ENDPOINT", None,
                                                                  aliases=("LANGCHAIN_ENDPOINT",)))
        n = 0
        for _ in client.list_runs(project_name=project, is_root=True, select=["id"]):
            n += 1
            if n >= cap:
                return n, True
        return n, False

    def to_trace(self, rec, *, project=None, group_by=None, **_):
        rec = _run_to_dict(rec)
        if rec.get("run_type") not in (None, "llm"):           # only LLM spans carry prompts + usage
            return None
        extra = rec.get("extra") or {}
        meta = extra.get("metadata") or {}
        inv = extra.get("invocation_params") or {}
        model = inv.get("model") or inv.get("model_name") or meta.get("ls_model_name")
        pt, ct = rec.get("prompt_tokens"), rec.get("completion_tokens")
        um = ((rec.get("outputs") or {}).get("llm_output") or {}).get("token_usage") or {}
        # Cache reads and writes (Anthropic's, via LangChain); 0 when unreported.
        cached = (um.get("cache_read_input_tokens") or um.get("cache_read")
                  or (um.get("input_token_details") or {}).get("cache_read")
                  or (rec.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        written = (um.get("cache_creation_input_tokens") or um.get("cache_creation")
                   or (um.get("input_token_details") or {}).get("cache_creation") or 0)
        usage = {"input_tokens": pt if pt is not None else um.get("prompt_tokens", 0),
                 "output_tokens": ct if ct is not None else um.get("completion_tokens", 0),
                 "cached_tokens": int(cached or 0), "cache_write_tokens": int(written or 0)}
        # Reasoning, native or through litellm (OpenAI shape: reasoning_tokens); a token count > 0 is the surest sign.
        det = (um.get("completion_tokens_details") or rec.get("completion_tokens_details")
               or ((rec.get("outputs") or {}).get("llm_output") or {}).get("completion_tokens_details") or {})
        reasoning_tok = int((det.get("reasoning_tokens") if isinstance(det, dict) else 0)
                            or um.get("reasoning_tokens") or 0)
        thinking_enabled = bool(reasoning_tok) or bool(
            inv.get("reasoning_effort") or inv.get("thinking") or inv.get("thinking_budget")
            or inv.get("reasoning") or (inv.get("extra_body") or {}).get("thinking"))
        # The original's output budget: a re-run needs the same room, or a thinking model truncates (thinking counts
        # against it). Every shape litellm may normalize to, in invocation_params and metadata.
        max_out = int(inv.get("max_output_tokens") or inv.get("max_tokens") or inv.get("max_completion_tokens")
                      or meta.get("max_output_tokens") or meta.get("ls_max_tokens") or 0)
        agent_id, node_id = _callsite(rec, meta, project, group_by)
        t = schema.build_trace(
            trace_id=rec.get("trace_id") or rec.get("id"),
            agent_id=agent_id, node_id=node_id, graph_path=_graph_path(meta),
            model=model,
            task_type=meta.get("task_type") or meta.get("ls_task") or "unknown",
            owner=meta.get("owner") or meta.get("team") or "n/a",
            input_messages=schema.norm_messages(rec.get("inputs")),
            output=schema.render_behavior(rec.get("outputs")),
            tools_defined=schema.norm_tools(inv.get("tools") or inv.get("functions")),
            tools_called=schema.tool_calls_from_output(rec.get("outputs")),
            tool_choice=inv.get("tool_choice"),          # replayed so a forced tool call is reproduced
            thinking_enabled=thinking_enabled,
            usage=usage,
            error=rec.get("error") or "",
            runtime_ms=_runtime_ms(rec),
            source="langsmith:%s" % (project or meta.get("ls_project") or ""),
        )
        _st = rec.get("start_time")
        t["start_time"] = _st.isoformat() if hasattr(_st, "isoformat") else (str(_st) if _st else "")
        t["max_output_tokens"] = max_out
        _c = rec.get("total_cost")                             # LangSmith's own $, to cross-check ours
        t["cost_ls"] = float(_c) if _c is not None else None   # Decimal -> float, JSON-safe
        t["feedback_score"], t["feedback_n"] = _feedback(rec.get("feedback_stats"))
        t["ttft_ms"] = _ttft_ms(rec)
        t["revision"] = _revision(meta)
        return t

    def to_record(self, rec, *, project=None, group_by=None, **_):
        """A neutral record for every run in the tree (chain, llm, tool) with its topology; a trace for llm runs."""
        rec = _run_to_dict(rec)
        meta = (rec.get("extra") or {}).get("metadata") or {}
        rt = rec.get("run_type")
        trace = self.to_trace(rec, project=project, group_by=group_by) if rt in (None, "llm") else None
        return {"id": str(rec.get("id") or rec.get("trace_id") or ""),
                "trace_id": str(rec.get("trace_id") or rec.get("id") or ""),
                "parent_run_id": rec.get("parent_run_id"),
                "dotted_order": rec.get("dotted_order") or "",
                "run_type": rt, "name": rec.get("name") or "",
                "start_time": _iso(rec.get("start_time")), "end_time": _iso(rec.get("end_time")),
                "agent_hint": meta.get("agent_id") or meta.get("agent"),
                "node_hint": meta.get("langgraph_node") or meta.get("node"),
                # also for non-llm nodes (tool, retriever), which have no trace
                "graph_path": _graph_path(meta), "error": rec.get("error") or "",
                "runtime_ms": _runtime_ms(rec),
                "out_sample": _out_preview(rec.get("outputs")) if rt not in (None, "llm") else "",
                "trace": trace}


ADAPTER = LangSmithAdapter()      # discovered by connectors.registry.load_adapters()
