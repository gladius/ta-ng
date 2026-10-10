"""A tree-structured LangGraph sample: a multi-subgraph agent run tree for the whole-deployment graph.

Folded offline through the real langsmith adapter and build_graph, it plants the failure cases that
tests/test_tree_deployment.py asserts (each trace below says which). Structural test and demo data, never a source of
cost claims. Runs are raw LangSmith Run dicts; order comes from dotted_order + start_time, the tree from parent_run_id.
"""

SONNET = "claude-sonnet-4-5-20250101"
MINI = "gpt-4o-mini"
UNKNOWN = "some-new-model-9000"
AGENT = "support-agent"

_TOOLS = [
    {"type": "function", "function": {"name": "lookup_account",
        "description": "Look up a customer account by id",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "issue_refund",
        "description": "Issue a refund to an account",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}, "amount": {"type": "number"}}}}},
]


class _Clock:
    """Monotonic tick -> (dotted_order, start_time, end_time); non-llm node latency needs a positive wall-clock."""
    def __init__(self):
        self.n = 0

    def stamp(self, dur_ms=800):
        self.n += 1                                       # each run starts one second after the last (order)
        def iso(us):
            sec, micro = divmod(us, 1_000_000)
            return "2026-08-01T00:%02d:%02d.%06dZ" % (sec // 60, sec % 60, micro)
        start_us = self.n * 1_000_000
        return "%020d" % self.n, iso(start_us), iso(start_us + dur_ms * 1000)


def _ai_message(content, tool_calls=None):
    kw = {"content": content}
    if tool_calls:
        kw["tool_calls"] = [{"name": n, "args": a} for n, a in tool_calls]
    return {"generations": [[{"text": content if not tool_calls else "",
                              "message": {"lc": 1, "type": "constructor",
                                          "id": ["langchain", "schema", "messages", "AIMessage"], "kwargs": kw}}]]}


def _run(clk, rid, tid, rt, name, parent, *, node=None, ns=None, model=None, tools=None,
         sys=None, user=None, tool_msg=None, out=None, tool_calls=None, error="", rev=None,
         pt=None, ct=None, fb=None, ttft=False, task=None, dur_ms=800):
    """One raw LangSmith Run dict; only llm runs carry messages and usage. `node`/`ns` become langgraph_node /
    langgraph_checkpoint_ns metadata."""
    do, start, end = clk.stamp(dur_ms)
    meta = {"agent_id": AGENT}
    if node is not None:
        meta["langgraph_node"] = node
    if ns:
        meta["langgraph_checkpoint_ns"] = ns
    if rev:
        meta["revision_id"] = rev
    if task:
        meta["task_type"] = task
    inv = {}
    if model:
        inv["model"] = model
    if tools:
        inv["tools"] = tools
    rec = {"id": rid, "trace_id": tid, "run_type": rt, "name": name,
           "parent_run_id": parent, "dotted_order": do, "start_time": start, "end_time": end,
           "error": error or "", "extra": {"metadata": meta, "invocation_params": inv}}
    if rt in (None, "llm"):
        msgs = []
        if sys is not None:
            msgs.append({"role": "system", "content": sys})
        if user is not None:
            msgs.append({"role": "user", "content": user})
        if tool_msg is not None:
            msgs.append({"role": "tool", "content": tool_msg})
        rec["inputs"] = {"messages": msgs}
        rec["outputs"] = _ai_message(out or "", tool_calls)
        rec["prompt_tokens"] = pt if pt is not None else 1000
        rec["completion_tokens"] = ct if ct is not None else 40
        if ttft:
            rec["first_token_time"] = start[:-1] + "500Z"      # +500ms first token (streaming)
        if fb is not None:
            rec["feedback_stats"] = {"correctness": {"avg": fb, "n": 3}}
    else:
        rec["inputs"] = {"id": "123"}
        rec["outputs"] = {} if error else {"result": "ok"}
    return rec


def runs():
    """The whole corpus as a flat list of raw run dicts (one connected tree per trace_id)."""
    clk = _Clock()
    R = []

    # ── T1: billing path, supervisor routes, worker does a ReAct tool loop (one node, two call-sites) ──
    R += [
        _run(clk, "t1-root", "t1", "chain", "LangGraph", None),                      # generic root -> filtered
        _run(clk, "t1-sup", "t1", "chain", "supervisor", "t1-root", node="supervisor"),
        _run(clk, "t1-sup-l", "t1", "llm", "ChatAnthropic", "t1-sup", node="supervisor",
             model=SONNET, rev="sha1", task="routing",
             sys="You are a supervisor. Route the ticket to a subgraph.",
             user="My card was charged twice for order 123.", out="route: billing"),
        _run(clk, "t1-wk", "t1", "chain", "worker", "t1-root", node="worker", ns="billing:uuidB"),
        _run(clk, "t1-wk-l1", "t1", "llm", "ChatAnthropic", "t1-wk", node="worker", ns="billing:uuidB",
             model=SONNET, tools=_TOOLS, fb=0.95, ttft=True,
             sys="You are the billing worker. Resolve the dispute; call tools when needed.",
             user="Ticket: double charge on order 123.", out="Looking up the account.",
             tool_calls=[("lookup_account", {"id": "123"})], pt=3200, ct=90),
        _run(clk, "t1-wk-tool", "t1", "tool", "lookup_account", "t1-wk", ns="billing:uuidB", dur_ms=120),
        _run(clk, "t1-wk-l2", "t1", "llm", "ChatAnthropic", "t1-wk", node="worker", ns="billing:uuidB",
             model=SONNET, tools=_TOOLS,
             sys="You are the billing worker. Resolve the dispute; call tools when needed.",
             user="Ticket: double charge on order 123.",
             tool_msg="account 123: two charges of $20 on 2026-08-01",
             out="I refunded the duplicate $20 charge on order 123.", pt=3400, ct=70),
    ]

    # ── T2: refund path, same node name 'worker' in another subgraph; the issue_refund tool errors ──
    R += [
        _run(clk, "t2-root", "t2", "chain", "LangGraph", None),
        _run(clk, "t2-sup", "t2", "chain", "supervisor", "t2-root", node="supervisor"),
        _run(clk, "t2-sup-l", "t2", "llm", "ChatAnthropic", "t2-sup", node="supervisor",
             model=SONNET, rev="sha1", task="routing",
             sys="You are a supervisor. Route the ticket to a subgraph.",
             user="Please refund my subscription.", out="route: refund"),
        _run(clk, "t2-wk", "t2", "chain", "worker", "t2-root", node="worker", ns="refund:uuidR"),
        _run(clk, "t2-wk-l1", "t2", "llm", "ChatAnthropic", "t2-wk", node="worker", ns="refund:uuidR",
             model=SONNET, tools=_TOOLS,
             sys="You are the refund worker. Issue refunds; call tools when needed.",
             user="Refund the subscription on account 456.", out="Issuing the refund.",
             tool_calls=[("issue_refund", {"id": "456", "amount": 12})], pt=2600, ct=60),
        _run(clk, "t2-wk-tool", "t2", "tool", "issue_refund", "t2-wk", ns="refund:uuidR",
             error="RefundGatewayTimeout: upstream 504", dur_ms=3000),
        _run(clk, "t2-wk-l2", "t2", "llm", "ChatAnthropic", "t2-wk", node="worker", ns="refund:uuidR",
             model=SONNET, error="transient: retried", tools=_TOOLS,
             sys="You are the refund worker. Issue refunds; call tools when needed.",
             user="Refund the subscription on account 456.",
             tool_msg="issue_refund failed: gateway timeout",
             out="The refund gateway timed out; I have queued a manual refund for account 456.",
             pt=2800, ct=80),   # error + real output -> recovered, kept
    ]

    # ── T3: research subgraph, a retriever node, an unknown-model summarize, an untagged deep-agent llm ──
    R += [
        _run(clk, "t3-root", "t3", "chain", "LangGraph", None),
        _run(clk, "t3-res", "t3", "chain", "research", "t3-root", node="research"),
        _run(clk, "t3-res-l", "t3", "llm", "ChatAnthropic", "t3-res", node="research", model=SONNET,
             sys="You are the research node. Decide what to look up.",
             user="What is the refund policy for subscriptions?", out="Searching the knowledge base."),
        _run(clk, "t3-ret", "t3", "retriever", "kb_search", "t3-res", dur_ms=200),
        _run(clk, "t3-sum", "t3", "chain", "summarize", "t3-root", node="summarize"),
        _run(clk, "t3-sum-l", "t3", "llm", "ChatAnthropic", "t3-sum", node="summarize", model=UNKNOWN,
             sys="Summarize the retrieved policy for the agent.",
             user="Summarize: subscriptions are refundable within 30 days.",
             out="Subscriptions are refundable within 30 days of renewal."),
        # untagged: no langgraph_node and a generic parent, so the node falls back to run.name
        _run(clk, "t3-seq", "t3", "chain", "RunnableSequence", "t3-root"),           # generic -> filtered
        _run(clk, "t3-untag-l", "t3", "llm", "ChatOpenAI", "t3-seq", model=MINI,
             sys="Draft the final reply.", user="Draft a reply about the refund policy.",
             out="Here is the policy summary you asked for."),
    ]

    # ── T4: a failed llm at billing/worker, errored with empty output -> excluded but counted ──
    R += [
        _run(clk, "t4-root", "t4", "chain", "LangGraph", None),
        _run(clk, "t4-sup", "t4", "chain", "supervisor", "t4-root", node="supervisor"),
        _run(clk, "t4-sup-l", "t4", "llm", "ChatAnthropic", "t4-sup", node="supervisor", model=SONNET,
             sys="You are a supervisor.", user="charge dispute order 999", out="route: billing"),
        _run(clk, "t4-wk", "t4", "chain", "worker", "t4-root", node="worker", ns="billing:uuidB"),
        _run(clk, "t4-wk-l", "t4", "llm", "ChatAnthropic", "t4-wk", node="worker", ns="billing:uuidB",
             model=SONNET, error="RateLimit: 429", sys="You are the billing worker.",
             user="Ticket: charge dispute order 999.", out="", pt=3100, ct=0),   # empty output -> excluded
    ]

    # ── T5: responder node with Sonnet (mixed-model half 1) ──
    R += [
        _run(clk, "t5-root", "t5", "chain", "LangGraph", None),
        _run(clk, "t5-resp", "t5", "chain", "responder", "t5-root", node="responder"),
        _run(clk, "t5-resp-l", "t5", "llm", "ChatAnthropic", "t5-resp", node="responder",
             model=SONNET, rev="sha2", fb=0.8,
             sys="Draft a friendly, apologetic reply and confirm the resolution.",
             user="Customer double charged on order 123; refunded. Draft a reply.",
             out="Hi! So sorry about the double charge on order 123 — it is refunded.", pt=1500, ct=120),
    ]

    # ── T6: window-sliced trace, parent_run_id points outside the fetched set ──
    R += [
        _run(clk, "t6-l", "t6", "llm", "ChatOpenAI", "sliced-parent-not-in-window", model=MINI,
             sys="Draft the final reply.", user="Confirm the refund for order 456.",
             out="Your refund for order 456 is on its way!", pt=900, ct=30),
    ]

    # ── T7: responder with gpt-4o-mini (mixed-model half 2) under a generic __start__ ──
    R += [
        _run(clk, "t7-root", "t7", "chain", "LangGraph", None),
        _run(clk, "t7-start", "t7", "chain", "__start__", "t7-root"),                # generic -> filtered
        _run(clk, "t7-resp", "t7", "chain", "responder", "t7-root", node="responder"),
        _run(clk, "t7-resp-l", "t7", "llm", "ChatOpenAI", "t7-resp", node="responder",
             model=MINI, rev="sha2",
             sys="Draft a friendly, apologetic reply and confirm the resolution.",
             user="Customer refund for order 456 processed. Draft a short confirmation.",
             out="Good news — your refund for order 456 is on its way!", pt=1600, ct=110),
    ]

    return R
