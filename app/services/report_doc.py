"""Report export — a downloadable ZIP (a folder of small files), so the audit record scales even for large
agents (a single .md would balloon to megabytes). Built ONLY from the frozen proof — measured, nothing invented.

Unpacks to:
  model-tier-downgrade/
    summary.md                              ranked verdicts + $ (skim first)
    nodes/<node>/verdicts.md                per-input rate + reason (the index for that node)
    nodes/<node>/input-<i>/input.txt        the request
    nodes/<node>/input-<i>/original.txt     the recorded original  ("before")
    nodes/<node>/input-<i>/run-<j>.kept.txt   each cheaper re-run   ("after")  — filename says kept/drift
    nodes/<node>/input-<i>/run-<j>.drift.txt

Every instance (N) and every re-run (K) is its own small file, so you can diff original.txt vs run-*.txt and see
run-to-run agreement. Under a `model-tier-downgrade/` root so other strategies can add sibling folders later.
stdlib zipfile only — zero dependencies.
"""
import io
import re
import zipfile

_ROOT = "model-tier-downgrade"
_VERDICT = {"SAFE": "safe to downgrade", "BORDERLINE": "borderline — flips, don't downgrade",
            "NOT-SAFE": "not safe — keep current", "LOW-EVIDENCE": "needs more data",
            "N/A": "already cheapest tier"}


def _fmt(n):
    return "{:,.0f}".format(n)


def _slug(s):
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", str(s)).strip("-")[:60] or "node"


def _summary_md(f, proof):
    calls = f["calls"]
    unit = "%s calls" % _fmt(calls)
    total = (proof["downgrade_usd"] if proof else f["downgrade_total"])
    rows = [x for x in (proof["results"] if proof else []) if x.get("downgrade")]
    L = ["# Token Audit — %s · model-tier downgrade" % f["agent"], ""]
    L.append("**$%s per %s** %s" % (_fmt(total), unit, "· proven live" if proof else "· detected, not yet proven"))
    L.append("")
    L.append("> Advisory, out-of-path, read-only. Each input is re-run K times on the cheaper model; a downgrade is "
             "SAFE only if every input preserved behavior in every re-run (so verdicts don't flip between audits). "
             "All $ are per %s. Open a node's folder for its per-input evidence (original.txt vs run-*.txt)." % unit)
    L += ["", "| Call-site | Downgrade | Verdict | Inputs safe | $ / %s |" % unit, "|---|---|---|---|---|"]
    for x in rows:
        d = x["downgrade"]
        amt = "$" + _fmt(x["downgrade_usd"]) if d["verdict"] == "SAFE" else "—"
        safe = "%s/%s" % (d.get("safe_inputs", 0), d.get("n", 0)) if d.get("inputs") else "—"
        L.append("| `%s` | %s → %s | %s | %s | %s |" % (
            x["node"], d.get("model", "?"), d.get("cheaper", "-"), _VERDICT.get(d["verdict"], d["verdict"]), safe, amt))
    return "\n".join(L) + "\n"


def _node_md(x, d):
    L = ["# %s — %s → %s · %s" % (x["node"], d.get("model", "?"), d.get("cheaper", "-"),
                                  _VERDICT.get(d["verdict"], d["verdict"])), ""]
    if not d.get("inputs"):
        return "\n".join(L + [d.get("reason", "")]) + "\n"
    L.append("Tested %d distinct inputs × %d re-runs each on %s.\n" % (d.get("n", 0), d.get("k", 0), d.get("cheaper", "-")))
    for i, r in enumerate(d["inputs"], 1):
        L.append("## input-%d — %s (%d/%d kept)" % (i, r["verdict"], r["kept"], r["k"]))
        L.append("- reason: %s" % r.get("reason", ""))
        L.append("- request: %s" % (r.get("input", "")[:200] + ("…" if len(r.get("input", "")) > 200 else "")))
        L.append("- files: `input-%d/original.txt` vs `input-%d/run-*.txt` (filenames marked kept/drift)" % (i, i))
        L.append("")
    return "\n".join(L) + "\n"


def build_zip(f, proof):
    """Return the audit record as ZIP bytes (folder of small files under model-tier-downgrade/)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("%s/summary.md" % _ROOT, _summary_md(f, proof))
        for x in (proof["results"] if proof else []):
            d = x.get("downgrade")
            if not d:
                continue
            node = _slug(x["node"])
            z.writestr("%s/nodes/%s/verdicts.md" % (_ROOT, node), _node_md(x, d))
            for i, r in enumerate(d.get("inputs", []), 1):
                base = "%s/nodes/%s/input-%d" % (_ROOT, node, i)
                z.writestr("%s/input.txt" % base, r.get("input", ""))
                z.writestr("%s/original.txt" % base, r.get("recorded", ""))
                for j, s in enumerate(r.get("samples", []), 1):
                    tag = "kept" if s.get("preserved") else "drift"
                    z.writestr("%s/run-%d.%s.txt" % (base, j, tag), s.get("output", ""))
    return buf.getvalue()
