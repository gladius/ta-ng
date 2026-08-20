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


# ── Human-readable RAW-FORMAT capture — the "close it once and for all" report ────────────────────────────────────
def _meta(rec):
    return (rec.get("extra") or {}).get("metadata") or {}


_REV_KEYS = ("revision_id", "revision", "ls_revision_id",
             "LANGSMITH_HOST_REVISION_ID", "LANGSMITH_LANGGRAPH_API_REVISION")


def _has_model(r):
    ip = (r.get("extra") or {}).get("invocation_params") or {}
    return bool(ip.get("model") or ip.get("model_name") or _meta(r).get("ls_model_name"))


def _has_usage(r):
    if r.get("prompt_tokens") is not None or r.get("completion_tokens") is not None:
        return True
    return bool(((r.get("outputs") or {}).get("llm_output") or {}).get("token_usage"))


def _pc(k, d):
    return "%d/%d (%d%%)" % (k, d, round(100 * k / d) if d else 0)


def _usage_loc(r):
    """WHERE token usage lives — a format-diff signal (Gemini/other providers nest it differently)."""
    if r.get("prompt_tokens") is not None or r.get("completion_tokens") is not None:
        return "top-level (prompt_tokens/completion_tokens)"
    if ((r.get("outputs") or {}).get("llm_output") or {}).get("token_usage"):
        return "outputs.llm_output.token_usage"
    return "(not found)"


def _model_loc(r):
    """WHERE the model name lives — a format-diff signal."""
    ip = (r.get("extra") or {}).get("invocation_params") or {}
    if ip.get("model") or ip.get("model_name"):
        return "extra.invocation_params.model"
    if _meta(r).get("ls_model_name"):
        return "extra.metadata.ls_model_name"
    return "(not found)"


def _capture_md(source, project, g, sample, avail):
    """A FORMAT-CONFORMANCE + ROOT-CAUSE report (not raw dumps): does the data carry what the code reads, and if
    the graph is thin, exactly WHY (few traces / untagged collapse / no structural / no edges)."""
    from collections import Counter
    try:
        from connectors.graph import _is_generic
    except Exception:
        _is_generic = lambda s: not s
    by_tr = {}
    for r in sample:
        by_tr.setdefault(r.get("trace_id"), []).append(r)
    n = len(sample)
    llm = [r for r in sample if r.get("run_type") in (None, "llm")]
    struct_runs = [r for r in sample if r.get("run_type") in ("tool", "retriever")]
    rt = Counter(r.get("run_type") for r in sample)
    rev_key = next((k for r in sample for k in _REV_KEYS if _meta(r).get(k)), None)
    revs = sorted({_meta(r).get(rev_key) for r in sample if rev_key and _meta(r).get(rev_key)})
    tagged = sum(1 for r in llm if _meta(r).get("langgraph_node"))
    has_parent = sum(1 for r in sample if r.get("parent_run_id"))
    has_dotted = sum(1 for r in sample if r.get("dotted_order"))
    has_tid = sum(1 for r in sample if r.get("trace_id"))
    has_rt = sum(1 for r in sample if r.get("run_type") is not None)
    has_rev = sum(1 for r in sample if any(_meta(r).get(k) for k in _REV_KEYS))
    has_model = sum(1 for r in llm if _has_model(r))
    has_in = sum(1 for r in llm if r.get("inputs"))
    has_out = sum(1 for r in llm if r.get("outputs"))
    has_usage = sum(1 for r in llm if _has_usage(r))
    av_n, av_cap = avail if isinstance(avail, tuple) else (avail, False)
    nodes, structn, edges = g.get("nodes", []), g.get("structural_nodes", []), g.get("edges", [])
    untag = sum(1 for x in nodes if x.get("untagged"))
    tool_names = sorted({(r.get("name") or "") for r in struct_runs})
    tool_generic = sum(1 for r in struct_runs if _is_generic(r.get("name") or ""))
    nl = len(llm)
    m0 = _meta(llm[0]) if llm else {}
    provider = m0.get("ls_provider") or "?"
    model0 = (m0.get("ls_model_name")
              or ((llm[0].get("extra") or {}).get("invocation_params") or {}).get("model")) if llm else "?"
    full_rt = g.get("run_totals") or {}                   # run_type counts over ALL fetched traces (the full graph)
    full_runs = sum(full_rt.values())
    full_census = g.get("node_run_types") or {}           # per-node run_type census over ALL runs (not the sample)

    L = ["# Trace capture — `%s`  (source: %s)\n" % (project, source),
         "## Availability & fetch",
         "- Traces available in project (roots): **%s**%s" % (
             av_n if av_n is not None else "n/a", "  _(capped — at least this many)_" if av_cap else ""),
         "- Traces built into the graph: **%s** (the full fetch)   ·   runs across them: **%d**   ·   run_type mix: `%s`" % (
             g.get("traces"), full_runs, dict(full_rt)),
         "- _The format table below is checked on a small **%d-trace / %d-run** raw sample (cheap); the graph itself "
         "uses all %s traces above._" % (len(by_tr), n, g.get("traces")),
         "- Model / provider (sample llm): **%s** / **%s**\n" % (model0, provider),
         "## Format conformance — does the data carry what the code reads?",
         "| field the code reads | present in sample | drives |",
         "|---|---|---|",
         "| `trace_id` | %s | grouping runs into traces |" % _pc(has_tid, n),
         "| `parent_run_id` | %s | topology / edges (roots have none) |" % _pc(has_parent, n),
         "| `dotted_order` | %s | run ordering / completeness |" % _pc(has_dotted, n),
         "| `run_type` | %s | llm-vs-tool classification |" % _pc(has_rt, n),
         "| `metadata.langgraph_node` (llm) | %s | **call-site labels (tagged)** |" % _pc(tagged, nl),
         "| revision (any known key) | %s%s | version scoping |" % (
             _pc(has_rev, n), ("  ← via `%s`" % rev_key) if rev_key else "  ← **none present**"),
         "| model (invocation / ls_model_name) (llm) | %s | model downgrade |" % _pc(has_model, nl),
         "| `inputs` (llm) | %s | the prompt (audit) |" % _pc(has_in, nl),
         "| `outputs` (llm) | %s | the behavior (audit) |" % _pc(has_out, nl),
         "| token usage (llm) | %s | cost |\n" % _pc(has_usage, nl),
         "- revisions (in sample): %s\n" % (revs or "_(none)_"),
         "## Node census — distinct `langgraph_node` × run_type (FULL graph — all %s traces)" % g.get("traces"),
         "Distinct nodes seen: **%d**\n" % len(full_census),     # blank line BEFORE the table so it renders
         "| node | llm | chain | tool | other | total |",
         "|---|---|---|---|---|---|"]
    for _node, _c in sorted(full_census.items(), key=lambda kv: -sum(kv[1].values())):
        _llm = _c.get("llm", 0) + _c.get(None, 0)
        _tot = sum(_c.values())
        _other = _tot - _llm - _c.get("chain", 0) - _c.get("tool", 0)
        L.append("| %s | %d | %d | %d | %d | %d |" % (
            _node, _llm, _c.get("chain", 0), _c.get("tool", 0), _other, _tot))
    L += ["", "## Why the graph looks like it does (root cause)"]

    if av_n is not None and not av_cap and av_n <= 12:
        L.append("- **Traces:** only ~%s exist in the project — the low count is the DATA, not the fetch." % av_n)
    elif av_n and g.get("traces") is not None and g["traces"] < av_n:
        L.append("- **Traces:** %s%s available but built %s (fetch limit) — raise the limit to pull more." % (
            av_n, "+" if av_cap else "", g["traces"]))
    else:
        L.append("- **Traces:** built %s (available ~%s)." % (g.get("traces"), av_n))

    if llm and not tagged:
        L.append("- **Call-sites = %d (untagged %d):** `langgraph_node` MISSING on %s llm runs → every call "
                 "collapses to ONE untagged call-site. **This is the '1 call-site'.** Fix: tag nodes, or split "
                 "untagged ops in the connector." % (len(nodes), untag, _pc(nl - tagged, nl)))
    else:
        L.append("- **Call-sites = %d (untagged %d):** `langgraph_node` present on %s → nodes label + separate "
                 "correctly." % (len(nodes), untag, _pc(tagged, nl)))

    if not structn and not struct_runs:
        L.append("- **Structural (non-llm) nodes = 0:** the sample has NO `tool`/`retriever` runs — this agent uses "
                 "none, or they aren't traced.")
    elif not structn and struct_runs:
        L.append("- **Structural nodes = 0:** %d tool/retriever runs exist but %s have GENERIC names %s → filtered "
                 "out. They need real names to appear." % (len(struct_runs), _pc(tool_generic, len(struct_runs)), tool_names))
    else:
        L.append("- **Structural (non-llm) nodes = %d:** %d tool/retriever runs, names %s → captured." % (
            len(structn), len(struct_runs), tool_names))

    if not edges:
        why = []
        if has_parent < n * 0.5:
            why.append("`parent_run_id` missing on %s" % _pc(n - has_parent, n))
        if has_dotted < n * 0.5:
            why.append("`dotted_order` missing on %s" % _pc(n - has_dotted, n))
        why = why or ["nodes resolved to generic names (no keyable transitions)"]
        L.append("- **Edges = 0:** topology couldn't be built — %s." % "; ".join(why))
    else:
        L.append("- **Edges = %d:** `parent_run_id` %s + `dotted_order` %s → flow reconstructed." % (
            len(edges), _pc(has_parent, n), _pc(has_dotted, n)))

    if len(revs) > 1:
        L.append("- **Revisions:** %d versions in the window (%s) → the audit would MIX versions; pin one."
                 % (len(revs), revs))

    L.append("\n## Metadata keys on a sample llm run (`extra.metadata` — for a format-diff)")
    L.append("`%s`" % (sorted(_meta(llm[0]).keys()) if llm else "(no llm run in sample)"))
    L.append("\n## Top-level run keys on a sample llm run (the run object's OWN fields)")
    L.append("`%s`" % (sorted(llm[0].keys()) if llm else "(no llm run in sample)"))
    if llm:
        L.append("- token usage lives in: **%s**" % _usage_loc(llm[0]))
        L.append("- model name lives in: **%s**" % _model_loc(llm[0]))
    return "\n".join(L) + "\n"


def dump_capture(snap, source, ws, project, g):
    """AUDIT_DEBUG: write .audit_debug/<snap>/CAPTURE.md — a human-readable capture of the RAW trace FORMAT
    (with a couple of verbatim runs so you can see if metadata/langgraph_node is present) + how many traces are
    available vs fetched + a plain diagnosis. Does a SMALL extra read-only fetch (debug-only). Never breaks the audit."""
    if not (enabled() and snap and g):
        return
    try:
        import os as _os
        from connectors import get_adapter
        _os.environ["LANGSMITH_WORKSPACE_ID"] = ws or ""
        ad = get_adapter(source)
        try:
            sample = list(ad.fetch(project=project, traces=15))       # small, just to characterize the format
        except TypeError:
            sample = list(ad.fetch(project=project, limit=15))        # old connector (no `traces` kw)
        avail = getattr(ad, "available_traces", lambda *a, **k: (None, False))(project)
        d = _os.path.join(_ROOT, str(snap))
        _os.makedirs(d, exist_ok=True)
        with open(_os.path.join(d, "CAPTURE.md"), "w", encoding="utf-8") as f:
            f.write(_capture_md(source, project, g, sample, avail))
    except Exception as e:                                            # diagnostics must never break the audit
        print("[snapdump] capture dump failed for %s: %s" % (snap, e))
