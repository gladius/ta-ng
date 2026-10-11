"""The profiler: one model read per call-site that says what it is (a name, what it does, its task and output
shape), so every page can call a call-site by a name a person recognises. Stored by call-site id; never re-run for
a call-site already profiled."""
import json

from app.services import llm_client, store

MODEL_KEY = "AUDIT_PROFILE_MODEL"
DEFAULT_MODEL = "claude-sonnet-5"
TASKS = ("classify", "extract", "answer", "draft", "summarise", "converse", "orchestrate", "other")
OUTPUTS = ("json", "text", "tool_calls", "label")

_PROMPT = """You are naming a call-site: one place in a production system that sends a prompt to a model. Below is one
request as sent (the system prompt, which tools it could call, and the first user turn). Describe it for people who
will see hundreds of these in a dashboard. Return one json object:
- "name": 2 to 4 words a person would call this call-site (e.g. "Bill explainer", "Claim eligibility check").
- "does": one sentence, plain words, what it does for whom.
- "task": one of %s.
- "output": one of %s.
- "fixed": one short phrase: what stays the same every call (instructions, policies, examples, tool list...).
- "per_call": one short phrase: what changes per call (the question, customer details, retrieved documents...), or "".
Only the json object.

SYSTEM PROMPT:
%s

TOOLS: %s

FIRST USER TURN:
%s"""


def model():
    import credentials
    return credentials.get_config(MODEL_KEY) or DEFAULT_MODEL


def key(site):
    return ("profile", site)


def get(site):
    return store.peek(key(site))


def profile(site, system_prompt, tools, first_user=""):
    """The stored profile for `site`, made now if missing (one model call)."""
    p = get(site)
    if p:
        return p
    r = llm_client.complete(model=model(), max_tokens=1200, response_format={"type": "json_object"},
                            messages=[{"role": "user", "content": _PROMPT % (", ".join(TASKS), ", ".join(OUTPUTS),
                                                                             system_prompt, ", ".join(tools) or "none",
                                                                             first_user or "(not sampled)")}])
    txt = llm_client.text(r).strip()
    txt = txt[txt.find("{"):txt.rfind("}") + 1]
    d = json.loads(txt, strict=False)
    u = llm_client.usage(r)
    p = {"name": str(d.get("name") or "")[:60], "does": str(d.get("does") or "")[:300],
         "task": d.get("task") if d.get("task") in TASKS else "other",
         "output": d.get("output") if d.get("output") in OUTPUTS else "text",
         "fixed": str(d.get("fixed") or "")[:200], "per_call": str(d.get("per_call") or "")[:200],
         "model": model(), "tokens": u["input"] + u["output"]}
    store.put(key(site), p)
    return p
