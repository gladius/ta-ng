"""core.identity groups prompts without node ids: templates with per-call context stay one call-site, versions stay
with their call-site, a different agent's prompt separates, and anchors tell them apart."""
import random

from core.identity import skeleton

RULES = "\n".join("Rule %d: when the customer asks about topic %s, answer with the policy text for that topic." % (i, "abcdefghij"[i % 10])
                  for i in range(40))
OTHER = "\n".join("Specialist step %d: check the document named %s against the checklist and report a verdict." % (i, "xyz"[i % 3])
                  for i in range(30))


def _context(i):
    return "## Customer\nAccount: NT-%d\nName: %s\nPlan: %s\n" % (i, random.choice(["Ana Lee", "Bo Chen"]), random.choice(["Basic", "Plus"]))


def test_context_and_versions_stay_one_site_and_another_agent_separates():
    random.seed(1)
    prompts = []
    for i in range(60):
        rev = "\nRevision note: handle a refund as described above." if i >= 30 else ""
        prompts.append((_context(i) + RULES + rev, ["get_account", "change_plan"][: 1 + i % 2]))
    prompts += [("Request %d\n" % i + OTHER, []) for i in range(20)]
    stamps = ["2026-10-%02d" % (1 + i // 10) for i in range(60)] + ["2026-10-01"] * 20
    out = skeleton.describe("uc", prompts, stamps)
    assert [d["n"] for d in out] == [60, 20]
    assert not out[0]["unsure"] and not out[1]["unsure"]
    assert len(out[0]["versions"]) == 2 and len(out[1]["versions"]) == 1
    assert all(a in skeleton.mask(RULES) for a in out[0]["anchors"]) and all(a in skeleton.mask(OTHER) or a == "Request #" for a in out[1]["anchors"])
    assert out[0]["site"] != out[1]["site"]


def test_a_short_prompt_with_a_revision_line_is_not_split():
    base = "Label the message with one of: billing, outage, plan, device, other. Reply with the label only."
    prompts = [(base + ("\nRevision: treat roaming as plan." if i % 2 else ""), []) for i in range(20)]
    assert [d["n"] for d in skeleton.describe("uc", prompts)] == [20]


def test_lines_mask_numbers_and_drop_short_lines():
    assert skeleton.lines("Order 123 shipped\nok\nTotal: 45 items", ["t"]) == {"Order # shipped", "Total: # items", "TOOL:t"}
