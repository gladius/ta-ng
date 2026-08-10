"""Isolated probe: run the (improved) compression levers on meridian and print real verdicts + the ACTIONABLE output
(the replacement system prompt for the static case; the measured method saving for the RAG case). Paid proof pass."""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.services import graph, compress
from auditor.util import approx_tokens


def show(r):
    print("  verdict=%s  before=%s after=%s ratio=%s  save/1k=$%s  mode=%s"
          % (r.get("verdict"), r.get("before_tok"), r.get("after_tok"), r.get("ratio"),
             r.get("save_per_1k"), r.get("mode", "-")))
    if r.get("reason"):
        print("  reason:", r["reason"])
    for d in r.get("docs", []):
        print("   - DOC rewrite %d -> %d tok:  %s" % (d["before_tok"], d["after_tok"], d["rewritten"][:120].replace("\n", " ")))
    for i in r.get("inputs", []):
        print("   - input verdict=%s  %s" % (i.get("verdict"), (i.get("reason") or "")[:90]))


def main():
    g = graph.build("recorded", "recorded", "meridian-support")
    buckets = {n["node"]: g["buckets"][n["key"]] for n in g["nodes"]}

    print("### SYSTEM COMPRESSION — handler (static ~8.5k-token handbook)")
    r = compress.prove("handler", buckets["handler"])
    show(r)
    if r.get("compressed"):
        print("  --- ACTIONABLE: replacement system prompt (%d tokens), first 1600 chars ---" % approx_tokens(r["compressed"]))
        print(r["compressed"][:1600])

    print("\n### RAG CONTEXT COMPRESSION — kb_resolve (retrieved docs per call)")
    r = compress.prove_context("kb_resolve", buckets["kb_resolve"])
    show(r)


if __name__ == "__main__":
    main()
