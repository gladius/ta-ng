"""Downgrade audit — shared replay machinery + input sampling.

The DOWNGRADE verdict lives in app/services/downgrade_refset.py (prove_transform_refset): per input the ORIGINAL
model's own 5 outputs (recorded + re-runs) ARE the acceptable-behaviour reference set, and each cheaper re-run is
majority-voted to belong to it. `audit_node` samples N distinct inputs and delegates to it.

This module provides the pieces that path shares:
  replay(trace, model)   -> re-run the recorded request on `model`: {output (text + tool calls as ONE string),
                            usage, latency_ms, model as served, stop_reason, degraded}.
  _recorded(trace)       -> the ORIGINAL's recorded output — the reference we must preserve (never re-run).
  _distinct / _tools / _tool_choice / _render / _messages -> input sampling + provider-neutral request shaping.
"""
import json

from app.catalog import canonical_model, tier, release
from app import registry                                     # gateway-first max_output (deployment's real ceiling)
from app.services import llm_client
from app.config import AUDIT_SAMPLES, AUDIT_MIN_EVIDENCE, AUDIT_DOWNGRADE_K


def _user_text(t):
    return " ".join(m["content"] for m in t.get("input_messages", []) if m.get("role") == "user")


def _input_text(t):
    """The FULL input a call sent — every message (system + user + prior turns / tool results). The diversity key for
    _distinct: variation is caught wherever it lives (query / tool result / system), and the shared fixed part dilutes
    into the overlap instead of faking diversity."""
    return "\n".join((m.get("content") or "") for m in t.get("input_messages", []))


def _system_text(t):
    return "\n".join((m.get("content") or "") for m in t.get("input_messages", []) if m.get("role") == "system")


def _messages(trace):
    """Recorded conversation as an Anthropic messages array: system pulled out, roles mapped, consecutive
    same-role merged (the API needs alternation), must start with user. Recorded tool results are text."""
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
    """The recorded BOUND tools as NEUTRAL tool intents [{name, description, schema}] for llm_client.run. The stored
    schema may be Anthropic-style (`input_schema`) OR OpenAI/LangChain function-style (`parameters`) — read BOTH,
    else an OpenAI-format tool binds with an EMPTY schema and the cheaper model can't reproduce the recorded call
    (a false 'wrong tool/args' drift). Provider translation lives in llm_client, not here."""
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
    """A model's behavior as ONE string: its prose plus any tool call rendered as `[calls name({args})]`, so the
    ONE judge sees the full behavior — no structured/free-text branch. Deterministic arg order for readability."""
    parts = [text.strip()] if (text and text.strip()) else []
    for tc in tool_calls or []:
        parts.append("[calls %s(%s)]" % (tc.get("name", "?"), json.dumps(tc.get("args", {}), sort_keys=True)))
    return "\n".join(parts).strip() or "(empty output)"


def _tool_choice(raw):
    """Convert a recorded tool_choice to NEUTRAL form so a FORCED tool call is reproduced on replay (llm_client
    translates neutral -> provider). Returns None for auto / none / unset (let the model choose, matching the
    original), 'any' to force some tool, or {'tool': name} to force a specific one. Handles OpenAI ('required',
    {'type':'function','function':{'name':X}}), Anthropic ({'type':'any'|'tool'|'auto'}), and the bare tool-name
    string some frameworks log."""
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
        if t == "function":                               # OpenAI {'type':'function','function':{'name':X}}
            name = (raw.get("function") or {}).get("name")
            return {"tool": name} if name else None
    return None                                           # auto / none / unknown -> don't force


def replay(trace, model, max_tokens=None):
    """Re-run the recorded request on `model` and return the run record (behaviour rendered as one string + usage).
    This expresses NEUTRAL INTENT only — bind these tools, force this tool_choice, use reasoning if the node did —
    and hands it to llm_client.run, which owns the provider request shape (swap that one boundary for litellm in
    prod). Tools BOUND (read-only), never executed. Output budget SCALES to the recorded output (+headroom), capped
    at the model's real max_output. temperature left at the model DEFAULT on purpose — re-runs must SAMPLE the model's
    natural run-to-run variance (that's exactly what coherence/self-consistency measures), and the thinking path
    rejects a custom temperature anyway. The deterministic side (judges, optimizer, profiler) goes through complete()."""
    if max_tokens is None:
        # FAITHFUL budget: give the re-run the SAME room the ORIGINAL ran under (trace's max_output_tokens). A thinking
        # model (Gemini 2.5 thinks by default) counts thinking tokens AGAINST this budget, so a budget sized to the
        # visible output alone truncates/empties the re-run — a chopped output then looks "broke" to the judge. Applies
        # to BOTH the original re-runs and the cheaper re-runs so they're judged on equal footing.
        rec_max = int(trace.get("max_output_tokens") or 0)
        if rec_max > 0:
            max_tokens = min(registry.max_output(model), rec_max)
        else:                                             # not recorded -> 3x the original's OUTPUT as headroom
            rec_out = int((trace.get("usage") or {}).get("output_tokens", 0) or 0)
            if rec_out <= 0:
                rec_out = len(trace.get("output") or "") // 4
            max_tokens = min(registry.max_output(model), max(512, rec_out * 3))
    system = "\n".join(m["content"] for m in trace.get("input_messages", []) if m.get("role") == "system")
    tools = _tools(trace)
    res = llm_client.run(model=model, messages=_messages(trace), max_tokens=max_tokens,
                         system=(system.strip() or None), tools=tools,
                         tool_choice=(_tool_choice(trace.get("tool_choice")) if tools else None),
                         reasoning=bool(trace.get("thinking_enabled")))
    return {"output": _render(res["text"], res["tool_calls"]), "usage": res["usage"], "latency_ms": res["latency_ms"],
            "model": res["model"], "stop_reason": res["stop_reason"], "degraded": res["degraded"]}


def _recorded(trace):
    """The ORIGINAL's recorded behavior — the fixed reference we must preserve. The connector stores the model's
    full output (text + any tool call) here, so no original re-run is needed."""
    return (trace.get("output") or "").strip() or "(empty output)"


def _distinct(traces, n):
    """Up to n inputs that differ in their PER-CALL VARIABLE content — the part that actually changes behaviour.
    DETERMINISTIC (walks the bucket in order), so the SAMPLE is stable across audits; only the repeats measure noise.

    We key diversity on what VARIES per call, NOT the whole prompt. A call-site is a shared static skeleton (the system
    config / instructions, byte-identical every call) + per-call content (the query, a tool result, an injected id).
    The static carries ZERO behavioural variety yet can dwarf the prompt by bytes, so whole-prompt similarity goes
    BLIND to real variety under a big fixed prefix (8 genuinely different tickets under an 8k-token system read as 1 —
    letting a rule only SOME inputs trigger slip through as a false SAFE). So we STRIP the lines byte-identical across
    the whole bucket (the shared skeleton) and dedup on the remainder — the per-call variable.

    Dedup is WORD-SET (1-gram) Jaccard >= 0.8, deliberately NOT 3-gram: a genuinely SHORT variable (a classifier's
    one-word input under a big rubric) has no 3-grams, so a 3-gram key falls back to the whole prompt and COLLAPSES it
    to 1 — a false SAFE on the very simplest agents. A word set works from one word up. It cannot tell a genuine short
    variable from short NOISE (a session id), so it COUNTS both — the SAFE default for a prove-it tool: over-sampling a
    redundant real input is harmless; MISSING a real variation manufactures a false SAFE. A
    fully-identical bucket has no variable, so it correctly collapses to 1."""
    fulls = [_input_text(t) for t in traces]
    common = set.intersection(*[set(f.split("\n")) for f in fulls]) if fulls else set()  # the shared skeleton lines
    def words(s):
        return set(s.lower().split())                                   # 1-gram: robust from a single word up
    kept, kept_w = [], []
    for t, f in zip(traces, fulls):
        var = "\n".join(l for l in f.split("\n") if l not in common)     # the per-call VARIABLE (skeleton stripped)
        w = words(var) or words(f)                                      # no variable (fully static) -> full -> collapses identical
        if any((len(w & k) / max(1, len(w | k))) >= 0.8 for k in kept_w):
            continue
        kept.append(t); kept_w.append(w)
        if len(kept) >= n:
            break
    return kept


def audit_node(node_name, bucket, cheaper, n=AUDIT_SAMPLES, min_evidence=AUDIT_MIN_EVIDENCE):
    """Prove (or refute) a downgrade for one call-site across N inputs. Returns the verdict dict.
    Verdicts: SAFE (recommend) · NOT-SAFE (don't) · LOW-EVIDENCE (abstain).

    `cheaper` is the target the funnel already chose (node-aware net-saving pick) — passed in so the audit re-runs
    the SAME model the report shows."""
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
