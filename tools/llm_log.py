"""A replica of a production LLM log (shape: docs/vegas-common-log.md) in our sandbox BigQuery, with synthetic traffic.

    .venv/bin/python tools/llm_log.py create            # table litellm.llm_log + view litellm.v_llm_log (table ∪ gateway logs)
    .venv/bin/python tools/llm_log.py agents [--stub]   # Sonnet 5 writes each usecase's prompt, tools and conversations once
    .venv/bin/python tools/llm_log.py generate [--days 14] [--load]   # rows -> .data/llm_log/rows.jsonl.gz [-> bq load]

Usecases copy the shapes seen in production: dynamic context at the top of the system prompt with per-call tools and
multi-turn sessions, constant 5k-token prompts single-turn, numeric templates, a well-cached one, Claude with and
without cache marks, plus embedding and proxy rows the modules must skip. Cached / written tokens follow each
provider's rules against the prefix a call really shares with a recent one, and `synthetic` keeps the truth
(call-site, session, cached depth and why) so the cache module's findings can be checked exactly.
"""
import argparse
import gzip
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT, DATASET, TABLE, VIEW = "trust-router-sandbox", "litellm", "llm_log", "v_llm_log"
OUT = os.path.join(".data", "llm_log")
AGENTS = os.path.join(OUT, "agents.json")

# name, domain (for Sonnet), shape, models {name: share}, calls/day, traffic pattern, options
#   shapes: stable (constant system prompt, single turn), numeric (template with numeric slots), dynamic_top (per-call
#   context first, instructions last, tools sampled per call, sessions), dynamic_bottom (same, instructions first),
#   cached (constant big prompt, heavy flat traffic), claude (stable; mark=True sends cache_control), orchestrated (an
#   orchestrator call, 2-3 specialist calls, a final call, one orch txn id). Dynamic options: optional (blocks present
#   60% of the time), retrieved (free-prose passages from the knowledge base), jsonblob (a JSON context block), shuffle
#   (block order varies per session).
USECASES = [
    ("uc-assistant-platform", "general customer assistant for a telecom app: account, plans, devices, outages",
     "dynamic_top", {"gemini-3.5-flash-lite": .5, "gemini-3.6-flash": .45, "gemini-2.5-flash-lite": .05}, 7000,
     "business", {"chars": 6000, "tools": 24, "tools_per_call": 6, "turns": 8, "dyn": 3,
                  "optional": True, "retrieved": True, "jsonblob": True}),
    ("uc-order-triage", "classifies incoming order issues into a team, priority and next action, JSON out",
     "stable", {"gemini-3.5-flash": .7, "gemini-2.5-flash": .3}, 1500, "business",
     {"chars": 22000, "versions": 7, "null_model": .28}),
    ("uc-bill-explainer", "explains a bill line by line to a customer, plain language",
     "stable", {"gemini-3.1-flash-lite": .55, "gemini-3.5-flash-lite": .45}, 1200, "business",
     {"chars": 18000, "versions": 31, "null_model": .38}),
    ("uc-intent-router", "one-line intent label for a chat message, 40 intents",
     "stable", {"gemini-2.5-flash-lite": .77, "gemini-3.5-flash-lite": .23}, 450, "flat",
     {"chars": 700, "versions": 17}),
    ("uc-kb-answer", "answers from a long embedded product knowledge base pasted in the system prompt",
     "cached", {"gemini-3.5-flash-lite": .95, "gemini-3.1-flash-lite": .05}, 450, "flat", {"chars": 35000}),
    ("uc-risk-score", "scores a transaction's fraud risk from numeric features given in the prompt, JSON out",
     "numeric", {"gemini-3.7-flash": .97, "gemini-2.5-pro": .03}, 380, "nightly",
     {"chars": 15000, "dyn": 2}),
    ("uc-device-troubleshoot", "step-by-step device troubleshooting agent with diagnostic tools",
     "dynamic_bottom", {"gemini-3.6-flash": 1.0}, 600, "business",
     {"chars": 7000, "tools": 10, "tools_per_call": 10, "turns": 6, "dyn": 2}),
    ("uc-ticket-summary", "summarises a support ticket thread into 3 bullet points",
     "claude", {"claude-haiku-4-5": 1.0}, 220, "business", {"chars": 24000, "versions": 4, "mark": False}),
    ("uc-contract-qa", "answers questions over a 30-page contract pasted in the system prompt",
     "claude", {"claude-haiku-4-5": 1.0}, 150, "business", {"chars": 40000, "versions": 1, "mark": True}),
    ("uc-plan-advisor", "recommends a mobile plan from the customer's usage profile injected per call",
     "dynamic_top", {"gemini-3.5-flash": .8, "gemini-3.8-flash": .2}, 900, "business",
     {"chars": 5000, "tools": 6, "tools_per_call": 6, "turns": 4, "dyn": 2, "shuffle": True}),
    ("uc-claims-orchestrator", "insurance-style device protection claims: an orchestrator plans and calls specialist "
     "agents (eligibility check, document review, response writer) and composes the final answer",
     "orchestrated", {"gemini-3.6-flash": 1.0}, 250, "business", {"chars": 4000}),
    ("uc-email-draft", "drafts a reply email from a template with numeric account fields",
     "numeric", {"gemini-3.5-flash-lite": 1.0}, 500, "business", {"chars": 4000, "dyn": 1}),
    ("uc-outage-notice", "writes an outage notice from structured incident data",
     "stable", {"gemini-3.1-flash-lite": 1.0}, 90, "flat", {"chars": 3000, "versions": 2, "coexist": True}),
    (None, "legacy chat endpoint with no usecase tag", "stable", {"gemini-2.5-flash-lite": 1.0}, 700, "flat",
     {"chars": 2500, "versions": 3}),
    ("uc-search-embed", None, "embedding", {"text-embedding-005": .8, "text-embedding-004": .2}, 2500, "flat", {}),
    ("uc-legacy-proxy", None, "proxy", {None: 1.0}, 3000, "flat", {}),
]
DAYS_DEFAULT = 14
# provider rules for the simulation: min cacheable prefix, how long a prefix stays cached, write surcharge, read price
RULES = {"vertex_ai": {"min": 1024, "ttl": 300, "write": 0, "read": .25},
         "anthropic": {"min": 1024, "ttl": 300, "write": 1.25, "read": .10},
         "openai": {"min": 1024, "ttl": 300, "write": 0, "read": .10}}
PRICE = {  # $/M input, output
    "gemini-2.5-flash-lite": (.10, .40), "gemini-3.1-flash-lite": (.10, .40), "gemini-3.5-flash-lite": (.10, .40),
    "gemini-2.5-flash": (.30, 2.5), "gemini-3.5-flash": (.30, 2.5), "gemini-3.6-flash": (.30, 2.5),
    "gemini-3.7-flash": (.30, 2.5), "gemini-3.8-flash": (.30, 2.5), "gemini-2.5-pro": (1.25, 10),
    "claude-haiku-4-5": (1, 5), "text-embedding-004": (.025, 0), "text-embedding-005": (.025, 0)}


def provider(model):
    if model is None:
        return None
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith(("gemini", "text-embedding")):
        return "vertex_ai"
    return "openai"


def tokens(s):
    return max(1, len(s) // 4)


def h(*parts):
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:12]


# ----------------------------------------------------------------------------------------------------- bq objects
SCHEMA = [("vegas_common_services_txn_id", "STRING"), ("vegas_orch_services_txn_id", "STRING"),
          ("vegas_usecase_name", "STRING"), ("common_service_name", "STRING"), ("created_timestamp_utc", "TIMESTAMP"),
          ("duration_ms", "FLOAT64"), ("request_payload", "JSON"), ("response_payload", "JSON"),
          ("client_metadata", "JSON"), ("txn_metadata", "JSON"), ("mdl_metadata", "JSON"), ("synthetic", "JSON")]

VIEW_SQL = """
CREATE OR REPLACE VIEW `{p}.{d}.{v}` AS
SELECT {cols} FROM `{p}.{d}.{t}`
UNION ALL
SELECT
  JSON_VALUE(j, '$.id'), CAST(NULL AS STRING), JSON_VALUE(j, '$.metadata.user_api_key_alias'), 'LiteLLM',
  TIMESTAMP_MICROS(CAST(CAST(JSON_VALUE(j, '$.startTime') AS FLOAT64) * 1e6 AS INT64)),
  CAST(JSON_VALUE(j, '$.response_time') AS FLOAT64) * 1000,
  JSON_OBJECT('model', JSON_VALUE(j, '$.model'), 'messages', JSON_QUERY(j, '$.messages'),
              'tools', JSON_QUERY(j, '$.model_parameters.tools'),
              'max_completion_tokens', JSON_QUERY(j, '$.model_parameters.max_tokens'),
              'litellm_trace_id', JSON_VALUE(j, '$.trace_id'),
              'labels', JSON_OBJECT('vz_key_name', JSON_VALUE(j, '$.metadata.user_api_key_alias'))),
  JSON_QUERY(j, '$.response'),
  JSON_OBJECT('user_api_key_alias', JSON_VALUE(j, '$.metadata.user_api_key_alias'),
              'user_api_key_team_alias', JSON_VALUE(j, '$.metadata.user_api_key_team_alias')),
  JSON_OBJECT('response_cost', CAST(JSON_VALUE(j, '$.response_cost') AS FLOAT64),
              'custom_llm_provider', JSON_VALUE(j, '$.custom_llm_provider')),
  NULL, NULL
FROM (SELECT PARSE_JSON(data, wide_number_mode => 'round') AS j FROM `{p}.{d}.raw_logs`)
WHERE JSON_VALUE(j, '$.metadata.user_api_key_alias') != 'litellm-internal-health-check'
  AND JSON_VALUE(j, '$.status') = 'success'
"""


def bq(*args, **kw):
    return subprocess.run(["bq", "--project_id=" + PROJECT, "--quiet", *args], check=True, text=True, **kw)


def create():
    bq("mk", "--table", "--time_partitioning_field=created_timestamp_utc", "--time_partitioning_type=DAY",
       "--clustering_fields=vegas_usecase_name", "%s.%s" % (DATASET, TABLE),
       ",".join("%s:%s" % c for c in SCHEMA))
    bq("query", "--use_legacy_sql=false", VIEW_SQL.format(p=PROJECT, d=DATASET, t=TABLE, v=VIEW,
                                                           cols=", ".join(c for c, _ in SCHEMA)))
    print("created %s.%s and %s.%s" % (DATASET, TABLE, DATASET, VIEW))


# --------------------------------------------------------------------------------------------------------- agents
AGENT_PROMPT = """Write the material for a synthetic production LLM usecase so a log of its traffic can be simulated.

Usecase: %(domain)s
Shape: %(shape)s

Return one json object with exactly these keys:
- "instructions": the fixed system prompt text, about %(chars)d characters long. Make it read like a real,
  specific enterprise prompt: role, rules, output format, examples, edge cases, policies. Pad with realistic
  reference material (tables, policies, FAQ) rather than filler; it must reach the length.
- "dynamic_blocks": %(dyn)d blocks of per-call context, each {"title", "template"} where the template contains
  slots written as {{slot_name}}%(dyn_note)s. Empty list when 0.
- "slots": {slot_name: [12 realistic values]} for every slot used%(slot_note)s.
- "tools": %(tools)d OpenAI function tools, each {"name", "description", "parameters"} with a real JSON schema;
  empty list when 0.
- "conversations": 24 conversations. Each is a list of turns alternating {"user": "..."} and either
  {"assistant": "..."} or {"tool_call": {"name", "arguments": {...}}, "tool_result": "...", "assistant": "..."}
  (a tool turn: the call, its result, then the reply). %(turns)s Vary length, tone and topic. Assistant
  replies must follow the instructions' output format.
Only the json object, no prose."""


ORCH_PROMPT = """Write the material for a synthetic multi-agent production usecase so a log of its traffic can be simulated.

Usecase: %(domain)s

Return one json object with exactly these keys:
- "orchestrator": the orchestrator's fixed system prompt, about %(chars)d characters: role, the specialists it can
  call (%(specialists)s), how it plans, how it composes the final answer, output format, policies.
- "specialists": {name: system prompt of about %(chars)d characters} for each of %(specialists)s, each a real,
  specific prompt with rules, formats and reference material.
- "requests": 24 items, each {"user": the customer's request, "plan": [specialist names in call order, 2 or 3],
  "brief": what the orchestrator tells the specialists, "outputs": {specialist name: its reply text},
  "final": the orchestrator's final answer to the customer}. Vary topics, tone and outcome.
Only the json object, no prose."""
SPECIALISTS = ["eligibility_check", "document_review", "response_writer"]


def write_agents(stub):
    from app.services import llm_client
    os.makedirs(OUT, exist_ok=True)
    agents = json.load(open(AGENTS)) if os.path.exists(AGENTS) else {}
    for name, domain, shape, models, _, _, o in USECASES:
        key = name or "null"
        if shape in ("embedding", "proxy") or (not stub and key in agents and not agents[key].get("_stub")):
            continue
        if stub:
            agents[key] = {"instructions": "".join("Rule %d: %s\n" % (i, "lorem ipsum " * 8) for i in range(o["chars"] // 100)),
                           "dynamic_blocks": [{"title": "Context %d" % i, "template": "customer {{name}} id {{id}} tier {{tier}}"}
                                              for i in range(o.get("dyn", 0))],
                           "slots": {"name": ["Ana", "Bo", "Cy"], "id": ["1001", "2002", "3003"], "tier": ["gold", "silver"]},
                           "tools": [{"name": "tool_%d" % i, "description": "does %d" % i,
                                      "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}
                                     for i in range(o.get("tools", 0))],
                           "conversations": [[{"user": "question %d %d" % (c, t)}, {"assistant": "answer %d %d" % (c, t)}]
                                             * random.randint(1, 3) for c in range(6) for t in range(1)], "_stub": True}
            continue
        if shape == "orchestrated":
            prompt = ORCH_PROMPT % {"domain": domain, "chars": o["chars"], "specialists": ", ".join(SPECIALISTS)}
        turns = ("Conversations have 1 to %d user turns; later turns refer back to earlier ones." % o["turns"]
                 if o.get("turns", 1) > 1 else "Each conversation is a single user turn and one reply.")
        numeric = shape == "numeric"
        prompt = prompt if shape == "orchestrated" else AGENT_PROMPT % {"domain": domain, "shape": shape, "chars": o["chars"], "dyn": o.get("dyn", 0),
                                 "tools": o.get("tools", 0), "turns": turns,
                                 "dyn_note": " (every slot NUMERIC: ids, counts, amounts, scores)" if numeric else "",
                                 "slot_note": " (numbers as strings)" if numeric else ""}
        print("writing", key, file=sys.stderr)
        for attempt in range(2):
            r = llm_client.complete(model="claude-sonnet-5", messages=[{"role": "user", "content": prompt}],
                                    max_tokens=48000, response_format={"type": "json_object"}, timeout=600)
            txt = llm_client.text(r).strip()
            txt = txt[txt.find("{"):txt.rfind("}") + 1]
            try:
                if r.choices and r.choices[0].finish_reason == "length":
                    raise ValueError("cut at max_tokens")
                agents[key] = json.loads(txt, strict=False)      # raw newlines inside strings are fine
                break
            except ValueError as e:
                print("  bad json (%s), retrying" % e, file=sys.stderr)
        else:
            continue
        u = llm_client.usage(r)
        print("  %d chars, %d in / %d out tokens" % (len(agents[key].get("instructions") or agents[key].get("orchestrator", "")), u["input"], u["output"]),
              file=sys.stderr)
        json.dump(agents, open(AGENTS, "w"), indent=1)       # saved as we go: a rerun resumes
    print("wrote", AGENTS)


# ------------------------------------------------------------------------------------------------------- generate
def weight(ts, pattern):
    hour, wd = ts.hour + ts.minute / 60, ts.weekday()
    if pattern == "business":
        return (0.2 if wd >= 5 else 1.0) * max(0.05, math.exp(-((hour - 14) / 3.5) ** 2))
    if pattern == "nightly":
        return 1.0 if 1 <= hour < 3 else 0.02
    return 1.0


def times(day, n, pattern):
    """`n` timestamps in `day` following `pattern` (hourly weights), sorted."""
    w = [weight(day + timedelta(hours=k), pattern) for k in range(24)]
    tot = sum(w)
    out = []
    for k in range(24):
        for _ in range(round(n * w[k] / tot)):
            out.append(day + timedelta(hours=k, seconds=random.random() * 3600))
    return sorted(out)


class Cache:
    """Prefix cache per provider: deepest block prefix seen within the TTL on the same model."""
    def __init__(self):
        self.seen = {}

    def hit(self, model, prefixes, ts, max_depth):
        rule = RULES[provider(model)]
        depth = 0
        for i, p in enumerate(prefixes[:max_depth], 1):
            last = self.seen.get((model, p))
            if last is not None and (ts - last).total_seconds() <= rule["ttl"]:
                depth = i
            else:
                break
        for p in prefixes:
            self.seen[(model, p)] = ts
        return depth


def pick(d):
    r, acc = random.random(), 0
    for k, v in d.items():
        acc += v
        if r <= acc:
            return k
    return k


def fill(template, slots):
    out = template
    for s, vals in slots.items():
        out = out.replace("{{%s}}" % s, random.choice(vals))
    return out


REV_WORDS = ["a refund", "a cancellation", "roaming", "an outage", "a late fee", "a trade-in", "an upgrade", "a port-out",
             "autopay", "paperless billing", "a SIM swap", "a family plan", "a data cap", "a promo", "a deposit",
             "an escalation", "a chargeback", "a warranty", "international calls", "a hotspot", "a device lock",
             "a credit check", "a payment plan", "a loyalty offer", "a closed account", "a duplicate charge",
             "a proration", "an early termination", "a lost device", "a name change", "a transfer of billing"]


def gen_usecase(uc, agents, days, start, cache):
    name, _, shape, models, per_day, pattern, o = uc
    key = name or "null"
    svc = "Proxy_API" if shape == "proxy" else "LiteLLM"
    vsad = "jtfv" if name else "k9lx"
    key_name = "svc-%s-pr" % (name or "legacy")
    a = agents.get(key, {})
    convs = a.get("conversations") or [[{"user": "hi"}, {"assistant": "hello"}]]
    versions = o.get("versions", 1)
    sessions = {}                                   # usecase-level open sessions: (conv index, turn, dyn text, tools)
    for d in range(days):
        day = start + timedelta(days=d)
        stamps = times(day, per_day, pattern)
        for ts in stamps:
            model = pick(models)                         # per call; a session keeps the model of its first turn
            if shape == "embedding":
                text = random.choice(random.choice(convs))
                text = text.get("user") or text.get("assistant") or "x"
                req = {"model": model, "input": text}
                pt = tokens(text)
                resp = {"model": model, "object": "list", "data": [{"index": 0, "embedding": "<768 floats>"}],
                        "usage": {"prompt_tokens": pt, "total_tokens": pt, "cost": pt / 1e6 * PRICE[model][0]}}
                yield row(name, svc, ts, 40 + random.random() * 60, req, resp, vsad, key_name, model,
                          {"shape": shape, "skip": "embedding"})
                continue
            if shape == "orchestrated":
                yield from gen_orchestrated(name, svc, ts, model, a, vsad, key_name, cache)
                continue
            if shape == "proxy":
                req = {"prompt": "legacy %d" % random.randint(1, 9999), "max_tokens": 256}
                resp = {"text": "ok", "usage": {"prompt_tokens": 40, "completion_tokens": 8}}
                yield row(name, svc, ts, 300, req, resp, vsad, key_name, None, {"shape": shape, "skip": "proxy"})
                continue

            # which prompt version is live: versions replace each other over time; `coexist` keeps two live at once
            if o.get("coexist"):
                ver = random.randint(0, versions - 1)
            else:
                ver = min(versions - 1, int(d / days * versions))
            instructions = a["instructions"] + ("\n\nRevision note: when the customer mentions %s, follow the %s rule above."
                                                % (REV_WORDS[ver % len(REV_WORDS)], REV_WORDS[(ver * 7 + 3) % len(REV_WORDS)])
                                                if ver else "")
            # a session: a conversation played turn by turn, the dynamic context fixed for its length
            sid, state = None, None
            for k in [k for k, v in sessions.items() if (ts - v["last"]).total_seconds() > 1800]:
                del sessions[k]                          # abandoned sessions
            if o.get("turns", 1) > 1 and sessions and random.random() < 0.6:
                sid = random.choice(list(sessions))
                state = sessions[sid]
                ts = state["last"] + timedelta(seconds=15 + random.expovariate(1 / 60))   # a follow-up comes soon
            if state is None:
                sid = uuid.uuid4().hex[:12]
                conv = random.choice(convs)
                parts = [(b["title"], fill(b["template"], a.get("slots", {}))) for b in a.get("dynamic_blocks", [])
                         if not o.get("optional") or random.random() < 0.6]
                if o.get("retrieved"):
                    paras = [p for p in agents.get("uc-kb-answer", {}).get("instructions", "").split("\n\n") if len(p) > 200]
                    parts.append(("Retrieved Knowledge", "\n\n".join(random.sample(paras, min(len(paras), random.randint(1, 3))))))
                if o.get("jsonblob"):
                    parts.append(("Session State", json.dumps({k: random.choice(v) for k, v in a.get("slots", {}).items()})))
                if o.get("shuffle"):
                    random.shuffle(parts)
                dyn = "\n\n".join("## %s\n%s" % p for p in parts)
                pool = a.get("tools", [])
                k = o.get("tools_per_call", len(pool))
                tools = pool if k >= len(pool) else random.sample(pool, k)
                state = {"conv": conv, "turn": 0, "dyn": dyn, "tools": tools, "last": ts, "model": model}
                sessions[sid] = state
            conv, turn, model = state["conv"], state["turn"], state["model"]
            pairs = [t for t in conv if "user" in t]
            if turn >= len(pairs):
                del sessions[sid]
                continue
            system = ("%s\n\n%s" % (state["dyn"], instructions) if shape == "dynamic_top" and state["dyn"] else
                      "%s\n\n%s" % (instructions, state["dyn"]) if state["dyn"] else instructions)
            msgs = [{"role": "system", "content": system}]
            blocks = []                                   # (label, text) in prefix order: tools, system, turns
            tools_json = [{"type": "function", "function": t} for t in state["tools"]]
            if tools_json:
                blocks.append(("tools", json.dumps(tools_json, sort_keys=True)))
            if shape == "dynamic_top" and state["dyn"]:
                blocks += [("system_dynamic", state["dyn"]), ("system_fixed", instructions)]
            elif state["dyn"]:
                blocks += [("system_fixed", instructions), ("system_dynamic", state["dyn"])]
            else:
                blocks.append(("system_fixed", instructions))
            for i in range(turn + 1):
                u, reply = pairs[i], conv[conv.index(pairs[i]) + 1] if conv.index(pairs[i]) + 1 < len(conv) else {"assistant": "ok"}
                msgs.append({"role": "user", "content": u["user"]})
                blocks.append(("turn%d_user" % i, u["user"]))
                if i < turn:
                    if "tool_call" in reply:
                        call = {"id": "call_%s" % h(sid, str(i)), "type": "function",
                                "function": {"name": reply["tool_call"]["name"],
                                             "arguments": json.dumps(reply["tool_call"].get("arguments", {}))}}
                        msgs.append({"role": "assistant", "content": None, "tool_calls": [call]})
                        msgs.append({"role": "tool", "tool_call_id": call["id"], "content": reply.get("tool_result", "")})
                    msgs.append({"role": "assistant", "content": reply.get("assistant", "")})
                    blocks.append(("turn%d_assistant" % i, json.dumps(reply, sort_keys=True)))
            reply = conv[conv.index(pairs[turn]) + 1] if conv.index(pairs[turn]) + 1 < len(conv) else {"assistant": "ok"}
            if "tool_call" in reply:
                message = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_%s" % h(sid, str(turn)), "type": "function",
                     "function": {"name": reply["tool_call"]["name"],
                                  "arguments": json.dumps(reply["tool_call"].get("arguments", {}))}}]}
                finish = "tool_calls"
            else:
                message, finish = {"role": "assistant", "content": reply.get("assistant", "")}, "stop"
            # cache simulation
            mark = o.get("mark")
            if provider(model) == "anthropic" and mark:
                msgs[0]["content"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            prefixes, acc = [], ""
            for _, text in blocks:
                acc = h(acc, text)
                prefixes.append(acc)
            sys_depth = max(i for i, (lbl, _) in enumerate(blocks, 1) if lbl.startswith("system"))
            max_depth = (sys_depth if mark else 0) if provider(model) == "anthropic" else len(blocks)
            depth = cache.hit(model, prefixes, ts, max_depth)
            rule = RULES[provider(model)]
            pt = sum(tokens(t) for _, t in blocks)
            cached = sum(tokens(t) for _, t in blocks[:depth]) if depth else 0
            if cached < rule["min"]:
                cached = 0
            written = 0
            if provider(model) == "anthropic" and mark and not cached:
                written = sum(tokens(t) for _, t in blocks[:sys_depth])
                written = written if written >= rule["min"] else 0
            ct = tokens(json.dumps(message))
            pin, pout = PRICE[model]
            cost = ((pt - cached - written) * pin + cached * pin * rule["read"] + written * pin * rule["write"]
                    + ct * pout) / 1e6
            why = ("prefix read within ttl" if cached else "first call of this prefix" if depth == 0 and max_depth else
                   "no cache mark" if max_depth == 0 else "prefix below minimum" if depth else "prefix changed")
            req = {"model": model, "messages": msgs, "max_completion_tokens": 8192, "stream": True,
                   "metadata": {"deployment": "%s/%s" % (provider(model), model)},
                   "labels": {"vz_vsad": vsad, "vz_key_name": key_name},
                   "litellm_trace_id": h(sid, "trace")}
            if tools_json:
                req["tools"] = tools_json
            resp = {"id": "chatcmpl-" + uuid.uuid4().hex[:20], "object": "chat.completion",
                    "created": int(ts.timestamp()), "model": model,
                    "choices": [{"index": 0, "finish_reason": finish, "message": message}],
                    "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct,
                              "cache_read_input_tokens": cached, "cache_creation_input_tokens": written,
                              "prompt_tokens_details": {"cached_tokens": cached, "text_tokens": pt - cached},
                              "completion_tokens_details": {"text_tokens": ct, "reasoning_tokens": None},
                              "cost": round(cost, 7)}}
            if random.random() < o.get("null_model", 0):
                del resp["model"]
            truth = {"shape": shape, "callsite": h(key, str(ver), ",".join(sorted(t["name"] for t in state["tools"]))
                                                   if shape != "dynamic_top" else ""),
                     "version": ver, "session": sid, "turn": turn, "layout": shape,
                     "blocks": [[lbl, tokens(t)] for lbl, t in blocks], "cached_depth": depth, "why": why}
            state["turn"], state["last"] = turn + 1, ts
            yield row(name, svc, ts, 400 + ct * 25 + random.random() * 400, req, resp, vsad, key_name, model, truth,
                      cost=cost, orch=h(sid, "orch") if shape.startswith("dynamic") and random.random() < .15 else None)


def gen_orchestrated(name, svc, ts, model, a, vsad, key_name, cache):
    """One customer request: orchestrator plans -> each specialist answers -> orchestrator composes; one orch txn id."""
    r = random.choice(a["requests"])
    orch = str(uuid.uuid4())
    steps = [("orchestrator", a["orchestrator"], [{"role": "user", "content": r["user"]}],
              "PLAN: call %s\nBRIEF: %s" % (", ".join(r["plan"]), r["brief"]))]
    for sp in r["plan"]:
        steps.append((sp, a["specialists"][sp], [{"role": "user", "content": "Customer request: %s\n\nBrief: %s"
                                                  % (r["user"], r["brief"])}], r["outputs"].get(sp, "")))
    steps.append(("orchestrator", a["orchestrator"],
                  [{"role": "user", "content": r["user"]}, {"role": "assistant", "content": steps[0][3]},
                   {"role": "user", "content": "Specialist outputs:\n" + "\n\n".join(
                       "[%s]\n%s" % (sp, r["outputs"].get(sp, "")) for sp in r["plan"])}], r["final"]))
    for i, (role, system, msgs, answer) in enumerate(steps):
        ts = ts + timedelta(seconds=2 + random.random() * 6)
        blocks = [("system_fixed", system)] + [("turn%d_%s" % (k, m["role"]), m["content"]) for k, m in enumerate(msgs)]
        prefixes, acc = [], ""
        for _, text in blocks:
            acc = h(acc, text)
            prefixes.append(acc)
        depth = cache.hit(model, prefixes, ts, len(blocks))
        rule = RULES[provider(model)]
        pt = sum(tokens(t) for _, t in blocks)
        cached = sum(tokens(t) for _, t in blocks[:depth]) if depth else 0
        cached = cached if cached >= rule["min"] else 0
        ct = tokens(answer)
        pin, pout = PRICE[model]
        cost = ((pt - cached) * pin + cached * pin * rule["read"] + ct * pout) / 1e6
        req = {"model": model, "messages": [{"role": "system", "content": system}] + msgs,
               "max_completion_tokens": 4096, "stream": False,
               "metadata": {"deployment": "%s/%s" % (provider(model), model), "agent": role},
               "labels": {"vz_vsad": vsad, "vz_key_name": key_name}, "litellm_trace_id": h(orch, "trace")}
        resp = {"id": "chatcmpl-" + uuid.uuid4().hex[:20], "object": "chat.completion", "created": int(ts.timestamp()),
                "model": model, "choices": [{"index": 0, "finish_reason": "stop",
                                             "message": {"role": "assistant", "content": answer}}],
                "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct,
                          "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0,
                          "prompt_tokens_details": {"cached_tokens": cached, "text_tokens": pt - cached},
                          "completion_tokens_details": {"text_tokens": ct, "reasoning_tokens": None},
                          "cost": round(cost, 7)}}
        truth = {"shape": "orchestrated", "callsite": h(name, role), "role": role, "step": i, "steps": len(steps),
                 "blocks": [[lbl, tokens(t)] for lbl, t in blocks], "cached_depth": depth,
                 "why": "prefix read within ttl" if cached else "first call of this prefix"}
        yield row(name, svc, ts, 500 + ct * 25, req, resp, vsad, key_name, model, truth, cost=cost, orch=orch)


def row(name, svc, ts, ms, req, resp, vsad, key_name, model, truth, cost=None, orch=None):
    return {"vegas_common_services_txn_id": str(uuid.uuid4()), "vegas_orch_services_txn_id": orch,
            "vegas_usecase_name": name, "common_service_name": svc,
            "created_timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", "duration_ms": round(ms, 1),
            "request_payload": req, "response_payload": resp,
            "client_metadata": {"user_api_key_alias": key_name, "user_api_key_team_alias": "team-" + vsad},
            "txn_metadata": {"response_cost": cost, "custom_llm_provider": provider(model) if provider(model) != "anthropic" else None},
            "mdl_metadata": {"model_name": model}, "synthetic": truth}


def generate(days, load, seed):
    random.seed(seed)
    agents = json.load(open(AGENTS))
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "rows.jsonl.gz")
    start = (datetime.now(timezone.utc) - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    n, by = 0, {}
    cache = Cache()
    with gzip.open(path, "wt") as f:
        for uc in USECASES:
            for r in gen_usecase(uc, agents, days, start, cache):
                f.write(json.dumps(r) + "\n")
                n += 1
                by[uc[0]] = by.get(uc[0], 0) + 1
    print("%d rows -> %s" % (n, path))
    for k, v in by.items():
        print("  %-24s %d" % (k, v))
    if load:
        bq("load", "--source_format=NEWLINE_DELIMITED_JSON", "--replace", "%s.%s" % (DATASET, TABLE), path)
        print("loaded into %s.%s" % (DATASET, TABLE))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create")
    a = sub.add_parser("agents")
    a.add_argument("--stub", action="store_true", help="placeholder agents, no model call")
    g = sub.add_parser("generate")
    g.add_argument("--days", type=int, default=DAYS_DEFAULT)
    g.add_argument("--load", action="store_true")
    g.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    if args.cmd == "create":
        create()
    elif args.cmd == "agents":
        write_agents(args.stub)
    else:
        generate(args.days, args.load, args.seed)
