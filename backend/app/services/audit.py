"""Downgrade audit: replay and input sampling. The verdict itself is downgrade_refset.prove_transform_refset."""
import json

from app.catalog import canonical_model, tier, release
from app import registry
from app.services import llm_client
from app.config import AUDIT_SAMPLES, AUDIT_MIN_EVIDENCE, AUDIT_DOWNGRADE_K


def _user_text(t):
    return " ".join(m["content"] for m in t.get("input_messages", []) if m.get("role") == "user")


def _input_text(t):
    """The full input a call sent (every message): the diversity key for _distinct."""
    return "\n".join((m.get("content") or "") for m in t.get("input_messages", []))


def _system_text(t):
    return "\n".join((m.get("content") or "") for m in t.get("input_messages", []) if m.get("role") == "system")


def _messages(trace):
    """Recorded conversation as chat messages: system left out, same-role turns merged, starting with a user turn."""
    out = []
    for m in trace.get("input_messages", []):
        if m.get("role") == "system":
            continue
        role = "assistant" if m.get("role") == "assistant" else "user"
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + content
        else:
            out.append({"role": role, "content": content})
    if not out:
        out = [{"role": "user", "content": "(no user turn)"}]
    if out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": "(continue)"})
    return out


def _tools(trace):
    """The recorded bound tools as neutral intents [{name, description, schema}]. Reads `input_schema` or `parameters`:
    an OpenAI-format tool bound with an empty schema can't reproduce the recorded call (a false drift)."""
    out = []
    for name, sch in trace.get("tools_defined", []) or []:
        try:
            d = json.loads(sch) if isinstance(sch, str) else (sch or {})
            schema = d.get("input_schema") or d.get("parameters") or {"type": "object", "properties": {}}
            out.append({"name": d.get("name", name), "description": (d.get("description") or "")[:1024],
                        "schema": schema})
        except Exception:
            continue
    return out


def _render(text, tool_calls):
    """A model's behaviour as one string for the judge: prose plus `[calls name({args})]` per tool call."""
    parts = [text.strip()] if (text and text.strip()) else []
    for tc in tool_calls or []:
        parts.append("[calls %s(%s)]" % (tc.get("name", "?"), json.dumps(tc.get("args", {}), sort_keys=True)))
    return "\n".join(parts).strip() or "(empty output)"


def _tool_choice(raw):
    """A recorded tool_choice in neutral form (None, 'any' or {'tool': name}), so a forced call replays forced."""
    if not raw:
        return None
    if isinstance(raw, str):
        s = raw.lower()
        if s in ("required", "any"):
            return "any"
        if s in ("auto", "none", ""):
            return None
        return {"tool": raw}                              # a bare tool name -> force that tool
    if isinstance(raw, dict):
        t = (raw.get("type") or "").lower()
        if t in ("any", "required"):
            return "any"
        if t == "tool" and raw.get("name"):
            return {"tool": raw["name"]}
        if t == "function":
            name = (raw.get("function") or {}).get("name")
            return {"tool": name} if name else None
    return None


def replay(trace, model, max_tokens=None):
    """Re-run the recorded request on `model`: output as one string, usage, latency, served model, stop reason.
    A logged OpenAI request is sent as it was; only model and max_tokens change. Tools are bound, never executed.
    No temperature is invented (logged, else the model default): re-runs must sample the natural run-to-run variance."""
    if max_tokens is None:
        # The original's budget: thinking models spend it on thinking, so a smaller one truncates the re-run.
        rec_max = int(trace.get("max_output_tokens") or 0)
        if rec_max > 0:
            max_tokens = min(registry.max_output(model), rec_max)
        else:                                             # not recorded: 3x the original's output
            rec_out = int((trace.get("usage") or {}).get("output_tokens", 0) or 0)
            if rec_out <= 0:
                rec_out = len(trace.get("output") or "") // 4
            max_tokens = min(registry.max_output(model), max(512, rec_out * 3))
    logged = trace.get("logged_request")
    if logged:
        res = llm_client.run(model=model, messages=logged["messages"], max_tokens=max_tokens,
                             params={k: v for k, v in logged.items() if k != "messages"})
    else:
        system = "\n".join(m["content"] for m in trace.get("input_messages", []) if m.get("role") == "system")
        tools = _tools(trace)
        res = llm_client.run(model=model, messages=_messages(trace), max_tokens=max_tokens,
                             system=(system.strip() or None), tools=tools,
                             tool_choice=(_tool_choice(trace.get("tool_choice")) if tools else None),
                             reasoning=bool(trace.get("thinking_enabled")))
    return {"output": _render(res["text"], res["tool_calls"]), "usage": res["usage"], "latency_ms": res["latency_ms"],
            "model": res["model"], "stop_reason": res["stop_reason"], "degraded": res["degraded"]}


def _recorded(trace):
    """The original's recorded output (text and tool calls): the fixed reference, never re-run."""
    return (trace.get("output") or "").strip() or "(empty output)"


def _distinct(traces, n):
    """Up to n inputs that differ in per-call content, walked in bucket order so the sample is stable.
    Lines shared by the whole bucket (the static prompt) are stripped; the rest dedups on word-set Jaccard >= 0.8.
    Word sets, not 3-grams, so a one-word input still counts: a missed variation would make a false SAFE."""
    fulls = [_input_text(t) for t in traces]
    common = set.intersection(*[set(f.split("\n")) for f in fulls]) if fulls else set()
    def words(s):
        return set(s.lower().split())
    kept, kept_w = [], []
    for t, f in zip(traces, fulls):
        var = "\n".join(l for l in f.split("\n") if l not in common)
        w = words(var) or words(f)                                      # fully static: identical inputs collapse to 1
        if any((len(w & k) / max(1, len(w | k))) >= 0.8 for k in kept_w):
            continue
        kept.append(t); kept_w.append(w)
        if len(kept) >= n:
            break
    return kept


def audit_node(node_name, bucket, cheaper, n=AUDIT_SAMPLES, min_evidence=AUDIT_MIN_EVIDENCE):
    """Prove or refute a downgrade to `cheaper` for one call-site: SAFE, NOT-SAFE or LOW-EVIDENCE."""
    model = canonical_model(bucket[0].get("model")) or bucket[0].get("model")
    base = {"node": node_name, "model": model, "cheaper": cheaper,
            "from_tier": tier(model), "to_tier": tier(cheaper), "to_release": release(cheaper)}
    sample = _distinct(bucket, n)
    if len(sample) < min_evidence:
        return {**base, "verdict": "LOW-EVIDENCE", "n": len(sample), "inputs": [],
                "reason": "only %d distinct input(s), need %d" % (len(sample), min_evidence)}
    from app.services.downgrade_refset import prove_transform_refset      # lazy: avoids an import cycle
    inputs, verdict, safe = prove_transform_refset(sample, cheaper, model)
    return {**base, "verdict": verdict, "n": len(inputs), "k": AUDIT_DOWNGRADE_K, "safe_inputs": safe, "inputs": inputs}
