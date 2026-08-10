"""Profile-layer selection + full-bucket facts + structure-aware digests. Deterministic, $0.

Three jobs, none of which is a judgment (that's the LLM's job in analyze.py):
  1. SAMPLE — `_distinct(bucket, n)`, the SAME deterministic sampler the downgrade proof uses (app.services.audit),
     so the profile describes the EXACT instances the proof re-runs, and the raw traces flow on to the levers
     (compression is per-instance, so the sample must survive past profiling).
  2. FACTS  — counts over ALL N (not the sample). `distinct_system == 1` is the ground truth that the system prompt
     is identical on every call — a static verdict at 100% coverage, which the analyst reconciles against.
  3. DIGEST — a big instance (50k–200k tokens at enterprise scale) is reduced to a bounded head+tail skeleton so we
     never ship raw giant prompts to the analyst; a small block stays verbatim. Each system block carries a cheap
     "identical across the sample" mark.
"""
from collections import Counter

from app.services.audit import _distinct   # the ONE deterministic sampler — shared with the proof, by design


def _system_text(t):
    return "\n".join(m.get("content") or "" for m in t.get("input_messages", []) if m.get("role") == "system")


def _user_text(t):
    return "\n".join(m.get("content") or "" for m in t.get("input_messages", []) if m.get("role") == "user")


def bucket_facts(bucket):
    """$0 counts over the WHOLE bucket — the cross-check that turns a 5-instance read into a node-level claim."""
    systems = [_system_text(t) for t in bucket]
    users = [_user_text(t) for t in bucket]
    return {
        "n_total": len(bucket),
        "distinct_system": len(set(systems)),          # 1 => system prompt provably identical on every call (static)
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
            # cheap cross-instance evidence: is THIS whole system block identical across the sampled instances?
            d["identical_across_sample"] = sample_systems.count(m.get("content") or "") == len(sample_systems)
        msgs.append(d)
    out = str(trace.get("output") or "")
    return {"trace_id": trace.get("trace_id"), "model": trace.get("model"),
            "messages": msgs, "output": out[:2000], "output_chars": len(out)}


def select(bucket, n=5):
    """-> {sample, digests, facts, coverage}. `sample` = the raw traces (the levers reuse them per-instance)."""
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
