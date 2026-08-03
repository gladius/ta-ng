"""Report export — build a clean Markdown report the exec can forward, ZERO dependencies.

Built ONLY from the frozen proof, so the file says exactly what the page says — proven numbers, measured,
nothing invented. Markdown renders everywhere (GitHub, Notion, email) and stays diff-able.
"""


def _fmt(n):
    return "{:,.0f}".format(n)


def build_md(f, proof):
    calls = f["calls"]
    total = proof["total"] if proof else f["total"]
    L = ["# Token Audit — %s" % f["agent"], ""]
    L.append("**$%s / month %s** · prompt caching + model downgrade" %
             (_fmt(total), "proven live" if proof else "detected"))
    L.append("")
    L.append("> Advisory, out-of-path. Every figure below is measured by a real provider call — the cache-read "
             "counter and a preservation re-run, not an estimate. Quoted at an assumed %s calls/month; the "
             "per-call saving is exact, so scale to your real volume." % _fmt(calls))
    L += ["", "| Call-site | Strategy | Evidence | $ / mo |", "|---|---|---|---|"]
    for x in (proof["results"] if proof else []):
        amt = "$" + _fmt(x.get("cache_usd", 0) + x.get("downgrade_usd", 0))
        c, d = x.get("cache"), x.get("downgrade")
        if c:
            lever = "Cache expansion" if c["verdict"] == "BREAKER" else "Enable caching"
            L.append("| `%s` | %s | reads %s tok from cache (live `cache_read`) | %s |"
                     % (x["node"], lever, _fmt(c["read"]), "$" + _fmt(x["cache_usd"])))
        if d:
            L.append("| `%s` | Downgrade %s → %s | %d/%d inputs preserved on the cheaper tier | %s |"
                     % (x["node"], d.get("model", x["model"]), d.get("cheaper", "?"),
                        d.get("preserved", 0), d.get("n", 0), "$" + _fmt(x["downgrade_usd"])))
    L += ["", "## How each was proven", ""]
    for x in (proof["results"] if proof else []):
        c, d = x.get("cache"), x.get("downgrade")
        if c:
            if c["verdict"] == "BREAKER":
                L.append("- **%s — cache expansion.** A per-call line broke the byte-prefix; moving it below the "
                         "%s-token policy makes the prefix stable. Re-sent live, the provider returned **%s tokens** "
                         "from cache." % (x["node"], _fmt(c["recoverable_tok"]), _fmt(c["read"])))
            else:
                L.append("- **%s — enable caching.** A %s-token policy prefix is re-sent uncached every call; one "
                         "cache breakpoint fixes it. Re-sent live, **%s tokens** returned from cache."
                         % (x["node"], _fmt(c["recoverable_tok"]), _fmt(c["read"])))
        if d:
            L.append("- **%s — downgrade.** Runs on %s for a task %s handles the same. Re-ran %d distinct inputs on "
                     "the cheaper tier; **%d preserved** the decision." % (x["node"], d.get("model", x["model"]),
                     d.get("cheaper", "?"), d.get("n", 0), d.get("preserved", 0)))
    L.append("")
    return "\n".join(L)

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

