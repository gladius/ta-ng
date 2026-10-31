"""Platform PARSING helpers — turn raw observability payloads (LangChain-serialized messages, tool
schemas, generation outputs) into the shapes build_trace() expects. The canonical trace shape,
build_trace, and validate_trace live in the neutral `contract` package; this module is connector-side
and imports them, so an adapter still calls schema.build_trace() as before.
"""

import json

from contract import norm_role, norm_content, build_trace, validate_trace  # re-exported for adapters

__all__ = ["norm_role", "norm_content", "build_trace", "validate_trace",
           "norm_messages", "norm_tools", "extract_output", "tool_calls_from_output",
           "tool_calls_full", "render_behavior"]


def _one_message(m):
    """Normalize ONE message: plain {role,content}, LangChain-serialized constructor, or {type,data}."""
    if not isinstance(m, dict):
        return {"role": "user", "content": norm_content(m)}
    # LangChain serialized: {"lc":1,"type":"constructor","id":[...,"HumanMessage"],"kwargs":{...}}
    if "kwargs" in m and (isinstance(m.get("id"), list) or m.get("type") == "constructor"):
        kw = m.get("kwargs", {}) or {}
        cls = m["id"][-1] if isinstance(m.get("id"), list) and m["id"] else ""
        role = kw.get("role") or kw.get("type") or cls
        return {"role": norm_role(role), "content": norm_content(kw.get("content"))}
    # LangChain messages_from_dict form: {"type":"human","data":{"content":...}}
    if "data" in m and "type" in m and isinstance(m["data"], dict):
        return {"role": norm_role(m["type"]), "content": norm_content(m["data"].get("content"))}
    # plain {role|type, content}
    return {"role": norm_role(m.get("role") or m.get("type")), "content": norm_content(m.get("content"))}


def norm_messages(raw):
    """Accept messages as a flat list, a nested [[...]] list, an inputs dict, or a raw prompt string."""
    if isinstance(raw, dict):
        picked = []
        for k in ("messages", "input", "prompts", "prompt", "chat_history"):
            if raw.get(k):
                picked = raw[k]
                break
        raw = picked
    if isinstance(raw, str):
        return [{"role": "user", "content": raw}]
    if not isinstance(raw, list):
        return []
    if raw and isinstance(raw[0], list):          # unwrap one level ([[msg, msg]])
        raw = [m for grp in raw for m in grp]
    return [_one_message(m) for m in raw]


def norm_tools(tools):
    """[{type:function,function:{name,...}} | {name,...}] -> [[name, json_schema_str]]."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t["function"] if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        if name:
            out.append([name, json.dumps(fn)])
    return out


def _unwrap_generation(outputs):
    g = outputs.get("generations") if isinstance(outputs, dict) else None
    while isinstance(g, list) and g:
        g = g[0]
    return g if isinstance(g, dict) else None


def _msg_content(message):
    if isinstance(message, dict):
        if "kwargs" in message and isinstance(message["kwargs"], dict):
            return message["kwargs"].get("content")
        return message.get("content")
    return message


def extract_output(outputs):
    """Best-effort assistant text from outputs (generations / messages / choices / plain keys)."""
    if not outputs:
        return ""
    if isinstance(outputs, str):
        return outputs
    if isinstance(outputs, dict):
        g = _unwrap_generation(outputs)
        if g is not None:
            if isinstance(g.get("message"), dict):
                return norm_content(_msg_content(g["message"]))
            if g.get("text"):
                return g["text"]
        for key in ("output", "content", "text", "answer"):
            if isinstance(outputs.get(key), str):
                return outputs[key]
        msgs = outputs.get("messages")
        if isinstance(msgs, list) and msgs:
            return _one_message(msgs[-1])["content"]
        ch = outputs.get("choices")
        if isinstance(ch, list) and ch and isinstance(ch[0], dict):
            return norm_content((ch[0].get("message") or {}).get("content"))
        return json.dumps(outputs)
    return str(outputs)


def tool_calls_from_output(outputs):
    """Tool names the model actually invoked, read off the output AI message."""
    names = []
    try:
        g = _unwrap_generation(outputs) or {}
        msg = g.get("message", {}) if isinstance(g, dict) else {}
        kw = msg.get("kwargs", msg) if isinstance(msg, dict) else {}
        calls = kw.get("tool_calls") or (kw.get("additional_kwargs", {}) or {}).get("tool_calls") or []
        for tc in calls:
            if isinstance(tc, dict):
                n = tc.get("name") or (tc.get("function", {}) or {}).get("name")
                if n:
                    names.append(n)
    except Exception:
        pass
    return sorted(set(names))


def tool_calls_full(outputs):
    """Tool calls the model actually made — name AND arguments — off the output AI message. Args are parsed to a
    dict when the provider serialized them as a JSON string."""
    out = []
    try:
        g = _unwrap_generation(outputs) or {}
        msg = g.get("message", {}) if isinstance(g, dict) else {}
        kw = msg.get("kwargs", msg) if isinstance(msg, dict) else {}
        calls = kw.get("tool_calls") or (kw.get("additional_kwargs", {}) or {}).get("tool_calls") or []
        for tc in calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
            name = fn.get("name")
            args = fn.get("arguments", fn.get("args", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    pass
            if name:
                out.append({"name": name, "args": args if isinstance(args, dict) else {"_": args}})
    except Exception:
        pass
    return out


def render_behavior(outputs):
    """The model's FULL recorded behavior as one string: assistant text PLUS any tool call rendered as
    `[calls name({args})]`. This is the audit's comparison reference — so a tool node's own DECISION (which the
    plain text output drops) is preserved and the cheaper model's tool call can be judged against it."""
    calls = tool_calls_full(outputs)
    g = _unwrap_generation(outputs) or {}
    msg = g.get("message") if isinstance(g, dict) else None
    text = norm_content(_msg_content(msg)).strip() if isinstance(msg, dict) else ""
    if not text and not calls:                       # no tool call and no message text -> fall back to best-effort
        text = (extract_output(outputs) or "").strip()
    parts = [text] if text else []
    for c in calls:
        parts.append("[calls %s(%s)]" % (c["name"], json.dumps(c["args"], sort_keys=True)))
    return "\n".join(parts).strip()
