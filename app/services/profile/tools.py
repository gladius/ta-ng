"""Read-only, $0 inspection tools the analyst AGENT drives (Phase 3). Each returns a COMPACT, bounded,
JSON-serializable result over ONE node's recorded bucket — never a raw giant prompt. The agent calls these to
resolve the genuine ambiguity the fact-pack can't settle on its own (a same-shape semantic mix, an unclear job,
a varying frame); on an easy node it calls nothing. Deterministic: no LLM, no lever, no cost. Reuses select's
text helpers + facts' frame-strip so a tool result matches exactly what the deterministic layer already saw.

Matches CHARACTERIZATION-PLAN.md §5: sample · group · diff · flow (walk is DEFERRED to the error-loop).
"""
from app.services.audit import _distinct                       # the ONE shared deterministic sampler
from app.services.profile.select import _system_text, _user_text
from app.services.profile.facts import _norm, _out_shape

_CAP = 240                                                     # per-field char cap — the agent reasons over shape


def _short(s, cap=_CAP):
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= cap else s[:cap] + "…"


def _one(t):
    """One call reduced to its three decision-relevant fields, each capped."""
    return {"trace_id": t.get("trace_id"), "system": _short(_system_text(t)),
            "user": _short(_user_text(t)), "output": _short(str(t.get("output") or ""))}


def sample(bucket, strategy="diverse", n=5, seen=None):
    """Read a few calls. strategy: diverse (frame-stripped distinct — the default) · by_shape (one per output
    shape) · more (distinct calls NOT already seen — fetch-more for a rare mode). Bounded n<=8."""
    n = max(1, min(int(n or 5), 8))
    seen = set(seen or [])
    if strategy == "more":
        pool = [t for t in bucket if t.get("trace_id") not in seen]
        picks = _distinct(pool, n) if pool else []
    elif strategy == "by_shape":
        by = {}
        for t in bucket:                                       # first example seen per output shape
            by.setdefault(_out_shape(str(t.get("output") or "")), t)
        picks = list(by.values())[:n]
    else:
        picks = _distinct(bucket, n)
    return {"strategy": strategy, "returned": len(picks), "calls": [_one(t) for t in picks]}


def group(bucket, by="output"):
    """Partition the WHOLE bucket by a $0 key -> {distinct_groups, groups:[{label,count,example}]}. by: output
    (shape + frame-stripped template — the mixed-JOB signal, since two prose modes of different meaning fall in
    different template groups) · tool (tool names called) · system (the standing frame). Bounded to the 6 largest
    groups (+ an 'other' tally) so the result stays small even at 100k calls."""
    groups = {}
    for t in bucket:
        if by == "tool":
            k = ", ".join(sorted(t.get("tools_called") or [])) or "(no tools)"
        elif by == "system":
            k = _short(_system_text(t), 120) or "(no system)"
        else:                                                  # output: shape + frame-stripped template
            o = str(t.get("output") or "")
            k = "%s :: %s" % (_out_shape(o), _short(_norm(o), 120))
        g = groups.setdefault(k, {"count": 0, "example": None})
        g["count"] += 1
        if g["example"] is None:
            g["example"] = _one(t)
    ordered = sorted(groups.items(), key=lambda kv: -kv[1]["count"])
    out = {"by": by, "distinct_groups": len(groups),
           "groups": [dict(label=k, **v) for k, v in ordered[:6]]}
    if len(ordered) > 6:
        out["other_groups"] = len(ordered) - 6
        out["other_calls"] = sum(v["count"] for _, v in ordered[6:])
    return out


def diff(bucket, a, b):
    """Two calls side by side (by trace_id or 0-based index) — to see what actually changes between calls."""
    def _find(ref):
        for i, t in enumerate(bucket):
            if str(t.get("trace_id")) == str(ref) or i == ref:
                return t
        return None
    ta, tb = _find(a), _find(b)
    return {"found": bool(ta and tb), "a": _one(ta) if ta else None, "b": _one(tb) if tb else None}


def flow(edges, key):
    """Aggregate neighbours from the OBSERVED edges: what feeds this node / what it feeds (names -> counts).
    A node fed by a router is a worker; one feeding a responder is a mid-stage — $0 context from the graph."""
    up, down = {}, {}
    for e in edges or []:
        if e.get("dst") == key and e.get("src") != key:
            nm = e["src"].rsplit("/", 1)[-1]
            up[nm] = up.get(nm, 0) + e.get("count", 1)
        if e.get("src") == key and e.get("dst") != key:
            nm = e["dst"].rsplit("/", 1)[-1]
            down[nm] = down.get(nm, 0) + e.get("count", 1)
    return {"upstream": up, "downstream": down}
