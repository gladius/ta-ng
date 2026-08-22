# Fleet / deep-agent traces — what they look like & what the auditor needs

_Empirical findings from inspecting a real LangSmith **Fleet** (no-code Agent Builder) agent, 2026-08-21.
Agent: `vendor-contract-risk-desk` (orchestrator + `clause-extractor` + `risk-analyst` sub-agents,
Tavily benchmarking, file-memory). Traces land in the shared project **`fleet`**._

## TL;DR

Fleet is LangChain's no-code builder, **built on the `deepagents` package** and **deployed on the
LangGraph Platform / Agent Server**. Its traces are **rich and perfectly ordered/nested**, but hostile to
our profiler *as-is*: the semantic identity lives one layer **below** `langgraph_node`, and ~91% of runs are
framework **middleware** noise. The downgrade auditor, unchanged, sees **0 call-sites** and can't run.
It is fully fixable with a connector adaptation — not a dead end.

## The raw shape (2 traces)

- **796 runs / 2 traces (~398 runs/trace)**. run_type mix: `chain 725, llm 33, tool 38`.
- **~91% are `*Middleware.*` chain runs** — `SkillsPreload`, `TodoList`, `HumanInTheLoop`, `Memory`,
  `Summarization`, `Anthropic/BedrockPromptCaching`, `Sandbox`, `Filesystem`, `SubAgent`, … pure plumbing.
- **Every LLM run is tagged `langgraph_node = "model"`** — orchestrator, clause-extractor, risk-analyst all
  collapse into one generic node. `langgraph_node`-based grouping yields **0 usable call-sites**.
- **No `revision_id`.** Version axis is `assistant_id` / `graph_id` / `agent_version` (LangGraph-Platform deploy).

## Where the identity actually lives (recovered from the run tree)

The sub-agent name is the chain run directly under the nearest `task` tool:

```
llm (risk-analyst):     model  <- vendor-contract-risk-analyst[chain]   <- task[tool] <- ...
llm (clause-extractor): model  <- vendor-contract-clause-extractor[chain] <- task[tool] <- ...
llm (orchestrator):     model  <- SubAgentMiddleware...   (NO task ancestor)
```

`checkpoint_ns` mirrors it: orchestrator = `model:<uuid>` (one segment); sub-agent = `tools:<uuid>|model:<uuid>` (nested).

| What the auditor needs | Where our code looks (empty) | Where it lives in a Fleet/deep-agent trace |
|---|---|---|
| Call-site identity | `langgraph_node` → always `"model"` | sub-agent name = chain run under nearest `task` ancestor; orchestrator if none. Mirrored in `checkpoint_ns` nesting. |
| Which model | — | `ls_model_name` / `model_name` / `model` metadata |
| Version axis | `revision_id` → **absent** | `agent_version` / `assistant_id` |
| Tool nodes | buried in middleware | tool run `name` — already clean |
| Signal vs noise | everything counted | **drop every `*Middleware.*` run** |

Recovered tool names (clean): `task` ×2 (the sub-agents), `write_todos`, `write_file`/`read_file`/`ls`/`grep`/`glob`
(file-memory), `tavily_web_search` ×5 (fired but errored — no key), `execute` (sandbox), `find_tools`.

## assistant_id — not Fleet-specific, and not a pointer elsewhere

- `assistant_id` / `graph_id` appear on **any LangGraph-Platform deployment**, Fleet or plain LangGraph.
  Code-run LangGraph (traced via `LANGSMITH_TRACING`, e.g. the `fleet-*` fixtures) instead carries `revision_id`
  (a git SHA) and usually no `assistant_id`. So the version axis is deployment-mode-dependent.
- It is a **label on the run**, not a reference to another trace — no external pull needed. One trace = one
  `assistant_id` = one agent; sub-agents are ephemeral (spawned by the `task` tool) **in-band**, not separate deployments.
- BUT the **`fleet` project is shared by all Fleet agents in the workspace**. So "project ≠ one agent" here —
  isolate the target agent by filtering on `assistant_id` / `agent_name`, then scope to latest `agent_version`.

## Advisory implications (per-node downgrade & other levers)

**Granularity:** the real "nodes" are the orchestrator + each sub-agent. Audit at the **sub-agent** level.

**Is per-node model downgrade actionable? Depends on the SURFACE:**
- **Plain LangGraph (code):** yes — each node can bind its own model; standard practice (the `fleet-*` fixtures
  already mix haiku/sonnet per node). Per-node downgrade is directly applicable.
- **deep-agents (code / Fleet export):** yes — each sub-agent can be created with its own model.
- **Fleet no-code UI:** likely **one model for the whole agent** (per-sub-agent model not confirmed in the UI).
  So delivery there is: "whole agent is safe to downgrade" (only if **every** sub-agent passes), else "export to
  deepagents to set the safe sub-agents' models." The proof is per-sub-agent regardless; the *delivery* is
  surface-matched.

**Compression / caching (what actually has headroom here):**
- The big **static system prompt (rubric) + tool schemas repeat on every model call** (orchestrator + each
  sub-agent turn) → the prime target.
- **System-prompt compression is no-code-actionable**: produce a compressed rubric, prove it preserves behavior,
  user pastes it back into Fleet's system-prompt field.
- **Caching is largely native**: `AnthropicPromptCachingMiddleware` / `BedrockPromptCachingMiddleware` already run —
  advisory = verify/quantify coverage, not re-implement.
- **Context offloading/summarization is native** (`SummarizationMiddleware`, files) — advisory = confirm it's on.

## Proposed connector adaptation (additive — leaves the clean-LangGraph path untouched)

A `deepagents`/Fleet detection path that:
1. **Drops `*Middleware.*` chain runs.**
2. **Derives call-site = nearest `task`-ancestor sub-agent name** (fallback: `checkpoint_ns` nesting), not `langgraph_node`.
3. **Uses `agent_version`/`assistant_id`** for latest-only scoping (no `revision_id`); **filters the shared `fleet`
   project by `assistant_id`/`agent_name`** to isolate one agent.
4. Keeps tool nodes by `name`; reads model from `ls_model_name`.

Detection signal: root/`llm` metadata carries `agent_type`/`langgraph_host`/`assistant_id` **and** `langgraph_node=="model"`
with `*Middleware.*` siblings.

## Open items
- Confirm whether Fleet's no-code UI exposes **per-sub-agent model** (would make per-node downgrade no-code-actionable).
- Decide replay strategy for a sub-agent call-site (reconstruct its recorded input the same way as a LangGraph node).
- Verify Tavily errored vs returned empty (5 `tavily_web_search` runs; report said "no key").

## How to resume (engineering) — cold-start kit

**Branch:** `git checkout feat/agent-profiling && git checkout -b feat/deepagents-connector`

**Reproduce the trace (read-only, ~seconds; creds from `.env`, confirmed live):**
- Workspace `f9ad495c-cd3e-42f5-843d-a389073bb570` · Project `fleet` · Agent `vendor-contract-risk-desk`.
- Find projects:            `python -m tools.diag.ls_query`
- Trace shape + DIAGNOSIS:   `python -m tools.diag.inspect_agent --ws f9ad495c-cd3e-42f5-843d-a389073bb570 --project fleet --n 10`
- **Prove where identity lives:** `python -m tools.diag.fleet_probe --project fleet --n 5`  ← reusable probe, committed alongside this doc.
- **Two real Fleet traces already sit in the `fleet` project** — develop against them; no need to regenerate. For
  more/bigger volume, re-run `vendor-contract-risk-desk` in the Fleet UI, or invoke it via its `api_url`+`agent_id`
  (Advanced settings → Developer → View code snippets).

**Where the code changes go (all additive — leave the clean-LangGraph path untouched):**
- `connectors/langsmith/adapter.py`
  - `_callsite()` (L152) — node identity is chosen from `langgraph_node` (fallback chain L162–165). Add a deep-agents
    branch: call-site = nearest `task`-ancestor sub-agent name (see `tools/diag/fleet_probe.py::_subagent_of`),
    fallback to `langgraph_checkpoint_ns` nesting (the ns helper is at ~L103–107).
  - Version axis: `_REV_KEYS` (L122), `_revision()` (L126), `_root_rev()` (L136), `_scope_to_latest()` (L140) —
    `revision_id` is absent for Fleet; add `agent_version`/`assistant_id` as the scoping key, and filter the shared
    `fleet` project by `assistant_id`/`agent_name` to isolate one agent.
  - Fetch/normalize: `fetch()` (L185), `_fetch_traces()` (L229), `to_trace()` (L276), `to_record()` (L342) —
    read the model from `ls_model_name`.
- `connectors/graph.py`
  - `_GENERIC` scaffolding set (L32) + `_is_generic()` (L36) — the natural home for dropping `*Middleware.*` runs
    (extend the predicate to match `.*Middleware\..*`).
  - `_resolve_node()` (L122) / `_resolve_path()` (L145) — node/path resolution from the run tree; the task-ancestor
    grouping integrates here too. `build_graph()` (L181) is the entry point.

**Detection signal** (when to take the deep-agents path): root/`llm` metadata carries `agent_type` / `langgraph_host` /
`assistant_id` AND `langgraph_node=="model"` with `*Middleware.*` sibling runs.

**Validation target** (what "fixed" looks like on the `fleet` trace):
- call-sites resolve to `orchestrator`, `clause-extractor`, `risk-analyst` (not one collapsed `model`);
- structural/tool nodes = `task`/`write_file`/`read_file`/`tavily_web_search`/… (middleware dropped);
- one revision via `agent_version`; then the downgrade audit runs **per sub-agent**.
- Prefer a captured-fixture char/unit test (per the synthetic-golden approach) so it's testable at $0.
