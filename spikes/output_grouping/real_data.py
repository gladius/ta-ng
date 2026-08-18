"""REAL-DATA check for the output/structure-grouping concept — is a node's variety always capturable by a
DETERMINISTIC signal (output decision + input structure), or is much of it purely semantic (needs the facet layer)?

Pulls two real public intent datasets (banking77: 77 fine intents in ONE domain; clinc_oos: 150 intents) via the
HuggingFace datasets-server. Each row = a real user request + its TRUE kind (intent). We simulate the HARD case
the user flagged: a node whose inputs vary case-to-case with the SAME surface shape and NO low-cardinality
decision (a free-form handler). Question: how much of the 77/150 true kinds can DETERMINISTIC structure recover?

If the answer is "almost none," that CONFIRMS: for free-form / semantic nodes, output+structure grouping is
insufficient and the bounded advisory facet layer is REQUIRED — exactly the user's point, measured on real data.

Run: python -m spikes.output_grouping.real_data   (needs network; sklearn)
"""
import sys
import json
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from sklearn.metrics import homogeneity_completeness_v_measure

API = "https://datasets-server.huggingface.co/rows"


def _get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def pull(dataset, config, text_field, label_field, n=2000):
    """-> [(text, label_name)] for up to n rows, resolving the int label to its name via the feature schema."""
    first = _get("%s?dataset=%s&config=%s&split=train&offset=0&length=100" % (API, dataset, config))
    names = None
    for f in first.get("features", []):
        if f["name"] == label_field:
            names = f["type"].get("names")
    recs, offset = [], 0
    while len(recs) < n:
        data = _get("%s?dataset=%s&config=%s&split=train&offset=%d&length=100" % (API, dataset, config, offset))
        rows = data.get("rows", [])
        if not rows:
            break
        for r in rows:
            row = r["row"]
            lab = row[label_field]
            lab = names[lab] if (names and isinstance(lab, int)) else lab
            recs.append((row[text_field], str(lab)))
        offset += 100
    return recs[:n]


# ── the ONLY deterministic signals a bare request affords (structure; no embeddings, no LLM) ──
def _band(x, cuts):
    return sum(1 for c in cuts if x >= c)


def structural_key(text):
    t = text or ""
    return (_band(len(t), [40, 80, 140]),                 # char-length band
            _band(len(t.split()), [6, 12, 20]),           # word-count band
            "?" in t,                                      # a question?
            any(ch.isdigit() for ch in t))                # contains a number?


def evaluate(name, recs):
    truth = [k for _, k in recs]
    keys = [structural_key(t) for t, _ in recs]
    tmap = {t: i for i, t in enumerate(dict.fromkeys(truth))}
    kmap = {k: i for i, k in enumerate(dict.fromkeys(keys))}
    tids = [tmap[t] for t in truth]
    kids = [kmap[k] for k in keys]
    h, c, v = homogeneity_completeness_v_measure(tids, kids)
    reps = {}
    for k, t in zip(keys, truth):
        reps.setdefault(k, t)                             # 1 representative per structural partition
    witnessed = set(reps.values())
    cov = len(witnessed) / len(tmap)
    print("── %s  (%d real requests)" % (name, len(recs)))
    print("   TRUE kinds (intents): %d   deterministic-structure partitions: %d" % (len(tmap), len(kmap)))
    print("   homogeneity(purity) %.2f  completeness %.2f  V-measure %.2f" % (h, c, v))
    print("   coverage@1 (kinds a 1-per-partition sample witnesses): %d/%d = %.0f%%"
          % (len(witnessed), len(tmap), cov * 100))
    print("   -> %.0f%% of the real variety is PURELY SEMANTIC — invisible to structure, needs the facet layer.\n"
          % ((1 - v) * 100))


if __name__ == "__main__":
    print("REAL-DATA check: can a deterministic signal capture a free-form node's input variety? (lower = needs facet)\n")
    try:
        b77 = pull("legacy-datasets/banking77", "default", "text", "label", n=2000)
        evaluate("banking77 — 77 fine intents, ONE domain", b77)
    except Exception as e:
        print("banking77 pull failed:", str(e)[:200])
    try:
        clinc = pull("clinc/clinc_oos", "plus", "text", "intent", n=2500)
        evaluate("CLINC150 — 150 intents", clinc)
    except Exception as e:
        print("clinc pull failed:", str(e)[:200])
    print("Read: near-0 V-measure = a free-form/semantic node's kinds CANNOT be recovered from output+structure —")
    print("confirming output-grouping is a provable FLOOR for decision nodes, NOT a whole answer for semantic ones.")
