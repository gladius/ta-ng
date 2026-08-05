# litellm handoff — make the audit provider-agnostic (do this in the litellm environment)

## Context
This project audits recorded LLM-agent traces and PROVES model-tier downgrades: for a call-site it re-runs the
original and a cheaper model head-to-head on the same recorded inputs, then judges whether behavior is preserved.
It works today **against Anthropic directly**. The whole audit *logic* is provider-neutral — only the live
CALL layer (build request / parse response) is Anthropic-shaped. This file is the spec to move that layer onto a
single litellm-backed `complete()` so the same flow runs on Anthropic / OpenAI / Gemini unchanged.

## The one seam: `app/services/llm_client.complete()`
Make `complete()` take a NEUTRAL request and return a NEUTRAL result. litellm owns every provider detail
(system placement, tool schema, thinking, temperature, response shape).

**Neutral request (what the flow passes):**
```
complete(model: str,                    # catalog name, e.g. "claude-sonnet-5" / "gpt-5.6-terra" / "gemini-3.1-pro"
         messages: [{role, content}],   # roles: user/assistant (system passed separately below)
         system: str | None,
         tools: [{name, description, parameters}] | None,   # OpenAI-style JSON-schema "parameters"
         force_tool: str | None,         # force this tool by name (triage-style routing)
         reasoning_effort: str | None,   # "low"/"medium"/"high"/... -> thinking; None = no thinking
         max_tokens: int)
```
**Neutral result (what the flow expects back):**
```
{ "text": str,
  "tool_calls": [ {"name": str, "args": dict} ],
  "usage": {"input_tokens": int, "output_tokens": int} }
```

**litellm mapping (inside complete()):**
- `messages` = `[{"role":"system","content":system}] + messages` (litellm wants system as a message).
- `tools` -> OpenAI function format: `{"type":"function","function":{name,description,parameters}}`; `force_tool`
  -> `tool_choice={"type":"function","function":{"name":force_tool}}`.
- `reasoning_effort` -> pass through; litellm maps to each provider (Anthropic adaptive/output_config, Gemini
  thinking_level, OpenAI/DeepSeek effort). Omit when None.
- **Do NOT send `temperature`** (newest models reject it; not a reliability lever here). Set `litellm.drop_params=True`.
- Response -> `r.choices[0].message`: `text = message.content or ""`; `tool_calls` from `message.tool_calls`
  (`name = tc.function.name`, `args = json.loads(tc.function.arguments)`); `usage` from `r.usage`
  (`prompt_tokens`/`completion_tokens`).
- Keep the existing bounded retry (429/5xx/connection); 4xx raises.

## Flow spots to switch onto the neutral seam (small, contained)
These currently build Anthropic-shaped requests / parse Anthropic responses. Change them to call the neutral
`complete()` and read `{text, tool_calls, usage}`:
- `app/services/audit.py` -> `replay()`  (drop the anthropic `thinking=`/`output_config=` shaping + the
  `try/except` thinking fallback; instead pass `reasoning_effort="low" if trace.thinking_enabled else None`).
- `app/services/audit.py` -> `judge_preserved()`  (call neutral complete, read `result["text"]`).
- `app/services/audit.py` -> `_tools()`  (emit `{name, description, parameters}` not `input_schema`).
- `sample_agents/support_agent.py` -> `_call()`  (test harness; neutralize so it can generate multi-provider data).

## Do NOT touch
- The deterministic pipeline: `contract/`, `connectors/` (adapter, graph, schema), `app/services/graph.py`,
  `downgrade.py`, `funnel.py`, verdict aggregation. All provider-neutral already.
- `auditor/models.json` — economic truth (price/tier/cache/max_output). Do NOT add temperature or thinking-shape
  here; capability (supports_reasoning) + param support come from the gateway, not a second source that drifts.

## Acceptance test (should pass unchanged after the swap)
```
python -m tests.test_downgrade_spine     # $0 deterministic — must stay green
python -m tests.test_downgrade_proof     # $0 deterministic — must stay green
python -c "from app.services import audit; import json; print(json.dumps(audit.run('recorded','recorded','acme-support',n=5))[:400])"
```
The last one is the live audit; on any provider it must return PRESERVED/DRIFT/LOW-EVIDENCE verdicts with
`samples[].method in {tool-decision, judge}`. A capability drop (reasoning node -> non-reasoning tier) should
degrade gracefully, not crash.
