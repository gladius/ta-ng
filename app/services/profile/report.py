"""Render a NodeProfile as a readable per-node report (plain text for the console probe; a web view is P1)."""


def _line(k, v):
    return "  %-14s %s" % (k, v)


def render(prof):
    if prof.get("_parse_error"):
        return "NODE %s — PROFILE PARSE ERROR\n%s\n" % (prof.get("_node"), prof.get("_raw", ""))

    out = ["=" * 82,
           "NODE: %s   [confidence: %s]" % (prof.get("_node"), prof.get("confidence")),
           "-" * 82,
           _line("role", prof.get("role", ""))]

    sn = prof.get("system_nature", {}) or {}
    out.append(_line("system", "%s — %s" % (sn.get("kind"), sn.get("reason", ""))))
    out.append(_line("", "reconciled: %s" % sn.get("reconciled", "")))

    us = prof.get("user_structure", {}) or {}
    out.append(_line("user", "frame=%r slots=%s" % (us.get("frame", ""), ", ".join(us.get("slots", []) or []))))

    coh = prof.get("coherence", {}) or {}
    out.append(_line("coherence", "%s — %s" % (coh.get("verdict"), coh.get("reason", ""))))

    out.append("  components:")
    for c in prof.get("components", []) or []:
        ev = c.get("evidence") or {}
        out.append("    - %-15s %-7s %-8s %-9s  %-42s (%s)" % (
            c.get("type"), c.get("where"), c.get("variability"), c.get("compressibility"),
            ('"%s"' % (ev.get("quote") or "")[:40]), ev.get("trace_id", "")))

    out.append("  behavior:")
    for b in prof.get("behavior", []) or []:
        out.append("    - %s -> %s" % (b.get("input_type"), b.get("action")))

    out.append("  optimizability:")
    opt = prof.get("optimizability", {}) or {}
    for lever in ("cache", "downgrade", "compress_static", "compress_context"):
        d = opt.get(lever, {}) or {}
        extra = (" [%s]" % d.get("difficulty")) if lever == "downgrade" and d.get("difficulty") else ""
        out.append("    - %-16s %s%s — %s" % (lever, d.get("candidate"), extra, d.get("reason", "")))

    out.append(_line("coverage", prof.get("coverage", "")))
    if prof.get("notes"):
        out.append(_line("notes", prof.get("notes")))

    pis = prof.get("per_instance", []) or []
    if pis:
        out.append("  per-instance compress targets (what compression actually operates on):")
        for pi in pis:
            tgts = "; ".join("%s:%s (%s)" % (t.get("where"), t.get("what"), t.get("note", ""))
                             for t in (pi.get("compress_targets") or []))
            out.append("    - %s: %s" % (pi.get("trace_id"), tgts or "(none)"))

    out.append("")
    return "\n".join(out)
