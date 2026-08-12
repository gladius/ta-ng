"""Report export — a downloadable ZIP whose centerpiece is ONE self-contained `report.md`: an APPLY GUIDE across all
three levers (model-tier downgrade, cacheable-prefix expansion, prompt compression). Built ONLY from the frozen proof
— measured, nothing invented, no LLM in the loop, so the deliverable is reproducible.

The ZIP unpacks to:
  report.md                                     the apply guide: one block per proven fix (what / where / how / why /
                                                verify / caveat, artifacts INLINE) + a machine-readable index + the
                                                honest rejections. A human or a coding assistant can act on it alone.
  evidence/<lever>/<node>/verdict.md            WHAT THE JUDGE FOUND, co-located with the raw text: per-input
                                                verdict, rate (kept X/K · baseline Y/K), and reason.
  evidence/<lever>/<node>/input-<i>/            the raw outputs to diff:
    request.txt · before-original.txt · after-run-<j>.<kept|drift>.txt · baseline-<j>.<kept|drift>.txt
  evidence/cache/<node>/reorged-prefix.txt      the reorged cacheable prefix (the cache "what to do")

<lever> is `downgrade` or `cache` — BOTH are exported. The bulky per-run text stays OUT of report.md (a big agent
would balloon it) and lives under evidence/ instead. stdlib zipfile only — zero dependencies.
"""
import io
import json
import re
import zipfile

_VERDICT = {"SAFE": "safe to downgrade", "BORDERLINE": "borderline — flips, don't downgrade",
            "NOT-SAFE": "not safe — keep current", "LOW-EVIDENCE": "needs more data",
            "N/A": "already cheapest tier"}


def _fmt(n):
    return "{:,.0f}".format(n or 0)


def _slug(s):
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", str(s)).strip("-")[:60] or "node"


def _sys_first_line(detail):
    """A greppable fingerprint of the call-site's system prompt — its first non-blank line — so a human or coding
    assistant can locate the exact prompt in their source. From the compress lever's `original`, else the proof's
    captured `system_before`."""
    sysm = detail.get("original") or ""
    if not sysm:
        inps = detail.get("inputs") or []
        sysm = (inps[0].get("system_before") if inps else "") or ""
    return next((ln.strip() for ln in sysm.splitlines() if ln.strip()), "")[:140]


def _drift(detail):
    """Why a lever was rejected: the first NOT-SAFE input's judge reason, else the finding's own reason."""
    for r in detail.get("inputs") or []:
        if r.get("verdict") == "NOT-SAFE":
            return r.get("note") or r.get("reason") or ""
    return detail.get("reason") or ""


_LEVER_LABEL = {"downgrade": "Model downgrade", "cache": "Cache-prefix expansion", "compress": "Prompt compression"}


def _report_md(f, proof):
    """The single deliverable: an APPLY GUIDE across all three levers, built ONLY from the frozen proof. One block per
    recommended fix (what / where / how / why / verify / caveat, artifacts INLINE and paste-ready), a machine-readable
    index for a coding assistant, and an honest list of what was tested and rejected. Bulky per-run text still lives
    under evidence/; nothing here is invented — every figure is measured."""
    calls = f["calls"]
    unit = "%s calls" % _fmt(calls)
    results = proof["results"] if proof else []
    total = (proof.get("total", 0) if proof else 0)
    dg_total = (proof.get("downgrade_usd", 0) if proof else 0)
    ca_total = (proof.get("cache_usd", 0) if proof else 0)
    co_total = (proof.get("compress_usd", 0) if proof else 0)

    # ONE pass over the proof: split every lever result into a RECOMMENDED fix or an honest REJECTION.
    fixes, rejects = [], []                     # fixes: (lever, x, detail, usd)   rejects: (call_site, lever, verdict, reason)
    for x in results:
        key = x.get("key") or x["node"]
        d = x.get("downgrade")
        if d and d.get("verdict") == "SAFE":
            fixes.append(("downgrade", x, d, x.get("downgrade_usd", 0)))
        elif d:
            rejects.append((key, "model downgrade", d.get("verdict", "?"), _drift(d)))
        c = x.get("cache")
        if c and not c.get("informational"):
            if c.get("recommend"):
                fixes.append(("cache", x, c, x.get("cache_usd", 0)))
            else:
                rejects.append((key, "cache-prefix", c.get("verdict", "?"), _drift(c)))
        cp = x.get("compress")
        if cp and cp.get("verdict") == "SAFE":
            fixes.append(("compress", x, cp, x.get("compress_usd", 0)))
        elif cp:
            rejects.append((key, "compression", cp.get("verdict", "?"), _drift(cp)))

    L = ["# Token Audit — %s · Apply Guide" % f["agent"], ""]
    L.append("> These are **verified, behaviour-preserving** changes to your agent, each proven on real recorded "
             "inputs. Apply each at the named call-site **exactly as written** — the prompts and prefixes below were "
             "proven verbatim, so editing a proven artifact voids its proof. Advisory & read-only: you decide what to "
             "apply.")
    L.append("")
    if total > 0:
        L.append("**$%s in verified savings per %s** — ↓ $%s model downgrade · ⚡ $%s cache-prefix · − $%s compression."
                 % (_fmt(total), unit, _fmt(dg_total), _fmt(ca_total), _fmt(co_total)))
    else:
        L.append("**No safe savings on the audited call-sites** — each re-ran live and needs its current setup. A "
                 "verified result, not an empty one; see *Tested and not recommended* below.")
    L.append("")
    L.append("Audited **%d call-site(s)** across **%s traces**; **%d fix(es) to apply**. All $ per %s."
             % (len(results), f.get("traces", 0), len(fixes), unit))
    L.append("")

    # ── machine-readable index — a coding assistant can parse THIS instead of the prose ──
    idx = []
    for n, (lever, x, det, usd) in enumerate(fixes, 1):
        e = {"fix": n, "lever": lever, "call_site": (x.get("key") or x["node"]), "node": x["node"],
             "graph_path": x.get("graph_path", ""), "saving": round(usd, 2), "basis": unit}
        if lever == "downgrade":
            e["from_model"], e["to_model"] = det.get("model"), det.get("cheaper")
        elif lever == "compress":
            e["from_tokens"], e["to_tokens"] = det.get("before_tok"), det.get("after_tok")
        elif lever == "cache":
            e["cached_tokens_before"] = (det.get("before") or {}).get("read", 0)
            e["cached_tokens_after"] = (det.get("after") or {}).get("read", 0)
        idx.append(e)
    L += ["## Fixes — machine-readable index", "```json", json.dumps(idx, indent=2), "```", ""]

    # ── one APPLY BLOCK per recommended fix — what / where / how / why / verify / caveat, artifacts inline ──
    if fixes:
        L += ["## Fixes to apply", ""]
    for n, (lever, x, det, usd) in enumerate(fixes, 1):
        key = x.get("key") or x["node"]
        L.append("### Fix %d · %s · `%s` — saves $%s per %s" % (n, _LEVER_LABEL[lever], key, _fmt(usd), unit))
        L.append("")
        L.append("**Where**")
        L.append("- Call-site: node `%s`%s, agent `%s`, currently on model `%s`." % (
            x["node"], (" (subgraph `%s`)" % x["graph_path"]) if x.get("graph_path") else "", f["agent"], x["model"]))
        fp = _sys_first_line(det)
        if fp:
            L += ["- Find it by the node name, or by its system prompt which begins:", "  > %s" % fp]
        L += ["", "**Change**"]
        if lever == "downgrade":
            L += ["- Switch this call-site's model:  `%s`  →  `%s`" % (det.get("model"), det.get("cheaper")),
                  "- Nothing else changes — same prompt, same tools."]
        elif lever == "compress":
            L.append("- Replace this call-site's **system prompt** with the version below (paste verbatim). Tokens: "
                     "**%s → %s** (%s). Keep every per-call value exactly as-is."
                     % (det.get("before_tok"), det.get("after_tok"), det.get("mode", "-")))
            L += ["", "```text", det.get("compressed", ""), "```"]
        elif lever == "cache":
            b = (det.get("before") or {}).get("read", 0)
            a = (det.get("after") or {}).get("read", 0)
            L += ["- Make this exact block a **contiguous cached system prefix**: hoist it to the front, set the "
                  "provider's cache breakpoint at its end, and keep every per-call value **after** it, verbatim.",
                  "- Cached tokens per call: **%s → %s** (recovered %s)." % (b, a, det.get("recovered_tok", 0)),
                  "", "```text", det.get("prefix", ""), "```"]
        ni, si, k = det.get("n", 0), det.get("safe_inputs", 0), det.get("k", 0)
        L += ["", "**Why it's safe**"]
        if lever == "downgrade":
            L.append("- Re-ran %d distinct real inputs × %d each on `%s`, judged against the original model's own "
                     "behaviour envelope (the commitments it holds constant vs the variation it naturally allows); "
                     "held on **%d/%d**. The cheaper model gets exactly the latitude the original itself takes, so it "
                     "is never blamed for normal variance." % (ni, k, det.get("cheaper"), si, ni))
        elif lever == "compress":
            L.append("- The compressor kept every rule, number, code and tool name verbatim; behaviour then held on "
                     "**%d/%d** distinct real inputs × %d re-runs, judged against your recorded outputs." % (si, ni, k))
        elif lever == "cache":
            L.append("- Behaviour held on **%d/%d** distinct real inputs × %d re-runs (reordered prompt vs recorded "
                     "output), **and** a live provider round-trip confirmed the new prefix actually cached." % (si, ni, k))
        L.append("- Saves **$%s per %s**." % (_fmt(usd), unit))
        L += ["", "**Verify & caveats**",
              "- After applying, re-audit this call-site (or re-run these inputs and diff behaviour). Full evidence: "
              "`evidence/%s/%s/`." % (lever, _slug(key))]
        if lever == "compress":
            L.append("- Do **not** further shorten this text — it was proven exactly as written; more cuts void the proof.")
        if lever == "cache":
            L.append("- Savings assume calls arrive within the provider's cache window; a call-site called less often "
                     "than that window won't benefit.")
        L += ["", "---", ""]

    # ── composition: a call-site with more than one fix ──
    seen, multi = set(), []
    for _, x, _, _ in fixes:
        k = x.get("key") or x["node"]
        (multi.append(k) if k in seen else seen.add(k))
    if multi:
        L += ["## Call-sites with more than one fix",
              "Independent — apply both. (Downgrade changes the model; compression/cache change the prompt.) Order "
              "doesn't matter."] + ["- `%s`" % k for k in dict.fromkeys(multi)] + [""]

    # ── the honest 'no' — tested and rejected, so nothing is silently touched ──
    if rejects:
        L += ["## Tested and not recommended (keep current)",
              "We tried these and the proof said no — listed so you know what was tested and *not* to touch:"]
        for key, lever, verdict, reason in rejects:
            L.append("- `%s` · %s → **%s**%s" % (key, lever, verdict, (" — %s" % reason) if reason else ""))
        L.append("")

    L.append("---")
    L.append("_Figures per %s — a fixed basis, not a monthly total; multiply by your real call volume to scale. Built "
             "only from the frozen proof: measured live against your recorded traffic, nothing invented._" % unit)
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
        if inp.get("commitments"):        # profiled downgrade: the learned contract the cheaper was judged against
            L.append("- commitments (must hold): %s" % (inp["commitments"].replace("\n", " ")[:400]))
            L.append("- allowed variation: %s" % ((inp.get("allowed_variation") or "none").replace("\n", " ")[:300]))
            if inp.get("confidence"):
                L.append("- profile confidence: %s" % (inp["confidence"].splitlines()[0][:120]))
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
