"""Dev-only traceability dump. When AUDIT_DEBUG is set, write each pipeline stage per SNAPSHOT to disk so you can
inspect exactly what was fetched, how it was grouped into call-sites, and what was proven:

    .audit_debug/<snap>/
      traces/<call-site>.json   the raw recorded runs in each call-site bucket (what was downloaded)
      graph/nodes.json          the call-sites with their derived stats
      graph/edges.json          the graph edges
      graph/summary.json        agent, trace count, subgraphs, bucket keys
      results/proof.json        the frozen proof (verdicts, per-input evidence)

STRICTLY off unless AUDIT_DEBUG is truthy, so it never runs in production. Best-effort: a dump failure is logged and
swallowed — diagnostics must never break the audit. `.audit_debug/` is gitignored.
"""
import json
import os
import re

_ROOT = ".audit_debug"


def enabled():
    return os.environ.get("AUDIT_DEBUG") not in (None, "", "0", "false", "False")


def _slug(s):
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", str(s)).strip("-")[:80] or "node"


def _write(snap, sub, name, obj):
    d = os.path.join(_ROOT, str(snap), sub)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str, ensure_ascii=False)


def dump_graph(snap, g):
    """After the graph is built: raw per-call-site traces + the derived graph."""
    if not (enabled() and snap and g):
        return
    try:
        buckets = g.get("buckets", {}) or {}
        for key, traces in buckets.items():
            _write(snap, "traces", _slug(key) + ".json", traces)
        _write(snap, "graph", "nodes.json", g.get("nodes", []))
        _write(snap, "graph", "edges.json", g.get("edges", []))
        _write(snap, "graph", "summary.json", {
            "agent": g.get("agent"), "traces": g.get("traces"), "graphs": g.get("graphs", []),
            "node_count": len(g.get("nodes", [])), "bucket_keys": list(buckets.keys()),
            "bucket_sizes": {k: len(v) for k, v in buckets.items()}})
    except Exception as e:                                    # diagnostics must never break the audit
        print("[snapdump] graph dump failed for %s: %s" % (snap, e))


def dump_results(snap, proof):
    """After proving: the frozen proof."""
    if not (enabled() and snap and proof):
        return
    try:
        _write(snap, "results", "proof.json", proof)
    except Exception as e:
        print("[snapdump] results dump failed for %s: %s" % (snap, e))


def _judge_cache_verdict(n, reads, writes, sent, anth, avg_in):
    """Explain a 0% cache hit from EVIDENCE (was cache_control sent? treated as Anthropic? tokens above the min?),
    not a one-size guess — so the note points at the real cause (stripped locally vs. dropped by a gateway vs. below
    the model's minimum vs. write-but-never-read)."""
    if reads:
        return ("**Caching ENGAGED** — the shared judge prefix was written once and re-read: fewer full-price input "
                "tokens and lower latency on the repeat calls.")
    if sent == 0:
        return ("**Caching did NOT engage — `cache_control` was never sent.** Only %d/%d calls were treated as "
                "Anthropic, so the breakpoint was stripped before the request. If the judge really IS Claude, its model "
                "string (a litellm alias?) just doesn't contain `claude` — widen `llm_client._is_anthropic` to match "
                "the alias so the breakpoint survives." % (anth, n))
    if writes == 0 and avg_in < 1200:
        return ("**Caching did NOT engage — the prefix is below the model's ~1024-token minimum** (only ~%d input "
                "tokens/call). Expected for small agents; the win is on big-context (RAG/tool) agents." % avg_in)
    if writes == 0:
        return ("**`cache_control` WAS sent on %d/%d calls, but the provider reported 0 cache writes** at ~%d input "
                "tokens/call (above the ~1024 min). On a Claude judge the usual cause is a **litellm gateway dropping "
                "`cache_control`** before Anthropic (or not returning cache-usage fields). Confirm by pointing the "
                "judge directly at the Anthropic API for one run." % (sent, n, avg_in))
    return ("**Cache was written but never read** — the prefix isn't being reused. Likely the K votes fire in parallel "
            "and race the first write, or the reference-set prefix isn't byte-identical across the vote calls.")


def dump_judge_cache(snap):
    """AUDIT_DEBUG: the judge-cache PROOF — did prompt caching actually engage on the audit's judge calls, and is it
    cheaper + faster? Writes .audit_debug/<snap>/JUDGE_CACHE.md from llm_client's recorded per-call cache stats."""
    if not (enabled() and snap):
        return
    try:
        from app.services import debugcap
        stats = debugcap.calls()
        if not stats:
            return
        n = len(stats)
        writes = sum(1 for s in stats if s["cache_write"] > 0)
        reads = sum(1 for s in stats if s["cache_read"] > 0)
        wtok = sum(s["cache_write"] for s in stats)
        rtok = sum(s["cache_read"] for s in stats)
        itok = sum(s["input"] for s in stats)
        otok = sum(s.get("output", 0) for s in stats)
        avg_ms = round(sum(s["ms"] for s in stats) / n)
        avg_in, avg_out = round(itok / n), round(otok / n)
        sent = sum(1 for s in stats if s.get("cache_sent"))          # cache_control actually left our process
        anth = sum(1 for s in stats if s.get("anthropic"))           # treated as Claude (else breakpoint stripped)
        models = sorted({str(s.get("model")) for s in stats})
        bases = sorted({s.get("base_url") for s in stats if s.get("base_url")})
        L = ["# Judge-cache proof  (AUDIT_DEBUG)\n",
             "- judge / eval calls: **%d**   ·   model(s): %s" % (n, ", ".join("`%s`" % m for m in models)),
             "- base_url (gateway in path?): %s" % (", ".join("`%s`" % b for b in bases) or "_default — direct to Anthropic_"),
             "- treated as Anthropic (cache honored): **%d/%d**   ·   `cache_control` actually SENT: **%d/%d**" % (anth, n, sent, n),
             "- input tokens: **%d** (avg **%d**/call)   ·   output tokens: **%d** (avg **%d**/call)" % (itok, avg_in, otok, avg_out),
             "- calls that WROTE cache (`cache_creation`): **%d**  (%d tokens, ~+25%% once)" % (writes, wtok),
             "- calls that READ cache (`cache_read`): **%d**  (%d tokens billed at ~10%%)" % (reads, rtok),
             "- cache-hit rate: **%d%%**   ·   avg latency: **%dms**\n" % (round(100 * reads / max(1, n)), avg_ms),
             _judge_cache_verdict(n, reads, writes, sent, anth, avg_in)]
        d = os.path.join(_ROOT, str(snap))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "JUDGE_CACHE.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
    except Exception as e:
        print("[snapdump] judge-cache dump failed for %s: %s" % (snap, e))


def dump_judge_raw(snap):
    """AUDIT_DEBUG: EXACTLY what the fit judge returned (`raw`) next to what we parsed — so a canned
    'differs from the set' can be diagnosed at a glance: is `raw` BLANK / just a verdict word (the model gave no
    reason — not a parse bug), or does it hold a reason we failed to keep (a parse bug)? Writes JUDGE_RAW.md."""
    if not (enabled() and snap):
        return
    try:
        from app.services import debugcap
        js = debugcap.judges()
        if not js:
            return
        blank = sum(1 for j in js if not (j.get("raw") or "").strip())
        no_reason = sum(1 for j in js if (j.get("reason") or "") in ("matches the set", "differs from the set"))
        L = ["# Judge raw output  (AUDIT_DEBUG)\n",
             "The fit judge's ACTUAL reply (`raw`) vs. what we parsed. If `raw` is blank or only a verdict word, the",
             "model isn't returning a reason (fix the model/gateway, not the parser); if `raw` holds a reason but the",
             "parsed reason is the canned string, that's a parse bug.\n",
             "- fit-judge calls: **%d**" % len(js),
             "- replies that were BLANK: **%d / %d**" % (blank, len(js)),
             "- parsed reason fell back to the canned string: **%d / %d**\n" % (no_reason, len(js))]
        for i, j in enumerate(js[:60], 1):                    # bound the file; the counts above cover the whole run
            L += ["---",
                  "### call %d · model `%s`" % (i, j.get("model")),
                  "**parsed → kept=`%s`  reason=`%s`**\n" % (j.get("kept"), j.get("reason")),
                  "_input being judged (user):_", "```", (j.get("input") or "").strip() or "(none)", "```",
                  "_candidate (cheaper model's output being judged):_", "```", (j.get("candidate") or "").strip() or "(none)", "```",
                  "_judge's RAW reply:_", "```", (j.get("raw") or "").strip() or "(EMPTY REPLY — the model returned no text)", "```"]
        if len(js) > 60:
            L.append("\n_…and %d more (see the counts at the top for the whole run)._" % (len(js) - 60))
        d = os.path.join(_ROOT, str(snap))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "JUDGE_RAW.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
    except Exception as e:
        print("[snapdump] judge-raw dump failed for %s: %s" % (snap, e))
