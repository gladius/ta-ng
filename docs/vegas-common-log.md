# Vegas Common LLM log — what we know (2026-10-11)

Read from screenshots of a production-side analysis (7-day window, usecase names masked). No content left
production; structure and counts only.

## Columns we use
| Purpose | Column | Notes |
|---|---|---|
| Request | `request_payload` (JSON) | full LiteLLM body: `model`, `messages[]`, `tools[]`, `max_completion_tokens`, `stream`, `metadata.deployment`, `labels.vz_vsad`, `labels.vz_key_name` |
| Response | `response_payload` (JSON) | OpenAI chat completion; `usage` has `prompt_tokens`, `completion_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `prompt_tokens_details.cached_tokens`, `cost` |
| Model | `response_payload.model` / `request_payload.model` | null on 28–38% of some usecases' rows; also `mdl_metadata.model_name` |
| Usecase | `vegas_usecase_name` (STRING) | 18% null |
| Service | `common_service_name` | LiteLLM / Proxy_API / Inference API; only LiteLLM rows have `messages[]` |
| Key / team | `client_metadata.user_api_key_alias`, `.user_api_key_team_alias` | set by the gateway (unforgeable) |
| Org | `request_payload.labels.vz_vsad` | 4-char org |
| Trace | `request_payload.litellm_trace_id` | OTEL root |
| Request id | `vegas_common_services_txn_id` | UUID per call |
| Orchestration | `vegas_orch_services_txn_id` | links orchestrator → sub-agent calls (6% of calls); not a session id |
| Time | `created_timestamp_utc` | request time |
| Latency | `duration_ms` | |
| Cost | `txn_metadata.response_cost` | USD, LiteLLM-computed; also `usage.cost` |

Multi-turn: the whole history is inside `messages[]`; there is no session id.

## Fleet (7 days)
- 58.4M calls (~8M/day). Any analysis must be BigQuery aggregates, never row downloads.
- 5.8% no system message; 37.3% system message > 4,000 chars; 15.1% have tools; 6% orchestrated.
- Providers: vertex_ai 57%, null 43% (Claude rows are unlabeled: derive provider from the model name).
- Models: almost all Gemini flash tiers on Vertex (2.5 / 3.1 / 3.5 / 3.6 / 3.7 / 3.8), one usecase on
  claude-haiku-4-5, ~25% of the biggest usecase is embeddings (text-embedding-004/005).

## Top 10 usecases
| UC | calls | distinct sys prompts (raw → digits masked) | tool sets | median msgs | cache reads | median prompt tokens | models |
|---|---|---|---|---|---|---|---|
| 01 | 21.8M | 3.5M → 3.3M | 763k (30.7k by tool names) | 6 | 20% | 4,014 | gemini-3.5-flash-lite 33%, 3.6-flash 30%, embeddings 25% |
| 02 | 16.4M | — (Proxy_API, no messages) | | | | | |
| 03 | 4.2M | 7 → 7 | 1 | 2 | 7.4% | 5,729 | gemini-3.5-flash 66%, null 28% |
| 04 | 3.4M | 31 → 31 | 1 | 2 | 2.3% | 4,595 | null 38%, gemini-3.1-flash-lite 35%, 3.5-flash-lite 27% |
| 05 | 1.9M | — (Inference API) | | | | | |
| 06 | 1.5M | — (Inference API) | | | | | |
| 07 | 1.3M | 17 → 17 | 6 | 2 | 0.2% | 215 | gemini-2.5-flash-lite 77% |
| 08 | 1.3M | 1 → 1 | 3 | — | 45.7% | 8,826 | gemini-3.5-flash-lite 95% |
| 09 | 1.1M | 15,768 → 1,293 | 67 | 1 | 17.3% | 3,969 | gemini-3.7-flash 97% |
| 10 | 0.6M | 4 → 4 | 1 | 2 | 0% | 6,044 | claude-haiku-4-5 100% |

UC-01 deep drill: one key, one team, one org. 617k distinct first-500-char openings vs 86k distinct last-500-char
endings of the system prompt: per-call context is injected at the TOP, the fixed instructions sit at the bottom.
Tools are chosen per call (30.7k distinct name sets).

## What it means
1. Three usecase shapes, each needing its own call-site rule:
   - stable templates (03, 04, 07, 08, 10): call-site = hash(system prompt, digits masked) + hash(tool names);
   - numeric templates (09): digits masked gets most of the way; the rest needs the "skeleton" (lines common
     across the usecase's prompts);
   - fully dynamic (01): call-site = skeleton of the stable tail; tools vary per call and don't identify it.
2. Biggest cache opportunities: UC-01 (dynamic context first, so the shared instructions never form a prefix:
   a layout change, needs proof) and UC-03/04/10 (constant 4.5–6k-token prompts, millions of calls, 0–7% cache
   reads: cause unknown until the module runs on real rows).
3. Exclude non-chat rows (no `messages[]`, embedding models) everywhere.
4. Model = coalesce(response.model, request.model, mdl_metadata.model_name); provider from the model name.
5. Validating Vegas usecases (downgrade, new-model checks) later needs Gemini models on our gateway.
