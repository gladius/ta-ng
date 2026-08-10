"""Report export — a downloadable ZIP whose centerpiece is ONE self-contained `report.md` covering BOTH levers
(model-tier downgrade AND cacheable prefix). Built ONLY from the frozen proof — measured, nothing invented, no LLM
in the loop, so the deliverable is reproducible.

The ZIP unpacks to:
  report.md                                     the whole advisory: verdicts, $, and — for cache — the exact reorg
                                                to apply. Read this; it stands on its own.
  evidence/<lever>/<node>/verdict.md            WHAT THE JUDGE FOUND, co-located with the raw text: per-input
                                                verdict, rate (kept X/K · baseline Y/K), and reason.
  evidence/<lever>/<node>/input-<i>/            the raw outputs to diff:
    request.txt · before-original.txt · after-run-<j>.<kept|drift>.txt · baseline-<j>.<kept|drift>.txt
  evidence/cache/<node>/reorged-prefix.txt      the reorged cacheable prefix (the cache "what to do")

<lever> is `downgrade` or `cache` — BOTH are exported. The bulky per-run text stays OUT of report.md (a big agent
would balloon it) and lives under evidence/ instead. stdlib zipfile only — zero dependencies.
"""
import io
import re
import zipfile

_VERDICT = {"SAFE": "safe to downgrade", "BORDERLINE": "borderline — flips, don't downgrade",
            "NOT-SAFE": "not safe — keep current", "LOW-EVIDENCE": "needs more data",
            "N/A": "already cheapest tier"}


def _fmt(n):
    return "{:,.0f}".format(n or 0)


def _slug(s):
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", str(s)).strip("-")[:60] or "node"


def _report_md(f, proof):
    """The single deliverable: both levers, from the frozen proof."""
    calls = f["calls"]
    unit = "%s calls" % _fmt(calls)
    results = proof["results"] if proof else []
    dg = [x for x in results if x.get("downgrade")]
    ca = [x for x in results if x.get("cache") and not x["cache"].get("informational")]
    co = [x for x in results if x.get("compress")]
    total = (proof.get("total", 0) if proof else 0)
    dg_total = (proof.get("downgrade_usd", 0) if proof else 0)
    ca_total = (proof.get("cache_usd", 0) if proof else 0)
    co_total = (proof.get("compress_usd", 0) if proof else 0)

    L = ["# Token Audit — %s" % f["agent"], ""]
    if total > 0:
        L.append("**$%s in verified savings** — ↓ $%s downgrade · ⚡ $%s cache · − $%s compress."
                 % (_fmt(total), _fmt(dg_total), _fmt(ca_total), _fmt(co_total)))
    else:
        L.append("**No safe savings on the audited call-sites.** Each re-ran live and needs its current setup — a "
                 "verified result, not an empty one.")
    L.append("")
    L.append("Audited **%d call-site(s)** across **%s traces**. All $ per %s." % (len(results), f.get("traces", 0), unit))
    L.append("")
    L.append("> Advisory, out-of-path, read-only. Every dollar is earned by a live re-run of real recorded inputs; "
             "unproven potential is never counted as savings. You decide whether to apply.")
    L.append("")

    # ── Model-tier downgrade ──────────────────────────────────────────────────
    L.append("## Model-tier downgrade")
    L.append("")
    if dg:
        L += ["| Call-site | Downgrade | Verdict | Inputs safe | $ / %s |" % unit, "|---|---|---|---|---|"]
        for x in dg:
            d = x["downgrade"]
            amt = "$" + _fmt(x["downgrade_usd"]) if d["verdict"] == "SAFE" else "—"
            safe = "%s/%s" % (d.get("safe_inputs", 0), d.get("n", 0)) if d.get("inputs") else "—"
            L.append("| `%s` | %s → %s | %s | %s | %s |" % (
                x["node"], d.get("model", "?"), d.get("cheaper", "-"), _VERDICT.get(d["verdict"], d["verdict"]), safe, amt))
        L.append("")
        for x in dg:
            d = x["downgrade"]
            L.append("### `%s` — %s → %s · %s" % (x["node"], d.get("model", "?"), d.get("cheaper", "-"),
                                                  _VERDICT.get(d["verdict"], d["verdict"])))
            if d.get("inputs"):
                L.append("Tested %d distinct inputs × %d re-runs each on %s. Evidence: `evidence/downgrade/%s/`."
                         % (d.get("n", 0), d.get("k", 0), d.get("cheaper", "-"), _slug(x.get("key") or x["node"])))
                for i, r in enumerate(d["inputs"], 1):
                    rate = "cheaper %d/%d" % (r["kept"], r["k"])
                    if r.get("self_kept") is not None:
                        rate += ", baseline %d/%d" % (r["self_kept"], r["k"])
                    req = (r.get("input", "") or "")[:160].replace("\n", " ")
                    L.append("- **input %d** — %s (%s) — %s  \n  _req:_ %s" % (
                        i, _IV.get(r["verdict"], r["verdict"]), rate, (r.get("note") or r.get("reason", "")), req))
            else:
                L.append(d.get("reason", ""))
            L.append("")
    else:
        L += ["_No downgrade opportunities among the audited call-sites._", ""]

    # ── Cacheable prefix ──────────────────────────────────────────────────────
    L.append("## Cacheable prefix")
    L.append("")
    if ca:
        for x in ca:
            c = x["cache"]
            ok = c.get("recommend")
            b = (c.get("before") or {}).get("read", 0) or 0
            a = (c.get("after") or {}).get("read", 0) or 0
            L.append("### `%s` — %s" % (x["node"], "safe + cache-proven" if ok else "not recommended"))
            if ok:
                L.append("**Saves $%s per %s.** Cached tokens per call: **%d → %d** (recovered %d). Behaviour "
                         "preserved on %s/%s sampled inputs; the provider round-trip confirmed the cache read."
                         % (_fmt(x["cache_usd"]), unit, b, a, c.get("recovered_tok", 0),
                            c.get("safe_inputs", 0), c.get("n", 0)))
            else:
                L.append(c.get("reason") or "The reorg changed behaviour or did not cache on the round-trip — not "
                         "recommended.")
                if c.get("after"):
                    L.append("Cached tokens per call: **%d → %d** (the reorg's own caching; the verdict above is why "
                             "it's still not recommended)." % (b, a))
            if c.get("inputs"):                          # the behaviour test actually ran — show it (proof we tried)
                L.append("")
                L.append("Behaviour test — %d distinct inputs × %d re-runs each (reorged prompt vs recorded output):"
                         % (c.get("n", 0), c.get("k", 0)))
                for i, r in enumerate(c["inputs"], 1):
                    L.append("- input %d — %s (%d/%d) — %s" % (i, _IV.get(r["verdict"], r["verdict"]), r["kept"], r["k"],
                                                               (r.get("note") or r.get("reason", ""))))
            if ok:
                L.append("")
                L.append("**What to do:** move this fixed block to the FRONT as a cached **system** prefix, set the "
                         "cache breakpoint at its end, and keep every per-call value AFTER it, verbatim:")
                L.append("")
                L.append("```text")
                L.append(c.get("prefix", ""))
                L.append("```")
            L.append("")
    else:
        L += ["_No cacheable-prefix opportunities among the audited call-sites._", ""]

    # ── Prompt compression ────────────────────────────────────────────────────
    L.append("## Prompt compression")
    L.append("")
    if co:
        L += ["| Call-site | Tokens | Verdict | Inputs safe | $ / %s |" % unit, "|---|---|---|---|---|"]
        for x in co:
            c = x["compress"]
            amt = "$" + _fmt(x["compress_usd"]) if c["verdict"] == "SAFE" else "—"
            safe = "%s/%s" % (c.get("safe_inputs", 0), c.get("n", 0)) if c.get("inputs") else "—"
            toks = "%s → %s" % (c.get("before_tok", "?"), c.get("after_tok", "?")) if c.get("after_tok") else "—"
            L.append("| `%s` | %s | %s | %s | %s |" % (x["node"], toks, c["verdict"], safe, amt))
        L.append("")
        for x in co:
            c = x["compress"]
            L.append("### `%s` — %s" % (x["node"], c["verdict"]))
            if c.get("after_tok"):
                L.append("System prompt **%s → %s tokens** (%s). %s"
                         % (c.get("before_tok"), c.get("after_tok"), c.get("mode", "-"),
                            ("Behaviour preserved on %s/%s sampled inputs." % (c.get("safe_inputs", 0), c.get("n", 0))
                             if c["verdict"] == "SAFE" else "Not applied — see per-input results.")))
            else:
                L.append(c.get("reason", ""))
            if c.get("inputs"):
                L.append("")
                L.append("Behaviour test — %d distinct inputs × %d re-runs each (compressed prompt vs recorded output):"
                         % (c.get("n", 0), c.get("k", 0)))
                for i, r in enumerate(c["inputs"], 1):
                    L.append("- input %d — %s (%d/%d) — %s" % (i, _IV.get(r["verdict"], r["verdict"]), r["kept"],
                                                               r["k"], (r.get("note") or r.get("reason", ""))))
            if c["verdict"] == "SAFE" and c.get("compressed"):
                L.append("")
                L.append("**What to do:** replace this call-site's system prompt with the proven-shorter version "
                         "in `evidence/compress/%s/compressed-system.txt`." % _slug(x.get("key") or x["node"]))
            L.append("")
    else:
        L += ["_No safe prompt-compression opportunities among the audited call-sites._", ""]

    L.append("---")
    L.append("_Figures shown per %s — a fixed basis, not a monthly figure; multiply by your agent's real call "
             "volume to scale the dollars. Cache savings assume calls arrive within the provider's cache window; a "
             "call-site called less often than that window will not benefit._" % unit)
    return "\n".join(L) + "\n"


# per-INPUT rollup verdict, shown with the system's own words — BORDERLINE is its own state, NOT "not safe".
_IV = {"SAFE": "safe", "NOT-SAFE": "not safe", "BORDERLINE": "borderline", "LOW-EVIDENCE": "low evidence"}


def _rate(inp):
    r = "%d/%d" % (inp.get("kept", 0), inp.get("k", 0))
    if inp.get("self_kept") is not None:
        r += " · baseline %d/%d" % (inp["self_kept"], inp.get("k", 0))
    return r


def _verdict_md(node_name, lever_label, headline, r):
    """The judge's findings for one call-site's lever — written NEXT TO the raw text so 'what the judge found' is
    readable without cross-referencing report.md. Per input: rollup verdict, rate, and the reason."""
    L = ["# %s — %s" % (node_name, lever_label), "", headline, ""]
    inputs = r.get("inputs", [])
    if not inputs:
        L.append(r.get("reason", "") or "no behaviour test ran for this call-site.")
        return "\n".join(L) + "\n"
    L.append("Behaviour test — %d distinct inputs × %d re-runs each; every re-run is judged against the recorded "
             "original (before-original.txt) and tagged kept/drift in its filename." % (r.get("n", 0), r.get("k", 0)))
    L.append("")
    for i, inp in enumerate(inputs, 1):
        L.append("## input %d — %s (%s)" % (i, _IV.get(inp["verdict"], inp["verdict"]), _rate(inp)))
        L.append("- request: %s" % ((inp.get("input", "") or "")[:200].replace("\n", " ")))
        L.append("- judge: %s" % (inp.get("note") or inp.get("reason", "")))
        L.append("- raw: `input-%d/before-original.txt` vs `input-%d/after-run-*.txt`%s"
                 % (i, i, " (+ baseline-*.txt = the original's own re-runs)" if inp.get("baseline") else ""))
        L.append("")
    return "\n".join(L) + "\n"


def _write_inputs(z, root, inputs):
    """The raw outputs to diff, per input: request, the 'before' (recorded original), each 'after' re-run tagged
    kept/drift, and the self-variance baseline runs."""
    for i, inp in enumerate(inputs, 1):
        base = "%s/input-%d" % (root, i)
        z.writestr("%s/request.txt" % base, inp.get("input", ""))
        z.writestr("%s/before-original.txt" % base, inp.get("recorded", ""))
        for j, s in enumerate(inp.get("samples", []), 1):
            z.writestr("%s/after-run-%d.%s.txt" % (base, j, "kept" if s.get("preserved") else "drift"), s.get("output", ""))
        for j, s in enumerate(inp.get("baseline", []), 1):
            z.writestr("%s/baseline-%d.%s.txt" % (base, j, "kept" if s.get("preserved") else "drift"), s.get("output", ""))


def build_zip(f, proof):
    """Return the audit record as ZIP bytes: report.md (the deliverable) + COMPLETE evidence for BOTH levers."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("report.md", _report_md(f, proof))
        for x in (proof["results"] if proof else []):
            node = _slug(x.get("key") or x["node"])          # slug the UNIQUE key so same-labelled call-sites split
            d = x.get("downgrade")
            if d:
                head = "%s → %s · verdict: %s" % (d.get("model", "?"), d.get("cheaper", "-"),
                                                  _VERDICT.get(d["verdict"], d["verdict"]))
                root = "evidence/downgrade/%s" % node
                z.writestr("%s/verdict.md" % root, _verdict_md(x["node"], "model-tier downgrade", head, d))
                _write_inputs(z, root, d.get("inputs", []))
            c = x.get("cache")
            if c and not c.get("informational"):
                b = (c.get("before") or {}).get("read", 0) or 0
                a = (c.get("after") or {}).get("read", 0) or 0
                head = "cacheable-prefix reorg · verdict: %s" % ("recommended" if c.get("recommend") else "not recommended")
                if c.get("after"):
                    head += "\ncached tokens per call: before %d → after %d" % (b, a)
                root = "evidence/cache/%s" % node
                z.writestr("%s/verdict.md" % root, _verdict_md(x["node"], "cacheable prefix", head, c))
                if c.get("prefix"):
                    z.writestr("%s/reorged-prefix.txt" % root, c.get("prefix", ""))
                _write_inputs(z, root, c.get("inputs", []))
            cp = x.get("compress")
            if cp:
                head = "prompt compression · verdict: %s" % cp.get("verdict", "?")
                if cp.get("after_tok"):
                    head += "\nsystem prompt tokens: before %s → after %s (%s)" % (
                        cp.get("before_tok"), cp.get("after_tok"), cp.get("mode", "-"))
                root = "evidence/compress/%s" % node
                z.writestr("%s/verdict.md" % root, _verdict_md(x["node"], "prompt compression", head, cp))
                if cp.get("verdict") == "SAFE" and cp.get("compressed"):   # the "what to do": the shorter system prompt
                    z.writestr("%s/compressed-system.txt" % root, cp.get("compressed", ""))
                _write_inputs(z, root, cp.get("inputs", []))
    return buf.getvalue()
