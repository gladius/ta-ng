"""Report export — a clean Markdown report the reviewer can forward, ZERO dependencies.

Built ONLY from the frozen proof, so the file says exactly what the page says — measured, nothing invented.
Unlike the page (scannable), the download carries the FULL evidence: every input, every re-run, and the original
recorded behavior vs each cheaper re-run — so a downgrade call can be audited offline. Markdown renders everywhere.
"""


def _fmt(n):
    return "{:,.0f}".format(n)


_VERDICT = {"SAFE": "✅ safe to downgrade", "BORDERLINE": "⚠ borderline — flips, don't downgrade",
            "NOT-SAFE": "❌ not safe — keep current", "LOW-EVIDENCE": "⚠ needs more data",
            "N/A": "already cheapest tier"}


def build_md(f, proof):
    calls = f["calls"]
    unit = "%s calls" % _fmt(calls)
    total = (proof["downgrade_usd"] if proof else f["downgrade_total"])
    rows = [x for x in (proof["results"] if proof else []) if x.get("downgrade")]

    L = ["# Token Audit — %s · model-tier downgrade" % f["agent"], ""]
    L.append("**$%s per %s** %s" % (_fmt(total), unit, "· proven live" if proof else "· detected, not yet proven"))
    L.append("")
    L.append("> Advisory, out-of-path, read-only. Each candidate is tested on real recorded inputs, and each input "
             "is **re-run K times** on the cheaper model — a downgrade is called SAFE only if every input preserved "
             "behavior in **every** re-run (unanimous), so verdicts don't flip between audits. **All $ are per %s** "
             "(a fixed basis; multiply by your real call volume)." % unit)

    L += ["", "| Call-site | Downgrade | Verdict | Inputs safe | $ / %s |" % unit, "|---|---|---|---|---|"]
    for x in rows:
        d = x["downgrade"]
        amt = "$" + _fmt(x["downgrade_usd"]) if d["verdict"] == "SAFE" else "—"
        safe = "%s/%s" % (d.get("safe_inputs", 0), d.get("n", 0)) if d.get("inputs") else "—"
        L.append("| `%s` | %s → %s | %s | %s | %s |" % (
            x["node"], d.get("model", "?"), d.get("cheaper", "-"), _VERDICT.get(d["verdict"], d["verdict"]), safe, amt))

    L += ["", "## Evidence — every input, every re-run", ""]
    for x in rows:
        d = x["downgrade"]
        L.append("### `%s` — %s → %s · %s" % (x["node"], d.get("model", "?"), d.get("cheaper", "-"),
                                              _VERDICT.get(d["verdict"], d["verdict"])))
        if not d.get("inputs"):
            L += ["_%s_" % d.get("reason", ""), ""]
            continue
        for r in d["inputs"]:
            L.append("**[%s %d/%d]** input: %s" % (r["verdict"], r["kept"], r["k"], r.get("input", "")))
            L.append("- _original (%s, recorded):_ %s" % (d.get("model", "?"), r.get("recorded", "")))
            for i, s in enumerate(r.get("samples", []), 1):
                L.append("- _cheaper run %d (%s):_ %s — %s" % (
                    i, "kept" if s["preserved"] else "DRIFT", s.get("reason", ""), s.get("output", "")))
            L.append("")
    return "\n".join(L)
