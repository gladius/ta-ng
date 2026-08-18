"""$0 harness proof for the analyst AGENT (app/services/profile/agent.py). The LLM is a FAKE `run` returning
scripted JSON actions, so these prove the LOOP is correct — it executes the REAL deterministic tools, records
the trail, respects the budget, force-finalizes, and recovers from non-JSON — with NO model call and no cost.
The semantic tail (does a real model actually group the two jobs) is a separate LIVE spike, not a unit test.
"""
import json

from app.services.profile import agent as A
from app.services.profile import tools as T
from app.services.profile.facts import llm_facts


def _tr(tid, system, user, output, tools=None):
    return {"trace_id": tid,
            "input_messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "output": output, "tools_called": tools or []}


def _node(key="a/n", node="n", model="claude-sonnet-5"):
    return {"key": key, "node": node, "graph_path": "", "model": model, "calls": 0, "out_type": "prose"}


def _scripted(*replies):
    """A fake llm_client.run that returns each scripted reply in turn (last one repeats)."""
    state = {"i": 0, "seen": []}

    def run(**kw):
        i = state["i"]
        state["i"] += 1
        state["seen"].append(kw)
        r = replies[min(i, len(replies) - 1)]
        return {"text": r if isinstance(r, str) else json.dumps(r), "tool_calls": []}

    run.state = state
    return run


def test_easy_node_needs_no_inspection():
    bucket = [_tr("t%d" % i, "Classify the ticket.", "ticket %d" % i, "refund") for i in range(6)]
    f = llm_facts(_node(), bucket, [])
    run = _scripted({"final": {"op": "classifier", "summary": "routes tickets",
                               "coherence": {"verdict": "coherent"},
                               "evidence": {"trace_id": "t0", "quote": "refund"}}})
    out = A.characterize_node(_node(), bucket, f, _run=run)
    assert out["op"] == "classifier"
    assert out["inspections"] == 0 and out["escalated"] is False
    assert run.state["i"] == 1                                # ONE turn — no tool round-trips


def test_inspection_runs_the_real_tool_and_records_trail():
    b = [_tr("L%d" % i, "Do the task.", "clause %d" % i, "the clause sets termination terms and a cure period")
         for i in range(5)]
    b += [_tr("M%d" % i, "Do the task.", "tagline %d" % i, "wake up to bold mornings brewed fresh for you")
          for i in range(5)]
    f = llm_facts(_node(), b, [])
    run = _scripted({"inspect": {"tool": "group", "args": {"by": "output"}}},
                    {"final": {"op": "generator", "coherence": {"verdict": "possible_mixed_bucket"},
                               "evidence": {"trace_id": "L0", "quote": "the clause sets termination terms"}}})
    out = A.characterize_node(_node(), b, f, _run=run)
    assert out["inspections"] == 1 and out["escalated"] is True
    assert any(t.startswith("group(output)") for t in out["trail"])
    assert out["coherence"]["verdict"] == "possible_mixed_bucket"


def test_budget_bounds_inspections_and_forces_final():
    bucket = [_tr("t%d" % i, "sys", "u %d" % i, "out %d" % i) for i in range(6)]
    f = llm_facts(_node(), bucket, [])
    run = _scripted({"inspect": {"tool": "sample", "args": {"strategy": "more"}}})   # never volunteers a final
    out = A.characterize_node(_node(), bucket, f, _run=run, max_steps=3)
    assert out["inspections"] <= 3                            # bounded — never runs away
    assert run.state["i"] == 4                                # max_steps + 1 turns (last is forced)


def test_parse_error_recovers_then_finalizes():
    bucket = [_tr("t%d" % i, "sys", "u %d" % i, "out") for i in range(6)]
    f = llm_facts(_node(), bucket, [])
    run = _scripted("not json at all",
                    {"final": {"op": "responder", "coherence": {"verdict": "coherent"},
                               "evidence": {"trace_id": "t0", "quote": "out"}}})
    out = A.characterize_node(_node(), bucket, f, _run=run)
    assert "parse-retry" in out["trail"]
    assert out["op"] == "responder"


def test_group_tool_separates_two_same_shape_jobs():
    """Pure deterministic tool check (no agent, no LLM): the tool the agent reaches for actually splits the
    two same-shape jobs — the hard case distinct_shape can't see."""
    b = [_tr("L%d" % i, "s", "u", "the clause sets termination terms and a cure period") for i in range(50)]
    b += [_tr("M%d" % i, "s", "u", "wake up to bold mornings brewed fresh for you") for i in range(50)]
    g = T.group(b, by="output")
    assert g["distinct_groups"] == 2
    assert sorted(x["count"] for x in g["groups"]) == [50, 50]
