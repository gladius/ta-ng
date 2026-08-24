"""The trace contract: the canonical field spec, the constructor (build_trace), the validator, and
the call-site key. Platform-agnostic and self-standing — model-name canonicalization resolves LAZILY
against a catalog (app.catalog) only when one is present; until then a model is honestly flagged
model_known=False. The contract records whether a model is known; it does not own prices.

A Token Auditor trace:

  {trace_id, agent_id, node_id, owner, model, task_type, source, thinking_enabled,
   input_messages:[{role, content}],           role in ROLES = {system,user,assistant,tool}
   tools_defined:[[name, schema_json_str]],     schema is a JSON STRING, not a dict
   tools_called:[name],
   output,
   usage:{input_tokens, output_tokens, cached_tokens}}

build_trace also stamps model_raw / model_known (connector diagnostics). TRACE_VERSION bumps when this
shape changes incompatibly.
"""

import json


def _canonical_model(raw):
    """Canonicalize a raw model id against a price/name catalog (app.catalog) IF one is present, else None.
    LAZY: importing the contract pulls in no catalog — the catalog is a later (pricing) concern. Until it
    exists every model resolves to None and is honestly flagged model_known=False, never mis-labeled."""
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
                    continue                                   # extended-reasoning block — never part of the answer text
                parts.append(p.get("text") or p.get("content") or (json.dumps(p) if p else ""))
        return "\n".join(x for x in parts if x)
    if isinstance(c, dict):
        return c.get("text") or c.get("content") or json.dumps(c)
    return str(c)


def build_trace(*, trace_id, agent_id, model, input_messages, output, usage, node_id="", graph_path="",
                tools_defined=(), tools_called=(), tool_choice=None, task_type="unknown",
                owner="n/a", source="", thinking_enabled=False, error="", runtime_ms=0):
    """Assemble a contract-valid trace. `model` is resolved against the catalog; if unknown it is kept
    verbatim and flagged (model_known=False) so the caller can surface / add it.

    `graph_path` is the call-site's graph/subgraph nesting (e.g. 'router/billing_subgraph'), '' for a
    single-level agent. Carried so the audit can be selected / rolled up per graph; it does NOT change the
    call-site bucket key (a node-name collision across subgraphs is a real-data decision, not assumed here)."""
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
        "tool_choice": tool_choice,      # recorded selection policy (auto/required/specific-tool), raw form; None if unset
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
    """(usable, reason) — is this trace usable audit signal / a validation baseline?

    Deterministic, no LLM. Excludes traces whose AUDITED output is unusable — the call itself errored
    with no output, or an empty output. Intermediate/recovered errors that still produced an output
    are NOT excluded (an 'error' with a real output means the agent recovered)."""
    out = (trace.get("output") or "").strip()
    if trace.get("error") and not out:
        return False, "errored, no output"
    if not out:
        return False, "empty output"
    return True, "ok"


def callsite_key(trace):
    """The bucket key: 'agent/node' when a node_id is present, else just the agent_id.
    A call-site (one bucket) = all samples sharing the same (agent_id, node_id)."""
    agent = trace.get("agent_id", "unknown")
    node = trace.get("node_id")
    return "%s/%s" % (agent, node) if node else str(agent)
