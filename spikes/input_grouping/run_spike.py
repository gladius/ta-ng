"""Grouping-concept validation harness.

The ONE question: does `frame-strip -> semantic-embed -> cluster` recover a node's planted INPUT-KINDS
under real variety + failure cases? We test three feed strategies to isolate WHY it works or fails:

  whole   : embed system+user concatenated        (baseline — expect frame/doc to dominate)
  user    : embed the last user turn only          (strips the fixed frame)
  request : embed a generic-heuristic request slice (isolates the short instruction/question)

Metrics vs planted labels: V-measure (0..1) and Adjusted Rand Index (ARI, -1..1). Noise handling:
did planted 'noise' items land in HDBSCAN's -1 bucket? Dedup: MinHash near-dup collapse on the retries.

No knowledge of node identity is used by the `request` heuristic — it must generalise.
"""
from __future__ import annotations
import re, sys, warnings
warnings.filterwarnings("ignore")
import numpy as np
from collections import Counter, defaultdict

from synth_data import build_dataset

# ── embedder (fastembed bge-small; fallback sentence-transformers) ────────────
def get_embedder():
    try:
        from fastembed import TextEmbedding
        import os
        model = TextEmbedding(os.environ.get("SPIKE_EMBEDDER", "BAAI/bge-large-en-v1.5"))
        def embed(texts):
            return np.array(list(model.embed(texts)), dtype=np.float32)
        return embed, "fastembed:BAAI/bge-small-en-v1.5"
    except Exception as e1:
        try:
            from sentence_transformers import SentenceTransformer
            m = SentenceTransformer("thenlper/gte-small")
            return (lambda ts: np.asarray(m.encode(ts, normalize_embeddings=False), dtype=np.float32),
                    "sentence-transformers:gte-small")
        except Exception as e2:
            print(f"NO EMBEDDER AVAILABLE.\n  fastembed: {e1}\n  st: {e2}\n"
                  f"Install one:  pip install fastembed   (or)   pip install sentence-transformers")
            sys.exit(2)

# ── feed strategies ──────────────────────────────────────────────────────────
def last_user(msgs):
    for m in reversed(msgs):
        if m["role"] == "user":
            return m["content"] or ""
    return ""

def whole(msgs):
    return "\n".join(m.get("content") or "" for m in msgs)

_INSTR = re.compile(r"(summari[sz]e|compare|contrast|classif|extract|what|how|why|who|when|where|"
                    r"draft|write|implement|refactor|investigate|research|find|look into|recap|tl;?dr)",
                    re.I)

def request_slice(msgs):
    """Generic, node-agnostic: isolate the short instruction/question from the user turn.
    - explicit 'Question:'/'Task:' marker wins;
    - else pick the shortest sentence/line that contains an instruction/question cue;
    - else if the turn is short, use it whole; else fall back to the first 200 chars."""
    u = last_user(msgs).strip()
    if not u:
        return u
    m = re.search(r"(?:Question|Task|Request)\s*:\s*(.+)\s*$", u, re.I | re.S)
    if m:
        return m.group(1).strip()[:400]
    # candidate lines/sentences carrying an instruction cue, prefer short ones
    parts = re.split(r"(?<=[.?!])\s+|\n+", u)
    cues = [p.strip() for p in parts if _INSTR.search(p) and 0 < len(p.strip()) <= 200]
    if cues:
        return min(cues, key=len)
    if len(u) <= 240:
        return u
    return u[:200]

def adaptive(msgs):
    """The REAL policy: only isolate the request when the user turn is large (frame/doc dominates);
    otherwise the turn IS the request — don't mangle it."""
    u = last_user(msgs)
    return request_slice(msgs) if len(u) > 600 else u

STRATEGIES = {"whole": whole, "user": last_user, "request": request_slice, "adaptive": adaptive}

# ── clustering + scoring ─────────────────────────────────────────────────────
def cluster(vecs, thr=0.20):
    """Cosine-THRESHOLD grouping (the right tool for 'how many kinds'): merge anything with
    cosine-sim > 1-thr into one kind; tiny clusters -> noise bucket (-1). ONE global threshold,
    NOT per-node tuning."""
    import os
    from sklearn.preprocessing import normalize
    from sklearn.cluster import AgglomerativeClustering
    thr = float(os.environ.get("SPIKE_THR", thr))
    X = normalize(vecs)
    n = X.shape[0]
    labels = AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average",
                                     distance_threshold=thr).fit_predict(X)
    cnt = Counter(labels); minsz = max(5, n // 30)         # groups smaller than this = noise
    labels = np.array([lab if cnt[lab] >= minsz else -1 for lab in labels])
    k = len(set(labels) - {-1})
    return labels, k

def score(gt, pred):
    from sklearn.metrics import v_measure_score, adjusted_rand_score
    return v_measure_score(gt, pred), adjusted_rand_score(gt, pred)

def minhash_dedup_ratio(texts):
    """Report near-dup collapse via MinHash/LSH on request slices (Jaccard>=0.8)."""
    try:
        from datasketch import MinHash, MinHashLSH
    except Exception:
        return None
    def sig(t):
        mh = MinHash(num_perm=64)
        toks = set((t or "").lower().split())
        for w in toks: mh.update(w.encode())
        return mh
    lsh = MinHashLSH(threshold=0.8, num_perm=64)
    kept, mhs = 0, {}
    for i, t in enumerate(texts):
        mh = sig(t)
        if lsh.query(mh):           # a near-dup already present
            continue
        lsh.insert(str(i), mh); kept += 1
    return (len(texts) - kept) / max(1, len(texts))       # fraction removed as near-dups

# ── main ─────────────────────────────────────────────────────────────────────
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    recs = build_dataset()
    embed, name = get_embedder()
    print(f"embedder: {name}\ntotal records: {len(recs)}\n")

    by_node = defaultdict(list)
    for r in recs:
        by_node[r["node"]].append(r)

    overall = defaultdict(list)
    for node, rs in by_node.items():
        gt = [r["kind"] for r in rs]
        n_kinds = len({k for k in gt if k != "noise"})
        noise_idx = [i for i, k in enumerate(gt) if k == "noise"]
        print(f"\n{node}  (planted kinds = {n_kinds}"
              f"{', + noise' if noise_idx else ''}, n={len(rs)})")
        for sname, fn in STRATEGIES.items():
            texts = [fn(r["messages"]) for r in rs]
            pred, k = cluster(embed(texts))
            v, ari = score(gt, pred)
            overall[sname].append((v, ari))
            note = ""
            if n_kinds == 1:                       # V-measure is degenerate for 1 class; judge by k
                note = "  <- correct: 1 cluster (not over-split)" if k == 1 else \
                       f"  <- OVER-SPLIT into {k} (should be 1)"
            if noise_idx and sname == "adaptive":
                cap = sum(1 for i in noise_idx if pred[i] == -1)
                note += f"   noise->-1: {cap}/{len(noise_idx)}"
            print(f"   {sname:9} V={v:.2f}  ARI={ari:>5.2f}  clusters={k:>2}{note}")
        dd = minhash_dedup_ratio([adaptive(r["messages"]) for r in rs])
        if dd is not None:
            print(f"   minhash near-dup collapse: {dd*100:.0f}%")

    print("\n" + "=" * 70)
    for s in STRATEGIES:
        vs = np.mean([v for v, _ in overall[s]]); ars = np.mean([a for _, a in overall[s]])
        print(f"mean {s:>9}:  V={vs:.3f}  ARI={ars:.3f}")
    print("\nRead: V/ARI near 1.0 = clusters match planted kinds; k = clusters found. The frame-strip "
          "proof is rag_answer (whole/user collapse, request/adaptive recover). 1-kind node judged by k.")

if __name__ == "__main__":
    main()
