"""Profile sampling, whole-bucket facts and bounded digests. Deterministic, no LLM.
The sample is audit._distinct, the same one the downgrade proof re-runs; facts count all N calls; big messages are
cut to head + tail so the analyst never gets a giant prompt.
"""
from collections import Counter

from app.services.audit import _distinct


def _system_text(t):
    return "\n".join(m.get("content") or "" for m in t.get("input_messages", []) if m.get("role") == "system")


def _user_text(t):
    return "\n".join(m.get("content") or "" for m in t.get("input_messages", []) if m.get("role") == "user")


def bucket_facts(bucket):
    """Counts over the whole bucket: the cross-check that turns a 5-instance read into a node-level claim."""
    systems = [_system_text(t) for t in bucket]
    users = [_user_text(t) for t in bucket]
    return {
        "n_total": len(bucket),
        "distinct_system": len(set(systems)),          # 1: the system prompt is identical on every call
        "distinct_user": len(set(users)),
        "models": dict(Counter((t.get("model") or "?") for t in bucket)),
    }


def _digest_msg(content, small=1400, head=900, tail=400):
    """Big block -> head + '[omitted]' + tail; small block -> verbatim. Returns (text, original_char_count)."""
    content = content or ""
    n = len(content)
    if n <= small:
        return content, n
    return content[:head] + ("\n...[%d chars omitted]...\n" % (n - head - tail)) + content[-tail:], n


def _digest(trace, sample_systems):
    msgs = []
    for m in trace.get("input_messages", []):
        role = m.get("role", "user")
        body, n = _digest_msg(m.get("content"))
        d = {"role": role, "chars": n, "text": body}
        if role == "system":
            # is this whole system block identical across the sampled instances?
            d["identical_across_sample"] = sample_systems.count(m.get("content") or "") == len(sample_systems)
        msgs.append(d)
    out = str(trace.get("output") or "")
    return {"trace_id": trace.get("trace_id"), "model": trace.get("model"),
            "messages": msgs, "output": out[:2000], "output_chars": len(out)}


def select(bucket, n=5):
    """-> {sample, digests, facts, coverage}. `sample` = the raw traces."""
    sample = _distinct(bucket, n)
    facts = bucket_facts(bucket)
    sample_systems = [_system_text(t) for t in sample]
    digests = [_digest(t, sample_systems) for t in sample]
    coverage = {
        "sampled": len(sample),
        "n_total": facts["n_total"],
        "distinct_system_all_N": facts["distinct_system"],
        "distinct_system_in_sample": len(set(sample_systems)),
        "distinct_user_all_N": facts["distinct_user"],
    }
    return {"sample": sample, "digests": digests, "facts": facts, "coverage": coverage}
