"""Profile export — a downloadable ZIP mirroring report_doc: profile.html (the self-contained, styled deployment
profile — opens anywhere, prints to PDF from the browser) + profile.md (a compact, paste-able markdown summary built
only from the frozen snapshot profile, no LLM in the loop). stdlib zipfile only — zero dependencies."""
import io
import zipfile


def _usd(x):
    try:
        return "${:,.2f}".format(x)
    except (TypeError, ValueError):
        return "—"


def _int(x):
    try:
        return "{:,}".format(int(x or 0))
    except (TypeError, ValueError):
        return str(x)


def _profile_md(prof):
    h = (prof or {}).get("header", {}) or {}
    d = (prof or {}).get("deployment", {}) or {}
    basis_k = (h.get("cost_basis", 0) or 0) // 1000
    L = ["# Deployment profile — %s" % h.get("agent", "agent"), ""]
    if d.get("summary"):
        if d.get("kind"):
            L.append("**%s**" % d["kind"])
        L += [d["summary"], ""]

    L += ["## At a glance", ""]
    L.append("- Cost / %dk calls: **%s**" % (basis_k, _usd(h.get("total_cost_basis"))))
    if h.get("opportunity_total"):
        L.append("- Opportunity / %dk (before proof): **%s** across %d node(s)"
                 % (basis_k, _usd(h.get("opportunity_total")), h.get("opp_wins", 0)))
    L.append("- Call-sites (llm): **%s** · tool / retriever: **%s / %s**"
             % (h.get("call_sites", 0), h.get("tool_count", 0), h.get("retriever_count", 0)))
    L.append("- Traces sampled: **%s**" % _int(h.get("traces")))
    if h.get("revisions"):
        L.append("- Versions in window: %s" % ", ".join(str(x) for x in h["revisions"]))
    try:
        L.append("- Error rate: **{:.1%}**".format(h.get("error_rate", 0) or 0))
    except (TypeError, ValueError):
        pass
    L.append("")

    hs = (prof or {}).get("hotspots") or {}
    if hs:
        L += ["## Hotspots", ""]
        if hs.get("costliest"):
            L.append("- Costliest: **%s** (%s)" % (hs["costliest"].get("node"), _usd(hs["costliest"].get("per_basis"))))
        if hs.get("busiest"):
            L.append("- Busiest: **%s** (%s×)" % (hs["busiest"].get("node"), hs["busiest"].get("calls")))
        if hs.get("slowest"):
            L.append("- Slowest: **%s** (%sms)" % (hs["slowest"].get("node"), hs["slowest"].get("avg_ms")))
        L.append("")

    for sec in (prof or {}).get("sections", []):
        L += ["## %s%s" % (sec.get("name", "section"), " (subgraph)" if sec.get("graph_path") else ""), ""]
        for n in sec.get("nodes", []):
            L.append("### %s — %s" % (n.get("node", "node"), (n.get("op") or n.get("kind") or "")))
            if n.get("kind") == "llm":
                tag = [str(n.get("model", "?"))] + ([str(n["tier"])] if n.get("tier") else [])
                L.append("`%s`" % " · ".join(tag))
            if n.get("summary"):
                L += ["", n["summary"]]
            m = ["%s calls" % n.get("calls", 0)]
            if n.get("kind") == "llm":
                m.append("in %s → out %s" % (_int(n.get("avg_in")), _int(n.get("avg_out"))))
                per = (n.get("cost") or {}).get("per_basis")
                if per is not None:
                    m.append("%s/%dk" % (_usd(per), basis_k))
            elif n.get("avg_ms"):
                m.append("%sms avg" % n.get("avg_ms"))
            L += ["", "_%s_" % " · ".join(m)]
            opp, opps = n.get("opp") or {}, []
            if opp.get("downgrade_to"):
                opps.append("↓ downgrade → %s ~%s" % (opp["downgrade_to"], _usd(opp.get("downgrade_usd"))))
            if opp.get("cache_verdict") == "BREAKER":
                opps.append("⚡ cache reorg ~%s" % _usd(opp.get("cache_usd")))
            if opp.get("compress"):
                opps.append("− compress candidate")
            if opps:
                L += ["", "Opportunities (before proof): %s" % "; ".join(opps)]
            L.append("")

    L += ["---",
          "_Descriptive profile — no saving is asserted here; the audit levers prove those. Dollar figures per %dk "
          "calls, a fixed basis (not a monthly figure)._" % basis_k]
    return "\n".join(L) + "\n"


def build_zip(prof, html=None):
    """Return the profile as ZIP bytes: profile.html (self-contained, when the caller renders it) + profile.md."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if html:
            z.writestr("profile.html", html)
        z.writestr("profile.md", _profile_md(prof))
    return buf.getvalue()
