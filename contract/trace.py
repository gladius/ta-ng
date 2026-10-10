"""The trace contract: field spec, constructor (build_trace), validator and call-site key.

tools_defined is [[name, schema_json_str]]: the schema is a JSON string, not a dict.
TRACE_VERSION bumps when the shape changes incompatibly.
"""

import json


def _canonical_model(raw):
    """Catalog name for `raw`, or None; imported lazily so the contract works without a catalog."""
    try:
        from app.catalog import canonical_model
        return canonical_model(raw)
    except Exception:
        return None


TRACE_VERSION = 1
ROLES = ("system", "user", "assistant", "tool")
TRACE_FIELDS = ("trace_id", "agent_id", "node_id", "graph_path", "owner", "model", "task_type", "source",
                "thinking_enabled", "input_messages", "tools_defined", "tools_called", "tool_choice", "output",
                "usage", "error", "runtime_ms")

# platform message class / type / role -> contract role
_ROLE = {
    "human": "user", "humanmessage": "user", "user": "user",
    "ai": "assistant", "aimessage": "assistant", "assistant": "assistant",
    "system": "system", "systemmessage": "system",
    "tool": "tool", "toolmessage": "tool", "function": "tool", "functionmessage": "tool",
}


def norm_role(x):
    return _ROLE.get(str(x or "").lower(), "user")


def norm_content(c):
    """Flatten message content (str, content-parts list, or dict) to a string."""
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for p in c:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                if p.get("type") in ("thinking", "redacted_thinking"):
                    continue                                   # reasoning, not answer text
                parts.append(p.get("text") or p.get("content") or (json.dumps(p) if p else ""))
        return "\n".join(x for x in parts if x)
    if isinstance(c, dict):
        return c.get("text") or c.get("content") or json.dumps(c)
    return str(c)


def build_trace(*, trace_id, agent_id, model, input_messages, output, usage, node_id="", graph_path="",
                tools_defined=(), tools_called=(), tool_choice=None, task_type="unknown",
                owner="n/a", source="", thinking_enabled=False, error="", runtime_ms=0):
    """Assemble a contract-valid trace; an unknown `model` is kept verbatim with model_known=False.
    `graph_path` (e.g. 'router/billing_subgraph') is for per-graph roll-ups; it does not change the call-site key."""
    canon = _canonical_model(model)
    u = usage or {}
    return {
        "trace_id": str(trace_id or "")[:64],
        "agent_id": str(agent_id),
        "node_id": str(node_id or ""),
        "graph_path": str(graph_path or ""),
        "owner": owner or "n/a",
        "model": canon or (str(model) if model else "unknown"),
        "model_raw": str(model) if model else "",
        "model_known": canon is not None,
        "task_type": task_type or "unknown",
        "thinking_enabled": bool(thinking_enabled),
        "source": source or "",
        "input_messages": [{"role": norm_role(m.get("role")), "content": norm_content(m.get("content"))}
                           for m in (input_messages or []) if isinstance(m, dict)],
        "tools_defined": [list(t) for t in tools_defined],
        "tools_called": list(tools_called),
        "tool_choice": tool_choice,      # raw recorded policy (auto/required/a tool), None if unset
        "output": output or "",
        "error": str(error or ""),
        "runtime_ms": int(runtime_ms or 0),
        "usage": {"input_tokens": int(u.get("input_tokens", 0) or 0),
                  "output_tokens": int(u.get("output_tokens", 0) or 0),
                  "cached_tokens": int(u.get("cached_tokens", 0) or 0)},
    }


def validate_trace(t):
    """Return a list of problems; empty = usable. 'unknown model' is a warning, not fatal."""
    problems = []
    if not t.get("agent_id"):
        problems.append("missing agent_id")
    if not t.get("input_messages"):
        problems.append("no input_messages")
    elif not any(m["role"] == "user" for m in t["input_messages"]):
        problems.append("no user turn")
    if t.get("usage", {}).get("input_tokens", 0) <= 0:
        problems.append("no input_tokens")
    if not t.get("model_known"):
        problems.append("unknown model %r (not in the model catalog)" % t.get("model_raw"))
    return problems


def eligible(trace):
    """(usable, reason): unusable when the audited output is empty. An error with a real output means the
    agent recovered, so it stays usable."""
    out = (trace.get("output") or "").strip()
    if trace.get("error") and not out:
        return False, "errored, no output"
    if not out:
        return False, "empty output"
    return True, "ok"


def callsite_key(trace):
    """The call-site bucket key: 'agent/node' when node_id is set, else agent_id."""
    agent = trace.get("agent_id", "unknown")
    node = trace.get("node_id")
    return "%s/%s" % (agent, node) if node else str(agent)
