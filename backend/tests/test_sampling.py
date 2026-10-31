"""_distinct sampling contract — diversity is keyed on the PER-CALL VARIABLE (what changes behaviour), not the whole
prompt. So genuine variety under a big shared static is recovered (not collapsed into a false SAFE), a fully-identical
bucket collapses to 1, and a small-static node is unchanged. $0, no network. Run: python -m tests.test_sampling."""
from app.services.audit import _distinct

BIG_STATIC = "\n".join("RULE %02d: a standing policy line identical on every single call to this node." % i
                       for i in range(80))   # an ~8k-token-class shared skeleton that dwarfs the variable


def _tr(system, user):
    return {"input_messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}


def test_recovers_diversity_under_a_huge_static():
    # 6 genuinely different tickets under one huge shared system. Whole-prompt Jaccard would read them as 1; keyed on
    # the variable, they are 6 distinct -> we actually stress-test the variety (no false SAFE from testing just one).
    tickets = ["refund a double charge on order 12", "cannot log in sso okta redirect loop",
               "package arrived cracked screen damaged", "downgrade my team plan to pro tier",
               "cancel subscription renewal i cancelled", "export all my data gdpr request please"]
    bucket = [_tr(BIG_STATIC, q) for q in tickets]
    assert len(_distinct(bucket, 5)) == 5, "genuine variety under a big static must be recovered, not collapsed"
    print("[ok] diversity under a huge shared static is recovered (not a false-SAFE collapse to 1)")


def test_short_variable_under_big_static_recovered():
    # THE simple-agent case: a classifier with a BIG rubric + genuinely one-word inputs. A 3-gram key would collapse
    # these to 1 (one word has no 3-grams -> falls back to the whole prompt -> a false SAFE). Word-set recovers them.
    bucket = [_tr(BIG_STATIC, w) for w in ["positive", "negative", "neutral", "mixed"]]
    assert len(_distinct(bucket, 5)) == 4, "one-word inputs under a big rubric must be distinct, not collapsed"
    print("[ok] big rubric + one-word inputs -> recovered (no false-SAFE collapse on simple agents)")


def test_long_and_mixed_variables():
    # long paragraph variables (different domains) stay distinct; a mixed short+long bucket is handled; a genuine
    # near-duplicate merges. Covers the "long / mixed dynamic content" range, not just short tickets.
    P = ["charged twice for order 4471 please refund the duplicate charge and confirm by email today",
         "the app crashes on android 14 when opening the analytics dashboard white screen then it closes",
         "requesting a csv export with custom columns the pdf report does not fit our finance workflow",
         "formal gdpr article 17 erasure request please confirm deletion in writing within thirty days"]
    assert len(_distinct([_tr(BIG_STATIC, p) for p in P], 5)) == 4, "different long paragraphs must stay distinct"
    mixed = [_tr(BIG_STATIC, "positive"), _tr(BIG_STATIC, "negative"), _tr(BIG_STATIC, P[0]), _tr(BIG_STATIC, P[1])]
    assert len(_distinct(mixed, 5)) == 4, "a mixed short+long bucket must sample all four"
    near = [_tr(BIG_STATIC, P[0]), _tr(BIG_STATIC, P[0] + " thanks")]
    assert len(_distinct(near, 5)) == 1, "a genuine near-duplicate merges"
    print("[ok] long paragraphs distinct; mixed short+long handled; near-dups merge")


def test_fully_identical_collapses_to_one():
    bucket = [_tr(BIG_STATIC, "the exact same request") for _ in range(5)]
    assert len(_distinct(bucket, 5)) == 1, "no per-call variety -> collapse to 1 (never manufacture diversity)"
    print("[ok] a fully-identical bucket collapses to 1")


def test_small_static_node_unchanged():
    # small skeleton, variable dominates anyway -> behaves the same as before the switch
    bucket = [_tr("route the ticket", q) for q in ["billing help", "login broken", "refund please", "cancel plan"]]
    assert len(_distinct(bucket, 5)) == 4
    print("[ok] small-static node samples its distinct inputs unchanged")


def test_single_input_returns_one():
    assert len(_distinct([_tr(BIG_STATIC, "only one")], 5)) == 1
    print("[ok] a single input returns exactly one")


if __name__ == "__main__":
    test_recovers_diversity_under_a_huge_static()
    test_short_variable_under_big_static_recovered()
    test_long_and_mixed_variables()
    test_fully_identical_collapses_to_one()
    test_small_static_node_unchanged()
    test_single_input_returns_one()
    print("\nALL SAMPLING-CONTRACT TESTS PASSED ($0, no network)")
